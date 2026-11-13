"""Material classification, explicit POD finalisation and collection traceability."""
import hashlib
import io
import json
import sqlite3
from datetime import datetime
from flask import abort, g, redirect, render_template, request, send_file, session, url_for
from binflow_handover import pod_weights_ready
from binflow_permissions import can_view_job, is_office, is_warehouse
from binflow_pods import ensure_pod_available
from prestart_media import signature_png


def migrate(conn):
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS bf_material_classes(
          id INTEGER PRIMARY KEY,name TEXT NOT NULL COLLATE NOCASE UNIQUE,
          active INTEGER NOT NULL DEFAULT 1,version INTEGER NOT NULL DEFAULT 1,
          updated_at TEXT NOT NULL,updated_by TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS bf_material_history(
          id INTEGER PRIMARY KEY,material_id INTEGER NOT NULL,action TEXT NOT NULL,
          name TEXT NOT NULL,actor TEXT NOT NULL,created_at TEXT NOT NULL);
    """)
    for table, columns in {
        'bf_jobs': [('pod_token','TEXT'),('pod_signed_at','TEXT'),('pod_signed_by','INTEGER REFERENCES users(id)'),
                    ('pod_signed_name',"TEXT NOT NULL DEFAULT ''"),('pod_signature','BLOB'),
                    ('pod_snapshot',"TEXT NOT NULL DEFAULT ''")],
        'bf_job_bins': [('material_id','INTEGER REFERENCES bf_material_classes(id)'),
                        ('material_name',"TEXT NOT NULL DEFAULT ''")],
        'bf_events': [('from_label',"TEXT NOT NULL DEFAULT ''"),('to_label',"TEXT NOT NULL DEFAULT ''")]
    }.items():
        existing={r['name'] for r in conn.execute('PRAGMA table_info('+table+')')}
        for name,definition in columns:
            if name not in existing:conn.execute(f'ALTER TABLE {table} ADD COLUMN {name} {definition}')
    conn.execute("UPDATE bf_jobs SET pod_token=lower(hex(randomblob(24))) WHERE pod_token IS NULL")
    conn.executescript("""
        CREATE UNIQUE INDEX IF NOT EXISTS bf_pod_token_unique ON bf_jobs(pod_token);
        CREATE TRIGGER IF NOT EXISTS bf_pod_token_insert AFTER INSERT ON bf_jobs WHEN NEW.pod_token IS NULL
        BEGIN UPDATE bf_jobs SET pod_token=lower(hex(randomblob(24))) WHERE id=NEW.id; END;
    """)


def status(job):
    if job['pod_signed_at']:return 'FINAL · SIGNED'
    if not job['collected_at']:return 'HANDOVER PENDING'
    if job['status']!='complete':return 'WEIGHT PENDING'
    return 'SIGN-OFF PENDING'


def label(conn, location_id=None, truck_id=None):
    if truck_id:
        r=conn.execute('SELECT name FROM bf_trucks WHERE id=?',(truck_id,)).fetchone()
    else:
        r=conn.execute('SELECT name FROM bf_locations WHERE id=?',(location_id,)).fetchone()
    return r['name'] if r else ''


def elapsed(start, end):
    try:
        seconds=(datetime.fromisoformat(end)-datetime.fromisoformat(start)).total_seconds()
        if seconds<0:return 'Timing requires review'
        minutes=int(seconds//60);days,minutes=divmod(minutes,1440);hours,minutes=divmod(minutes,60)
        return (f'{days}d ' if days else '')+f'{hours}h {minutes}m'
    except (ValueError,TypeError):return 'Duration not recorded'


def journey(conn, job, item):
    # End at this docket's last movement; never follow the bin into a later rotation.
    end=conn.execute('SELECT MAX(id) FROM bf_events WHERE job_id=? AND bin_id=?',
                     (job['id'],item['bin_id'])).fetchone()[0]
    if not end:return dict(events=[],stays=[],cycle_start=0,notice='No movement history was recorded for this bin on this docket.')
    rows=[dict(r) for r in conn.execute("""SELECT e.*,j.code AS docket FROM bf_events e
        LEFT JOIN bf_jobs j ON j.id=e.job_id WHERE e.bin_id=? AND e.id<=? ORDER BY e.id""",(item['bin_id'],end))]
    cycle=next((r['id'] for r in reversed(rows) if r['to_location']==job['site_id']
                and r['from_location']!=job['site_id'] and r['job_id']!=job['id']),0)
    events=[];stays=[];held=None
    for r in rows:
        source=r['from_label'] or label(conn,r['from_location'],r['from_truck'])
        target=r['to_label'] or label(conn,r['to_location'],r['to_truck'])
        origin=('truck',r['from_truck']) if r['from_truck'] else ('location',r['from_location']) if r['from_location'] else None
        destination=('truck',r['to_truck']) if r['to_truck'] else ('location',r['to_location']) if r['to_location'] else None
        movement=bool(destination and destination!=origin) or r['event']=='Full bin collected; returning to warehouse'
        if r['event']=='Full bin collected; returning to warehouse':
            destination=('transit',r['job_id']);target='In transit to warehouse'
        if movement and not origin and held and held['key'][0]=='transit':
            origin=held['key'];source=held['location']
        if movement and origin and destination!=origin:
            if held and held['key']==origin:
                stays.append(dict(location=held['location'],from_at=held['at'],to_at=r['created_at'],
                                  duration=elapsed(held['at'],r['created_at']),start_id=held['id'],end_id=r['id']))
            else:
                stays.append(dict(location=source or 'Unknown custody',from_at=None,to_at=r['created_at'],
                                  duration='Arrival not recorded',start_id=r['id'],end_id=r['id']))
            held=None
        if movement and destination and (not held or held['key']!=destination):
            held=dict(key=destination,location=target,at=r['created_at'],id=r['id'])
        source_client=conn.execute("SELECT client_id FROM bf_locations WHERE id=? AND kind='client'",(r['from_location'],)).fetchone()
        events.append(dict(restricted_source=bool(source_client and source_client['client_id']!=job['pod_client_id']),id=r['id'],event=r['event'],at=r['created_at'],actor=r['actor'],
                           source=source,target=target,docket=r['docket'],notes=r['notes'],job_id=r['job_id']))
    # The final warehouse stay is measured only up to this record's last event.
    if held:
        stays.append(dict(location=held['location'],from_at=held['at'],to_at=rows[-1]['created_at'],
                          duration=elapsed(held['at'],rows[-1]['created_at']),start_id=held['id'],end_id=rows[-1]['id']))
    if not cycle:
        cycle=next((r['id'] for r in rows if r['job_id']==job['id']),end)
    return dict(events=events,stays=stays,cycle_start=cycle,notice='Durations use recorded movements. Missing scans are shown as unknown; historical location names may reflect later edits.')


def qr_bytes(url):
    import qrcode
    output=io.BytesIO()
    qrcode.make(url,box_size=8,border=4).save(output,format='PNG')
    return output.getvalue()


def confirm_pickup_material(conn, item_id, material_id):
    material = conn.execute('SELECT id,name FROM bf_material_classes WHERE id=? AND active=1',
                            (material_id,)).fetchone()
    if not material:
        raise ValueError('Choose the material in this bin before confirming its pickup.')
    conn.execute('UPDATE bf_job_bins SET material_id=?,material_name=? WHERE id=?',
                 (material['id'],material['name'],item_id))
    return material['name']


def run_signoff(conn, job, user):
    """The PODs received by this warehouse from one truck on one date."""
    if not job['truck_id']:
        return None
    stops = conn.execute("""SELECT j.*, COUNT(i.id) AS collected_bins,
            COUNT(i.weighed_at) AS weighed_bins, COALESCE(SUM(i.net_g),0) AS net,
            COUNT(CASE WHEN i.id IS NOT NULL AND (i.material_id IS NULL OR i.material_name='') THEN 1 END) AS missing
        FROM bf_jobs j LEFT JOIN bf_job_bins i ON i.job_id=j.id AND i.direction='collect'
        WHERE j.truck_id=? AND j.scheduled_date=? AND j.warehouse_id=? AND j.schedule_live=1
          AND j.approval_status='approved' AND j.status!='cancelled' AND j.pod_deleted_at IS NULL
        GROUP BY j.id ORDER BY j.run_order,j.id""",
        (job['truck_id'],job['scheduled_date'],job['warehouse_id'])).fetchall()
    if len(stops)<2 or not any(r['id']==job['id'] for r in stops):
        return None
    if any(not can_view_job(conn,user,r) for r in stops):
        # An authorised warehouse can see a stop once it has been collected.
        # Pending stops still block this run's sign-off without revealing their details.
        return dict(stops=[],ready=False,version='',unsigned=0)
    pending = [r for r in stops if not r['pod_signed_at']]
    version = hashlib.sha256(json.dumps([(r['id'],r['pod_version'],r['status'],r['pod_signed_at'])
                                         for r in stops]).encode()).hexdigest()
    ready = bool(pending) and all(r['status']=='complete' and r['collected_at'] and not r['missing']
                                  and r['collected_bins']==r['weighed_bins'] for r in pending)
    return dict(stops=stops,ready=ready,version=version,unsigned=len(pending))


def register(app,db,access,require,render,job,items,success,failure,now,actor):
    app.jinja_env.globals['pod_weights_ready'] = lambda j: pod_weights_ready(db(), j)
    app.jinja_env.globals['pod_run_signoff']=lambda j: run_signoff(db(),j,g.user)
    app.jinja_env.globals['pod_status']=status
    app.jinja_env.globals['pod_final']=lambda j:bool(j['pod_signed_at'])
    app.context_processor(lambda:dict(material_classes=db().execute(
        'SELECT * FROM bf_material_classes ORDER BY active DESC,name').fetchall()) if request.endpoint in ('bf_setup','bf_job','bf_pod','bf_run','bf_qr_driver') else {})

    @app.post('/binflow/setup/materials')
    @require('admin')
    def bf_material_save():
        try:
            name=request.form.get('name','').strip()
            if not name or len(name)>100 or any(ord(c)<32 for c in name):raise ValueError('Enter a material name of 1–100 characters.')
            db().execute('BEGIN IMMEDIATE')
            mid=request.form.get('material_id',type=int)
            active=int(request.form.get('active')=='yes')
            if mid:
                old=db().execute('SELECT * FROM bf_material_classes WHERE id=?',(mid,)).fetchone()
                if not old or old['version']!=request.form.get('version',type=int):raise ValueError('This classification changed. Refresh Setup.')
                db().execute('UPDATE bf_material_classes SET name=?,active=?,version=version+1,updated_at=?,updated_by=? WHERE id=?',
                             (name,active,now(),actor(),mid))
            else:
                mid=db().execute('INSERT INTO bf_material_classes(name,active,updated_at,updated_by) VALUES(?,?,?,?)',
                                 (name,active,now(),actor())).lastrowid
            db().execute('INSERT INTO bf_material_history(material_id,action,name,actor,created_at) VALUES(?,?,?,?,?)',
                         (mid,'Active' if active else 'Inactive',name,actor(),now()))
            return success('Material classification saved. Existing POD classifications are unchanged.','bf_setup',_anchor='materials')
        except sqlite3.IntegrityError:
            return failure(ValueError('That material classification already exists.'),'bf_setup',_anchor='materials')
        except ValueError as error:return failure(error,'bf_setup',_anchor='materials')

    @app.post('/binflow/pickups/<int:job_id>/pod/materials')
    @access
    def bf_pod_materials(job_id):
        try:
            db().execute('BEGIN IMMEDIATE');j=job(job_id);ensure_pod_available(j)
            if not (is_office(g.user) or is_warehouse(g.user) or getattr(g,'bf_demo_warehouse',False)):abort(403)
            if j['pod_signed_at']:raise ValueError('This POD is signed. Its material record is locked.')
            if not j['collected_at'] or j['status']=='cancelled':raise ValueError('Select the material at the client when collecting each full bin.')
            if request.form.get('version',type=int)!=j['pod_version']:raise ValueError('This POD changed. Refresh before saving.')
            for row in items(job_id):
                if row['direction']!='collect':continue
                mid=request.form.get('material_'+str(row['id']),type=int)
                if mid==row['material_id']:continue
                if row['material_id']:
                    raise ValueError('This bin’s material was confirmed at client pickup and is already recorded on the POD.')
                material=db().execute('SELECT * FROM bf_material_classes WHERE id=? AND active=1',(mid,)).fetchone()
                if not material:raise ValueError('Choose an active material classification for every collected bin.')
                db().execute('UPDATE bf_job_bins SET material_id=?,material_name=? WHERE id=?',(mid,material['name'],row['id']))
            db().execute('UPDATE bf_jobs SET pod_version=pod_version+1 WHERE id=?',(job_id,))
            db().execute('INSERT INTO bf_pod_history(job_id,action,user_id,created_at) VALUES(?,?,?,?)',
                         (job_id,'Material classifications saved',g.user['id'],now()))
            return success('POD material classifications saved.','bf_job',job_id=job_id,_anchor='pod-finalisation')
        except ValueError as error:return failure(error,'bf_job',job_id=job_id,_anchor='pod-finalisation')

    @app.post('/binflow/pickups/<int:job_id>/pod/sign')
    @access
    def bf_pod_sign(job_id):
        try:
            db().execute('BEGIN IMMEDIATE');j=job(job_id);ensure_pod_available(j)
            if not (is_office(g.user) or is_warehouse(g.user) or getattr(g,'bf_demo_warehouse',False)):abort(403)
            if j['pod_signed_at']:raise ValueError('This POD has already been signed off.')
            if request.form.get('version',type=int)!=j['pod_version']:raise ValueError('This POD changed. Refresh and review it before signing.')
            if not pod_weights_ready(db(), j):
                raise ValueError('Complete all weights for this run before warehouse sign-off.')
            targets = [j]
            if request.form.get('scope')=='run':
                run = run_signoff(db(),j,g.user)
                if not run or not run['ready']:
                    raise ValueError('Finish the warehouse receipts and all weights for this run before signing off.')
                if run['version'] != request.form.get('run_version'):
                    raise ValueError('The run changed. Refresh and review the PODs before signing.')
                targets = [r for r in run['stops'] if not r['pod_signed_at']]
            elif run_signoff(db(),j,g.user):
                raise ValueError('Review all PODs for this run and sign once the warehouse weights are complete.')
            snapshots = []
            for target in targets:
                rows = items(target['id'])
                if not target['collected_at'] or target['status']!='complete' or target['approval_status']!='approved':
                    raise ValueError('Finish the handover, warehouse receipt and all weights before signing off.')
                collected=[i for i in rows if i['direction']=='collect']
                if any(i['weighed_at'] is None or i['net_g'] is None for i in collected):
                    raise ValueError('Every collected bin must be weighed before signing off.')
                if any(not i['material_id'] or not i['material_name'] for i in collected):
                    raise ValueError('Classify the material missing from the earlier pickup record before signing off.')
                snapshots.append(dict(items=[dict(i) for i in rows],journeys={
                    str(i['id']):journey(db(),target,i) for i in rows}))
            if request.form.get('confirm')!='yes':raise ValueError('Confirm that you have checked the warehouse weights.')
            signature=signature_png(request.form.get('signature',''))
            stamp=now()
            for target,snapshot in zip(targets,snapshots):
                snapshot.update(signed_at=stamp,signed_by=actor())
                db().execute('UPDATE bf_jobs SET pod_signed_at=?,pod_signed_by=?,pod_signed_name=?,pod_signature=?,pod_snapshot=?,pod_version=pod_version+1 WHERE id=?',
                             (stamp,g.user['id'],actor(),signature,json.dumps(snapshot),target['id']))
                db().execute('INSERT INTO bf_pod_history(job_id,action,user_id,created_at) VALUES(?,?,?,?)',
                             (target['id'],'Warehouse weights checked and POD signed off',g.user['id'],stamp))
            if any(i['direction']=='collect' for snapshot in snapshots for i in snapshot['items']):
                from binflow_auto_invoices import reconcile
                reconcile(db(),stamp,pods_only=True)
            message = f"{len(targets)} PODs signed off for this run." if len(targets)>1 else 'POD signed off. The final record is now available to the client.'
            return success(message,'bf_daily_pods',date=j['scheduled_date'],view='unfinished',_anchor='pod-list')
        except ValueError as error:return failure(error,'bf_job',job_id=job_id,_anchor='pod-finalisation')

    def authorised(token):
        j=db().execute('SELECT * FROM bf_jobs WHERE pod_token=?',(token,)).fetchone()
        if not j:abort(404)
        if g.user:
            if not can_view_job(db(),g.user,j):abort(403)
            account=None
        else:
            account=db().execute('SELECT * FROM bf_client_accounts WHERE id=? AND active=1',(session.get('bf_client_uid'),)).fetchone()
            if not account or account['auth_version']!=session.get('bf_client_version'):
                session['binflow_next']=url_for('bf_pod_record',token=token)
                return j,None,False
            from binflow_management import permitted_pod
            from binflow_client_access import set_context
            if not permitted_pod(db(),account,j['id'],set_context(db(),account)):abort(404)
        ensure_pod_available(j)
        if not j['collected_at']:abort(404)
        if not pod_weights_ready(db(), j):
            abort(409, 'POD pending. Complete all warehouse weights for this run first.')
        return j,account,True

    @app.get('/binflow/pod/<token>')
    def bf_pod_record(token):
        j,account,ok=authorised(token)
        if not ok:return render_template('binflow/pod_login.html')
        if g.user:return redirect(url_for('bf_pod',job_id=j['id']))
        return render_template('binflow/client_pod_record.html',account=account,job=j,items=items(j['id']),
                               net=sum(i['net_g'] or 0 for i in items(j['id'])))

    @app.get('/binflow/pod/<token>/qr.png')
    def bf_pod_qr(token):
        j,account,ok=authorised(token)
        if not ok:abort(403)
        url=app.config['APP_URL'].rstrip('/')+url_for('bf_pod_record',token=token)
        return send_file(io.BytesIO(qr_bytes(url)),mimetype='image/png')

    @app.get('/binflow/pod/<token>/signature.png')
    def bf_pod_signature(token):
        j,account,ok=authorised(token)
        if not ok or not j['pod_signature']:abort(404)
        return send_file(io.BytesIO(j['pod_signature']),mimetype='image/png')

    @app.get('/binflow/pod/<token>/journey/<int:item_id>')
    def bf_pod_journey(token,item_id):
        j,account,ok=authorised(token)
        if not ok:return render_template('binflow/pod_login.html')
        item=next((i for i in items(j['id']) if i['id']==item_id),None)
        if not item:abort(404)
        trace=json.loads(j['pod_snapshot'])['journeys'][str(item_id)] if j['pod_signed_at'] else journey(db(),j,item)
        full=bool(g.user and is_office(g.user))
        if not full:
            trace=dict(trace,events=[dict(e,source='Previous client (restricted)' if e.get('restricted_source') else e['source']) for e in trace['events'] if e['id']>=trace['cycle_start']],
                       stays=[s for s in trace['stays'] if s['start_id']>=trace['cycle_start']])
        # Public client view never links to internal bin or other clients' dockets.
        return render_template('binflow/pod_journey.html',account=account,job=j,item=item,trace=trace,full_history=full)
