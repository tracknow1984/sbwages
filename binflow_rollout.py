"""Manual state rollout and site-by-site coverage against the current register."""
import json
from collections import Counter, defaultdict
from datetime import date, timedelta

from flask import abort, g, request

from binflow_collections import INTERVALS, STATES, dated, occurs, parse_frequency
from binflow_client_profiles import register_rows, site_bins


def migrate(conn):
    from binflow_rollout_stock import migrate as migrate_stock
    migrate_stock(conn)
    conn.executescript('''
        CREATE TABLE IF NOT EXISTS bf_rollout_states (
            state TEXT PRIMARY KEY, first_week TEXT NOT NULL, changed_by INTEGER NOT NULL REFERENCES users(id));
        CREATE TABLE IF NOT EXISTS bf_rollout_sites (
            profile_id INTEGER PRIMARY KEY REFERENCES bf_client_site_profiles(id),
            first_pickup TEXT NOT NULL, schedule TEXT NOT NULL, changed_by INTEGER NOT NULL REFERENCES users(id));
    ''')


def state_starts(conn):
    return {r['state']: r['first_week'] for r in conn.execute('SELECT state,first_week FROM bf_rollout_states')}


def collection_pattern(schedule, first):
    cadence, days, visits = parse_frequency({'frequency': schedule})
    if cadence not in (*INTERVALS, 'monthly'):
        return None
    if schedule.strip().lower() == 'weekdays':
        days = list(range(5))
    if cadence in INTERVALS and visits and visits * INTERVALS[cadence] / 4 > max(1, len(days)):
        return None  # Multiple visits with unspecified weekdays need confirmation.
    return dict(cadence=cadence, days=days or [first.weekday()], start_date=first.isoformat(),
                monthly_rule='last_weekday' if cadence == 'monthly' and days else '')


def register_view(conn, first, state, starts):
    profiles = [p for p in register_rows(conn) if not p['archived'] and (not state or p['state'] == state)]
    targets = defaultdict(dict)
    for r in conn.execute('''SELECT r.location_id,r.quantity,s.name FROM bf_site_requirements r
            JOIN bf_sizes s ON s.id=r.size_id JOIN bf_site_profiles sp ON sp.location_id=r.location_id
            WHERE sp.active=1 AND r.quantity>0 ORDER BY s.litres'''):
        targets[r['location_id']][r['name']] = r['quantity']
    stock = defaultdict(Counter)
    for b in site_bins(conn):
        stock[b['profile_id'], b['relationship']][b['size_name']] += 1
    for r in conn.execute("""SELECT p.id,z.name,COUNT(*) AS n FROM bf_rollout_slots s JOIN bf_jobs j ON j.id=s.job_id
            JOIN bf_client_site_profiles p ON p.location_id=j.site_id JOIN bf_sizes z ON z.id=s.size_id
            WHERE s.item_id IS NULL AND j.status='scheduled' AND j.approval_status!='rejected' GROUP BY p.id,z.id"""):
        stock[r['id'], 'allocated'][r['name']] += r['n']
    deliveries = defaultdict(list)
    for j in conn.execute("""SELECT DISTINCT j.id,j.code,j.site_id,j.scheduled_date,j.schedule_live,j.approval_status,
            j.schedule_week_id FROM bf_jobs j
            WHERE (EXISTS(SELECT 1 FROM bf_job_bins i WHERE i.job_id=j.id AND i.active=1 AND i.direction='deliver')
                OR (j.status='scheduled' AND EXISTS(SELECT 1 FROM bf_rollout_slots s WHERE s.job_id=j.id AND s.item_id IS NULL)))
                AND j.status!='cancelled' AND j.approval_status!='rejected'
            ORDER BY j.scheduled_date,j.id"""):
        label = ('In draft week' if j['schedule_week_id'] else 'Needs rescheduling') if not j['schedule_live'] else (
            'Awaiting approval' if j['approval_status']=='pending' else 'Approved')
        deliveries[j['site_id']].append(dict(id=j['id'],code=j['code'],label=label))
    anchors = {r['profile_id']: r for r in conn.execute('SELECT * FROM bf_rollout_sites')}
    visits = defaultdict(list)
    # A draft can move a linked job without changing its dispatch date until publication.
    planned_dates = {}
    for w in conn.execute('SELECT id, plan_json FROM bf_schedule_weeks WHERE week_start=?', (first.isoformat(),)):
        for day in json.loads(w['plan_json'])['days']:
            for item in day['items']:
                if item['kind'] == 'job':
                    planned_dates[item['job_id']] = day['date']
    last = first + timedelta(days=6)
    for j in conn.execute('''SELECT j.id,j.site_id,j.code,j.scheduled_date,j.approval_status,j.schedule_live,
            j.schedule_week_id,w.week_start FROM bf_jobs j LEFT JOIN bf_schedule_weeks w ON w.id=j.schedule_week_id
            WHERE (j.scheduled_date BETWEEN ? AND ? OR w.week_start=?) AND j.status!='cancelled'
            AND j.approval_status!='rejected' AND (j.schedule_live=1 OR w.id IS NOT NULL)
            AND EXISTS (SELECT 1 FROM bf_job_bins i WHERE i.job_id=j.id AND i.direction='collect')''',
            (first.isoformat(), last.isoformat(), first.isoformat())):
        when = planned_dates.get(j['id']) if j['schedule_week_id'] else j['scheduled_date']
        if when:
            visits[j['site_id'], when].append(dict(j))
    result = []
    for p in profiles:
        pid, lid = p['id'], p['location_id']
        at, incoming = stock[pid, 'at_site'], stock[pid, 'allocated']
        sizes = [dict(name=k, target=n, at=at[k], incoming=incoming[k]) for k, n in targets[lid].items()]
        ready = bool(sizes) and all(s['at'] >= s['target'] for s in sizes)
        anchor = anchors.get(pid)
        cadence = parse_frequency({'frequency': p['schedule']})[0]
        pattern = collection_pattern(p['schedule'], date.fromisoformat(anchor['first_pickup']) if anchor else first)
        due = []
        if anchor and pattern and anchor['schedule'] == p['schedule']:
            for offset in range(7):
                day = first + timedelta(days=offset)
                if occurs(pattern, day):
                    due.append(dict(date=day.isoformat(), jobs=visits[lid, day.isoformat()]))
        if not lid or p['state'] not in STATES:
            stage = 'Check site details'
        elif not sizes:
            stage = 'Set bin quantities'
        elif not ready:
            if all(s['at'] + s['incoming'] >= s['target'] for s in sizes):
                stage = 'Awaiting empty-bin delivery'
            elif any(at.values()) or any(incoming.values()):
                stage = 'More empties needed'
            else:
                stage = 'Needs empty bins'
        else:
            stage = 'Bins on site'
        if cadence in ('adhoc', 'paused'):
            service = 'On request' if cadence == 'adhoc' else 'Paused'
        elif not pattern or (anchor and anchor['schedule'] != p['schedule']):
            service = 'Review schedule'
        elif not anchor:
            service = 'Set first pickup' if ready else 'Awaiting bins'
        elif any(not d['jobs'] for d in due):
            service = 'Pickup needs booking'
        else:
            service = 'Allocated this week' if due else 'No pickup due this week'
        result.append(dict(profile=p, sizes=sizes, deliveries=deliveries[lid], stage=stage, ready=ready, service=service, due=due,
                           first_pickup=anchor['first_pickup'] if anchor else '',
                           can_set=ready and bool(pattern) and p['state'] in starts))
    return result


def register(app, db, require, render, success, failure, now):
    @app.route('/binflow/collection-calendar/rollout', methods=['GET','POST'])
    @require('admin')
    def bf_collection_rollout():
        from binflow_route_calendar import START
        state = request.values.get('state', '')
        if state not in ('', *STATES):
            abort(400)
        try:
            chosen = dated(request.values.get('date') or now()[:10])
        except ValueError:
            abort(400, 'Choose a valid week.')
        first = chosen - timedelta(days=chosen.weekday())
        back = dict(state=state, date=first.isoformat())
        conn = db()
        if request.method == 'POST':
            conn.execute('BEGIN IMMEDIATE')
        starts = state_starts(conn)
        rows = register_view(conn, first, state, starts)
        if request.method == 'GET':
            focus = request.args.get('focus', '')
            groups = {
                '': rows,
                'bins': [r for r in rows if not r['ready']],
                'first': [r for r in rows if r['service']=='Set first pickup'],
                'book': [r for r in rows if r['service']=='Pickup needs booking'],
                'review': [r for r in rows if r['service']=='Review schedule' or r['stage'] in ('Set bin quantities','Check site details')],
            }
            if focus not in groups:
                abort(400)
            today = date.fromisoformat(now()[:10])
            default_day = today if first <= today <= first+timedelta(days=6) else first
            saved = [dict(w) for w in conn.execute('''SELECT state,week_start,status FROM bf_schedule_weeks
                WHERE week_start>=? AND (?='' OR state=?) ORDER BY week_start,state''', (first.isoformat(),state,state))]
            return render('rollout', section='collection_calendar', rows=groups[focus], focus=focus,
                          total=len(rows), first_dates=len(groups['first']), default_day=default_day.isoformat(),
                          starts=starts, selected=state,
                          first=first.isoformat(), last=(first+timedelta(days=6)).isoformat(), saved=saved,
                          needing=sum(r['stage']!='Bins on site' for r in rows),
                          missing=sum(not d['jobs'] for r in rows for d in r['due']))
        try:
            if request.form.get('action') == 'start':
                today = date.fromisoformat(now()[:10])
                if not state or first < START or first < today - timedelta(days=today.weekday()):
                    raise ValueError('Choose a state and the current or a future week to start manual planning.')
                if state in starts and starts[state] != first.isoformat():
                    raise ValueError('Manual planning already has a start week for this state.')
                conn.execute('INSERT OR IGNORE INTO bf_rollout_states VALUES(?,?,?)',(state,first.isoformat(),g.user['id']))
                return success('Manual rollout starts in '+state+' the week of '+first.isoformat()+'.', 'bf_collection_rollout', **back)
            if request.form.get('action') != 'anchor':
                raise ValueError('Choose a rollout action.')
            pid = request.form.get('profile_id',type=int)
            row = next((r for r in rows if r['profile']['id']==pid), None)
            if not row or not row['can_set']:
                raise ValueError('Confirm the state rollout, on-site bins and collection frequency first.')
            if request.form.get('previous') != row['first_pickup'] or request.form.get('schedule') != row['profile']['schedule']:
                raise ValueError('This site changed. Review the latest schedule before saving.')
            day = dated(request.form.get('first_pickup',''))
            p = row['profile']
            if day.isoformat() < starts[p['state']] or not occurs(collection_pattern(p['schedule'],day), day):
                raise ValueError('Choose a first pickup on or after rollout, matching the agreed service day.')
            conn.execute('''INSERT INTO bf_rollout_sites VALUES(?,?,?,?) ON CONFLICT(profile_id) DO UPDATE SET
                first_pickup=excluded.first_pickup,schedule=excluded.schedule,changed_by=excluded.changed_by''',
                (pid,day.isoformat(),p['schedule'],g.user['id']))
            return success('Pickup cycle saved. Book the due visits from the rollout register.', 'bf_collection_rollout', state=p['state'],date=day.isoformat())
        except ValueError as exc:
            return failure(exc,'bf_collection_rollout',**back)
