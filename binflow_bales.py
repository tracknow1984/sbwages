"""Weighed bale custody, warehouse capacity and scanned interstate manifests."""
import csv
import io
import json
import math
import secrets
from decimal import Decimal, InvalidOperation
from functools import wraps
from urllib.parse import urlparse

from flask import abort, g, jsonify, redirect, render_template, request, send_file, session, url_for
from prestart_media import signature_png


def migrate(conn):
    conn.executescript('''
    CREATE TABLE IF NOT EXISTS bf_bale_sources (
      bale_id INTEGER NOT NULL REFERENCES bf_bales(id),
      item_id INTEGER NOT NULL UNIQUE REFERENCES bf_job_bins(id),
      bin_id INTEGER NOT NULL REFERENCES bf_bins(id), bin_code TEXT NOT NULL,
      material_id INTEGER NOT NULL, material_name TEXT NOT NULL, net_g INTEGER NOT NULL,
      scanned_at TEXT NOT NULL, scanned_by TEXT NOT NULL,
      PRIMARY KEY(bale_id,bin_id));
    CREATE TABLE IF NOT EXISTS bf_bale_settings (
      warehouse_id INTEGER PRIMARY KEY REFERENCES bf_locations(id),
      max_bales INTEGER, max_g INTEGER, target_g INTEGER, warning_percent INTEGER NOT NULL DEFAULT 90,
      receiving_hub INTEGER NOT NULL DEFAULT 0, version INTEGER NOT NULL DEFAULT 0,
      updated_at TEXT NOT NULL, updated_by TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS bf_bale_managers (
      user_id INTEGER PRIMARY KEY REFERENCES users(id), warehouse_id INTEGER NOT NULL REFERENCES bf_locations(id));
    CREATE TABLE IF NOT EXISTS bf_bales (
      id INTEGER PRIMARY KEY, code TEXT UNIQUE, token TEXT NOT NULL UNIQUE, request_key TEXT NOT NULL UNIQUE,
      origin_id INTEGER NOT NULL REFERENCES bf_locations(id), warehouse_id INTEGER REFERENCES bf_locations(id),
      material_id INTEGER NOT NULL REFERENCES bf_material_classes(id), material_name TEXT NOT NULL,
      net_g INTEGER NOT NULL CHECK(net_g>0), status TEXT NOT NULL DEFAULT 'stored'
        CHECK(status IN ('stored','loading','transit','received','handed_over','void')),
      version INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL, created_by TEXT NOT NULL);
    CREATE INDEX IF NOT EXISTS bf_bales_stock ON bf_bales(warehouse_id,status);
    CREATE TABLE IF NOT EXISTS bf_bale_pickups (
      id INTEGER PRIMARY KEY, warehouse_id INTEGER NOT NULL REFERENCES bf_locations(id), reason TEXT NOT NULL,
      status TEXT NOT NULL CHECK(status IN ('ready','actioned','cleared')), created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
    CREATE UNIQUE INDEX IF NOT EXISTS bf_bale_pickup_ready ON bf_bale_pickups(warehouse_id) WHERE status='ready';
    CREATE TABLE IF NOT EXISTS bf_bale_transfers (
      id INTEGER PRIMARY KEY, request_key TEXT NOT NULL UNIQUE, code TEXT UNIQUE,
      origin_id INTEGER NOT NULL REFERENCES bf_locations(id), destination_id INTEGER NOT NULL REFERENCES bf_locations(id),
      origin_name TEXT NOT NULL, destination_name TEXT NOT NULL, carrier TEXT NOT NULL, vehicle TEXT NOT NULL,
      payload_g INTEGER NOT NULL CHECK(payload_g>0), status TEXT NOT NULL DEFAULT 'loading'
        CHECK(status IN ('loading','transit','received','handed_over','cancelled')),
      version INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL, created_by TEXT NOT NULL,
      departed_at TEXT, departed_by TEXT, received_at TEXT, signed_at TEXT, signed_by TEXT,
      recipient TEXT, signature BLOB, snapshot TEXT, pickup_id INTEGER REFERENCES bf_bale_pickups(id));
    CREATE TABLE IF NOT EXISTS bf_bale_transfer_items (
      transfer_id INTEGER NOT NULL REFERENCES bf_bale_transfers(id), bale_id INTEGER NOT NULL REFERENCES bf_bales(id),
      code TEXT NOT NULL, material_name TEXT NOT NULL, net_g INTEGER NOT NULL,
      loaded_at TEXT NOT NULL, loaded_by TEXT NOT NULL, received_at TEXT, received_by TEXT,
      PRIMARY KEY(transfer_id,bale_id));
    CREATE TABLE IF NOT EXISTS bf_bale_events (
      id INTEGER PRIMARY KEY, bale_id INTEGER REFERENCES bf_bales(id), transfer_id INTEGER REFERENCES bf_bale_transfers(id),
      warehouse_id INTEGER REFERENCES bf_locations(id), action TEXT NOT NULL, details TEXT NOT NULL,
      created_at TEXT NOT NULL, actor TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS bf_bale_storage (
      bale_id INTEGER PRIMARY KEY REFERENCES bf_bales(id), transfer_id INTEGER NOT NULL REFERENCES bf_bale_transfers(id),
      code TEXT NOT NULL, material_name TEXT NOT NULL, net_g INTEGER NOT NULL, signed_at TEXT NOT NULL,
      recipient TEXT NOT NULL, recorded_by TEXT NOT NULL);
    ''')


def weight(raw, optional=False):
    if optional and not str(raw).strip():return None
    try:
        value=Decimal(str(raw))
        if not value.is_finite() or value<=0 or value>1000000 or value*1000!=int(value*1000):raise ValueError()
        return int(value*1000)
    except (ValueError,InvalidOperation,OverflowError):raise ValueError('Enter a positive weight in kilograms, with up to three decimal places.') from None


def stock(conn,wid):
    return dict(conn.execute("""SELECT COUNT(*) AS count,COALESCE(SUM(net_g),0) AS grams,
      COALESCE(SUM(CASE WHEN status='stored' THEN 1 ELSE 0 END),0) AS available,
      COALESCE(SUM(CASE WHEN status='stored' THEN net_g ELSE 0 END),0) AS available_g
      FROM bf_bales WHERE warehouse_id=? AND status IN ('stored','loading','received')""",(wid,)).fetchone())


def readiness(conn,wid,stamp):
    cfg=conn.execute('SELECT * FROM bf_bale_settings WHERE warehouse_id=?',(wid,)).fetchone()
    s=stock(conn,wid);reasons=[]
    if cfg and not cfg['receiving_hub'] and s['available']:
        if cfg['max_bales'] and s['count']>=math.ceil(cfg['max_bales']*cfg['warning_percent']/100):reasons.append('Bale capacity threshold reached')
        if cfg['max_g'] and s['grams']*100>=cfg['max_g']*cfg['warning_percent']:reasons.append('Warehouse weight threshold reached')
        if cfg['target_g'] and s['available_g']>=cfg['target_g']:reasons.append('Interstate load target reached')
    if conn.execute("SELECT 1 FROM bf_bale_transfers WHERE origin_id=? AND status='loading'",(wid,)).fetchone():reasons=[]
    current=conn.execute("SELECT * FROM bf_bale_pickups WHERE warehouse_id=? AND status='ready'",(wid,)).fetchone()
    if reasons:
        reason=' · '.join(reasons)
        if current:conn.execute('UPDATE bf_bale_pickups SET reason=?,updated_at=? WHERE id=?',(reason,stamp,current['id']))
        else:conn.execute("INSERT INTO bf_bale_pickups(warehouse_id,reason,status,created_at,updated_at) VALUES(?,?,'ready',?,?)",(wid,reason,stamp,stamp))
    elif current:conn.execute("UPDATE bf_bale_pickups SET status='cleared',updated_at=? WHERE id=?",(stamp,current['id']))


def register(app,db,access,require,render,success,failure,now,actor):
    def manager(fn):
        @wraps(fn)
        @access
        def wrapped(*args,**kwargs):
            if g.user['role']!='admin' and g.user['business_role']!='manager':abort(403)
            return fn(*args,**kwargs)
        return wrapped

    def allowed(wid):
        w=db().execute("SELECT * FROM bf_locations WHERE id=? AND kind='warehouse'",(wid,)).fetchone()
        if not w:abort(404)
        if g.user['role']!='admin':
            assigned=db().execute('SELECT warehouse_id FROM bf_bale_managers WHERE user_id=?',(g.user['id'],)).fetchone()
            if w['state']!=g.user['working_state'] or (assigned and assigned['warehouse_id']!=wid):abort(403)
        return w

    def warehouses():
        rows=db().execute("SELECT * FROM bf_locations WHERE kind='warehouse' ORDER BY state,name").fetchall()
        if g.user['role']=='admin':return rows
        assigned=db().execute('SELECT warehouse_id FROM bf_bale_managers WHERE user_id=?',(g.user['id'],)).fetchone()
        return [w for w in rows if w['state']==g.user['working_state'] and (not assigned or assigned['warehouse_id']==w['id'])]

    def transfer(tid,side=None):
        t=db().execute('SELECT * FROM bf_bale_transfers WHERE id=?',(tid,)).fetchone()
        if not t:abort(404)
        if side:allowed(t[side+'_id'])
        elif g.user['role']!='admin' and not {t['origin_id'],t['destination_id']} & {w['id'] for w in warehouses()}:abort(403)
        return t

    def items(tid):
        return db().execute('SELECT * FROM bf_bale_transfer_items WHERE transfer_id=? ORDER BY code',(tid,)).fetchall()

    def event(action,bid=None,tid=None,wid=None,details=''):
        db().execute('INSERT INTO bf_bale_events(bale_id,transfer_id,warehouse_id,action,details,created_at,actor) VALUES(?,?,?,?,?,?,?)',
          (bid,tid,wid,action,details,now(),actor()))

    def check_version(row):
        if str(row['version'])!=request.form.get('version'):raise ValueError('This record changed. Refresh and check it before continuing.')

    def material():
        m=db().execute('SELECT * FROM bf_material_classes WHERE id=? AND active=1',(request.form.get('material_id'),)).fetchone()
        if not m:raise ValueError('Choose an active material classification in Setup.')
        return m

    def view(name,**values):
        return render(name,section='bales',bale_mode=True,**values)

    @app.get('/binflow/bales')
    @manager
    def bf_bales():
        ws=warehouses();selected=request.args.get('warehouse',type=int)
        if selected:allowed(selected)
        state=request.args.get('state','')
        shown=[w for w in ws if (not selected or w['id']==selected) and (not state or w['state']==state)]
        cards=[];totals={}
        for w in shown:
            s=stock(db(),w['id']);cfg=db().execute('SELECT * FROM bf_bale_settings WHERE warehouse_id=?',(w['id'],)).fetchone()
            pending=db().execute("SELECT * FROM bf_bale_pickups WHERE warehouse_id=? AND status='ready'",(w['id'],)).fetchone()
            cards.append(dict(warehouse=w,stock=s,config=cfg,pickup=pending))
            totals.setdefault(w['state'],dict(count=0,grams=0));totals[w['state']]['count']+=s['count'];totals[w['state']]['grams']+=s['grams']
        ids={w['id'] for w in shown}
        transfers=[t for t in db().execute('SELECT * FROM bf_bale_transfers ORDER BY id DESC') if t['origin_id'] in ids or t['destination_id'] in ids]
        rows=[b for b in db().execute('SELECT * FROM bf_bales ORDER BY id DESC') if b['warehouse_id'] in ids and b['status'] in ('stored','loading','received')]
        return view('bales',cards=cards,totals=totals,warehouses=ws,selected=selected,state=state,transfers=transfers,bales=rows)

    def source_bin(code,wid,mid):
        code=str(code).strip()
        if '/binflow/qr/' in code:code=urlparse(code).path.rstrip('/').split('/')[-1]
        b=db().execute('SELECT * FROM bf_bins WHERE code=? OR token=?',(code.upper(),code)).fetchone()
        if not b:raise ValueError('Bin QR code not recognised. Scan a physical bin label.')
        if b['location_id']!=wid or b['truck_id'] or b['status']!='weighed':
            raise ValueError(f"{b['code']}: receive and weigh this bin at the selected warehouse first.")
        if db().execute('SELECT 1 FROM bf_job_bins WHERE bin_id=? AND active=1',(b['id'],)).fetchone():
            raise ValueError(f"{b['code']}: this bin is reserved for another job.")
        i=db().execute("SELECT * FROM bf_job_bins WHERE bin_id=? AND direction='collect' ORDER BY id DESC LIMIT 1",(b['id'],)).fetchone()
        if not i or not i['weighed_at'] or not i['material_id'] or not i['net_g']:
            raise ValueError(f"{b['code']}: weigh and classify its collected material on the POD first.")
        if i['material_id']!=mid:
            raise ValueError(f"{b['code']}: material is {i['material_name']}. Only bins matching the selected classification can be added.")
        if db().execute('SELECT 1 FROM bf_bale_sources WHERE item_id=?',(i['id'],)).fetchone():
            raise ValueError(f"{b['code']}: these contents have already been used in a bale.")
        return b,i

    @app.post('/binflow/bales/source-scan')
    @manager
    def bf_bale_source_scan():
        try:
            wid=int(request.form.get('warehouse_id','0'));allowed(wid);m=material()
            b,i=source_bin(request.form.get('code',''),wid,m['id'])
            return jsonify(ok=True,code=b['code'],item_id=i['id'],material=i['material_name'],net_g=i['net_g'])
        except ValueError as error:return jsonify(ok=False,message=str(error)),409

    @app.route('/binflow/bales/new',methods=['GET','POST'])
    @manager
    def bf_bale_new():
        if request.method=='POST':
            try:
                wid=int(request.form.get('warehouse_id','0'));allowed(wid)
                key=request.form.get('request_key','')
                if not 20<=len(key)<=100:raise ValueError('Refresh the create bale form and try again.')
                db().execute('BEGIN IMMEDIATE')
                old=db().execute('SELECT * FROM bf_bales WHERE request_key=?',(key,)).fetchone()
                if old:
                    allowed(old['origin_id']);return success('This bale was already created.','bf_bale',bid=old['id'])
                m=material();grams=weight(request.form.get('weight',''))
                if request.form.get('confirm_sources')!='yes':raise ValueError('Confirm that all scanned bins have been emptied into this bale.')
                codes=request.form.getlist('source_code');versions=request.form.getlist('source_item')
                if not codes or len(codes)>200 or len(codes)!=len(versions):
                    raise ValueError('Scan between 1 and 200 source bins before creating the bale.')
                sources=[source_bin(code,wid,m['id']) for code in codes]
                if len({b['id'] for b,i in sources})!=len(sources):raise ValueError('Each source bin can only be scanned once.')
                if any(str(i['id'])!=v for (b,i),v in zip(sources,versions)):
                    raise ValueError('A source bin changed since scanning. Scan the bins again.')
                bid=db().execute('''INSERT INTO bf_bales(token,request_key,origin_id,warehouse_id,material_id,material_name,net_g,created_at,created_by)
                  VALUES(?,?,?,?,?,?,?,?,?)''',(secrets.token_hex(24),key,wid,wid,m['id'],m['name'],grams,now(),actor())).lastrowid
                db().execute('UPDATE bf_bales SET code=? WHERE id=?',(f'BA{bid:07d}',bid))
                for b,i in sources:
                    db().execute('INSERT INTO bf_bale_sources VALUES(?,?,?,?,?,?,?,?,?)',
                      (bid,i['id'],b['id'],b['code'],i['material_id'],i['material_name'],i['net_g'],now(),actor()))
                    db().execute("UPDATE bf_bins SET status='empty' WHERE id=?",(b['id'],))
                    db().execute('INSERT INTO bf_events(bin_id,job_id,event,from_location,to_location,actor,notes,created_at) VALUES(?,?,?,?,?,?,?,?)',
                      (b['id'],i['job_id'],'Contents processed into bale',wid,wid,actor(),f'BA{bid:07d} · '+m['name'],now()))
                event('Bale created and weighed',bid,wid=wid,details=json.dumps(dict(net_g=grams,material=m['name'])))
                readiness(db(),wid,now())
                return success('Bale created. Print and attach its label.','bf_bale',bid=bid)
            except (ValueError,InvalidOperation) as error:return failure(error,'bf_bale_new')
        return view('bale_new',warehouses=warehouses(),selected=request.args.get('warehouse',type=int),request_key=secrets.token_urlsafe(24),
          materials=db().execute('SELECT * FROM bf_material_classes WHERE active=1 ORDER BY name').fetchall())

    def bale(bid):
        b=db().execute('SELECT * FROM bf_bales WHERE id=?',(bid,)).fetchone()
        if not b:abort(404)
        own={w['id'] for w in warehouses()}
        if g.user['role']!='admin' and not ({b['origin_id'],b['warehouse_id']} & own):
            latest=db().execute('SELECT t.* FROM bf_bale_transfers t JOIN bf_bale_transfer_items i ON i.transfer_id=t.id WHERE i.bale_id=? ORDER BY t.id DESC LIMIT 1',(bid,)).fetchone()
            if not latest or latest['destination_id'] not in own:abort(403)
        return b

    @app.get('/binflow/bales/<int:bid>')
    @manager
    def bf_bale(bid):
        b=bale(bid)
        return view('bale',bale=b,warehouse=db().execute('SELECT * FROM bf_locations WHERE id=?',(b['warehouse_id'],)).fetchone(),
          sources=db().execute('SELECT s.*,i.job_id FROM bf_bale_sources s JOIN bf_job_bins i ON i.id=s.item_id WHERE s.bale_id=? ORDER BY s.bin_code',(bid,)).fetchall(),
          history=db().execute('SELECT * FROM bf_bale_events WHERE bale_id=? ORDER BY id',(bid,)).fetchall(),
          materials=db().execute('SELECT * FROM bf_material_classes WHERE active=1 ORDER BY name').fetchall())

    @app.post('/binflow/bales/<int:bid>/correct')
    @manager
    def bf_bale_correct(bid):
        try:
            db().execute('BEGIN IMMEDIATE');b=bale(bid);allowed(b['warehouse_id']);check_version(b)
            if b['status']!='stored':raise ValueError('Only unallocated stored bales can be corrected or voided.')
            reason=request.form.get('reason','').strip()
            if not reason or len(reason)>500:raise ValueError('Enter a correction reason (up to 500 characters).')
            if request.form.get('action')=='void':
                db().execute("UPDATE bf_bales SET status='void',warehouse_id=NULL,version=version+1 WHERE id=?",(bid,))
                event('Bale voided',bid,wid=b['warehouse_id'],details=reason)
            else:
                m=material();grams=weight(request.form.get('weight',''))
                if m['id']!=b['material_id'] and db().execute('SELECT 1 FROM bf_bale_sources WHERE bale_id=?',(bid,)).fetchone():
                    raise ValueError('Material is locked to the scanned source bins. It cannot be changed.')
                db().execute('UPDATE bf_bales SET net_g=?,material_id=?,material_name=?,version=version+1 WHERE id=?',(grams,m['id'],m['name'],bid))
                event('Bale corrected; label reprint required',bid,wid=b['warehouse_id'],details=json.dumps(dict(reason=reason,old_g=b['net_g'],new_g=grams,old_material=b['material_name'],material=m['name'])))
            readiness(db(),b['warehouse_id'],now())
            return success('Bale updated. Reprint the label if corrected.','bf_bale',bid=bid)
        except ValueError as error:return failure(error,'bf_bale',bid=bid)

    @app.get('/binflow/bales/qr/<token>')
    @manager
    def bf_bale_qr(token):
        b=db().execute('SELECT id FROM bf_bales WHERE token=?',(token,)).fetchone()
        if not b:abort(404)
        bale(b['id']);return redirect(url_for('bf_bale',bid=b['id']))

    @app.get('/binflow/bales/<int:bid>/label')
    @manager
    def bf_bale_label(bid):
        b=bale(bid)
        if b['status']=='void':abort(410)
        return view('bale_label',bale=b)

    @app.get('/binflow/bales/<int:bid>/qr.png')
    @manager
    def bf_bale_image(bid):
        import qrcode
        b=bale(bid);out=io.BytesIO()
        qrcode.make(url_for('bf_bale_qr',token=b['token'],_external=True)).save(out,format='PNG');out.seek(0)
        return send_file(out,mimetype='image/png')

    @app.route('/binflow/bales/settings',methods=['GET','POST'])
    @require('admin')
    def bf_bale_settings():
        if request.method=='POST':
            try:
                wid=int(request.form.get('warehouse_id','0'));w=allowed(wid)
                db().execute('BEGIN IMMEDIATE')
                cfg=db().execute('SELECT * FROM bf_bale_settings WHERE warehouse_id=?',(wid,)).fetchone()
                if request.form.get('version')!=str(cfg['version'] if cfg else 0):raise ValueError('Settings changed. Refresh before saving.')
                count=int(request.form['max_bales']) if request.form.get('max_bales') else None
                if count is not None and not 1<=count<=1000000:raise ValueError('Enter a bale capacity of at least one.')
                percent=int(request.form.get('warning_percent','90'))
                if not 1<=percent<=100:raise ValueError('The warning threshold must be between 1% and 100%.')
                hub=int(request.form.get('receiving_hub')=='yes')
                if hub and w['state']!='QLD':raise ValueError('The Brisbane receiving depot must be in QLD.')
                maxg=weight(request.form.get('max_kg',''),True);target=weight(request.form.get('target_kg',''),True)
                db().execute('''INSERT INTO bf_bale_settings VALUES(?,?,?,?,?,?,1,?,?) ON CONFLICT(warehouse_id) DO UPDATE SET
                  max_bales=excluded.max_bales,max_g=excluded.max_g,target_g=excluded.target_g,warning_percent=excluded.warning_percent,
                  receiving_hub=excluded.receiving_hub,version=bf_bale_settings.version+1,updated_at=excluded.updated_at,updated_by=excluded.updated_by''',
                  (wid,count,maxg,target,percent,hub,now(),actor()))
                event('Warehouse bale limits updated',wid=wid,details=json.dumps(dict(max_bales=count,max_g=maxg,target_g=target,warning_percent=percent,receiving_hub=hub)))
                readiness(db(),wid,now())
                return success('Warehouse limits saved.','bf_bale_settings')
            except ValueError as error:return failure(error,'bf_bale_settings')
        cfg={r['warehouse_id']:dict(r) for r in db().execute('SELECT * FROM bf_bale_settings')}
        managers=db().execute("SELECT u.id,u.first_name,u.last_name,u.working_state,m.warehouse_id FROM users u LEFT JOIN bf_bale_managers m ON m.user_id=u.id WHERE u.role='employee' AND u.business_role='manager' AND u.status='active' ORDER BY u.first_name").fetchall()
        return view('bale_settings',warehouses=warehouses(),configs=cfg,managers=managers)

    @app.post('/binflow/bales/managers')
    @require('admin')
    def bf_bale_managers():
        try:
            uid=int(request.form.get('user_id','0'));wid=int(request.form.get('warehouse_id','0'));w=allowed(wid)
            u=db().execute("SELECT * FROM users WHERE id=? AND role='employee' AND business_role='manager' AND status='active'",(uid,)).fetchone()
            if not u or u['working_state']!=w['state']:raise ValueError('Choose an active MANAGER whose working state matches the warehouse.')
            db().execute('INSERT INTO bf_bale_managers VALUES(?,?) ON CONFLICT(user_id) DO UPDATE SET warehouse_id=excluded.warehouse_id',(uid,wid))
            event('Warehouse manager assigned',wid=wid,details=str(uid))
            return success('Warehouse manager assigned. They use their own staff login.','bf_bale_settings')
        except ValueError as error:return failure(error,'bf_bale_settings')

    @app.route('/binflow/bales/transfers/new',methods=['GET','POST'])
    @manager
    def bf_bale_transfer_new():
        if request.method=='POST':
            try:
                oid=int(request.form.get('origin_id','0'));origin=allowed(oid)
                did=int(request.form.get('destination_id','0'))
                dest=db().execute("SELECT l.* FROM bf_locations l JOIN bf_bale_settings s ON s.warehouse_id=l.id WHERE l.id=? AND l.kind='warehouse' AND l.state='QLD' AND s.receiving_hub=1",(did,)).fetchone()
                if not dest or oid==did:raise ValueError('Choose a different configured Brisbane receiving depot.')
                carrier=request.form.get('carrier','').strip();vehicle=request.form.get('vehicle','').strip()
                if not carrier or not vehicle or max(len(carrier),len(vehicle))>150:raise ValueError('Enter the carrier and vehicle / trailer registration (up to 150 characters).')
                payload=weight(request.form.get('payload',''));key=request.form.get('request_key','')
                if not 20<=len(key)<=100:raise ValueError('Refresh the transfer form and try again.')
                db().execute('BEGIN IMMEDIATE')
                old=db().execute('SELECT * FROM bf_bale_transfers WHERE request_key=?',(key,)).fetchone()
                if old:
                    allowed(old['origin_id']);return success('Transfer already created.','bf_bale_transfer',tid=old['id'])
                pickup=db().execute("SELECT id FROM bf_bale_pickups WHERE warehouse_id=? AND status='ready'",(oid,)).fetchone()
                tid=db().execute('''INSERT INTO bf_bale_transfers(request_key,origin_id,destination_id,origin_name,destination_name,carrier,vehicle,payload_g,created_at,created_by,pickup_id)
                  VALUES(?,?,?,?,?,?,?,?,?,?,?)''',(key,oid,did,origin['name'],dest['name'],carrier,vehicle,payload,now(),actor(),pickup['id'] if pickup else None)).lastrowid
                db().execute('UPDATE bf_bale_transfers SET code=? WHERE id=?',(f'BT{tid:06d}',tid))
                if pickup:db().execute("UPDATE bf_bale_pickups SET status='actioned',updated_at=? WHERE id=?",(now(),pickup['id']))
                event('Transfer opened',tid=tid,wid=oid);readiness(db(),oid,now())
                return success('Transfer ready. Scan each bale as it goes onto the truck.','bf_bale_transfer',tid=tid)
            except ValueError as error:return failure(error,'bf_bale_transfer_new')
        return view('bale_transfer_new',warehouses=warehouses(),selected=request.args.get('warehouse',type=int),request_key=secrets.token_urlsafe(24),
          destinations=db().execute("SELECT l.* FROM bf_locations l JOIN bf_bale_settings s ON s.warehouse_id=l.id WHERE s.receiving_hub=1 AND l.state='QLD' AND l.kind='warehouse'").fetchall())

    @app.get('/binflow/bales/transfers/<int:tid>')
    @manager
    def bf_bale_transfer(tid):
        t=transfer(tid);rows=items(tid);own={w['id'] for w in warehouses()}
        return view('bale_transfer',transfer=t,items=rows,total=sum(i['net_g'] for i in rows),
          origin_access=t['origin_id'] in own,destination_access=t['destination_id'] in own)

    @app.post('/binflow/bales/transfers/<int:tid>/scan')
    @manager
    def bf_bale_scan(tid):
        try:
            db().execute('BEGIN IMMEDIATE');t=transfer(tid)
            action=request.form.get('action')
            if action not in ('load','receive'):raise ValueError('Choose load or receive.')
            allowed(t['origin_id'] if action=='load' else t['destination_id'])
            code=request.form.get('code','').strip()[:500]
            if '/binflow/bales/qr/' in code:code=urlparse(code).path.rstrip('/').split('/')[-1]
            b=db().execute('SELECT * FROM bf_bales WHERE code=? OR token=?',(code.upper(),code)).fetchone()
            if not b:raise ValueError('Bale not found. Scan its BINFLOW bale label or enter its BA number.')
            existing=db().execute('SELECT * FROM bf_bale_transfer_items WHERE transfer_id=? AND bale_id=?',(tid,b['id'])).fetchone()
            duplicate=bool(existing and (action=='load' or existing['received_at']))
            if action=='load':
                if t['status']!='loading':raise ValueError('This manifest is locked after dispatch.')
                if not duplicate:
                    if b['status']!='stored' or b['warehouse_id']!=t['origin_id']:raise ValueError('This bale is not available at the departure warehouse.')
                    total=sum(i['net_g'] for i in items(tid))
                    if total+b['net_g']>t['payload_g']:raise ValueError('This bale would exceed the confirmed truck payload limit.')
                    db().execute('INSERT INTO bf_bale_transfer_items VALUES(?,?,?,?,?,?,?,?,?)',(tid,b['id'],b['code'],b['material_name'],b['net_g'],now(),actor(),None,None))
                    db().execute("UPDATE bf_bales SET status='loading',version=version+1 WHERE id=?",(b['id'],))
                    event('Scanned onto truck',b['id'],tid,t['origin_id'])
            else:
                if t['status'] not in ('transit','received'):raise ValueError('This transfer is not awaiting warehouse receipt.')
                if not existing:raise ValueError('This bale is not on this truck manifest.')
                if not duplicate:
                    if b['status']!='transit':raise ValueError('This bale is not in transit.')
                    db().execute('UPDATE bf_bale_transfer_items SET received_at=?,received_by=? WHERE transfer_id=? AND bale_id=?',(now(),actor(),tid,b['id']))
                    db().execute("UPDATE bf_bales SET status='received',warehouse_id=?,version=version+1 WHERE id=?",(t['destination_id'],b['id']))
                    event('Scanned into Brisbane depot',b['id'],tid,t['destination_id'])
                    if not db().execute('SELECT 1 FROM bf_bale_transfer_items WHERE transfer_id=? AND received_at IS NULL',(tid,)).fetchone():
                        db().execute("UPDATE bf_bale_transfers SET status='received',received_at=? WHERE id=?",(now(),tid))
            if not duplicate:db().execute('UPDATE bf_bale_transfers SET version=version+1 WHERE id=?',(tid,))
            db().commit();t=transfer(tid);rows=items(tid)
            return jsonify(ok=True,duplicate=duplicate,message=('Already recorded: ' if duplicate else 'Scan saved: ')+b['code'],version=t['version'],stage=t['status'],redirect=url_for('bf_bale_transfer',tid=tid),
              html=render_template('binflow/bale_manifest.html',transfer=t,items=rows,total=sum(i['net_g'] for i in rows)))
        except ValueError as error:
            db().rollback();return jsonify(ok=False,message=str(error)),409

    @app.post('/binflow/bales/transfers/<int:tid>/action')
    @manager
    def bf_bale_transfer_action(tid):
        try:
            db().execute('BEGIN IMMEDIATE');t=transfer(tid);check_version(t)
            action=request.form.get('action');rows=items(tid)
            if action in ('remove','cancel','depart'):
                allowed(t['origin_id'])
                if t['status']!='loading':raise ValueError('The manifest is locked after dispatch.')
                if action=='remove':
                    bid=int(request.form.get('bale_id','0'))
                    if not any(i['bale_id']==bid for i in rows):raise ValueError('Bale is not on this manifest.')
                    db().execute('DELETE FROM bf_bale_transfer_items WHERE transfer_id=? AND bale_id=?',(tid,bid))
                    db().execute("UPDATE bf_bales SET status='stored',version=version+1 WHERE id=?",(bid,))
                    event('Removed from loading manifest',bid,tid,t['origin_id'])
                elif action=='cancel':
                    for i in rows:
                        db().execute("UPDATE bf_bales SET status='stored',version=version+1 WHERE id=?",(i['bale_id'],))
                        event('Loading cancelled; returned to stock',i['bale_id'],tid,t['origin_id'])
                    db().execute("UPDATE bf_bale_transfers SET status='cancelled' WHERE id=?",(tid,))
                else:
                    if not rows or request.form.get('confirm')!='yes':raise ValueError('Load at least one bale and confirm the truck manifest.')
                    if sum(i['net_g'] for i in rows)>t['payload_g']:raise ValueError('Truck payload limit exceeded.')
                    for i in rows:
                        b=db().execute('SELECT * FROM bf_bales WHERE id=?',(i['bale_id'],)).fetchone()
                        if b['status']!='loading' or b['warehouse_id']!=t['origin_id']:raise ValueError('Bale custody changed. Review this manifest.')
                        db().execute("UPDATE bf_bales SET status='transit',warehouse_id=NULL,version=version+1 WHERE id=?",(i['bale_id'],))
                        event('Dispatched interstate',i['bale_id'],tid,t['origin_id'])
                    db().execute("UPDATE bf_bale_transfers SET status='transit',departed_at=?,departed_by=? WHERE id=?",(now(),actor(),tid))
                readiness(db(),t['origin_id'],now())
            elif action=='handover':
                allowed(t['destination_id'])
                if t['status']!='received' or not rows or any(not i['received_at'] for i in rows):raise ValueError('Scan every manifested bale into Brisbane before signing the handover.')
                recipient=request.form.get('recipient','').strip()
                if not recipient or len(recipient)>150 or request.form.get('confirm')!='yes':raise ValueError('Enter the BlockTexx recipient and confirm the storage handover.')
                signature=signature_png(request.form.get('signature','[]'))
                stamp=now();snapshot=dict(code=t['code'],origin=t['origin_name'],destination=t['destination_name'],carrier=t['carrier'],vehicle=t['vehicle'],
                  departed_at=t['departed_at'],received_at=t['received_at'],signed_at=stamp,recipient=recipient,signed_by=actor(),items=[dict(i) for i in rows],total_g=sum(i['net_g'] for i in rows))
                for i in rows:
                    b=db().execute('SELECT * FROM bf_bales WHERE id=?',(i['bale_id'],)).fetchone()
                    if b['status']!='received' or b['warehouse_id']!=t['destination_id']:raise ValueError('Bale custody changed. Review before sign-off.')
                    db().execute("UPDATE bf_bales SET status='handed_over',warehouse_id=NULL,version=version+1 WHERE id=?",(i['bale_id'],))
                    db().execute('INSERT INTO bf_bale_storage VALUES(?,?,?,?,?,?,?,?)',(i['bale_id'],tid,i['code'],i['material_name'],i['net_g'],stamp,recipient,actor()))
                    event('Handed to BlockTexx storage',i['bale_id'],tid,t['destination_id'],recipient)
                db().execute("UPDATE bf_bale_transfers SET status='handed_over',signed_at=?,signed_by=?,recipient=?,signature=?,snapshot=? WHERE id=?",(stamp,actor(),recipient,signature,json.dumps(snapshot),tid))
                readiness(db(),t['destination_id'],now())
            else:raise ValueError('Choose a valid transfer action.')
            db().execute('UPDATE bf_bale_transfers SET version=version+1 WHERE id=?',(tid,))
            event('Transfer '+action,tid=tid)
            return success('Transfer updated.','bf_bale_transfer',tid=tid)
        except ValueError as error:return failure(error,'bf_bale_transfer',tid=tid)

    @app.get('/binflow/bales/transfers/<int:tid>/note')
    @manager
    def bf_bale_note(tid):
        t=transfer(tid)
        if not t['signed_at']:abort(409,'The delivery note becomes final after the signed BlockTexx storage handover.')
        return view('bale_note',transfer=t,note=json.loads(t['snapshot']))

    @app.get('/binflow/bales/transfers/<int:tid>/signature.png')
    @manager
    def bf_bale_signature(tid):
        t=transfer(tid)
        if not t['signature']:abort(404)
        return send_file(io.BytesIO(t['signature']),mimetype='image/png')

    @app.get('/binflow/bales/storage.csv')
    @manager
    def bf_bale_storage_export():
        own={w['id'] for w in warehouses()};out=io.StringIO();writer=csv.writer(out)
        writer.writerow(['Delivery note','Bale','Material','Net kg','Origin','Brisbane depot','Storage handover','Recipient','Recorded by'])
        def safe(v):
            v=str(v);return "'"+v if v.startswith(('=','+','-','@','\t','\r')) else v
        for r in db().execute('SELECT s.*,t.code AS note,t.origin_id,t.destination_id,t.origin_name,t.destination_name FROM bf_bale_storage s JOIN bf_bale_transfers t ON t.id=s.transfer_id ORDER BY s.signed_at,s.bale_id'):
            if r['origin_id'] in own or r['destination_id'] in own:
                writer.writerow([safe(v) for v in (r['note'],r['code'],r['material_name'],str(Decimal(r['net_g'])/1000),r['origin_name'],r['destination_name'],r['signed_at'],r['recipient'],r['recorded_by'])])
        return send_file(io.BytesIO(out.getvalue().encode('utf-8-sig')),mimetype='text/csv',as_attachment=True,download_name='BlockTexx-storage-handovers.csv')
