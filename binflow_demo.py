"""Isolated per-user sample runs using the production journey and scan handlers.

Authentication, CSRF and session checks always use the real application database.
Only explicitly listed BinFlow endpoints may use a session-owned sample database.
No real stock, client, credential or payroll records are copied into it.
"""
import io
import secrets
import sqlite3
from pathlib import Path

from flask import abort, g, has_request_context, redirect, request, send_file, session, url_for
from binflow_permissions import has_binflow_access, selected_state, state_scope

DEMO_ENDPOINTS = frozenset({
    'bf_workday','bf_start_workday','bf_workday_vehicle','bf_finish_workday','bf_accept_overview',
    'bf_overview_state','bf_workday_break','bf_clock_state','bf_test_prestart','bf_run','bf_run_state',
    'bf_run_prestart','bf_accept_run','bf_run_action','bf_run_scan','bf_truck','bf_truck_scan',
    'bf_workday_plan_state','bf_load_list','bf_job','bf_pod_record','bf_pod_qr','bf_pod_signature','bf_pod_journey','bf_pod','bf_pod_pdf','bf_daily_pods',
    'bf_demo_labels','bf_demo_qr','bf_demo_inbound',
})
SUBURBS = {
    'QLD': ('North Maclean','Logan Central','Browns Plains'),
    'NSW': ('Wetherill Park','Smithfield','Fairfield'),
    'VIC': ('Laverton','Altona','Sunshine'), 'SA': ('Wingfield','Port Adelaide','Mawson Lakes'),
    'WA': ('Welshpool','Cannington','Kewdale'), 'ACT': ('Fyshwick','Kingston','Mitchell'),
    'TAS': ('Glenorchy','Moonah','Hobart'), 'NT': ('Berrimah','Palmerston','Darwin'),
}


def install_demo_database(app, real_db, now):
    app.extensions['bf_real_db'] = real_db

    @app.before_request
    def select_driver_demo():
        token = request.args.get('demo')
        if token is None:
            return
        saved = session.get('driver_demo', {})
        if (not g.user or not has_binflow_access(g.user) or request.endpoint not in DEMO_ENDPOINTS or
                not token or not secrets.compare_digest(token, saved.get('token', '')) or saved.get('uid') != g.user['id'] or
                (state_scope(g.user) and state_scope(g.user) != saved.get('state'))):
            abort(403, 'This sample run is not available in your session. Open Driver day and start a new sample run.')
        path = demo_path(app, token)
        if not path.is_file():
            abort(410, 'This sample run expired. Open Driver day and start a new sample run.')
        conn = sqlite3.connect(path, timeout=15)
        conn.row_factory = sqlite3.Row
        conn.execute('PRAGMA foreign_keys=ON')
        from binflow_reports import migrate_timings
        migrate_timings(conn)
        from binflow_pod_records import migrate as migrate_pod_records
        migrate_pod_records(conn)
        conn.commit()
        g.bf_demo_db = conn
        g.bf_demo_token = token

    @app.url_defaults
    def preserve_driver_demo(endpoint, values):
        if has_request_context() and getattr(g, 'bf_demo_token', None) and endpoint.startswith('bf_') and endpoint!='bf_demo_start' and 'demo' not in values:
            values['demo'] = g.bf_demo_token

    @app.teardown_appcontext
    def close_driver_demo(error):
        conn = g.pop('bf_demo_db', None)
        if conn is not None:
            conn.close()

    return lambda: g.bf_demo_db if getattr(g, 'bf_demo_db', None) is not None else real_db()


def demo_path(app, token):
    # The token is server-generated and taken from a signed, owner-checked session.
    return Path(app.config['DATABASE']).resolve().parent / 'driver-samples' / (token + '.sqlite')


def seed_demo(source, target, user, state, stamp):
    schema = source.execute("""SELECT sql FROM sqlite_master WHERE sql IS NOT NULL AND name NOT LIKE 'sqlite_%'
        ORDER BY CASE type WHEN 'table' THEN 0 WHEN 'index' THEN 1 WHEN 'view' THEN 2 ELSE 3 END,name""").fetchall()
    target.executescript(';\n'.join(row['sql'] for row in schema)+';')
    target.execute('PRAGMA foreign_keys=ON')
    target.execute('''INSERT INTO users(id,role,first_name,last_name,email,username,business_role,working_state)
        VALUES(?,?,?,?,?,?,?,?)''', (user['id'],user['role'],user['first_name'],user['last_name'],
                                   'sample@example.invalid','sample-driver',user['business_role'],state))
    places = SUBURBS[state]
    target.execute("INSERT INTO bf_locations(id,kind,name,state,address,created_at) VALUES(1,'warehouse',?,?,?,?)",
                   ('SAMPLE · '+places[0]+' depot',state,places[0]+', '+state+', Australia',stamp))
    target.execute('INSERT INTO bf_trucks(id,name,state,created_at) VALUES(1,?,?,?)', ('SAMPLE · '+state+' truck',state,stamp))
    for sid,litres,tare in ((1,1100,65000),(2,240,13000),(3,660,40000)):
        target.execute('INSERT INTO bf_sizes(id,name,litres,default_tare_g) VALUES(?,?,?,?)',(sid,f'{litres} L',litres,tare))
    for cid in (1,2):
        name = 'SAMPLE · Client '+('One' if cid==1 else 'Two')
        target.execute('INSERT INTO bf_clients(id,name,created_at) VALUES(?,?,?)',(cid,name,stamp))
        target.execute("INSERT INTO bf_locations(id,kind,client_id,name,state,address,created_at) VALUES(?,'client',?,?,?,?,?)",
                       (cid+1,cid,places[cid]+' sample stop',state,places[cid]+', '+state+', Australia',stamp))
        target.execute('''INSERT INTO bf_jobs(id,code,request_key,site_id,warehouse_id,driver_id,scheduled_date,status,
            client_name,site_name,site_address,warehouse_name,state,created_at,created_by,truck_id,scan_mode,run_order,notes)
            VALUES(?,?,?,?,1,?,?,'scheduled',?,?,?,?,?,?,?,1,1,?,?)''',
                       (cid,f'SAMPLE-POD-{cid:02d}',secrets.token_urlsafe(24),cid+1,user['id'],stamp[:10],name,
                        places[cid]+' sample stop',places[cid]+', '+state+', Australia','SAMPLE · '+places[0]+' depot',
                        state,stamp,user['id'],cid,'Sample run only. Suburb centres are used for test navigation.'))
    for bid,sid,jid,direction in ((1,1,1,'deliver'),(2,2,1,'deliver'),(3,3,2,'deliver'),
                                  (4,1,1,'collect'),(5,2,1,'collect'),(6,3,2,'collect')):
        size = target.execute('SELECT * FROM bf_sizes WHERE id=?',(sid,)).fetchone()
        code = f'DEMO-{size["litres"]}-'+('E' if direction=='deliver' else 'F')+f'{bid:02d}'
        target.execute('''INSERT INTO bf_bins(id,code,token,size_id,state,location_id,status,tare_g,created_at)
            VALUES(?,?,?,?,?,?,?,?,?)''',(bid,code,secrets.token_urlsafe(24),sid,state,
            1 if direction=='deliver' else jid+1,'empty' if direction=='deliver' else 'full',size['default_tare_g'],stamp))
        target.execute('INSERT INTO bf_job_bins(job_id,bin_id,direction,bin_code,size_name,tare_g) VALUES(?,?,?,?,?,?)',
                       (jid,bid,direction,code,size['name'],size['default_tare_g']))
    target.commit()


def register_demo(app, db, access, render, now):
    @app.post('/binflow/driver/demo')
    @access
    def bf_demo_start():
        state = selected_state(g.user,request.form.get('state') or state_scope(g.user) or 'QLD')
        if state not in SUBURBS:
            abort(400)
        saved = session.get('driver_demo',{})
        stamp = now()
        if (request.form.get('reset')!='yes' and saved.get('uid')==g.user['id'] and saved.get('date')==stamp[:10]
                and saved.get('state')==state and saved.get('token') and demo_path(app,saved['token']).is_file()):
            return redirect(url_for('bf_workday',demo=saved['token']))
        token = secrets.token_hex(24)
        path = demo_path(app,token)
        path.parent.mkdir(parents=True,exist_ok=True)
        try:
            with sqlite3.connect(path) as target:
                target.row_factory = sqlite3.Row
                seed_demo(app.extensions['bf_real_db'](),target,g.user,state,stamp)
        except Exception:
            path.unlink(missing_ok=True)
            raise
        session['driver_demo'] = dict(token=token,uid=g.user['id'],state=state,date=stamp[:10])
        # Reset only this session's previous isolated sample. Never touch live data.
        if saved.get('uid')==g.user['id'] and saved.get('token'):
            demo_path(app,saved['token']).unlink(missing_ok=True)
        return redirect(url_for('bf_workday',demo=token))

    def require_demo():
        if not getattr(g,'bf_demo_token',None):
            abort(404)

    @app.get('/binflow/driver/demo/labels')
    @access
    def bf_demo_labels():
        require_demo()
        return render('demo_labels',section='driver',driver_mode=True,
                      sample_bins=db().execute('''SELECT b.*,s.name AS size_name,j.client_name,i.direction FROM bf_bins b
                          JOIN bf_sizes s ON s.id=b.size_id JOIN bf_job_bins i ON i.bin_id=b.id JOIN bf_jobs j ON j.id=i.job_id
                          ORDER BY b.id''').fetchall())

    @app.get('/binflow/driver/demo/qr/<int:bin_id>')
    @access
    def bf_demo_qr(bin_id):
        require_demo()
        row = db().execute('SELECT code FROM bf_bins WHERE id=?',(bin_id,)).fetchone()
        if not row:
            abort(404)
        import qrcode
        image = qrcode.make(row['code'])
        output = io.BytesIO()
        image.save(output,format='PNG'); output.seek(0)
        return send_file(output,mimetype='image/png')

    @app.get('/binflow/driver/demo/inbound')
    @access
    def bf_demo_inbound():
        require_demo()
        return render('demo_inbound',section='driver',driver_mode=True,
                      inbound=db().execute('''SELECT b.*,s.name AS size_name,l.name AS warehouse_name FROM bf_bins b
                        JOIN bf_sizes s ON s.id=b.size_id JOIN bf_locations l ON l.id=b.location_id
                        WHERE b.status='received' ORDER BY b.code''').fetchall())
