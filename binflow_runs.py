"""Guided daily truck runs. All custody mutations are gated on the server."""
import json
import re
import secrets
from datetime import date
from urllib.parse import urlparse

from flask import abort, g, jsonify, redirect, request, url_for

from prestart_config import VEHICLE_CHECKS, DECLARATION
from prestart_media import signature_png
from binflow_requirements import load_plan
from binflow_approvals import ensure_approved
from binflow_acceptance import acceptance_ok, manifest, schedule_changed
from binflow_workday import run_workday, driver_today
from binflow_permissions import can_view_job, is_office, require_state, selected_state, eligible_driver

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
    return conn.execute("SELECT * FROM bf_jobs WHERE truck_id=? AND scheduled_date=? AND schedule_live=1 AND approval_status='approved' AND status!='cancelled' ORDER BY run_order,id",
                        (truck_id, run_date)).fetchall()


def run_data(conn, truck_id, run_date, driver_id, today):
    truck = conn.execute('SELECT * FROM bf_trucks WHERE id=?', (truck_id,)).fetchone()
    if not truck:
        abort(404)
    jobs = run_jobs(conn, truck_id, run_date)
    run = conn.execute('SELECT * FROM bf_runs WHERE truck_id=? AND run_date=?', (truck_id, run_date)).fetchone()
    run = dict(run) if run else dict(truck_id=truck_id, run_date=run_date, driver_id=None, departed_at=None, returned_at=None,
        acceptance_required=1,accepted_at=None,accepted_by=None,accepted_name='',accepted_manifest='')
    accepted=acceptance_ok(conn,run,truck_id,run_date)
    workday=run_workday(conn,driver_id,truck_id,run_date)
    person=conn.execute('SELECT role FROM users WHERE id=?',(driver_id,)).fetchone()
    clock_required=bool(person and person['role']=='employee' and run.get('clock_required',1))
    clock_ready=bool((workday and not workday['finished_at']) or (not clock_required and not workday))
    rows = conn.execute('''SELECT i.*,j.client_name,j.site_name,j.warehouse_name,j.warehouse_id,j.site_id
        FROM bf_job_bins i JOIN bf_jobs j ON j.id=i.job_id
        WHERE j.truck_id=? AND j.scheduled_date=? AND j.schedule_live=1 AND j.approval_status='approved' AND j.status!='cancelled'
        ORDER BY j.run_order,j.id,i.direction,i.bin_code''', (truck_id, run_date)).fetchall()
    current = next((j for j in jobs if j['status']=='scheduled'), None)
    hold = conn.execute('''SELECT i.*,p.checks_json,p.notes,p.employee_name,p.submitted_at FROM bf_run_inspections i
        JOIN prestarts p ON p.id=i.prestart_id WHERE i.truck_id=? AND i.passed=0 AND i.resolved_at IS NULL
        ORDER BY i.id DESC LIMIT 1''', (truck_id,)).fetchone()
    passed = conn.execute('''SELECT i.* FROM bf_run_inspections i WHERE truck_id=? AND inspection_date=? AND driver_id=?
        AND passed=1 AND id>COALESCE((SELECT MAX(id) FROM bf_run_inspections WHERE truck_id=? AND passed=0),0)
        ORDER BY id DESC LIMIT 1''', (truck_id,today,driver_id,truck_id)).fetchone()
    simple_flow = bool(workday and workday['flow_version']==2)
    last_fail = conn.execute('SELECT MAX(id) FROM bf_run_inspections WHERE truck_id=? AND passed=0', (truck_id,)).fetchone()[0]
    # A test acknowledgement never clears a real defect, even after office resolution.
    test_prestart_allowed = not last_fail
    if simple_flow and test_prestart_allowed and run.get('test_prestart_by')==driver_id and run.get('test_prestart_date')==today:
        passed = dict(testing=True, submitted_at=run['test_prestart_at'])
    waiting = [r for r in rows if r['direction']=='deliver' and not r['loaded_at'] and not r['delivered_at']]
    returns = [r for r in rows if r['direction']=='collect' and not r['returned_at']]
    loading_job = next((j for j in jobs if waiting and j['id']==waiting[0]['job_id']), None)
    depot_id = returns[0]['warehouse_id'] if returns else jobs[-1]['warehouse_id'] if jobs else None
    depot = conn.execute('SELECT * FROM bf_locations WHERE id=?',(depot_id,)).fetchone() if depot_id else None
    depot_arrived = bool(depot and conn.execute('SELECT 1 FROM bf_run_depot_arrivals WHERE truck_id=? AND run_date=? AND warehouse_id=?',
                                              (truck_id,run_date,depot_id)).fetchone())
    current_rows = [r for r in rows if current and r['job_id']==current['id']]
    action, scan_rows = None, []
    if workday and workday['finished_at']:
        stage='finished'
    elif clock_required and not workday:
        stage='startday'
    elif workday and run_date>today:
        stage='planned'
    elif workday and hold:
        stage='hold'
    elif simple_flow and workday['break_started_at'] and not workday['break_finished_at']:
        stage='break'
    elif simple_flow and jobs and not accepted:
        stage='acceptance'
    elif workday and not passed:
        stage='prestart'
    elif not jobs:
        stage = 'empty'
    elif run['returned_at']:
        stage = 'done' if all(j['status']=='complete' for j in jobs) else 'weighing'
    elif not accepted:
        stage = 'acceptance'
    elif run['accepted_by'] and run['accepted_by']!=driver_id:
        stage = 'assigned'
    elif run_date > today:
        stage = 'planned'
    elif hold:
        stage = 'hold'
    elif not passed:
        stage = 'prestart'
    elif not run['departed_at']:
        stage = 'load' if waiting else 'ready'
        action, scan_rows = ('load',waiting) if waiting else (None,[])
        if simple_flow and waiting:
            scan_rows = [r for r in waiting if r['job_id']==loading_job['id']]
    elif current:
        deliver = [r for r in current_rows if r['direction']=='deliver' and not r['delivered_at']]
        collect = [r for r in current_rows if r['direction']=='collect' and not r['picked_up_at']]
        stage = 'deliver' if deliver else 'collect' if collect else 'handover'
        action = stage if stage != 'handover' else None
        scan_rows = deliver if deliver else collect
        if simple_flow and not current['driver_arrived_at']:
            stage,action,scan_rows = 'navigate',None,[]
    elif simple_flow and depot and not depot_arrived:
        stage = 'navigate_depot'
    elif returns:
        stage, action, scan_rows = 'return', 'return', returns
        if simple_flow:
            scan_rows = [r for r in returns if r['warehouse_id']==depot_id]
    else:
        stage = 'arrive'
    onboard = conn.execute('''SELECT b.code,b.truck_load,s.name AS size_name,j.client_name,j.site_name FROM bf_bins b
        JOIN bf_sizes s ON s.id=b.size_id LEFT JOIN bf_job_bins i ON i.bin_id=b.id AND i.active=1
        LEFT JOIN bf_jobs j ON j.id=i.job_id WHERE b.truck_id=? ORDER BY b.code''',(truck_id,)).fetchall()
    step_key = stage+':'+str(loading_job['id'] if stage=='load' and loading_job else depot_id if stage=='return' else current['id'] if current else '')
    return dict(truck=truck,run=run,jobs=jobs,rows=rows,current=current,current_rows=current_rows,
                simple_flow=simple_flow,test_prestart_allowed=test_prestart_allowed,loading_job=loading_job,
                depot=depot,onboard=onboard,step_key=step_key,
                hold=hold,passed=passed,stage=stage,action=action,scan_rows=scan_rows,
                remaining=len(scan_rows),stop_number=next((n for n,j in enumerate(jobs,1) if current and j['id']==current['id']),0),
                completed_stops=sum(bool(j['collected_at']) for j in jobs),acceptance_ok=accepted,
                workday=workday,clock_ready=clock_ready)


def check_owner(conn, truck_id, run_date, user):
    truck=conn.execute('SELECT state FROM bf_trucks WHERE id=?',(truck_id,)).fetchone()
    if not truck: abort(404)
    require_state(user,truck['state'])
    if is_office(user):
        return
    run = conn.execute('SELECT driver_id FROM bf_runs WHERE truck_id=? AND run_date=?', (truck_id,run_date)).fetchone()
    assigned = {r['driver_id'] for r in run_jobs(conn,truck_id,run_date) if r['driver_id']}
    if (run and run['driver_id'] and run['driver_id']!=user['id']) or (assigned and assigned!={user['id']}):
        abort(403, 'This run belongs to another driver. Ask the office to update the schedule.')


def guard_movement(conn, j, action, user, today):
    ensure_approved(j)
    if not j['schedule_live']:
        raise ValueError('This job is not in a published schedule. Ask the administrator to publish it first.')
    check_owner(conn,j['truck_id'],j['scheduled_date'],user)
    data = run_data(conn,j['truck_id'],j['scheduled_date'],user['id'],today)
    if not data['clock_ready']:
        raise ValueError('Start your driver day and select this vehicle before scanning. A finalised day cannot record more movements.')
    if action=='unload' and data['acceptance_ok'] and (not data['run']['accepted_by'] or data['run']['accepted_by']==user['id']) and data['passed'] and not data['hold'] and data['stage']!='break' and not data['run']['departed_at']:
        return
    expected = 'handover' if action=='exchange' else action
    if data['stage'] != expected:
        labels = {'acceptance':'Accept this schedule, then complete today’s prestart before scanning.',
                  'assigned':'This schedule was accepted by another driver.',
                  'prestart':'Complete today’s vehicle prestart first.', 'hold':'This truck is on hold. Contact the office.',
                  'load':'Load all scheduled empties before leaving the warehouse.', 'ready':'Tap Start first stop before client scans.',
                  'deliver':'Finish dropping the empty bins at the current client.', 'collect':'Finish picking up the full bins at the current client.',
                  'handover':'Finish the current client handover first.', 'return':'Return and scan the full bins into the warehouse.',
                  'planned':'This run is scheduled for a future day.'}
        raise ValueError(labels.get(data['stage'],'This movement is closed for this run.'))
    if action in ('deliver','collect','exchange') and (not data['current'] or data['current']['id']!=j['id']):
        raise ValueError('This is not your current client. Follow the next stop on your driver screen.')
    if data['simple_flow'] and action=='load' and (not data['loading_job'] or data['loading_job']['id']!=j['id']):
        raise ValueError('Load the bins for the client shown on screen first.')
    if data['simple_flow'] and action=='return' and (not data['depot'] or data['depot']['id']!=j['warehouse_id']):
        raise ValueError('This bin belongs to a different receiving warehouse. Follow the next depot shown on screen.')


def mark_returned(conn, truck_id, run_date, timestamp):
    if conn.execute("SELECT 1 FROM bf_jobs WHERE truck_id=? AND scheduled_date=? AND schedule_live=1 AND status='scheduled'", (truck_id,run_date)).fetchone():
        return
    if conn.execute('''SELECT 1 FROM bf_job_bins i JOIN bf_jobs j ON j.id=i.job_id WHERE j.truck_id=? AND j.scheduled_date=?
        AND j.schedule_live=1 AND j.status!='cancelled' AND i.direction='collect' AND i.returned_at IS NULL''', (truck_id,run_date)).fetchone():
        return
    conn.execute('UPDATE bf_runs SET returned_at=COALESCE(returned_at,?) WHERE truck_id=? AND run_date=?', (timestamp,truck_id,run_date))


def driver_plan_data(conn,user,run_date,state=''):
    jobs=conn.execute('''SELECT j.*,t.name AS truck_name,u.first_name||' '||u.last_name AS driver_name,
        r.departed_at,r.returned_at,r.accepted_at,r.accepted_name,r.acceptance_required,
        (SELECT COUNT(*) FROM bf_job_bins i WHERE i.job_id=j.id AND i.direction='collect' AND i.weighed_at IS NULL) AS unweighed,
        (SELECT COUNT(*) FROM bf_job_bins i WHERE i.job_id=j.id AND (i.loaded_at IS NOT NULL OR i.picked_up_at IS NOT NULL)) AS moved
        FROM bf_jobs j LEFT JOIN bf_trucks t ON t.id=j.truck_id
        LEFT JOIN bf_runs r ON r.truck_id=j.truck_id AND r.run_date=j.scheduled_date
        LEFT JOIN users u ON u.id=COALESCE(r.driver_id,j.driver_id)
        WHERE j.scheduled_date=? AND j.schedule_live=1 AND j.approval_status='approved' AND j.status!='cancelled' '''+
        ('AND j.state=? ' if state else '')+'ORDER BY t.state,t.name,j.run_order,j.id',
        (run_date,state) if state else (run_date,)).fetchall()
    groups={}
    for row in jobs:
        if not can_view_job(conn,user,row):
            continue
        j=dict(row)
        if j['received_at'] or (j['returned_at'] and j['collected_at']):
            j.update(colour='orange',phase='Back at warehouse',detail='POD closed' if j['status']=='complete' else f'{j["unweighed"]} bins to weigh')
        elif j['collected_at']:
            j.update(colour='green',phase='Client completed',detail='POD closed' if j['status']=='complete' else 'Returning full bins')
        else:
            active=(j['departed_at'] and not any(k['status']=='scheduled' for k in groups.get(j['truck_id'],{}).get('jobs',[]))) or j['moved']
            j.update(colour='blue' if active else 'queued',phase='In progress' if active else 'Scheduled',detail='')
        groups.setdefault(j['truck_id'],dict(truck_id=j['truck_id'],name=j['truck_name'] or 'Truck unassigned',driver=j['driver_name'],
            accepted_at=j['accepted_at'],accepted_name=j['accepted_name'],acceptance_required=j['acceptance_required'],jobs=[]))['jobs'].append(j)
    holds=conn.execute('''SELECT i.*,t.name,p.employee_name,p.checks_json,p.notes FROM bf_run_inspections i
        JOIN bf_trucks t ON t.id=i.truck_id JOIN prestarts p ON p.id=i.prestart_id
        WHERE i.passed=0 AND i.resolved_at IS NULL '''+('AND t.state=? ' if state else '')+
        'ORDER BY i.id DESC',(state,) if state else ()).fetchall()
    if not is_office(user):
        holds=[h for h in holds if h['driver_id']==user['id']]
    from binflow_schedule import monday
    clauses=["w.week_start=?", "w.status='published'"];params=[monday(run_date)]
    if state: clauses.append('w.state=?');params.append(state)
    if not is_office(user): clauses.append('w.driver_id=?');params.append(user['id'])
    plans=[]
    for row in conn.execute("""SELECT w.*,u.first_name||' '||u.last_name AS driver_name
            FROM bf_schedule_weeks w JOIN users u ON u.id=w.driver_id WHERE """+' AND '.join(clauses),params):
        plan=json.loads(row['plan_json'])
        day=next((d for d in plan['days'] if d['date']==run_date),None)
        if day and (day['items'] or day.get('notes')):
            plans.append(dict(day=day,state=row['state'],driver=row['driver_name'],resource=plan['resource']))
    return dict(groups=list(groups.values()),holds=[dict(h,checks=json.loads(h['checks_json'])) for h in holds],plans=plans)


def register_runs(app, db, access, require, render, success, failure, now, actor):
    today = lambda: driver_today(db(),g.user,now())

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
        assigned={j['driver_id'] for j in data['jobs'] if j['driver_id']}
        if data['run']['driver_id']: assigned.add(data['run']['driver_id'])
        can_accept=not assigned or assigned=={g.user['id']}
        return render('run',journey_mode=data['simple_flow'],section='driver',driver_mode=True,run_date=run_date,today=today(),checks=CHECKS,declaration=DECLARATION,
                      submission_token=session_key,loading=load_plan(db(),truck_id,run_date),can_accept=can_accept,
                      manifest_version=manifest(db(),truck_id,run_date)[2],
                      acceptance_history=db().execute('SELECT * FROM bf_run_acceptance_history WHERE truck_id=? AND run_date=? ORDER BY id',(truck_id,run_date)).fetchall(),**data)

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
            from binflow_reports import begin_current
            begin_current(db(),g.user,truck_id,run_date,now())
            return success(('Prestart passed. Review and accept today’s schedule.' if data['workday'] else 'Prestart passed. Load your scheduled bins.') if passed else 'Prestart saved. Truck on hold — contact the office.',
                           'bf_run',truck_id=truck_id,date=run_date)
        except ValueError as error:
            return failure(error,'bf_run',truck_id=truck_id,date=run_date if run_date else today())

    @app.post('/binflow/prestarts/<int:inspection_id>/resolve')
    @require('admin')
    def bf_resolve_prestart(inspection_id):
        row=db().execute('SELECT * FROM bf_run_inspections WHERE id=?',(inspection_id,)).fetchone()
        if not row: abort(404)
        try:
            note=request.form.get('resolution','').strip()
            if not note or len(note)>2000:
                raise ValueError('Record the repair / resolution before requesting a fresh prestart.')
            db().execute('BEGIN IMMEDIATE')
            db().execute('UPDATE bf_run_inspections SET resolved_at=?,resolved_by=?,resolution=? WHERE id=? AND passed=0 AND resolved_at IS NULL',
                         (now(),actor(),note,inspection_id))
            return success('Resolution recorded. The driver must pass a new prestart.','admin_prestart_detail',prestart_id=row['prestart_id'])
        except ValueError as error:
            return failure(error,'admin_prestart_detail',prestart_id=row['prestart_id'])

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
                if data['simple_flow']:
                    expected={r['bin_code'] for r in data['rows'] if r['direction']=='deliver' and not r['delivered_at']}
                    if expected!={r['code'] for r in data['onboard']}:
                        raise ValueError('The truck contains bins outside this manifest. Contact the office before leaving.')
                    db().execute('UPDATE bf_runs SET manifest_confirmed_at=?,onboard_manifest=? WHERE truck_id=? AND run_date=?',
                                 (now(),json.dumps([dict(r) for r in data['onboard']]),truck_id,run_date))
                db().execute('UPDATE bf_runs SET departed_at=? WHERE truck_id=? AND run_date=?',(now(),truck_id,run_date))
                message='Run started. Follow your first client stop.'
            elif action=='client-arrival':
                if data['stage']!='navigate' or request.form.get('job_id',type=int)!=data['current']['id']:
                    raise ValueError('Refresh and follow your current stop.')
                db().execute('UPDATE bf_jobs SET driver_arrived_at=COALESCE(driver_arrived_at,?) WHERE id=?',
                             (now(),data['current']['id']))
                message='Arrival saved. Scan the bins for this client.'
            elif action=='depot-arrival':
                if data['stage']!='navigate_depot' or request.form.get('warehouse_id',type=int)!=data['depot']['id']:
                    raise ValueError('Refresh and follow your current receiving depot.')
                db().execute('INSERT OR IGNORE INTO bf_run_depot_arrivals VALUES(?,?,?,?,?)',
                             (truck_id,run_date,data['depot']['id'],now(),g.user['id']))
                mark_returned(db(),truck_id,run_date,now())
                message='Depot arrival saved. Scan each full bin off the truck.'
            elif action=='arrive':
                if data['stage'] not in ('arrive','done','weighing'):
                    raise ValueError('Finish all client stops and scan every returning bin first.')
                mark_returned(db(),truck_id,run_date,now())
                message='Warehouse return recorded. The office can finalise any remaining weights.'
            else:
                abort(404)
            from binflow_reports import begin_current
            begin_current(db(),g.user,truck_id,run_date,now())
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
            requested_action=request.form.get('action') or data['action']
            if requested_action not in ('load','deliver','collect','return'):
                raise ValueError('Refresh and follow the current scan step.')
            value=request.form.get('code','').strip()[:1000]
            if value.startswith(('http://','https://')):
                value=urlparse(value).path.rstrip('/').split('/')[-1]
            row=db().execute('''SELECT i.job_id FROM bf_bins b JOIN bf_job_bins i ON i.bin_id=b.id
                JOIN bf_jobs j ON j.id=i.job_id WHERE (b.code=? COLLATE NOCASE OR b.token=?)
                AND j.truck_id=? AND j.scheduled_date=? AND j.status!='cancelled'
                AND i.direction=? ORDER BY i.active DESC,j.id DESC LIMIT 1''',
                (value,value,truck_id,run_date,'deliver' if requested_action in ('load','deliver') else 'collect')).fetchone()
            if data['stage']=='acceptance':
                raise ValueError('Accept this schedule before scanning bins.')
            if not row:
                raise ValueError('This bin is not expected for the current step. Check the bin ID and the list below.')
            # Use the same transactional scan implementation as the detailed manifest.
            return app.extensions['bf_scan'](truck_id, guided_job=row['job_id'], guided_action=requested_action, guided_date=run_date)
        except ValueError as error:
            return jsonify(ok=False,message=str(error)),409

    @app.get('/binflow/driver/<int:truck_id>/day/state')
    @access
    def bf_run_state(truck_id):
        try:
            data=data_for(truck_id,request.args.get('date',today()))
            return jsonify(stage=data['stage'],step_key=data['step_key'],remaining=data['remaining'],current=data['current']['id'] if data['current'] else None,
                           version=manifest(db(),truck_id,request.args.get('date',today()))[2])
        except ValueError as error:
            return jsonify(error=str(error)),400

    @app.get('/binflow/calendar')
    @access
    def bf_calendar():
        state=selected_state(g.user,request.args.get('state',''))
        if state not in ('','QLD','NSW','VIC','SA','WA','TAS','ACT','NT'): abort(400)
        try: run_date=valid_date(request.args.get('date',today()))
        except ValueError as error: abort(400,str(error))
        return redirect(url_for('bf_collection_calendar',state=state,date=run_date) if is_office(g.user)
                        else url_for('bf_workday',state=state,date=run_date,summary='1'))

    @app.get('/binflow/calendar/state')
    @app.get('/binflow/driver/my-day/plan-state')
    @access
    def bf_workday_plan_state():
        from flask import render_template
        state=selected_state(g.user,request.args.get('state',''))
        if state not in ('','QLD','NSW','VIC','SA','WA','TAS','ACT','NT'): abort(400)
        try: run_date=valid_date(request.args.get('date',today()))
        except ValueError as error: abort(400,str(error))
        return jsonify(html=render_template('binflow/driver_plan_cards.html',run_date=run_date,
                       **driver_plan_data(db(),g.user,run_date,state)))

    @app.post('/binflow/driver/<int:truck_id>/schedule')
    @require('admin')
    def bf_schedule_run(truck_id):
        run_date=request.form.get('date','')
        try:
            valid_date(run_date)
            db().execute('BEGIN IMMEDIATE')
            jobs=run_jobs(db(),truck_id,run_date)
            if not jobs: raise ValueError('Assign client jobs to this truck first.')
            if any(j['schedule_week_id'] for j in jobs):
                raise ValueError('Manage this driver and stop order in Collection calendar, then save to republish.')
            existing=db().execute('SELECT departed_at,returned_at FROM bf_runs WHERE truck_id=? AND run_date=?',(truck_id,run_date)).fetchone()
            if (existing and (existing['departed_at'] or existing['returned_at'])) or any(j['status']!='scheduled' for j in jobs) or db().execute('''SELECT 1 FROM bf_job_bins i JOIN bf_jobs j ON j.id=i.job_id
                WHERE j.truck_id=? AND j.scheduled_date=? AND (i.loaded_at IS NOT NULL OR i.picked_up_at IS NOT NULL)''',(truck_id,run_date)).fetchone():
                raise ValueError('This run has started. Its driver and stop order are locked to protect bin movements.')
            uid=int(request.form.get('driver_id','0'))
            truck=db().execute('SELECT state FROM bf_trucks WHERE id=?',(truck_id,)).fetchone()
            if not truck or not eligible_driver(db(),uid,truck['state']):
                raise ValueError('Choose an active driver or contractor allocated to this state, or a manager.')
            orders=[]
            before=manifest(db(),truck_id,run_date)[2]
            for j in jobs:
                order=int(request.form.get('order_'+str(j['id']),'0'))
                clock=request.form.get('time_'+str(j['id']),'')
                if not 1<=order<=len(jobs) or (clock and not re.fullmatch(r'(?:[01]\d|2[0-3]):[0-5]\d',clock)):
                    raise ValueError('Use a unique stop number from 1 to the number of clients, and valid arrival times.')
                orders.append(order)
                db().execute('UPDATE bf_jobs SET run_order=?,scheduled_time=?,driver_id=? WHERE id=?',(order,clock,uid,j['id']))
            if len(set(orders))!=len(jobs): raise ValueError('Each client needs a different stop number.')
            if before!=manifest(db(),truck_id,run_date)[2]:
                schedule_changed(db(),truck_id,run_date,g.user['id'],actor(),now(),'Office updated driver or stop order.')
            db().execute('INSERT INTO bf_runs(truck_id,run_date,driver_id) VALUES(?,?,?) ON CONFLICT(truck_id,run_date) DO UPDATE SET driver_id=excluded.driver_id',(truck_id,run_date,uid))
            return success('Driver and stop order saved.','bf_collection_calendar',date=run_date)
        except (ValueError,TypeError) as error:
            return failure(error if str(error) else ValueError('Check the schedule fields.'),'bf_collection_calendar',date=run_date or today())
