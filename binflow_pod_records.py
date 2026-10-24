"""Material classification, explicit POD finalisation and collection traceability."""
import io
import json
import sqlite3
from datetime import datetime
from flask import abort, g, redirect, render_template, request, send_file, session, url_for
from binflow_permissions import can_view_job, is_office
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


def register(app,db,access,require,render,job,items,success,failure,now,actor):
    app.jinja_env.globals['pod_status']=status
    app.jinja_env.globals['pod_final']=lambda j:bool(j['pod_signed_at'])
    app.context_processor(lambda:dict(material_classes=db().execute(
        'SELECT * FROM bf_material_classes ORDER BY active DESC,name').fetchall()) if request.endpoint in ('bf_setup','bf_job','bf_pod') else {})

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
            if not is_office(g.user):abort(403)
            if j['pod_signed_at']:raise ValueError('This POD is signed. Its material record is locked.')
            if j['status']=='cancelled':raise ValueError('This job is cancelled.')
            if request.form.get('version',type=int)!=j['pod_version']:raise ValueError('This POD changed. Refresh before saving.')
            for row in items(job_id):
                if row['direction']!='collect':continue
                mid=request.form.get('material_'+str(row['id']),type=int)
                if mid==row['material_id']:continue
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
            if not is_office(g.user):abort(403)
            if j['pod_signed_at']:raise ValueError('This POD has already been signed off.')
            if request.form.get('version',type=int)!=j['pod_version']:raise ValueError('This POD changed. Refresh and review it before signing.')
            rows=items(job_id)
            if not j['collected_at'] or j['status']!='complete' or j['approval_status']!='approved':
                raise ValueError('Finish the handover, warehouse receipt and all weights before signing off.')
            collected=[i for i in rows if i['direction']=='collect']
            if any(i['weighed_at'] is None or i['net_g'] is None for i in collected):
                raise ValueError('Every collected bin must be weighed before signing off.')
            if any(not i['material_id'] or not i['material_name'] for i in collected):
                raise ValueError('Classify the material in every collected bin before signing off.')
            if request.form.get('confirm')!='yes':raise ValueError('Confirm that you have checked the materials and weights.')
            signature=signature_png(request.form.get('signature',''))
            stamp=now()
            snapshot=dict(items=[dict(i) for i in rows],journeys={str(i['id']):journey(db(),j,i) for i in rows},
                          signed_at=stamp,signed_by=actor())
            db().execute('UPDATE bf_jobs SET pod_signed_at=?,pod_signed_by=?,pod_signed_name=?,pod_signature=?,pod_snapshot=?,pod_version=pod_version+1 WHERE id=?',
                         (stamp,g.user['id'],actor(),signature,json.dumps(snapshot),job_id))
            db().execute('INSERT INTO bf_pod_history(job_id,action,user_id,created_at) VALUES(?,?,?,?)',
                         (job_id,'POD weighed, classified and signed off',g.user['id'],stamp))
            return success('POD signed off. The final record is now available to the client.','bf_pod',job_id=job_id)
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
            if not j['pod_signed_at'] or not db().execute('SELECT 1 FROM bf_client_account_scope WHERE account_id=? AND client_id=?',
                (account['id'],j['pod_client_id'])).fetchone():abort(404)
        ensure_pod_available(j)
        if not j['collected_at']:abort(404)
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
