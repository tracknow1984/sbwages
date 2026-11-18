"""Final daily PODs, with separately authenticated and explicitly scoped client accounts."""
import hashlib
import re
import secrets
import sqlite3
import time
from functools import wraps

from flask import abort, flash, g, redirect, render_template, request, session, url_for
from werkzeug.security import check_password_hash, generate_password_hash

import binflow_period_reports as reports
import binflow_client_access as portal
import binflow_warehouse_messages as warehouse_messages
from binflow_management import pod_scope, permitted_pod, register as register_management
from binflow_runs import valid_date
from binflow_pods import migrate_pods, register_pods


def migrate_portal(conn):
    conn.executescript('''
    CREATE TABLE IF NOT EXISTS bf_client_accounts (
        id INTEGER PRIMARY KEY,username TEXT NOT NULL COLLATE NOCASE UNIQUE,name TEXT NOT NULL,
        password_hash TEXT NOT NULL,active INTEGER NOT NULL DEFAULT 1,auth_version INTEGER NOT NULL DEFAULT 1,
        created_at TEXT NOT NULL,updated_at TEXT NOT NULL,updated_by TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS bf_client_account_scope (
        account_id INTEGER NOT NULL REFERENCES bf_client_accounts(id),client_id INTEGER NOT NULL REFERENCES bf_clients(id),
        PRIMARY KEY(account_id,client_id));
    CREATE TABLE IF NOT EXISTS bf_client_access_history (
        id INTEGER PRIMARY KEY,account_id INTEGER NOT NULL REFERENCES bf_client_accounts(id),
        action TEXT NOT NULL,scopes TEXT NOT NULL,actor TEXT NOT NULL,created_at TEXT NOT NULL);
    ''')
    if 'pod_client_id' not in {r['name'] for r in conn.execute('PRAGMA table_info(bf_jobs)')}:
        conn.execute('ALTER TABLE bf_jobs ADD COLUMN pod_client_id INTEGER REFERENCES bf_clients(id)')
        conn.execute('UPDATE bf_jobs SET pod_client_id=(SELECT client_id FROM bf_locations WHERE id=bf_jobs.site_id)')
    warehouse_messages.migrate(conn)
    portal.migrate(conn)
    migrate_pods(conn)


def register_portal(app, db, access, require, render, success, failure, now, actor, pdf):
    def client_access(fn):
        @wraps(fn)
        def wrapped(*args,**kwargs):
            account=db().execute('SELECT * FROM bf_client_accounts WHERE id=? AND active=1', (session.get('bf_client_uid'),)).fetchone()
            if not account or account['auth_version']!=session.get('bf_client_version'):
                session.pop('bf_client_uid',None);session.pop('bf_client_version',None)
                if request.blueprint == 'sbtx_client':
                    return redirect(url_for('sbtx_client_login'))
                if request.endpoint in ('bf_client_warehouses', 'bf_client_warehouse_chat'):
                    return redirect(url_for('bf_client_login', warehouse=kwargs.get('state', 'directory')))
                return redirect(url_for('bf_client_login', transfers=1) if request.endpoint.startswith('bf_client_interstate') else url_for('bf_client_login'))
            rights=portal.set_context(db(),account)
            if portal.ENDPOINTS.get(request.endpoint) not in rights:
                abort(403)
            return fn(*args,**kwargs)
        return wrapped

    def limited(key):
        epoch=int(time.time())
        row=db().execute('SELECT * FROM login_attempts WHERE key=?',(key,)).fetchone()
        if row and epoch-row['started']<900 and row['count']>=10: return True
        db().execute('''INSERT INTO login_attempts(key,count,started) VALUES(?,1,?) ON CONFLICT(key) DO UPDATE
            SET count=CASE WHEN started<? THEN 1 ELSE count+1 END,
            started=CASE WHEN started<? THEN excluded.started ELSE started END''',(key,epoch,epoch-900,epoch-900))
        db().execute('DELETE FROM login_attempts WHERE started<?',(epoch-86400,))
        db().commit()
        return False

    @app.route('/sbtx/client/login',endpoint='sbtx_client_login',methods=['GET','POST'])
    @app.route('/binflow/client/login',methods=['GET','POST'])
    def bf_client_login():
        sbtx_login = request.endpoint == 'sbtx_client_login'
        template = 'sbtx/login.html' if sbtx_login else 'binflow/client_login.html'
        if request.method=='POST':
            username=request.form.get('username','').strip().lower()[:100]
            keys=[hashlib.sha256(f'bf-client:{value}'.encode()).hexdigest() for value in (username,'ip:'+str(request.remote_addr))]
            if any(limited(k) for k in keys):
                return render_template(template,error='Too many attempts. Try again in 15 minutes.'),429
            account=db().execute('SELECT * FROM bf_client_accounts WHERE username=?',(username,)).fetchone()
            matched=check_password_hash(account['password_hash'] if account else app.config['DUMMY_HASH'],request.form.get('password','')[:200])
            if account and account['active'] and matched:
                pod_next=session.get('binflow_next','')
                session.clear()
                session.update(bf_client_uid=account['id'],bf_client_version=account['auth_version'],csrf=secrets.token_urlsafe(32))
                session.permanent=True
                db().execute('DELETE FROM login_attempts WHERE key=?',(keys[0],));db().commit()
                rights=portal.set_context(db(),account)
                if sbtx_login:
                    if 'sbtx.view' in rights:
                        return redirect(url_for('sbtx_client.calendar'))
                    session.clear()
                    return render_template(template, error='This login does not have SBTX access.'),403
                if 'pods.view' in rights and pod_next.startswith('/binflow/pod/'):
                    return redirect(pod_next)
                destination = request.args.get('warehouse')
                if destination and 'warehouses.view' in rights:
                    if destination in warehouse_messages.WAREHOUSES:
                        return redirect(url_for('bf_client_warehouse_chat', state=destination))
                    if destination == 'directory':
                        return redirect(url_for('bf_client_warehouses'))
                booking_first=request.args.get('transfers')=='1' or db().execute(
                    'SELECT 1 FROM bf_interstate_accounts WHERE account_id=?',(account['id'],)).fetchone()
                homes=['bf_client_interstate','bf_client_calendar'] if booking_first else ['bf_client_calendar','bf_client_interstate']
                for endpoint in homes+['bf_client_national','bf_client_pods','bf_client_profiles_portal','bf_client_decomm_partners','bf_client_requests','bf_client_invoices','sbtx_client.calendar']:
                    if portal.ENDPOINTS[endpoint] in rights:
                        return redirect(url_for(endpoint))
                return render_template('binflow/client_inactive.html',account=account)
            return render_template(template,error='The username or password is incorrect.'),401
        return render_template(template,error=None)

    @app.post('/sbtx/client/logout', endpoint='sbtx_client_logout')
    @app.post('/binflow/client/logout')
    def bf_client_logout():
        session.clear()
        return redirect(url_for('sbtx_client_login' if request.endpoint == 'sbtx_client_logout' else 'bf_client_login'))

    @app.route('/binflow/client/pods',methods=['GET','POST'])
    @client_access
    def bf_client_pods():
        national='pods.all' in g.client_rights
        run_date=request.args.get('date','' if national else now()[:10])
        kind=request.args.get('period','day')
        if not run_date and kind!='day': run_date=now()[:10]
        period=reports.period_view(run_date,kind)
        if run_date:
            try: valid_date(run_date)
            except ValueError as error: abort(400,str(error))
        scope,params=pod_scope(g.bf_client,g.client_rights)
        dates=db().execute('SELECT DISTINCT j.scheduled_date FROM bf_jobs j WHERE '+scope+
            ' ORDER BY j.scheduled_date DESC LIMIT 60',params).fetchall()
        if run_date:
            scope+=' AND j.scheduled_date BETWEEN ? AND ?'
            params+= (period['first'],period['last'])
        page=max(1,request.args.get('page',1,type=int))
        count=db().execute('SELECT COUNT(*) FROM bf_jobs j WHERE '+scope,params).fetchone()[0]
        pages=max(1,(count+49)//50)
        page=min(page,pages)
        query="""SELECT j.id,j.code,j.pod_token,j.scheduled_date,j.client_name,j.site_name,j.state,j.warehouse_name,
            j.status,j.collected_at,j.pod_signed_at,j.pod_signed_name,j.pod_deleted_at,
            (SELECT COUNT(*) FROM bf_job_bins i WHERE i.job_id=j.id AND i.direction='collect') AS collection_bins,
            (SELECT SUM(net_g) FROM bf_job_bins i WHERE i.job_id=j.id AND i.direction='collect') AS net
            FROM bf_jobs j WHERE """+scope
        order=' ORDER BY j.scheduled_date DESC,j.client_name,j.site_name,j.id'
        if request.method=='POST':
            if request.form.get('selection')!='period':
                ids=reports.selected_ids()
                # JSON binding keeps large selections within SQLite's variable limit.
                import json
                query+=" AND CAST(j.id AS TEXT) IN (SELECT value FROM json_each(?))"
                params+= (json.dumps(sorted(ids)),)
            jobs=db().execute(query+order,params).fetchall()
            if request.form.get('selection')!='period' and len(jobs)!=len(ids): abort(404)
            return reports.pod_download(jobs,request.form.get('format','xls'),pdf,'Client-PODs-'+(period['first'] or 'all-dates'))
        jobs=db().execute(query+order+' LIMIT 50 OFFSET ?',(*params,(page-1)*50)).fetchall()
        return render_template('binflow/client_pods.html',account=g.bf_client,jobs=jobs,dates=dates,
            run_date=run_date,national=national,page=page,pages=pages,period=period,count=count)

    @app.get('/binflow/client/pods/<int:job_id>.pdf')
    @client_access
    def bf_client_pdf(job_id):
        if not permitted_pod(db(),g.bf_client,job_id,g.client_rights): abort(404)
        return pdf(job_id)

    from sbtx import register as register_sbtx
    register_sbtx(app, db, client_access, now)
    from binflow_interstate import register as register_interstate
    register_interstate(app,db,access,require,render,client_access,now,actor)
    from binflow_invoices import register as register_invoices
    register_invoices(app,db,require,render,client_access,now,actor)
    register_management(app, db, client_access, now)
    warehouse_messages.register(app, db, client_access, access, render, now)
    from binflow_client_directory import register as register_directory
    register_directory(app,db,client_access,now)
    from binflow_supplier_profiles import register_suppliers
    register_suppliers(app, db, require, render, now, actor, client_access)

    register_pods(app, db, access, require, render, success, failure, now, pdf)

    @app.route('/binflow/settings/client-access',methods=['GET','POST'])
    @require('admin')
    def bf_client_accounts():
        fixed=db().execute("SELECT * FROM bf_client_accounts WHERE username='blocktexx'").fetchone()
        if fixed:
            return management_settings(fixed)
        if request.method=='POST':
            try:
                aid=int(request.form.get('account_id','0'))
                name=request.form.get('name','').strip()
                username=request.form.get('username','').strip().lower()
                password=request.form.get('password','')
                scopes={int(v) for v in request.form.getlist('client_id')}
                active=int(request.form.get('active')=='yes')
                if not name or len(name)>150 or not re.fullmatch(r'[a-z0-9][a-z0-9@._+-]{2,99}',username):
                    raise ValueError('Enter a name and a unique username or email (3–100 characters).')
                if (not aid or password) and not 12<=len(password)<=200:
                    raise ValueError('Use a password between 12 and 200 characters.')
                if active and not scopes: raise ValueError('Choose which clients this account may view.')
                db().execute('BEGIN IMMEDIATE')
                if any(not db().execute('SELECT 1 FROM bf_clients WHERE id=?',(cid,)).fetchone() for cid in scopes):
                    raise ValueError('Choose valid client accounts.')
                if aid:
                    previous=db().execute('SELECT * FROM bf_client_accounts WHERE id=? AND id NOT IN (SELECT account_id FROM bf_interstate_accounts)',(aid,)).fetchone()
                    if not previous: abort(404)
                    hashed=generate_password_hash(password) if password else previous['password_hash']
                    db().execute('''UPDATE bf_client_accounts SET username=?,name=?,password_hash=?,active=?,auth_version=auth_version+1,
                        updated_at=?,updated_by=? WHERE id=?''',(username,name,hashed,active,now(),actor(),aid))
                else:
                    aid=db().execute('''INSERT INTO bf_client_accounts(username,name,password_hash,active,created_at,updated_at,updated_by)
                        VALUES(?,?,?,?,?,?,?)''',(username,name,generate_password_hash(password),active,now(),now(),actor())).lastrowid
                portal.bind_named_default(db())
                db().execute('DELETE FROM bf_client_account_scope WHERE account_id=?',(aid,))
                db().executemany('INSERT INTO bf_client_account_scope(account_id,client_id) VALUES(?,?)',[(aid,cid) for cid in sorted(scopes)])
                db().execute('INSERT INTO bf_client_access_history(account_id,action,scopes,actor,created_at) VALUES(?,?,?,?,?)',
                    (aid,'Access saved; sessions revoked',','.join(map(str,sorted(scopes))),actor(),now()))
                return success('Client login saved. Share the portal address and password securely with that client.','bf_client_accounts')
            except (ValueError,sqlite3.IntegrityError) as error:
                return failure(error if isinstance(error,ValueError) else ValueError('That username is already in use.'),'bf_client_accounts')
        accounts=db().execute('SELECT id,username,name,active,updated_at FROM bf_client_accounts WHERE id NOT IN (SELECT account_id FROM bf_interstate_accounts) ORDER BY name').fetchall()
        selected=request.args.get('edit',type=int)
        record=db().execute('SELECT id,username,name,active FROM bf_client_accounts WHERE id=? AND id NOT IN (SELECT account_id FROM bf_interstate_accounts)',(selected,)).fetchone() if selected else None
        if selected and not record: abort(404)
        scopes=[r['client_id'] for r in db().execute('SELECT client_id FROM bf_client_account_scope WHERE account_id=?',(selected,))]
        clients=db().execute('SELECT id,name FROM bf_clients ORDER BY name').fetchall()
        return render('client_accounts',section='setup',accounts=accounts,record=record,scopes=scopes,clients=clients)

    def management_settings(account):
        if request.method=='POST':
            try:
                sites={int(v) for v in request.form.getlist('site_id')}
                password=request.form.get('password','')
                if password and not 12<=len(password)<=200:raise ValueError('Use a new password between 12 and 200 characters.')
                db().execute('BEGIN IMMEDIATE')
                if any(not db().execute("SELECT 1 FROM bf_locations WHERE id=? AND kind='client'",(sid,)).fetchone() for sid in sites):
                    raise ValueError('Choose valid collection sites.')
                aid=account['id']
                db().execute("UPDATE bf_client_accounts SET active=0,auth_version=auth_version+1 WHERE id!=? AND id NOT IN (SELECT account_id FROM bf_interstate_accounts)",(aid,))
                db().execute('UPDATE bf_client_accounts SET password_hash=?,active=?,auth_version=auth_version+1,updated_at=?,updated_by=? WHERE id=?',
                  (generate_password_hash(password) if password else account['password_hash'],int(request.form.get('active')=='yes'),now(),actor(),aid))
                db().execute('DELETE FROM bf_client_site_scope WHERE account_id=?',(aid,))
                db().executemany('INSERT INTO bf_client_site_scope VALUES(?,?)',[(aid,sid) for sid in sorted(sites)])
                db().execute('DELETE FROM bf_client_account_scope WHERE account_id=?',(aid,))
                db().execute("""INSERT INTO bf_client_account_scope SELECT DISTINCT ?,l.client_id FROM bf_locations l
                  JOIN bf_client_site_scope s ON s.site_id=l.id WHERE s.account_id=?""",(aid,aid))
                db().execute('INSERT INTO bf_client_access_history(account_id,action,scopes,actor,created_at) VALUES(?,?,?,?,?)',
                  (aid,'BlockTexx site access saved; sessions revoked',','.join(map(str,sorted(sites))),actor(),now()))
                return success('BlockTexx access saved.','bf_client_accounts')
            except (ValueError,sqlite3.IntegrityError) as error:
                return failure(ValueError(str(error)),'bf_client_accounts')
        sites=db().execute("SELECT l.id,l.name,l.state,c.name AS client FROM bf_locations l JOIN bf_clients c ON c.id=l.client_id WHERE l.kind='client' ORDER BY l.state,c.name,l.name").fetchall()
        selected={r['site_id'] for r in db().execute('SELECT site_id FROM bf_client_site_scope WHERE account_id=?',(account['id'],))}
        return render('management_access',section='setup',account=account,sites=sites,selected=selected)
