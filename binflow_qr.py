"""Public bin collection requests; authenticated, timestamped driver QR actions."""
import hashlib
import re
import secrets
import sqlite3
import time
from datetime import datetime
from zoneinfo import ZoneInfo

from flask import abort, flash, g, redirect, render_template, request, url_for

from binflow_runs import valid_date, check_owner


def migrate_qr(conn):
    conn.executescript('''
    CREATE TABLE IF NOT EXISTS bf_collection_requests (
        id INTEGER PRIMARY KEY, request_key TEXT NOT NULL UNIQUE,
        bin_id INTEGER NOT NULL REFERENCES bf_bins(id),site_id INTEGER NOT NULL REFERENCES bf_locations(id),
        client_id INTEGER NOT NULL REFERENCES bf_clients(id),bin_code TEXT NOT NULL,
        requester TEXT NOT NULL,contact TEXT NOT NULL,preferred_date TEXT NOT NULL DEFAULT '',
        notes TEXT NOT NULL DEFAULT '',status TEXT NOT NULL DEFAULT 'new'
        CHECK(status IN ('new','scheduled','collected','closed')),
        job_id INTEGER REFERENCES bf_jobs(id),created_at TEXT NOT NULL,updated_at TEXT NOT NULL,
        resolution TEXT NOT NULL DEFAULT '');
    CREATE UNIQUE INDEX IF NOT EXISTS bf_one_open_request ON bf_collection_requests(bin_id) WHERE status IN ('new','scheduled');
    CREATE TABLE IF NOT EXISTS bf_collection_request_history (
        id INTEGER PRIMARY KEY,request_id INTEGER NOT NULL REFERENCES bf_collection_requests(id),
        action TEXT NOT NULL,actor TEXT NOT NULL,notes TEXT NOT NULL DEFAULT '',created_at TEXT NOT NULL);
    ''')
    conn.execute('CREATE INDEX IF NOT EXISTS bf_request_history_lookup ON bf_collection_request_history(request_id,id)')
    columns={r['name'] for r in conn.execute('PRAGMA table_info(bf_collection_requests)')}
    for name,definition in {
        'review_state': "TEXT NOT NULL DEFAULT 'pending' CHECK(review_state IN ('pending','referred'))",
        'review_note': "TEXT NOT NULL DEFAULT ''",
    }.items():
        if name not in columns:
            conn.execute(f'ALTER TABLE bf_collection_requests ADD COLUMN {name} {definition}')


REQUEST_FILTERS = [('all','All requests'),('attention','Needs action'),('referred','Referred to BLOCKTEXX'),
                   ('approved','Schedule approved'),('closed','Dismissed'),('collected','Collected')]


def request_queue(conn,state='',selected='all',limit=None):
    if selected not in dict(REQUEST_FILTERS) and selected not in ('new','scheduled'):
        abort(400)
    rows=[dict(r) for r in conn.execute('''SELECT r.*,l.name AS site_name,l.state,c.name AS client_name,
        s.name AS size_name,b.location_id AS current_location_id,b.status AS bin_status,
        j.code AS job_code,j.scheduled_date,j.approval_status,
        h.action AS last_action,h.actor AS last_actor,h.created_at AS last_action_at
        FROM bf_collection_requests r JOIN bf_locations l ON l.id=r.site_id
        JOIN bf_clients c ON c.id=r.client_id JOIN bf_bins b ON b.id=r.bin_id
        JOIN bf_sizes s ON s.id=b.size_id LEFT JOIN bf_jobs j ON j.id=r.job_id
        LEFT JOIN bf_collection_request_history h ON h.id=(SELECT MAX(id) FROM bf_collection_request_history WHERE request_id=r.id)'''+
        (' WHERE l.state=?' if state else '')+' ORDER BY r.updated_at DESC,r.id DESC',(state,) if state else ())]
    counts={key:0 for key,_ in REQUEST_FILTERS}
    for r in rows:
        r['bucket']=('closed' if r['status']=='closed' else 'collected' if r['status']=='collected' else
                     'approved' if r['status']=='scheduled' and r['approval_status']=='approved' else
                     'referred' if r['status']=='new' and r['review_state']=='referred' else 'attention')
        r['label']='Awaiting allocation approval' if r['status']=='scheduled' and r['approval_status']=='pending' else dict(REQUEST_FILTERS)[r['bucket']]
        counts[r['bucket']]+=1
    counts['all']=len(rows)
    rows.sort(key=lambda r: {'attention':0,'referred':1}.get(r['bucket'],2))
    rows=[r for r in rows if selected=='all' or (r['status']==selected if selected in ('new','scheduled') else r['bucket']==selected)]
    total=len(rows)
    if limit: rows=rows[:limit]
    history={}
    # Bound each parameter list even when the full request register is large.
    for start in range(0,len(rows),200):
        ids=[r['id'] for r in rows[start:start+200]]
        for h in conn.execute('SELECT * FROM bf_collection_request_history WHERE request_id IN ('+','.join('?' for _ in ids)+') ORDER BY id DESC',ids):
            history.setdefault(h['request_id'],[]).append(h)
    return dict(requests=rows,request_counts=counts,request_total=total,request_history=history,
                request_selected=selected,request_filters=REQUEST_FILTERS)


def request_history(conn, rid, action, actor, stamp, notes=''):
    conn.execute('INSERT INTO bf_collection_request_history(request_id,action,actor,notes,created_at) VALUES(?,?,?,?,?)',
                 (rid,action,actor,notes,stamp))


def link_request(conn, rid, jid, site_id, collection_ids, actor, stamp):
    row=conn.execute('SELECT * FROM bf_collection_requests WHERE id=?',(rid,)).fetchone()
    if not row or row['status']!='new' or row['site_id']!=site_id or row['bin_id'] not in collection_ids:
        raise ValueError('This request changed, or its bin is missing from the collection. Review the request before booking.')
    conn.execute("UPDATE bf_collection_requests SET status='scheduled',review_state='pending',review_note='',job_id=?,updated_at=? WHERE id=?",(jid,stamp,rid))
    request_history(conn,rid,'Collection allocated; awaiting approval',actor,stamp,f'Docket BF-{jid:06d}')


def pickup_requests(conn,jid,bid,actor,stamp):
    rows=conn.execute("SELECT id FROM bf_collection_requests WHERE bin_id=? AND status IN ('new','scheduled')",(bid,)).fetchall()
    for row in rows:
        conn.execute("UPDATE bf_collection_requests SET status='collected',job_id=?,updated_at=? WHERE id=?",(jid,stamp,row['id']))
        request_history(conn,row['id'],'Driver pickup recorded',actor,stamp)


def reopen_requests(conn,jid,actor,stamp):
    rows=conn.execute("SELECT id FROM bf_collection_requests WHERE job_id=? AND status='scheduled'",(jid,)).fetchall()
    for row in rows:
        conn.execute("UPDATE bf_collection_requests SET status='new',review_state='pending',review_note='',job_id=NULL,updated_at=? WHERE id=?",(stamp,row['id']))
        request_history(conn,row['id'],'Allocation released; request returned to office queue',actor,stamp)


def approve_requests(conn,jid,actor,stamp):
    for row in conn.execute("SELECT id FROM bf_collection_requests WHERE job_id=? AND status='scheduled'",(jid,)).fetchall():
        conn.execute('UPDATE bf_collection_requests SET updated_at=? WHERE id=?',(stamp,row['id']))
        request_history(conn,row['id'],'Schedule approved',actor,stamp,f'Docket BF-{jid:06d}')


def register_qr(app,db,access,require,allowed,render,success,failure,now,actor):
    app.jinja_env.filters['bfstamp']=lambda value: datetime.fromisoformat(value).astimezone(ZoneInfo('Australia/Brisbane')).strftime('%d.%m.%Y · %H:%M:%S') if value else '—'

    def bin_record(token):
        row=db().execute('''SELECT b.*,s.name AS size_name,l.kind,l.client_id FROM bf_bins b JOIN bf_sizes s ON s.id=b.size_id
            LEFT JOIN bf_locations l ON l.id=b.location_id WHERE b.token=?''',(token,)).fetchone()
        if not row: abort(404)
        return row

    def public_render(name,b,**values):
        # Never pass the private bin record, location or contact history to a public template.
        return render_template('binflow/'+name+'.html',b=dict(code=b['code'],size_name=b['size_name'],token=b['token']),
            can_request=b['kind']=='client' and b['status'] in ('at_client','full'),staff=allowed(),**values)

    def limited():
        key=hashlib.sha256(('bf-public-request:'+str(request.remote_addr)).encode()).hexdigest()
        epoch=int(time.time())
        row=db().execute('SELECT * FROM login_attempts WHERE key=?',(key,)).fetchone()
        if row and epoch-row['started']<900 and row['count']>=15: return True
        db().execute('''INSERT INTO login_attempts(key,count,started) VALUES(?,1,?) ON CONFLICT(key) DO UPDATE
            SET count=CASE WHEN started<? THEN 1 ELSE count+1 END,started=CASE WHEN started<? THEN excluded.started ELSE started END''',
            (key,epoch,epoch-900,epoch-900))
        db().execute('DELETE FROM login_attempts WHERE started<?',(epoch-86400,))
        db().commit()
        return False

    @app.get('/binflow/qr/<token>')
    def bf_qr_open(token):
        return public_render('qr_actions',bin_record(token))

    @app.get('/binflow/qr/<token>/staff')
    @access
    def bf_qr_staff(token):
        bin_record(token)
        return redirect(url_for('bf_qr_open',token=token))

    @app.route('/binflow/qr/<token>/collection',methods=['GET','POST'])
    def bf_qr_collection(token):
        b=bin_record(token)
        values={key:request.form.get(key,'').strip() for key in ('requester','contact','preferred_date','notes')}
        key=request.form.get('request_key','') if request.method=='POST' else secrets.token_urlsafe(24)
        error=None;status=200
        if request.method=='POST':
            if limited():
                return public_render('qr_collection',b,values=values,request_key=key,today=now()[:10],error='Too many requests. Please try again in 15 minutes.'),429
            try:
                if not re.fullmatch(r'[A-Za-z0-9_-]{24,80}',key): raise ValueError('Refresh this form and try again.')
                if not 1<=len(values['requester'])<=120 or not 5<=len(values['contact'])<=200 or len(values['notes'])>1000:
                    raise ValueError('Enter your name and phone number or email. Keep notes under 1,001 characters.')
                if values['preferred_date'] and valid_date(values['preferred_date'])<now()[:10]:
                    raise ValueError('Choose today or a future collection date.')
                db().execute('BEGIN IMMEDIATE')
                b=bin_record(token)
                previous=db().execute('SELECT id FROM bf_collection_requests WHERE request_key=? AND bin_id=?',(key,b['id'])).fetchone()
                if previous:
                    return success('Your collection request has already been received.','bf_qr_open',token=token)
                if b['kind']!='client' or b['status'] not in ('at_client','full'):
                    raise ValueError('This bin is not currently available for a client collection. Please contact the office.')
                pending=db().execute("SELECT id FROM bf_collection_requests WHERE bin_id=? AND status IN ('new','scheduled')",(b['id'],)).fetchone()
                booked=db().execute("SELECT 1 FROM bf_job_bins i JOIN bf_jobs j ON j.id=i.job_id WHERE i.bin_id=? AND i.direction='collect' AND i.active=1 AND j.status='scheduled'",(b['id'],)).fetchone()
                if pending or booked:
                    return success('A collection is already requested or booked for this bin. Please contact the office for changes.','bf_qr_open',token=token)
                stamp=now()
                rid=db().execute('''INSERT INTO bf_collection_requests(request_key,bin_id,site_id,client_id,bin_code,requester,contact,preferred_date,notes,created_at,updated_at)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?)''',(key,b['id'],b['location_id'],b['client_id'],b['code'],values['requester'],values['contact'],values['preferred_date'],values['notes'],stamp,stamp)).lastrowid
                request_history(db(),rid,'Public QR collection requested','Public QR request',stamp)
                db().execute('INSERT INTO bf_events(bin_id,event,actor,notes,created_at) VALUES(?,?,?,?,?)',
                    (b['id'],'Collection requested via public QR','Public QR request',f'Request QR-{rid:06d}; office confirmation required.',stamp))
                return success(f'Collection request QR-{rid:06d} received. The office will confirm the collection date.','bf_qr_open',token=token)
            except (ValueError,sqlite3.IntegrityError) as exc:
                db().rollback();error=str(exc) if isinstance(exc,ValueError) else 'Refresh this form and try again.';status=400
        return public_render('qr_collection',b,values=values,request_key=key,today=now()[:10],error=error),status

    @app.route('/binflow/qr/<token>/driver/<movement>',methods=['GET','POST'])
    @access
    def bf_qr_driver(token,movement):
        if movement not in ('load','pickup','delivery'): abort(404)
        b=bin_record(token)
        direction='collect' if movement=='pickup' else 'deliver'
        action='load' if movement=='load' else direction
        selected=request.form.get('job_id',type=int) if request.method=='POST' else request.args.get('job',type=int)
        rows=db().execute('''SELECT i.*,j.code,j.truck_id,j.scheduled_date,j.client_name,j.site_name,j.status AS job_status
            FROM bf_job_bins i JOIN bf_jobs j ON j.id=i.job_id WHERE i.bin_id=? AND i.direction=?
            AND j.scan_mode=1 AND j.approval_status='approved' AND j.status!='cancelled' '''+(' AND j.id=?' if selected else '')+
            ' ORDER BY i.active DESC,j.id DESC LIMIT 1',(b['id'],direction,selected) if selected else (b['id'],direction)).fetchall()
        item=rows[0] if rows else None
        if item and item['truck_id']: check_owner(db(),item['truck_id'],item['scheduled_date'],g.user)
        if request.method=='POST':
            if not selected or not item or not item['truck_id']:
                return failure(ValueError('This bin needs a scheduled job and assigned truck before the driver can record it.'),'bf_qr_driver',token=token,movement=movement)
            return app.extensions['bf_scan'](item['truck_id'],guided_job=item['job_id'],guided_action=action,
                scanned_code=b['token'],qr_token=token)
        stamp=item[{'load':'loaded_at','pickup':'picked_up_at','delivery':'delivered_at'}[movement]] if item else None
        recorded=db().execute('SELECT actor FROM bf_events WHERE bin_id=? AND job_id=? AND event=? ORDER BY id DESC LIMIT 1',
            (b['id'],item['job_id'],{'load':'Empty bin scanned onto truck','pickup':'Full bin scanned onto truck at client','delivery':'Empty bin scanned to client'}[movement])).fetchone() if item and stamp else None
        return render('qr_driver',section='driver',driver_mode=True,b=b,item=item,movement=movement,stamp=stamp,recorded_by=recorded['actor'] if recorded else None)

    @app.get('/binflow/collection-requests')
    @access
    def bf_collection_requests():
        state=request.args.get('state','')
        if state not in ('','QLD','NSW','VIC','SA','WA','TAS','ACT','NT'): abort(400)
        return render('collection_requests',section='requests',state=state,request_dashboard=False,
                      **request_queue(db(),state,request.args.get('status','all')))

    def request_destination():
        state=request.form.get('state','')
        if state not in ('','QLD','NSW','VIC','SA','WA','TAS','ACT','NT'): state=''
        return ('bf_dashboard' if request.form.get('return_to')=='dashboard' else 'bf_collection_requests'),dict(state=state)

    @app.post('/binflow/collection-requests/<int:request_id>/refer')
    @require('admin')
    def bf_refer_collection_request(request_id):
        endpoint,values=request_destination()
        try:
            notes=request.form.get('notes','').strip()
            if len(notes)>1000: raise ValueError('Keep referral notes under 1,001 characters.')
            db().execute('BEGIN IMMEDIATE')
            row=db().execute('SELECT * FROM bf_collection_requests WHERE id=?',(request_id,)).fetchone()
            if not row: abort(404)
            if row['status']!='new': raise ValueError('This request has already been handled. Review its current status.')
            if row['review_state']=='referred': return success('Already referred to BLOCKTEXX.',endpoint,**values)
            stamp=now()
            db().execute("UPDATE bf_collection_requests SET review_state='referred',review_note=?,updated_at=? WHERE id=?",(notes,stamp,request_id))
            request_history(db(),request_id,'Referred to BLOCKTEXX',actor(),stamp,notes)
            return success('Marked for BLOCKTEXX follow-up.',endpoint,**values)
        except ValueError as exc:
            return failure(exc,endpoint,**values)

    @app.post('/binflow/collection-requests/<int:request_id>/close')
    @require('admin')
    def bf_close_collection_request(request_id):
        endpoint,values=request_destination()
        try:
            notes=request.form.get('resolution','').strip()
            if not notes or len(notes)>1000: raise ValueError('Enter a reason for dismissing this request (up to 1,000 characters).')
            db().execute('BEGIN IMMEDIATE')
            row=db().execute('SELECT * FROM bf_collection_requests WHERE id=?',(request_id,)).fetchone()
            if not row: abort(404)
            if row['status']=='closed': return success('Request already dismissed.',endpoint,**values)
            if row['status']!='new': raise ValueError('Manage an allocated collection through its scheduled docket.')
            stamp=now()
            db().execute("UPDATE bf_collection_requests SET status='closed',resolution=?,updated_at=? WHERE id=?",(notes,stamp,request_id))
            request_history(db(),request_id,'Request dismissed',actor(),stamp,notes)
            return success('Request dismissed. The reason is saved.',endpoint,**values)
        except ValueError as exc:
            return failure(exc,endpoint,**values)

    app.context_processor(lambda: dict(bf_request_count=db().execute("""SELECT COUNT(*) FROM bf_collection_requests r
        LEFT JOIN bf_jobs j ON j.id=r.job_id WHERE (r.status='new' AND r.review_state='pending')
        OR (r.status='scheduled' AND j.approval_status='pending')""").fetchone()[0] if allowed() else 0))
