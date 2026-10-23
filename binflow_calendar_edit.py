"""Atomic calendar moves using the existing weekly approval and custody rules."""
import copy
import hashlib
import json
import re
import sqlite3
from datetime import date, timedelta

from flask import g, jsonify, request


def fingerprint(row, plan):
    value = dict(revision=row['revision'] if row else 0, plan=plan,
                 driver=row['driver_id'] if row else None, status=row['status'] if row else 'draft')
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def key(state, day):
    from binflow_schedule import monday
    return state + ':' + monday(day)


def checked_day(value):
    from binflow_route_calendar import START
    try:
        day = date.fromisoformat(value)
        if day < START or day.year > 2100:
            raise ValueError()
        return day
    except (ValueError, TypeError):
        raise ValueError('Choose a calendar date from 26 October 2026 to 31 December 2100.') from None


def apply_move(plans, operation):
    """Only move existing work; never accept replacement plans from the browser."""
    from binflow_schedule import STATES
    if not isinstance(operation, dict):
        raise ValueError('Invalid calendar move.')
    state = operation.get('state')
    if state not in STATES or operation.get('kind') not in ('day', 'item'):
        raise ValueError('Choose a state and a day or stop to move.')
    uid = operation.get('uid', '')
    if not isinstance(uid, str) or not re.fullmatch(r'[a-f0-9]{32}', uid):
        raise ValueError('Invalid move reference. Reload the calendar.')
    source_date = checked_day(operation.get('source')).isoformat()
    target_date = checked_day(operation.get('target')).isoformat()
    source_key, target_key = key(state, source_date), key(state, target_date)
    source = next(d for d in plans[source_key]['days'] if d['date'] == source_date)
    target = next(d for d in plans[target_key]['days'] if d['date'] == target_date)
    cross_week = source_key != target_key
    if operation['kind'] == 'day':
        if source_date == target_date:
            return
        source['items'], target['items'] = target['items'], source['items']
        source['notes'], target['notes'] = target['notes'], source['notes']
        if cross_week:
            for label, day in (('a', source), ('b', target)):
                for n, item in enumerate(day['items']):
                    item['id'] = f'cal-{uid}-{label}{n}'
    else:
        item_id = operation.get('item_id')
        item = next((i for i in source['items'] if i['id'] == item_id), None)
        if item is None:
            raise ValueError('That stop has changed. Reload the calendar before moving it.')
        before = operation.get('before')
        if before == item_id and source is target:
            return
        if before is not None and not any(i['id'] == before for i in target['items']):
            raise ValueError('The destination stop has changed. Reload the calendar.')
        source['items'].remove(item)
        position = next((n for n, i in enumerate(target['items']) if i['id'] == before), len(target['items']))
        if cross_week:
            item['id'] = f'cal-{uid}-item'
        target['items'].insert(position, item)
    source['estimate_valid'] = target['estimate_valid'] = False


def register_calendar_edit(app, db, require, now, actor):
    from binflow_schedule import (STATES, monday, get_plan, clean_plan, drivers,
                                  job_positions, ensure_job_editable, sync_jobs, refresh_runs)

    @app.get('/binflow/schedule/calendar-progress')
    @require('admin')
    def bf_calendar_progress():
        from binflow_calendar_progress import progress
        try:
            first = date.fromisoformat(request.args.get('first',''))
            last = date.fromisoformat(request.args.get('last',''))
            state = request.args.get('state','')
            if not 0 <= (last-first).days <= 41 or state not in ('',*STATES):
                raise ValueError('Choose one calendar month and a valid state.')
            response = jsonify(days=progress(db(),first,last,state))
            response.headers['Cache-Control'] = 'no-store'
            return response
        except (ValueError,TypeError) as exc:
            return jsonify(error=str(exc)),400

    @app.get('/binflow/schedule/calendar-data')
    @require('admin')
    def bf_calendar_edit_data():
        try:
            state = request.args.get('state')
            if state not in STATES:
                raise ValueError('Choose a state to edit.')
            first = date.fromisoformat(monday(request.args.get('first')))
            last = date.fromisoformat(monday(request.args.get('last')))
            if not 0 <= (last-first).days <= 42:
                raise ValueError('Load one calendar month at a time.')
            from binflow_route_calendar import START
            weeks = []
            while first <= last:
                if first >= START:
                    start = first.isoformat()
                    row, plan = get_plan(db(), state, start)
                    weeks.append(dict(key=key(state, start), state=state, start=start, plan=plan,
                                      token=fingerprint(row, plan), status=row['status'] if row else 'draft',
                                      driver=row['driver_name'] if row else None))
                first += timedelta(days=7)
            return jsonify(weeks=weeks)
        except (ValueError, TypeError) as exc:
            return jsonify(error=str(exc)), 400

    @app.post('/binflow/schedule/calendar-save')
    @require('admin')
    def bf_calendar_edit_save():
        try:
            try:
                operations = json.loads(request.form.get('moves', ''))
                tokens = json.loads(request.form.get('tokens', ''))
            except (ValueError, TypeError):
                raise ValueError('The calendar changes could not be read.') from None
            if not isinstance(operations, list) or not 1 <= len(operations) <= 200 or not isinstance(tokens, dict):
                raise ValueError('Save between 1 and 200 calendar moves at a time.')
            affected = set()
            for op in operations:
                if not isinstance(op, dict) or op.get('state') not in STATES:
                    raise ValueError('Choose a valid state for each move.')
                for field in ('source', 'target'):
                    affected.add(key(op['state'], checked_day(op.get(field)).isoformat()))
            if len(affected) > 48:
                raise ValueError('Save a smaller range of calendar changes.')
            if len({op.get('uid') for op in operations if isinstance(op.get('uid'), str)}) != len(operations):
                raise ValueError('Each calendar move needs its own reference.')
            conn = db()
            conn.execute('BEGIN IMMEDIATE')
            rows, original, plans = {}, {}, {}
            for identity in sorted(affected):
                state, start = identity.split(':')
                row, plan = get_plan(conn, state, start)
                if tokens.get(identity) != fingerprint(row, plan):
                    conn.rollback()
                    return jsonify(error='This calendar changed in another session. Reload before saving; no moves were applied.'), 409
                rows[identity], original[identity], plans[identity] = row, plan, copy.deepcopy(plan)
            for operation in operations:
                apply_move(plans, operation)

            # Cross-week transfers retain the docket, physical reservations and truck.
            # Detach only the weekly ownership inside this transaction, then reassign.
            old_owner = {jid: identity for identity, plan in original.items() for jid in job_positions(plan)}
            new_owner = {}
            for identity, plan in plans.items():
                for jid in job_positions(plan):
                    if jid in new_owner:
                        raise ValueError('A job cannot appear in two weeks.')
                    new_owner[jid] = identity
            transfers = {jid for jid in old_owner if new_owner.get(jid) != old_owner[jid]}
            records, changed_runs = {}, set()
            for jid in transfers:
                job = conn.execute('SELECT * FROM bf_jobs WHERE id=?', (jid,)).fetchone()
                ensure_job_editable(conn, job)
                records[jid] = job
                if job['truck_id']:
                    changed_runs.add((job['truck_id'], job['scheduled_date']))
                conn.execute('UPDATE bf_jobs SET schedule_week_id=NULL WHERE id=?', (jid,))

            for identity in sorted(affected):
                state, start = identity.split(':')
                row = rows[identity]
                plans[identity] = clean_plan(conn, plans[identity], original[identity], state, start,
                                             row['id'] if row else None)
                if row and row['status'] == 'published' and row['driver_id'] not in {u['id'] for u in drivers(conn, state)}:
                    raise ValueError(f'Open Calendar options in the calendar to update the assigned {state} driver before saving.')

            stamp, uid = now(), g.user['id']
            for identity in sorted(affected):
                state, start = identity.split(':')
                row, plan = rows[identity], plans[identity]
                if not row:
                    wid = conn.execute('''INSERT INTO bf_schedule_weeks
                        (state,week_start,revision,status,plan_json,updated_at,updated_by)
                        VALUES(?,?,0,'draft','{}',?,?)''', (state, start, stamp, uid)).lastrowid
                    row = dict(id=wid, revision=0, status='draft', driver_id=None)
                previous = copy.deepcopy(original[identity])
                for day in previous['days']:
                    day['items'] = [i for i in day['items'] if i.get('job_id') not in transfers]
                for jid in set(job_positions(previous)) | set(job_positions(plan)):
                    records.setdefault(jid, conn.execute('SELECT * FROM bf_jobs WHERE id=?', (jid,)).fetchone())
                publish = row['status'] == 'published'
                sync_jobs(conn, previous, plan, row, row['driver_id'], publish, uid, actor(), stamp,
                          app.extensions['bf_review_allocation'], deferred_runs=changed_runs)
                revision = row['revision'] + 1
                conn.execute('''UPDATE bf_schedule_weeks SET revision=?,plan_json=?,updated_at=?,updated_by=? WHERE id=?''',
                             (revision, json.dumps(plan), stamp, uid, row['id']))
                snapshot = dict(plan=plan, state=state, week=start, driver_id=row['driver_id'], status=row['status'])
                conn.execute('''INSERT INTO bf_schedule_history(week_id,revision,action,actor_id,created_at,snapshot)
                    VALUES(?,?,?,?,?,?)''', (row['id'], revision, 'Calendar moves saved and republished' if publish else 'Calendar moves saved as draft',
                                            uid, stamp, json.dumps(snapshot)))
            # Check destination truck assignments only after every transfer is in place.
            refresh_runs(conn, changed_runs, records, uid, actor(), stamp)
            conn.commit()
            return jsonify(message='Calendar changes saved. Published weeks updated; draft weeks still need approval.')
        except (ValueError, TypeError, sqlite3.IntegrityError) as exc:
            db().rollback()
            return jsonify(error=str(exc) if isinstance(exc, ValueError) else 'The calendar could not be saved. Reload and try again.'), 400
