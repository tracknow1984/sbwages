"""Guided daily truck runs. All custody mutations are gated on the server."""
import json
import re
import secrets
from datetime import date, timedelta
from urllib.parse import urlparse

from flask import abort, g, jsonify, redirect, request, url_for

from prestart_config import VEHICLE_CHECKS, DECLARATION
from prestart_media import signature_png
from binflow_requirements import load_plan

CHECKS = VEHICLE_CHECKS + [('lift', 'Tail lift / bin lifter and controls (if fitted)', True)]


def migrate_runs(conn):
    conn.executescript('''
    CREATE TABLE IF NOT EXISTS bf_runs (
        truck_id INTEGER NOT NULL REFERENCES bf_trucks(id), run_date TEXT NOT NULL,
        driver_id INTEGER REFERENCES users(id), departed_at TEXT, returned_at TEXT,
        PRIMARY KEY(truck_id,run_date));
    CREATE TABLE IF NOT EXISTS bf_run_inspections (
        id INTEGER PRIMARY KEY, truck_id INTEGER NOT NULL REFERENCES bf_trucks(id),
        inspection_date TEXT NOT NULL, driver_id INTEGER NOT NULL REFERENCES users(id),
        prestart_id INTEGER NOT NULL UNIQUE REFERENCES prestarts(id), passed INTEGER NOT NULL,
        resolved_at TEXT, resolved_by TEXT, resolution TEXT NOT NULL DEFAULT '');
    CREATE INDEX IF NOT EXISTS bf_inspection_day ON bf_run_inspections(truck_id,inspection_date,driver_id);
    ''')
    columns = {r['name'] for r in conn.execute('PRAGMA table_info(bf_jobs)')}
    for name, definition in [('run_order', 'INTEGER NOT NULL DEFAULT 0'), ('scheduled_time', "TEXT NOT NULL DEFAULT ''")]:
        if name not in columns:
            conn.execute(f'ALTER TABLE bf_jobs ADD COLUMN {name} {definition}')


def valid_date(value):
    try:
        parsed = date.fromisoformat(value)
        if parsed.isoformat() != value:
            raise ValueError()
        return value
    except (TypeError, ValueError):
        raise ValueError('Choose a valid run date.') from None


def run_jobs(conn, truck_id, run_date):
    return conn.execute("SELECT * FROM bf_jobs WHERE truck_id=? AND scheduled_date=? AND status!='cancelled' ORDER BY run_order,id",
                        (truck_id, run_date)).fetchall()


def run_data(conn, truck_id, run_date, driver_id, today):
    truck = conn.execute('SELECT * FROM bf_trucks WHERE id=?', (truck_id,)).fetchone()
    if not truck:
        abort(404)
    jobs = run_jobs(conn, truck_id, run_date)
    run = conn.execute('SELECT * FROM bf_runs WHERE truck_id=? AND run_date=?', (truck_id, run_date)).fetchone()
    run = dict(run) if run else dict(truck_id=truck_id, run_date=run_date, driver_id=None, departed_at=None, returned_at=None)
    rows = conn.execute('''SELECT i.*,j.client_name,j.site_name,j.warehouse_name,j.warehouse_id,j.site_id
        FROM bf_job_bins i JOIN bf_jobs j ON j.id=i.job_id
        WHERE j.truck_id=? AND j.scheduled_date=? AND j.status!='cancelled'
        ORDER BY j.run_order,j.id,i.direction,i.bin_code''', (truck_id, run_date)).fetchall()
    current = next((j for j in jobs if j['status']=='scheduled'), None)
    hold = conn.execute('''SELECT i.*,p.checks_json,p.notes,p.employee_name,p.submitted_at FROM bf_run_inspections i
        JOIN prestarts p ON p.id=i.prestart_id WHERE i.truck_id=? AND i.passed=0 AND i.resolved_at IS NULL
        ORDER BY i.id DESC LIMIT 1''', (truck_id,)).fetchone()
    passed = conn.execute('''SELECT i.* FROM bf_run_inspections i WHERE truck_id=? AND inspection_date=? AND driver_id=?
        AND passed=1 AND id>COALESCE((SELECT MAX(id) FROM bf_run_inspections WHERE truck_id=? AND passed=0),0)
        ORDER BY id DESC LIMIT 1''', (truck_id,today,driver_id,truck_id)).fetchone()
    waiting = [r for r in rows if r['direction']=='deliver' and not r['loaded_at'] and not r['delivered_at']]
    returns = [r for r in rows if r['direction']=='collect' and not r['returned_at']]
    current_rows = [r for r in rows if current and r['job_id']==current['id']]
    action, scan_rows = None, []
    if not jobs:
        stage = 'empty'
    elif run_date > today:
        stage = 'planned'
    elif run['returned_at']:
        stage = 'done' if all(j['status']=='complete' for j in jobs) else 'weighing'
    elif hold:
        stage = 'hold'
    elif not passed:
        stage = 'prestart'
    elif not run['departed_at']:
        stage = 'load' if waiting else 'ready'
        action, scan_rows = ('load',waiting) if waiting else (None,[])
    elif current:
        deliver = [r for r in current_rows if r['direction']=='deliver' and not r['delivered_at']]
        collect = [r for r in current_rows if r['direction']=='collect' and not r['picked_up_at']]
        stage = 'deliver' if deliver else 'collect' if collect else 'handover'
        action = stage if stage != 'handover' else None
        scan_rows = deliver if deliver else collect
    elif returns:
        stage, action, scan_rows = 'return', 'return', returns
    else:
        stage = 'arrive'
    return dict(truck=truck,run=run,jobs=jobs,rows=rows,current=current,current_rows=current_rows,
                hold=hold,passed=passed,stage=stage,action=action,scan_rows=scan_rows,
                remaining=len(scan_rows),stop_number=next((n for n,j in enumerate(jobs,1) if current and j['id']==current['id']),0),
                completed_stops=sum(bool(j['collected_at']) for j in jobs))


def check_owner(conn, truck_id, run_date, user):
    if user['role']=='admin':
        return
    run = conn.execute('SELECT driver_id FROM bf_runs WHERE truck_id=? AND run_date=?', (truck_id,run_date)).fetchone()
    assigned = {r['driver_id'] for r in run_jobs(conn,truck_id,run_date) if r['driver_id']}
    if (run and run['driver_id'] and run['driver_id']!=user['id']) or (assigned and assigned!={user['id']}):
        abort(403, 'This run belongs to another driver. Ask the office to update the schedule.')


def guard_movement(conn, j, action, user, today):
    check_owner(conn,j['truck_id'],j['scheduled_date'],user)
    data = run_data(conn,j['truck_id'],j['scheduled_date'],user['id'],today)
    if action=='unload' and data['passed'] and not data['hold'] and not data['run']['departed_at']:
        return
    expected = 'handover' if action=='exchange' else action
    if data['stage'] != expected:
        labels = {'prestart':'Complete today’s vehicle prestart first.', 'hold':'This truck is on hold. Contact the office.',
                  'load':'Load all scheduled empties before leaving the warehouse.', 'ready':'Tap Start first stop before client scans.',
                  'deliver':'Finish dropping the empty bins at the current client.', 'collect':'Finish picking up the full bins at the current client.',
                  'handover':'Finish the current client handover first.', 'return':'Return and scan the full bins into the warehouse.',
                  'planned':'This run is scheduled for a future day.'}
        raise ValueError(labels.get(data['stage'],'This movement is closed for this run.'))
    if action in ('deliver','collect','exchange') and (not data['current'] or data['current']['id']!=j['id']):
        raise ValueError('This is not your current client. Follow the next stop on your driver screen.')


def mark_returned(conn, truck_id, run_date, timestamp):
    if conn.execute("SELECT 1 FROM bf_jobs WHERE truck_id=? AND scheduled_date=? AND status='scheduled'", (truck_id,run_date)).fetchone():
        return
    if conn.execute('''SELECT 1 FROM bf_job_bins i JOIN bf_jobs j ON j.id=i.job_id WHERE j.truck_id=? AND j.scheduled_date=?
        AND j.status!='cancelled' AND i.direction='collect' AND i.returned_at IS NULL''', (truck_id,run_date)).fetchone():
        return
    conn.execute('UPDATE bf_runs SET returned_at=COALESCE(returned_at,?) WHERE truck_id=? AND run_date=?', (timestamp,truck_id,run_date))


def register_runs(app, db, access, require, render, success, failure, now, actor):
    today = lambda: now()[:10]

    def data_for(truck_id, run_date):
        valid_date(run_date)
        check_owner(db(),truck_id,run_date,g.user)
        return run_data(db(),truck_id,run_date,g.user['id'],today())

    @app.get('/binflow/driver/<int:truck_id>/day')
    @access
    def bf_run(truck_id):
        run_date = request.args.get('date', today())
        try:
            data = data_for(truck_id,run_date)
        except ValueError as error:
            abort(400,str(error))
        if data['stage']=='prestart':
            session_key = secrets.token_urlsafe(24)
        else:
            session_key = ''
        return render('run',section='driver',driver_mode=True,run_date=run_date,today=today(),checks=CHECKS,declaration=DECLARATION,
                      submission_token=session_key,loading=load_plan(db(),truck_id,run_date),**data)

    @app.post('/binflow/driver/<int:truck_id>/prestart')
    @access
    def bf_run_prestart(truck_id):
        run_date = request.form.get('date','')
        try:
            valid_date(run_date)
            db().execute('BEGIN IMMEDIATE')
            data = data_for(truck_id,run_date)
            token = request.form.get('submission_token','')
            if not re.fullmatch(r'[A-Za-z0-9_-]{24,80}',token):
                raise ValueError('Refresh the prestart form and try again.')
            if db().execute('SELECT 1 FROM prestarts WHERE submission_token=?',(token,)).fetchone():
                return success('Prestart already saved.','bf_run',truck_id=truck_id,date=run_date)
            if data['stage']!='prestart':
                raise ValueError('A prestart is not available for this run now. Refresh your driver screen.')
            reading = request.form.get('reading','').strip()
            if not re.fullmatch(r'\d{1,8}(\.\d{1,2})?',reading):
                raise ValueError('Enter a valid odometer reading in kilometres.')
            results=[]
            for key,label,allow_na in CHECKS:
                answer=request.form.get('check_'+key)
                note=request.form.get('note_'+key,'').strip()
                if answer not in (('pass','fail','na') if allow_na else ('pass','fail')):
                    raise ValueError('Answer every vehicle check.')
                if len(note)>500 or (answer=='fail' and not note):
                    raise ValueError('Add a short defect note for each failed check.')
                results.append(dict(key=key,label=label,value=answer,note=note))
            fit=request.form.get('fit_for_duty')
            notes=request.form.get('notes','').strip()
            if fit not in ('yes','no') or len(notes)>2000 or (fit=='no' and not notes):
                raise ValueError('Confirm fit for duty and explain any concern.')
            signature=signature_png(request.form.get('signature',''))
            if request.form.get('signed')!='yes':
                raise ValueError('Confirm the prestart declaration.')
            failures=sum(r['value']=='fail' for r in results)
            passed=not failures and fit=='yes'
            pid=db().execute('''INSERT INTO prestarts(user_id,employee_name,asset,asset_type,reading,reading_unit,
                checks_json,failure_count,fit_for_duty,notes,signature,declaration,submitted_at,submission_token,signature_image)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''', (g.user['id'],actor(),data['truck']['name'],'vehicle',reading,'km',
                json.dumps(results),failures,fit,notes,actor(),DECLARATION,now(),token,signature)).lastrowid
            db().execute('INSERT INTO bf_run_inspections(truck_id,inspection_date,driver_id,prestart_id,passed) VALUES(?,?,?,?,?)',
                         (truck_id,today(),g.user['id'],pid,int(passed)))
            db().execute('INSERT OR IGNORE INTO bf_runs(truck_id,run_date,driver_id) VALUES(?,?,?)',(truck_id,run_date,g.user['id']))
            db().execute('UPDATE bf_runs SET driver_id=COALESCE(driver_id,?) WHERE truck_id=? AND run_date=?',(g.user['id'],truck_id,run_date))
            return success('Prestart passed. Load your scheduled bins.' if passed else 'Prestart saved. Truck on hold — contact the office.',
                           'bf_run',truck_id=truck_id,date=run_date)
        except ValueError as error:
            return failure(error,'bf_run',truck_id=truck_id,date=run_date if run_date else today())

    @app.post('/binflow/prestarts/<int:inspection_id>/resolve')
    @require('admin')
    def bf_resolve_prestart(inspection_id):
        try:
            note=request.form.get('resolution','').strip()
            if not note or len(note)>2000:
                raise ValueError('Record the repair / resolution before requesting a fresh prestart.')
            db().execute('BEGIN IMMEDIATE')
            row=db().execute('SELECT * FROM bf_run_inspections WHERE id=?',(inspection_id,)).fetchone()
            if not row: abort(404)
            db().execute('UPDATE bf_run_inspections SET resolved_at=?,resolved_by=?,resolution=? WHERE id=? AND passed=0 AND resolved_at IS NULL',
                         (now(),actor(),note,inspection_id))
            return success('Resolution recorded. The driver must pass a new prestart.','bf_calendar')
        except ValueError as error:
            return failure(error,'bf_calendar')

    @app.post('/binflow/driver/<int:truck_id>/day/<action>')
    @access
    def bf_run_action(truck_id,action):
        run_date=request.form.get('date','')
        try:
            db().execute('BEGIN IMMEDIATE')
            data=data_for(truck_id,run_date)
            if action=='depart':
                if data['run']['departed_at']:
                    return success('Run already started.','bf_run',truck_id=truck_id,date=run_date)
                if data['stage']!='ready':
                    raise ValueError('Pass today’s prestart and scan all scheduled empties onto the truck first.')
                other=db().execute('''SELECT 1 FROM bf_bins b JOIN bf_job_bins i ON i.bin_id=b.id AND i.active=1
                    JOIN bf_jobs j ON j.id=i.job_id WHERE b.truck_id=? AND j.scheduled_date!=?''',(truck_id,run_date)).fetchone()
                if other:
                    raise ValueError('Finish and unload the previous run before starting this one.')
                db().execute('UPDATE bf_runs SET departed_at=? WHERE truck_id=? AND run_date=?',(now(),truck_id,run_date))
                message='Run started. Follow your first client stop.'
            elif action=='arrive':
                if data['stage'] not in ('arrive','done','weighing'):
                    raise ValueError('Finish all client stops and scan every returning bin first.')
                mark_returned(db(),truck_id,run_date,now())
                message='Warehouse return recorded. The office can finalise any remaining weights.'
            else:
                abort(404)
            return success(message,'bf_run',truck_id=truck_id,date=run_date)
        except ValueError as error:
            return failure(error,'bf_run',truck_id=truck_id,date=run_date or today())

    @app.post('/binflow/driver/<int:truck_id>/day/scan-bin')
    @access
    def bf_run_scan(truck_id):
        # Resolve a bin to its allocated job; the driver never chooses a client or movement.
        try:
            run_date=valid_date(request.form.get('date',''))
            data=data_for(truck_id,run_date)
            value=request.form.get('code','').strip()[:1000]
            if value.startswith(('http://','https://')):
                value=urlparse(value).path.rstrip('/').split('/')[-1]
            row=db().execute('''SELECT i.job_id FROM bf_bins b JOIN bf_job_bins i ON i.bin_id=b.id
                JOIN bf_jobs j ON j.id=i.job_id WHERE (b.code=? COLLATE NOCASE OR b.token=?)
                AND j.truck_id=? AND j.scheduled_date=? AND j.status!='cancelled'
                AND i.direction=? ORDER BY i.active DESC,j.id DESC LIMIT 1''',
                (value,value,truck_id,run_date,'deliver' if data['action'] in ('load','deliver') else 'collect')).fetchone()
            if not row or not data['action']:
                raise ValueError('This bin is not expected for the current step. Check the bin ID and the list below.')
            # Use the same transactional scan implementation as the detailed manifest.
            return app.extensions['bf_scan'](truck_id, guided_job=row['job_id'], guided_action=data['action'], guided_date=run_date)
        except ValueError as error:
            return jsonify(ok=False,message=str(error)),409

    @app.get('/binflow/driver/<int:truck_id>/day/state')
    @access
    def bf_run_state(truck_id):
        try:
            data=data_for(truck_id,request.args.get('date',today()))
            return jsonify(stage=data['stage'],remaining=data['remaining'],current=data['current']['id'] if data['current'] else None)
        except ValueError as error:
            return jsonify(error=str(error)),400

    def calendar_data(run_date):
        jobs=db().execute('''SELECT j.*,t.name AS truck_name,u.first_name||' '||u.last_name AS driver_name,
            r.departed_at,r.returned_at,
            (SELECT COUNT(*) FROM bf_job_bins i WHERE i.job_id=j.id AND i.direction='collect' AND i.weighed_at IS NULL) AS unweighed,
            (SELECT COUNT(*) FROM bf_job_bins i WHERE i.job_id=j.id AND (i.loaded_at IS NOT NULL OR i.picked_up_at IS NOT NULL)) AS moved
            FROM bf_jobs j LEFT JOIN bf_trucks t ON t.id=j.truck_id
            LEFT JOIN bf_runs r ON r.truck_id=j.truck_id AND r.run_date=j.scheduled_date
            LEFT JOIN users u ON u.id=COALESCE(r.driver_id,j.driver_id)
            WHERE j.scheduled_date=? AND j.status!='cancelled' ORDER BY t.state,t.name,j.run_order,j.id''',(run_date,)).fetchall()
        groups={}
        for row in jobs:
            j=dict(row)
            if j['received_at'] or (j['returned_at'] and j['collected_at']):
                j.update(colour='orange',phase='Back at warehouse',detail='POD closed' if j['status']=='complete' else f'{j["unweighed"]} bins to weigh')
            elif j['collected_at']:
                j.update(colour='green',phase='Client completed',detail='POD closed' if j['status']=='complete' else 'Returning full bins')
            else:
                active=(j['departed_at'] and not any(k['status']=='scheduled' for k in groups.get(j['truck_id'],{}).get('jobs',[]))) or j['moved']
                j.update(colour='blue' if active else 'queued',phase='In progress' if active else 'Scheduled',detail='')
            groups.setdefault(j['truck_id'],dict(truck_id=j['truck_id'],name=j['truck_name'] or 'Truck unassigned',driver=j['driver_name'],jobs=[]))['jobs'].append(j)
        holds=db().execute('''SELECT i.*,t.name,p.employee_name,p.checks_json,p.notes FROM bf_run_inspections i
            JOIN bf_trucks t ON t.id=i.truck_id JOIN prestarts p ON p.id=i.prestart_id
            WHERE i.passed=0 AND i.resolved_at IS NULL ORDER BY i.id DESC''').fetchall()
        return dict(groups=list(groups.values()),holds=[dict(h,checks=json.loads(h['checks_json'])) for h in holds])

    @app.get('/binflow/calendar')
    @access
    def bf_calendar():
        try: run_date=valid_date(request.args.get('date',today()))
        except ValueError as error: abort(400,str(error))
        day=date.fromisoformat(run_date)
        week=[(day+timedelta(days=n)).isoformat() for n in range(-3,4)]
        employees=db().execute("SELECT id,first_name,last_name FROM users WHERE status='active' AND (role='admin' OR id IN (SELECT user_id FROM bf_access)) ORDER BY first_name,last_name").fetchall()
        return render('calendar',section='calendar',run_date=run_date,week=week,employees=employees,**calendar_data(run_date))

    @app.get('/binflow/calendar/state')
    @access
    def bf_calendar_state():
        from flask import render_template
        try: run_date=valid_date(request.args.get('date',today()))
        except ValueError as error: abort(400,str(error))
        return jsonify(html=render_template('binflow/calendar_cards.html',run_date=run_date,**calendar_data(run_date)))

    @app.post('/binflow/driver/<int:truck_id>/schedule')
    @require('admin')
    def bf_schedule_run(truck_id):
        run_date=request.form.get('date','')
        try:
            valid_date(run_date)
            db().execute('BEGIN IMMEDIATE')
            jobs=run_jobs(db(),truck_id,run_date)
            if not jobs: raise ValueError('Assign client jobs to this truck first.')
            existing=db().execute('SELECT departed_at,returned_at FROM bf_runs WHERE truck_id=? AND run_date=?',(truck_id,run_date)).fetchone()
            if (existing and (existing['departed_at'] or existing['returned_at'])) or any(j['status']!='scheduled' for j in jobs) or db().execute('''SELECT 1 FROM bf_job_bins i JOIN bf_jobs j ON j.id=i.job_id
                WHERE j.truck_id=? AND j.scheduled_date=? AND (i.loaded_at IS NOT NULL OR i.picked_up_at IS NOT NULL)''',(truck_id,run_date)).fetchone():
                raise ValueError('This run has started. Its driver and stop order are locked to protect bin movements.')
            uid=int(request.form.get('driver_id','0'))
            if not db().execute("SELECT 1 FROM users WHERE id=? AND status='active' AND (role='admin' OR id IN (SELECT user_id FROM bf_access))",(uid,)).fetchone():
                raise ValueError('Choose a staff member with transport access.')
            orders=[]
            for j in jobs:
                order=int(request.form.get('order_'+str(j['id']),'0'))
                clock=request.form.get('time_'+str(j['id']),'')
                if not 1<=order<=len(jobs) or (clock and not re.fullmatch(r'(?:[01]\d|2[0-3]):[0-5]\d',clock)):
                    raise ValueError('Use a unique stop number from 1 to the number of clients, and valid arrival times.')
                orders.append(order)
                db().execute('UPDATE bf_jobs SET run_order=?,scheduled_time=?,driver_id=? WHERE id=?',(order,clock,uid,j['id']))
            if len(set(orders))!=len(jobs): raise ValueError('Each client needs a different stop number.')
            db().execute('INSERT INTO bf_runs(truck_id,run_date,driver_id) VALUES(?,?,?) ON CONFLICT(truck_id,run_date) DO UPDATE SET driver_id=excluded.driver_id',(truck_id,run_date,uid))
            return success('Driver and stop order saved.','bf_calendar',date=run_date)
        except (ValueError,TypeError) as error:
            return failure(error if str(error) else ValueError('Check the schedule fields.'),'bf_calendar',date=run_date or today())
