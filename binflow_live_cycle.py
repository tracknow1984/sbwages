"""A labelled acceptance run in the ordinary BinFlow registers."""
import secrets

from flask import abort, g, has_request_context, request
from werkzeug.security import check_password_hash


def migrate(conn):
    conn.execute("""CREATE TABLE IF NOT EXISTS bf_live_cycle (
        id INTEGER PRIMARY KEY CHECK(id=1), driver_id INTEGER NOT NULL UNIQUE REFERENCES users(id),
        truck_id INTEGER NOT NULL REFERENCES bf_trucks(id), origin_id INTEGER NOT NULL REFERENCES bf_locations(id),
        destination_id INTEGER NOT NULL REFERENCES bf_locations(id), client_id INTEGER NOT NULL REFERENCES bf_clients(id),
        payer_id INTEGER NOT NULL REFERENCES bf_client_accounts(id), carrier_id INTEGER NOT NULL REFERENCES bf_supplier_site_profiles(id))""")


def client_calendar():
    return has_request_context() and bool(g.get('bf_client'))


def client_job_filter():
    if client_calendar():
        return ' AND NOT EXISTS (SELECT 1 FROM bf_live_cycle x WHERE x.client_id=j.pod_client_id) '
    return ''


def authenticate(conn, app, username, password, stamp):
    if username != 'testdriver' or not app.config['BINFLOW_TESTDRIVER_HASH']:
        return None
    if not check_password_hash(app.config['BINFLOW_TESTDRIVER_HASH'], password):
        return None
    with conn:
        conn.execute('BEGIN IMMEDIATE')
        run = conn.execute('SELECT * FROM bf_live_cycle').fetchone()
        uid = run['driver_id'] if run else provision(conn, stamp)
        return conn.execute("SELECT * FROM users WHERE id=? AND status='active'",(uid,)).fetchone()


def provision(c, stamp):
    actor = 'LIVE TEST · Test Driver'
    uid = c.execute("""INSERT INTO users(role,first_name,last_name,email,username,business_role,working_state)
        VALUES('employee','Live Test','Driver',?,?,'driver','NSW')""",
        ('binflow-'+secrets.token_hex(8)+'@example.invalid','bf-live-'+secrets.token_hex(8))).lastrowid
    truck = c.execute("INSERT INTO bf_trucks(name,state,created_at) VALUES(?,'NSW',?)",
                      ('LIVE TEST · NSW rigid',stamp)).lastrowid
    def depot(name,state):
        return c.execute("INSERT INTO bf_locations(kind,name,state,address,created_at) VALUES('warehouse',?,?,?,?)",
                         ('LIVE TEST · '+name,state,name+', '+state,stamp)).lastrowid
    origin, dest = depot('Wetherill Park','NSW'), depot('Brisbane','QLD')
    c.execute('INSERT INTO bf_bale_managers VALUES(?,?)',(uid,origin))
    client = c.execute('INSERT INTO bf_clients(name,created_at) VALUES(?,?)',('LIVE TEST · Textile collections',stamp)).lastrowid
    payer = c.execute("""INSERT INTO bf_client_accounts(username,name,password_hash,active,created_at,updated_at,updated_by)
        VALUES(?,?,'',0,?,?,?)""",('bf-test-'+secrets.token_hex(8),'LIVE TEST · Draft only',stamp,stamp,actor)).lastrowid
    c.execute('INSERT INTO bf_pod_bill_to VALUES(?,?)',(client,payer))
    carrier = c.execute("""INSERT INTO bf_supplier_site_profiles(category,group_name,customer_site,updated_at,updated_by)
        VALUES('industry-suppliers','TRANSPORT','LIVE TEST · Internal carrier',?,?)""",(stamp,actor)).lastrowid
    c.execute('INSERT INTO bf_live_cycle VALUES(1,?,?,?,?,?,?,?)',(uid,truck,origin,dest,client,payer,carrier))
    for material in ('COTTON','PC MIX'):
        c.execute('INSERT OR IGNORE INTO bf_material_classes(name,updated_at,updated_by) VALUES(?,?,?)',(material,stamp,actor))
    for seq, (place, volumes) in enumerate((('Smithfield',(1100,240)),('Fairfield',(660,))),1):
        name = 'LIVE TEST · '+place
        site = c.execute("""INSERT INTO bf_locations(kind,client_id,name,state,address,created_at)
            VALUES('client',?,?,'NSW',?,?)""",(client,name,place+', NSW',stamp)).lastrowid
        jid = c.execute("""INSERT INTO bf_jobs(code,request_key,site_id,warehouse_id,driver_id,scheduled_date,status,
            client_name,site_name,site_address,warehouse_name,state,created_at,created_by,truck_id,scan_mode,run_order,
            pod_client_id,notes,schedule_live,approval_status)
            VALUES(?,?,?,?,?,?,'scheduled',?,?,?,'LIVE TEST · Wetherill Park','NSW',?,?,?,1,?,?,?,1,'approved')""",
            (f'LIVE-TEST-POD-{seq:02}',secrets.token_urlsafe(24),site,origin,uid,stamp[:10],
             'LIVE TEST · Textile collections',name,place+', NSW',stamp,uid,truck,seq,client,
             'Internal live test. Record the exchange with the allocated LIVE TEST bins.')).lastrowid
        for litres in volumes:
            size=c.execute('SELECT * FROM bf_sizes WHERE litres=? ORDER BY id LIMIT 1',(litres,)).fetchone()
            if not size:
                sid=c.execute('INSERT INTO bf_sizes(name,litres,default_tare_g) VALUES(?,?,?)',
                              (f'{litres} L',litres,13000 if litres==240 else 42000)).lastrowid
                size=c.execute('SELECT * FROM bf_sizes WHERE id=?',(sid,)).fetchone()
            for direction in ('deliver','collect'):
                empty = direction=='deliver'
                code=f'LT-{litres}-'+('EMPTY' if empty else 'FULL')
                bid=c.execute("""INSERT INTO bf_bins(code,token,size_id,state,location_id,status,tare_g,created_at)
                    VALUES(?,?,?,'NSW',?,?,?,?)""",(code,secrets.token_urlsafe(24),size['id'],origin if empty else site,
                        'empty' if empty else 'full',size['default_tare_g'],stamp)).lastrowid
                c.execute('INSERT INTO bf_job_bins(job_id,bin_id,direction,bin_code,size_name,tare_g) VALUES(?,?,?,?,?,?)',
                          (jid,bid,direction,code,size['name'],size['default_tare_g']))
    return uid


# Only normal driver and warehouse operations needed for the named test stock.
ENDPOINTS = frozenset("""
static index logout login employee_dashboard bf_live_labels bf_live_label_qr bf_workday bf_resume bf_switch_workspace
bf_start_workday bf_accept_overview bf_overview_state bf_workday_vehicle bf_finish_workday
bf_workday_break bf_clock_state bf_run bf_run_state bf_run_prestart bf_accept_run bf_load_list
bf_run_scan bf_run_action bf_workday_plan_state bf_daily_pods bf_warehouse bf_warehouse_scan
bf_qr_image bf_job bf_weigh bf_pod_materials bf_pod_sign bf_pod bf_pod_pdf bf_pod_record bf_pod_signature bf_pod_qr bf_pod_journey
bf_bales bf_bale_new bf_bale_source_scan bf_bale bf_bale_label bf_bale_image bf_bale_qr
bf_bale_transfer_new bf_bale_transfer bf_bale_scan bf_bale_transfer_action bf_bale_note bf_bale_signature
""".split())


def guard(conn, run):
    if request.endpoint not in ENDPOINTS or 'demo' in request.args or 'sample_key' in (request.view_args or {}):
        abort(403)
    p = request.view_args or {}
    if 'truck_id' in p and p['truck_id'] != run['truck_id']:
        abort(403)
    if 'job_id' in p and not conn.execute('SELECT 1 FROM bf_jobs WHERE id=? AND pod_client_id=?',
                                           (p['job_id'],run['client_id'])).fetchone():
        abort(403)
    if 'tid' in p and not conn.execute('SELECT 1 FROM bf_bale_transfers WHERE id=? AND origin_id=? AND destination_id=?',
                                       (p['tid'],run['origin_id'],run['destination_id'])).fetchone():
        abort(403)
    if request.endpoint=='bf_bale_transfer_new':
        if 'booking' in request.args:
            abort(403)
        if request.method=='POST' and any(request.form.get(key,type=int)!=run[field] for key,field in (
                ('origin_id','origin_id'),('destination_id','destination_id'),('carrier_supplier_id','carrier_id'))):
            abort(403)


def register(app, db, access, require, render):
    @app.get('/admin/binflow/live-test')
    @require('admin')
    def bf_live_watch():
        run=db().execute('SELECT * FROM bf_live_cycle').fetchone()
        jobs,bins,bales,transfers=[],[],[],[]
        day=None
        if run:
            jobs=db().execute("""SELECT j.id,j.code,j.site_name,j.status,j.pod_signed_at,j.scheduled_date,
                    COUNT(i.id) AS expected,COUNT(i.returned_at) AS returned,COUNT(i.weighed_at) AS weighed,
                    SUM(i.net_g) AS net_g FROM bf_jobs j JOIN bf_job_bins i ON i.job_id=j.id AND i.direction='collect'
                    WHERE j.pod_client_id=? GROUP BY j.id ORDER BY j.run_order""",(run['client_id'],)).fetchall()
            bins=db().execute("""SELECT i.bin_code,i.direction,i.loaded_at,i.delivered_at,i.picked_up_at,i.returned_at,
                    i.weighed_at,i.net_g,j.site_name,j.pod_signed_at,bs.bale_id,b.code AS bale_code
                    FROM bf_job_bins i JOIN bf_jobs j ON j.id=i.job_id
                    LEFT JOIN bf_bale_sources bs ON bs.item_id=i.id LEFT JOIN bf_bales b ON b.id=bs.bale_id
                    WHERE j.pod_client_id=? ORDER BY j.run_order,i.direction DESC,i.bin_id""",(run['client_id'],)).fetchall()
            bales=db().execute('SELECT id,code,net_g,status FROM bf_bales WHERE origin_id=? ORDER BY id',(run['origin_id'],)).fetchall()
            transfers=db().execute("""SELECT t.id,t.code,t.status,COUNT(i.bale_id) AS bales,SUM(i.net_g) AS net_g
                    FROM bf_bale_transfers t LEFT JOIN bf_bale_transfer_items i ON i.transfer_id=t.id
                    WHERE t.origin_id=? GROUP BY t.id ORDER BY t.id""",(run['origin_id'],)).fetchall()
            day=db().execute('SELECT * FROM bf_workdays WHERE user_id=? ORDER BY id DESC LIMIT 1',(run['driver_id'],)).fetchone()
        return render('live_watch',section='driver_test',run=run,jobs=jobs,bins=bins,day=day,bales=bales,transfers=transfers)

    @app.get('/binflow/driver/live-test/labels')
    @access
    def bf_live_labels():
        run = g.get('live_cycle') or db().execute('SELECT * FROM bf_live_cycle').fetchone()
        if not run or (not g.get('live_cycle') and g.user['role']!='admin'):
            abort(403)
        rows=db().execute("""SELECT i.bin_code,i.size_name,i.bin_id,i.direction,i.tare_g,j.site_name
            FROM bf_job_bins i JOIN bf_jobs j ON j.id=i.job_id WHERE j.pod_client_id=? ORDER BY j.run_order,i.direction DESC,i.bin_id""",
            (run['client_id'],)).fetchall()
        return render('live_labels',section='driver',rows=rows)

    @app.get('/binflow/driver/live-test/labels/<int:bin_id>.png')
    @access
    def bf_live_label_qr(bin_id):
        import io
        import qrcode
        from flask import send_file
        if not g.get('live_cycle') and g.user['role']!='admin':
            abort(403)
        row=db().execute("""SELECT b.code FROM bf_bins b JOIN bf_job_bins i ON i.bin_id=b.id
            JOIN bf_jobs j ON j.id=i.job_id JOIN bf_live_cycle x ON x.client_id=j.pod_client_id WHERE b.id=?""",(bin_id,)).fetchone()
        if not row: abort(404)
        out=io.BytesIO()
        qrcode.make(row['code']).save(out,format='PNG')
        out.seek(0)
        return send_file(out,mimetype='image/png')
