"""The phone-first driver journey, built on the existing bin custody ledger."""
import hashlib
import json
from datetime import datetime

from flask import abort, g, jsonify, request

from binflow_acceptance import manifest, ensure_run_unstarted
from binflow_permissions import selected_state


def migrate_journey(conn):
    for table, additions in {
        'bf_workdays': [('flow_version', 'INTEGER NOT NULL DEFAULT 0'), ('overview_hash', "TEXT NOT NULL DEFAULT ''"),
                        ('overview_accepted_at', 'TEXT'), ('break_started_at', 'TEXT'), ('break_finished_at', 'TEXT')],
        'bf_runs': [('test_prestart_at', 'TEXT'), ('test_prestart_date', "TEXT NOT NULL DEFAULT ''"), ('test_prestart_by', 'INTEGER REFERENCES users(id)'),
                    ('manifest_confirmed_at', 'TEXT'), ('onboard_manifest', "TEXT NOT NULL DEFAULT ''")],
        'bf_jobs': [('driver_arrived_at', 'TEXT')],
    }.items():
        columns = {r['name'] for r in conn.execute(f'PRAGMA table_info({table})')}
        for name, definition in additions:
            if name not in columns:
                conn.execute(f'ALTER TABLE {table} ADD COLUMN {name} {definition}')
    conn.execute('''CREATE TABLE IF NOT EXISTS bf_run_depot_arrivals (
        truck_id INTEGER NOT NULL REFERENCES bf_trucks(id), run_date TEXT NOT NULL,
        warehouse_id INTEGER NOT NULL REFERENCES bf_locations(id), arrived_at TEXT NOT NULL,
        driver_id INTEGER NOT NULL REFERENCES users(id), PRIMARY KEY(truck_id,run_date,warehouse_id))''')


def overview(conn, user, day, state):
    from binflow_runs import driver_plan_data
    plan = driver_plan_data(conn, user, day, state)
    runs = []
    for group in plan['groups']:
        if not group['truck_id']:
            continue
        jobs, _, version = manifest(conn, group['truck_id'], day)
        for job in jobs:
            job['address'] = conn.execute('SELECT site_address FROM bf_jobs WHERE id=?',(job['id'],)).fetchone()[0]
        runs.append(dict(truck_id=group['truck_id'], truck_name=group['name'], jobs=jobs, version=version))
    payload = dict(date=day, state=state, runs=runs, plans=plan['plans'],
                   unassigned=[dict(j) for group in plan['groups'] if not group['truck_id'] for j in group['jobs']])
    encoded = json.dumps(payload, sort_keys=True, separators=(',', ':'))
    return dict(**payload, version=hashlib.sha256(encoded.encode()).hexdigest())


def accept_vehicle_run(conn, user, day, truck, stamp):
    """Bind an unchanged, accepted overview to the selected vehicle atomically."""
    jobs, snapshot, version = manifest(conn, truck['id'], day['work_date'])
    if not jobs:
        return
    ensure_run_unstarted(conn, truck['id'], day['work_date'])
    if conn.execute('''SELECT 1 FROM bf_jobs WHERE truck_id=? AND scheduled_date=? AND schedule_live=1
            AND approval_status='approved' AND status!='cancelled' AND (status!='scheduled' OR scan_mode!=1)''',
                    (truck['id'], day['work_date'])).fetchone() or any(not j['bins'] for j in jobs):
        raise ValueError('The office needs to finish the bin allocations for this run.')
    if conn.execute('''SELECT 1 FROM bf_runs WHERE driver_id=? AND run_date=? AND truck_id!=?
            AND accepted_at IS NOT NULL AND returned_at IS NULL''',
                    (user['id'], day['work_date'], truck['id'])).fetchone():
        raise ValueError('Finish your other accepted run before selecting this vehicle.')
    conn.execute('''UPDATE bf_jobs SET driver_id=?,vehicle=? WHERE truck_id=? AND scheduled_date=?
        AND schedule_live=1 AND approval_status='approved' AND status!='cancelled' ''',
                 (user['id'], truck['name'], truck['id'], day['work_date']))
    _, snapshot, version = manifest(conn, truck['id'], day['work_date'])
    person = (user['first_name'] + ' ' + user['last_name']).strip()
    conn.execute('''UPDATE bf_runs SET acceptance_required=1,accepted_at=?,accepted_by=?,accepted_name=?,accepted_manifest=?
        WHERE truck_id=? AND run_date=?''', (stamp, user['id'], person, version, truck['id'], day['work_date']))
    conn.execute('''INSERT INTO bf_run_acceptance_history(truck_id,run_date,action,user_id,actor,manifest,created_at)
        VALUES(?,?,'Daily overview accepted; vehicle selected',?,?,?,?)''',
                 (truck['id'], day['work_date'], user['id'], person, snapshot, stamp))


def complete_scanned_client(conn, job, user, stamp):
    """Create an honest driver QR confirmation after the last client scan, once."""
    from binflow_workday import run_workday
    day = run_workday(conn, user['id'], job['truck_id'], job['scheduled_date'])
    if not day or day['flow_version'] != 2 or job['status'] != 'scheduled':
        return
    rows = conn.execute('''SELECT i.*,b.truck_id,b.truck_load,b.location_id,b.status AS bin_status
        FROM bf_job_bins i JOIN bf_bins b ON b.id=i.bin_id WHERE i.job_id=?''', (job['id'],)).fetchall()
    if not rows or any(not (r['delivered_at'] if r['direction'] == 'deliver' else r['picked_up_at']) for r in rows):
        return
    for r in rows:
        if r['direction'] == 'deliver':
            if r['location_id'] != job['site_id'] or r['truck_id'] or r['bin_status'] not in ('at_client', 'full'):
                raise ValueError('A delivered bin changed location. Contact the office.')
        elif r['truck_id'] != job['truck_id'] or r['truck_load'] != 'full':
            raise ValueError('A collected bin is no longer on this truck. Contact the office.')
    collected = any(r['direction'] == 'collect' for r in rows)
    conn.execute("UPDATE bf_job_bins SET active=0 WHERE job_id=? AND direction='deliver'", (job['id'],))
    conn.execute('''UPDATE bf_jobs SET status=?,collected_at=?,collected_by=?,confirmation='driver_scanned',
        confirmation_note='Driver confirmed each delivery and collection by QR scan. No client signature was collected.',
        completed_at=? WHERE id=? AND status='scheduled' ''',
                 ('collected' if collected else 'complete', stamp,
                  (user['first_name'] + ' ' + user['last_name']).strip(), None if collected else stamp, job['id']))


def register_journey(app, db, access, render, success, failure, now):
    from binflow_workday import workday_event, active_workday

    def owned(day_id):
        day = db().execute('SELECT * FROM bf_workdays WHERE id=? AND user_id=?', (day_id, g.user['id'])).fetchone()
        if not day:
            abort(404)
        selected_state(g.user, day['state'])
        if day['finished_at']:
            raise ValueError('This day is already finished.')
        return day

    @app.post('/binflow/driver/my-day/<int:day_id>/overview')
    @access
    def bf_accept_overview(day_id):
        try:
            db().execute('BEGIN IMMEDIATE')
            day = owned(day_id)
            if day['truck_id']:
                raise ValueError('Continue the run for your selected vehicle.')
            state = selected_state(g.user, request.form.get('state', day['state']))
            if state not in ('QLD','NSW','VIC','SA','WA','TAS','ACT','NT'):
                raise ValueError('Select your operating state.')
            plan = overview(db(), g.user, day['work_date'], state)
            if request.form.get('version') != plan['version']:
                raise ValueError('Your daily overview changed. Review the updated stops and accept again.')
            db().execute('UPDATE bf_workdays SET state=?,overview_hash=?,overview_accepted_at=? WHERE id=?',
                         (state, plan['version'], now(), day_id))
            workday_event(db(), day_id, g.user['id'], 'Daily overview accepted', now(), plan['version'])
            return success('Daily overview accepted. Select your vehicle.', 'bf_workday')
        except ValueError as error:
            return failure(error, 'bf_workday', summary='1')

    @app.get('/binflow/driver/my-day/overview-state')
    @access
    def bf_overview_state():
        day = active_workday(db(), g.user['id'])
        if not day:
            abort(404)
        state = selected_state(g.user, request.args.get('state', day['state']))
        return jsonify(version=overview(db(), g.user, day['work_date'], state)['version'])

    @app.post('/binflow/driver/my-day/<int:day_id>/break/<action>')
    @access
    def bf_workday_break(day_id, action):
        try:
            db().execute('BEGIN IMMEDIATE')
            day = owned(day_id)
            stamp = now()
            if action == 'start':
                if not day['break_started_at']:
                    db().execute('UPDATE bf_workdays SET break_started_at=? WHERE id=?', (stamp, day_id))
                    workday_event(db(), day_id, g.user['id'], '30-minute break started', stamp)
            elif action == 'finish':
                if not day['break_started_at']:
                    raise ValueError('Start your break first.')
                if (datetime.fromisoformat(stamp) - datetime.fromisoformat(day['break_started_at'])).total_seconds() < 1800:
                    raise ValueError('Your 30-minute break is still running.')
                if not day['break_finished_at']:
                    db().execute('UPDATE bf_workdays SET break_finished_at=? WHERE id=?', (stamp, day_id))
                    workday_event(db(), day_id, g.user['id'], '30-minute break finished', stamp)
            else:
                abort(404)
            return success('Break saved.', 'bf_workday')
        except ValueError as error:
            return failure(error, 'bf_workday')

    @app.get('/binflow/driver/my-day/clock-state')
    @access
    def bf_clock_state():
        day = active_workday(db(), g.user['id'])
        return jsonify(server_now=now(), day=dict(day) if day else None)

    @app.post('/binflow/driver/<int:truck_id>/test-prestart')
    @access
    def bf_test_prestart(truck_id):
        from binflow_runs import run_data, check_owner, valid_date
        from binflow_workday import driver_today
        run_date = request.form.get('date', '')
        try:
            valid_date(run_date)
            db().execute('BEGIN IMMEDIATE')
            check_owner(db(), truck_id, run_date, g.user)
            data = run_data(db(), truck_id, run_date, g.user['id'], driver_today(db(), g.user, now()))
            if not data['simple_flow'] or data['stage'] != 'prestart' or not data['test_prestart_allowed']:
                raise ValueError('The testing prestart is not available for this vehicle. Follow the current step.')
            db().execute('UPDATE bf_runs SET test_prestart_at=?,test_prestart_date=?,test_prestart_by=? WHERE truck_id=? AND run_date=?',
                         (now(), driver_today(db(), g.user, now()), g.user['id'], truck_id, run_date))
            workday_event(db(), data['workday']['id'], g.user['id'], 'Testing prestart OK', now(),
                          'Temporary test acknowledgement only; no signed vehicle inspection created.')
            return success('Testing prestart OK. Load your bins.', 'bf_run', truck_id=truck_id, date=run_date)
        except ValueError as error:
            return failure(error, 'bf_run', truck_id=truck_id, date=run_date or now()[:10])
