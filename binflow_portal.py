"""Final daily PODs, with separately authenticated and explicitly scoped client accounts."""
import hashlib
import re
import secrets
import sqlite3
import time
from functools import wraps

from flask import abort, flash, g, redirect, render_template, request, session, url_for
from werkzeug.security import check_password_hash, generate_password_hash

from binflow_runs import valid_date


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


def register_portal(app, db, access, require, render, success, failure, now, actor, pdf):
    def client_access(fn):
        @wraps(fn)
        def wrapped(*args,**kwargs):
            account=db().execute('SELECT * FROM bf_client_accounts WHERE id=? AND active=1', (session.get('bf_client_uid'),)).fetchone()
            if not account or account['auth_version']!=session.get('bf_client_version'):
                session.pop('bf_client_uid',None);session.pop('bf_client_version',None)
                return redirect(url_for('bf_client_login'))
            g.bf_client=account
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

    @app.route('/binflow/client/login',methods=['GET','POST'])
    def bf_client_login():
        if request.method=='POST':
            username=request.form.get('username','').strip().lower()[:100]
            keys=[hashlib.sha256(f'bf-client:{value}'.encode()).hexdigest() for value in (username,'ip:'+str(request.remote_addr))]
            if any(limited(k) for k in keys):
                return render_template('binflow/client_login.html',error='Too many attempts. Try again in 15 minutes.'),429
            account=db().execute('SELECT * FROM bf_client_accounts WHERE username=?',(username,)).fetchone()
            matched=check_password_hash(account['password_hash'] if account else app.config['DUMMY_HASH'],request.form.get('password','')[:200])
            if account and account['active'] and matched:
                session.clear()
                session.update(bf_client_uid=account['id'],bf_client_version=account['auth_version'],csrf=secrets.token_urlsafe(32))
                session.permanent=True
                db().execute('DELETE FROM login_attempts WHERE key=?',(keys[0],));db().commit()
                return redirect(url_for('bf_client_pods'))
            return render_template('binflow/client_login.html',error='The username or password is incorrect.'),401
        return render_template('binflow/client_login.html',error=None)

    @app.post('/binflow/client/logout')
    def bf_client_logout():
        session.clear()
        return redirect(url_for('bf_client_login'))

    @app.get('/binflow/client/pods')
    @client_access
    def bf_client_pods():
        run_date=request.args.get('date',now()[:10])
        try: valid_date(run_date)
        except ValueError as error: abort(400,str(error))
        jobs=db().execute('''SELECT j.*,(SELECT SUM(net_g) FROM bf_job_bins i WHERE i.job_id=j.id AND i.direction='collect') AS net
            FROM bf_jobs j JOIN bf_client_account_scope s ON s.client_id=j.pod_client_id
            WHERE s.account_id=? AND j.status='complete' AND j.scheduled_date=? ORDER BY j.client_name,j.site_name,j.id''',
            (g.bf_client['id'],run_date)).fetchall()
        dates=db().execute('''SELECT DISTINCT j.scheduled_date FROM bf_jobs j JOIN bf_client_account_scope s ON s.client_id=j.pod_client_id
            WHERE s.account_id=? AND j.status='complete' ORDER BY j.scheduled_date DESC LIMIT 60''',(g.bf_client['id'],)).fetchall()
        return render_template('binflow/client_pods.html',account=g.bf_client,jobs=jobs,dates=dates,run_date=run_date)

    @app.get('/binflow/client/pods/<int:job_id>.pdf')
    @client_access
    def bf_client_pdf(job_id):
        permitted=db().execute('''SELECT 1 FROM bf_jobs j JOIN bf_client_account_scope s ON s.client_id=j.pod_client_id
            WHERE j.id=? AND s.account_id=? AND j.status='complete' ''',(job_id,g.bf_client['id'])).fetchone()
        if not permitted: abort(404)
        return pdf(job_id)

    @app.get('/binflow/pods')
    @access
    def bf_daily_pods():
        run_date=request.args.get('date',now()[:10])
        try: valid_date(run_date)
        except ValueError as error: abort(400,str(error))
        jobs=db().execute('''SELECT j.*,(SELECT SUM(net_g) FROM bf_job_bins i WHERE i.job_id=j.id AND i.direction='collect') AS net,
            (SELECT COUNT(*) FROM bf_job_bins i WHERE i.job_id=j.id AND i.direction='collect' AND i.weighed_at IS NULL) AS unweighed
            FROM bf_jobs j WHERE j.scheduled_date=? AND j.approval_status='approved' AND j.status!='cancelled' ORDER BY j.client_name,j.site_name,j.id''',(run_date,)).fetchall()
        return render('daily_pods',section='pods',run_date=run_date,jobs=jobs,closed=sum(j['status']=='complete' for j in jobs))

    @app.route('/binflow/settings/client-access',methods=['GET','POST'])
    @require('admin')
    def bf_client_accounts():
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
                    previous=db().execute('SELECT * FROM bf_client_accounts WHERE id=?',(aid,)).fetchone()
                    if not previous: abort(404)
                    hashed=generate_password_hash(password) if password else previous['password_hash']
                    db().execute('''UPDATE bf_client_accounts SET username=?,name=?,password_hash=?,active=?,auth_version=auth_version+1,
                        updated_at=?,updated_by=? WHERE id=?''',(username,name,hashed,active,now(),actor(),aid))
                else:
                    aid=db().execute('''INSERT INTO bf_client_accounts(username,name,password_hash,active,created_at,updated_at,updated_by)
                        VALUES(?,?,?,?,?,?,?)''',(username,name,generate_password_hash(password),active,now(),now(),actor())).lastrowid
                db().execute('DELETE FROM bf_client_account_scope WHERE account_id=?',(aid,))
                db().executemany('INSERT INTO bf_client_account_scope(account_id,client_id) VALUES(?,?)',[(aid,cid) for cid in sorted(scopes)])
                db().execute('INSERT INTO bf_client_access_history(account_id,action,scopes,actor,created_at) VALUES(?,?,?,?,?)',
                    (aid,'Access saved; sessions revoked',','.join(map(str,sorted(scopes))),actor(),now()))
                return success('Client login saved. Share the portal address and password securely with that client.','bf_client_accounts')
            except (ValueError,sqlite3.IntegrityError) as error:
                return failure(error if isinstance(error,ValueError) else ValueError('That username is already in use.'),'bf_client_accounts')
        accounts=db().execute('SELECT id,username,name,active,updated_at FROM bf_client_accounts ORDER BY name').fetchall()
        selected=request.args.get('edit',type=int)
        record=db().execute('SELECT id,username,name,active FROM bf_client_accounts WHERE id=?',(selected,)).fetchone() if selected else None
        if selected and not record: abort(404)
        scopes=[r['client_id'] for r in db().execute('SELECT client_id FROM bf_client_account_scope WHERE account_id=?',(selected,))]
        clients=db().execute('SELECT id,name FROM bf_clients ORDER BY name').fetchall()
        return render('client_accounts',section='setup',accounts=accounts,record=record,scopes=scopes,clients=clients)
