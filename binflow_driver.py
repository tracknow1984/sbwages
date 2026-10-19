"""Individual, transactional scans between warehouses, trucks and client sites."""
from urllib.parse import urlparse
from binflow_runs import guard_movement, check_owner, mark_returned, run_data, valid_date
import sqlite3
from binflow_qr import pickup_requests
from binflow_approvals import ensure_approved
from binflow_acceptance import schedule_cards, ensure_run_unstarted, schedule_changed

from flask import abort, g, jsonify, redirect, render_template, request, url_for


def migrate_driver(conn):
    # Additive migration: existing jobs and historical PODs keep their original workflow.
    conn.execute('''CREATE TABLE IF NOT EXISTS bf_trucks (
        id INTEGER PRIMARY KEY, name TEXT NOT NULL COLLATE NOCASE UNIQUE,
        state TEXT NOT NULL, created_at TEXT NOT NULL)''')
    additions = {
        'bf_bins': [('truck_id', 'INTEGER REFERENCES bf_trucks(id)'),
                    ('truck_load', "TEXT CHECK(truck_load IN ('empty','full'))")],
        'bf_jobs': [('truck_id', 'INTEGER REFERENCES bf_trucks(id)'),
                    ('scan_mode', 'INTEGER NOT NULL DEFAULT 0 CHECK(scan_mode IN (0,1))')],
        'bf_job_bins': [(key, 'TEXT') for key in ('loaded_at', 'delivered_at', 'picked_up_at', 'returned_at')],
        'bf_events': [('from_truck', 'INTEGER REFERENCES bf_trucks(id)'),
                      ('to_truck', 'INTEGER REFERENCES bf_trucks(id)')],
    }
    for table, columns in additions.items():
        existing = {row['name'] for row in conn.execute(f'PRAGMA table_info({table})')}
        for name, definition in columns:
            if name not in existing:
                conn.execute(f'ALTER TABLE {table} ADD COLUMN {name} {definition}')
    conn.execute('CREATE INDEX IF NOT EXISTS bf_bins_truck ON bf_bins(truck_id)')
    for operation in ('INSERT', 'UPDATE'):
        conn.execute(f'''CREATE TRIGGER IF NOT EXISTS bf_truck_custody_{operation.lower()}
            BEFORE {operation} ON bf_bins
            WHEN (NEW.truck_id IS NOT NULL AND (NEW.location_id IS NOT NULL OR NEW.status!='transit'
                  OR NEW.truck_load IS NULL)) OR (NEW.truck_id IS NULL AND NEW.truck_load IS NOT NULL)
            BEGIN SELECT RAISE(ABORT, 'Invalid truck custody'); END''')


def register_driver(app, db, access, require, bins, job, items, render, event,
                    success, failure, text_field, integer, now, states):
    def truck(truck_id):
        row = db().execute('SELECT * FROM bf_trucks WHERE id=?', (truck_id,)).fetchone()
        if not row:
            abort(404)
        return row

    def snapshot(truck_id, job_id=None):
        t = truck(truck_id)
        stock = bins('b.truck_id=?', (truck_id,))
        j = job(job_id) if job_id else None
        if j and j['truck_id'] != truck_id:
            abort(404)
        if j:
            check_owner(db(),truck_id,j['scheduled_date'],g.user)
        manifest = db().execute('''SELECT i.*,b.location_id,b.truck_id,b.truck_load,b.status,
            COALESCE(t.name,l.name,'In transit') AS current_location
            FROM bf_job_bins i JOIN bf_bins b ON b.id=i.bin_id
            LEFT JOIN bf_locations l ON l.id=b.location_id LEFT JOIN bf_trucks t ON t.id=b.truck_id
            WHERE i.job_id=? ORDER BY i.direction,i.bin_code''', (job_id,)).fetchall() if j else []
        ready = bool(j and all(i['delivered_at'] if i['direction']=='deliver' else i['picked_up_at'] for i in manifest))
        return dict(truck=t, stock=stock, job=j, manifest=manifest, handover_ready=ready,
                    empties=sum(b['truck_load']=='empty' for b in stock), full=sum(b['truck_load']=='full' for b in stock))

    @app.route('/binflow/driver', methods=['GET', 'POST'])
    @access
    def bf_drivers():
        if request.method == 'POST':
            if g.user['role'] != 'admin':
                abort(403)
            try:
                name, state = text_field('name', 80), text_field('state')
                if state not in states:
                    raise ValueError('Choose an Australian state or territory.')
                db().execute('BEGIN IMMEDIATE')
                tid = db().execute('INSERT INTO bf_trucks(name,state,created_at) VALUES(?,?,?)',
                                   (name, state, now())).lastrowid
                return success('Truck added. Assign a pickup / delivery to start scanning.', 'bf_truck', truck_id=tid)
            except (ValueError, sqlite3.IntegrityError) as error:
                return failure(error if isinstance(error, ValueError) else ValueError('That truck is already registered.'), 'bf_drivers')
        try:
            run_date=valid_date(request.args.get('date',now()[:10]))
        except ValueError as error:
            abort(400,str(error))
        state=request.args.get('state','')
        if state not in ('',)+tuple(states): abort(400)
        jobs = db().execute("SELECT * FROM bf_jobs WHERE approval_status='approved' AND status='scheduled' AND truck_id IS NULL ORDER BY scheduled_date,id").fetchall()
        previous=db().execute("""SELECT DISTINCT j.truck_id,j.scheduled_date,t.name,t.state FROM bf_jobs j JOIN bf_trucks t ON t.id=j.truck_id
            WHERE j.scheduled_date<? AND j.status IN ('scheduled','collected') ORDER BY j.scheduled_date DESC,t.name""",(run_date,)).fetchall()
        if g.user['role']!='admin':
            def mine(tid,day):
                run=db().execute('SELECT driver_id FROM bf_runs WHERE truck_id=? AND run_date=?',(tid,day)).fetchone()
                assigned={r['driver_id'] for r in db().execute("SELECT driver_id FROM bf_jobs WHERE truck_id=? AND scheduled_date=? AND status!='cancelled'",(tid,day)) if r['driver_id']}
                return (not run or not run['driver_id'] or run['driver_id']==g.user['id']) and (not assigned or assigned=={g.user['id']})
            previous=[r for r in previous if mine(r['truck_id'],r['scheduled_date'])]
        previous=[r for r in previous if state and r['state']==state]
        jobs=[j for j in jobs if state and j['state']==state and j['scheduled_date']==run_date]
        pending=db().execute("SELECT COUNT(*) FROM bf_jobs WHERE approval_status='pending' AND state=? AND scheduled_date=?",(state,run_date)).fetchone()[0]
        return render('drivers', section='driver', jobs=jobs,run_date=run_date,previous=previous,
            selected_state=state,pending=pending,**schedule_cards(db(),state,run_date,now()[:10],g.user))

    @app.post('/binflow/pickups/<int:job_id>/truck')
    @require('admin')
    def bf_assign_truck(job_id):
        try:
            db().execute('BEGIN IMMEDIATE')
            j, t = job(job_id), truck(integer('truck_id'))
            ensure_approved(j)
            run = db().execute('SELECT departed_at,returned_at FROM bf_runs WHERE truck_id=? AND run_date=?',(t['id'],j['scheduled_date'])).fetchone()
            if j['truck_id'] != t['id'] and run and (run['departed_at'] or run['returned_at']):
                raise ValueError('This truck has already started its run for this date. Choose an unstarted run.')
            if j['status'] != 'scheduled':
                raise ValueError('Choose a truck before completing the client handover.')
            if j['state'] != t['state']:
                raise ValueError('Choose a truck from the same state as this job.')
            if j['truck_id'] != t['id'] and any(i['loaded_at'] or i['delivered_at'] or i['picked_up_at'] for i in items(job_id)):
                raise ValueError('This job has bins in movement. Unload any unused empties before changing trucks.')
            if j['truck_id'] != t['id']:
                ensure_run_unstarted(db(),j['truck_id'],j['scheduled_date'])
                ensure_run_unstarted(db(),t['id'],j['scheduled_date'])
                for tid in (j['truck_id'],t['id']):
                    schedule_changed(db(),tid,j['scheduled_date'],g.user['id'],(g.user['first_name']+' '+g.user['last_name']).strip(),now(),'Truck allocation changed: '+j['code'])
                db().execute('UPDATE bf_jobs SET truck_id=?,scan_mode=1,vehicle=? WHERE id=?', (t['id'],t['name'],job_id))
                for i in items(job_id):
                    event(i['bin_id'], 'Truck assigned', job_id, notes=t['name'])
            return success('Ready to scan.', 'bf_truck', truck_id=t['id'], job=job_id)
        except ValueError as error:
            return failure(error, 'bf_job', job_id=job_id)

    @app.get('/binflow/driver/<int:truck_id>')
    @access
    def bf_truck(truck_id):
        t = truck(truck_id)
        selected = request.args.get('job', type=int)
        j = job(selected) if selected else None
        if j and j['truck_id'] != truck_id:
            abort(404)
        run_date = j['scheduled_date'] if j else request.args.get('date',now()[:10])
        return redirect(url_for('bf_run',truck_id=truck_id,date=run_date))

    @app.get('/binflow/driver/<int:truck_id>/state')
    @access
    def bf_truck_state(truck_id):
        data = snapshot(truck_id, request.args.get('job', type=int))
        return jsonify(html=render_template('binflow/driver_stock.html', **data))

    @app.post('/binflow/driver/<int:truck_id>/scan')
    @access
    def bf_truck_scan(truck_id, guided_job=None, guided_action=None, guided_date=None, scanned_code=None, qr_token=None):
        jid = guided_job or request.form.get('job_id', type=int)
        wants_json = request.headers.get('Accept') == 'application/json'
        try:
            db().execute('BEGIN IMMEDIATE')
            t, j = truck(truck_id), job(jid or integer('job_id'))
            ensure_approved(j)
            check_owner(db(),truck_id,j['scheduled_date'],g.user)
            if j['truck_id'] != truck_id or not j['scan_mode']:
                raise ValueError('This job is not assigned to this truck. Choose the correct job or truck.')
            if j['status'] == 'cancelled':
                raise ValueError('This job has been cancelled.')
            action = guided_action or text_field('action')
            columns = {'load':'loaded_at', 'deliver':'delivered_at', 'collect':'picked_up_at', 'return':'returned_at'}
            if action not in (*columns, 'unload'):
                raise ValueError('Choose a scan action.')
            value = scanned_code or text_field('code', 1000)
            if value.startswith(('https://', 'http://')):
                value = urlparse(value).path.rstrip('/').split('/')[-1]
            stock = bins('b.code=? COLLATE NOCASE OR b.token=?', (value, value))
            if not stock:
                raise ValueError('Bin not found. Scan its QR label or enter the BF number.')
            b = stock[0]
            i = db().execute('SELECT * FROM bf_job_bins WHERE job_id=? AND bin_id=?', (j['id'],b['id'])).fetchone()
            if not i:
                raise ValueError(f'{b["code"]} is not allocated to this client stop. Check the selected job.')
            if (i['direction']=='deliver') != (action in ('load', 'deliver', 'unload')):
                raise ValueError('This bin is allocated for a different movement. Check the scan action.')
            # A repeated request is an acknowledgement, never a second movement (even after later steps).
            duplicate = bool(action != 'unload' and i[columns[action]])
            if action == 'unload' and not i['loaded_at'] and not i['delivered_at'] and b['location_id']==j['warehouse_id'] and b['status']=='empty':
                duplicate = True
            if duplicate:
                message = f'{b["code"]} — already scanned for this step. Stock unchanged.'
            else:
                guard_movement(db(),j,action,g.user,now()[:10])
                if b['reserved_job'] != j['id'] or b['state'] != t['state']:
                    raise ValueError('The bin allocation changed. Refresh the job before scanning.')
                if action in ('load','deliver','collect','unload') and j['status'] != 'scheduled':
                    raise ValueError('The client handover is already complete. This movement is closed.')
                origin, target, from_truck, to_truck = None, None, None, None
                if action == 'load':
                    if b['status'] != 'empty' or b['location_id'] != j['warehouse_id'] or b['truck_id']:
                        raise ValueError('This empty bin is not at the selected warehouse.')
                    status, load, target_truck = 'transit', 'empty', truck_id
                    origin, to_truck = j['warehouse_id'], truck_id
                    label = 'Empty bin scanned onto truck'
                elif action == 'deliver':
                    if not i['loaded_at'] or b['truck_id'] != truck_id or b['truck_load'] != 'empty':
                        raise ValueError('Scan this empty bin onto this truck at the warehouse first.')
                    status, load, target_truck = 'at_client', None, None
                    target, from_truck = j['site_id'], truck_id
                    label = 'Empty bin scanned to client'
                elif action == 'collect':
                    if b['location_id'] != j['site_id'] or b['status'] not in ('at_client','full') or b['truck_id']:
                        raise ValueError('This bin is not at the selected client site.')
                    status, load, target_truck = 'transit', 'full', truck_id
                    origin, to_truck = j['site_id'], truck_id
                    label = 'Full bin scanned onto truck at client'
                elif action == 'return':
                    if not j['collected_at'] or j['status'] not in ('collected','received'):
                        raise ValueError('Finish the client handover before scanning the warehouse return.')
                    if not i['picked_up_at'] or b['truck_id'] != truck_id or b['truck_load'] != 'full':
                        raise ValueError('This full bin is not on this truck. Scan its client pickup first.')
                    status, load, target_truck = 'received', None, None
                    target, from_truck = j['warehouse_id'], truck_id
                    label = 'Full bin scanned into warehouse; weight pending'
                else:
                    if i['delivered_at'] or b['truck_id'] != truck_id or b['truck_load'] != 'empty':
                        raise ValueError('Only unused empty bins on this truck can be unloaded.')
                    status, load, target_truck = 'empty', None, None
                    target, from_truck = j['warehouse_id'], truck_id
                    label = 'Unused empty bin unloaded at warehouse'
                db().execute('UPDATE bf_bins SET location_id=?,truck_id=?,truck_load=?,status=? WHERE id=?',
                             (target,target_truck,load,status,b['id']))
                column = columns.get(action, 'loaded_at')
                db().execute(f'UPDATE bf_job_bins SET {column}=? WHERE id=?', (None if action=='unload' else now(),i['id']))
                event(b['id'], label, j['id'], origin, target, from_truck=from_truck, to_truck=to_truck)
                if action == 'return' and not db().execute("SELECT 1 FROM bf_job_bins WHERE job_id=? AND direction='collect' AND returned_at IS NULL", (j['id'],)).fetchone():
                    db().execute("UPDATE bf_jobs SET status='received',received_at=? WHERE id=?", (now(),j['id']))
                if action == 'collect':
                    pickup_requests(db(),j['id'],b['id'],(g.user['first_name']+' '+g.user['last_name']).strip(),now())
                if action == 'return':
                    mark_returned(db(),truck_id,j['scheduled_date'],now())
                message = f'{b["code"]} — {label.lower()}.'
            db().commit()
            if qr_token:
                return success(message,'bf_qr_driver',token=qr_token,movement='pickup' if action=='collect' else 'delivery',job=j['id'])
            if guided_job:
                data=run_data(db(),truck_id,guided_date,g.user['id'],now()[:10])
                return jsonify(ok=True,message=message,duplicate=duplicate,stage=data['stage'],remaining=data['remaining'],
                               redirect=url_for('bf_run',truck_id=truck_id,date=guided_date),html=render_template('binflow/run_bins.html',**data))
            if wants_json:
                return jsonify(ok=True, message=message, duplicate=duplicate,
                    html=render_template('binflow/driver_stock.html', **snapshot(truck_id, j['id'])))
            return success(message, 'bf_truck', truck_id=truck_id, job=j['id'])
        except ValueError as error:
            db().rollback()
            if qr_token:
                return failure(error,'bf_qr_driver',token=qr_token,movement='pickup' if guided_action=='collect' else 'delivery',job=jid)
            if wants_json:
                return jsonify(ok=False, message=str(error)), 409
            return failure(error, 'bf_truck', truck_id=truck_id, job=jid)

    app.extensions['bf_scan'] = bf_truck_scan
