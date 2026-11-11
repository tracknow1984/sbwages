"""Dated interstate bookings and the link to the warehouse's scanned manifest."""
import re
import secrets
import smtplib
import sqlite3
from collections import defaultdict
from datetime import datetime, timedelta
from email.message import EmailMessage

from flask import abort, flash, g, redirect, render_template, request, url_for
from werkzeug.security import generate_password_hash

from binflow_collections import STATES, calendar_period, dated
from binflow_pods import send_smtp
import binflow_client_access as portal
import binflow_freight_rates as freight

PLACES = {
    'MEL': ('Melbourne', 'VIC'), 'SYD': ('Sydney', 'NSW'), 'BNE': ('Brisbane', 'QLD'),
    'ADL': ('Adelaide', 'SA'), 'WA': ('Western Australia', 'WA'),
    **{state: (state, state) for state in STATES if state != 'WA'},
}
LANES = {'QLD-VIC': 3, 'NSW-QLD': 2, 'SA-VIC': 2, 'QLD-SA': 4, 'QLD-WA': 5}
FIELDS = ('job_type','handling','origin','destination','pickup_date','delivery_date','client_name',
          'pickup_address','pickup_contact','pickup_phone','delivery_address','delivery_contact',
          'delivery_phone','email','goods','vehicle','pickup_company','delivery_company')
PROGRESS = {'loading':'Loading','transit':'In transit','received':'Received','handed_over':'Handed over'}


def migrate(conn):
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS bf_interstate_accounts (
            account_id INTEGER PRIMARY KEY REFERENCES bf_client_accounts(id));
        CREATE TABLE IF NOT EXISTS bf_interstate_bookings (
            id INTEGER PRIMARY KEY, request_key TEXT NOT NULL UNIQUE,
            account_id INTEGER REFERENCES bf_client_accounts(id), created_by TEXT NOT NULL, created_at TEXT NOT NULL,
            job_type TEXT NOT NULL CHECK(job_type IN ('internal','external')),
            handling TEXT NOT NULL CHECK(handling IN ('scanned','bulk')),
            origin TEXT NOT NULL,destination TEXT NOT NULL, pickup_date TEXT NOT NULL,delivery_date TEXT NOT NULL,
            client_name TEXT NOT NULL,pickup_address TEXT NOT NULL,pickup_contact TEXT NOT NULL,pickup_phone TEXT NOT NULL,
            delivery_address TEXT NOT NULL,delivery_contact TEXT NOT NULL,delivery_phone TEXT NOT NULL,
            email TEXT NOT NULL,goods TEXT NOT NULL,version INTEGER NOT NULL DEFAULT 1,
            locked_at TEXT, locked_by TEXT, bulk_dispatched_at TEXT, bulk_delivered_at TEXT,
            email_status TEXT NOT NULL DEFAULT 'pending',email_attempt_at TEXT);
        CREATE INDEX IF NOT EXISTS bf_interstate_pickups ON bf_interstate_bookings(pickup_date);
        CREATE INDEX IF NOT EXISTS bf_interstate_deliveries ON bf_interstate_bookings(delivery_date);
        CREATE TABLE IF NOT EXISTS bf_interstate_manifests (
            transfer_id INTEGER PRIMARY KEY REFERENCES bf_bale_transfers(id),
            booking_id INTEGER NOT NULL REFERENCES bf_interstate_bookings(id),
            expected_bales INTEGER NOT NULL CHECK(expected_bales>0),
            blocktexx_handover INTEGER NOT NULL DEFAULT 0);
        CREATE INDEX IF NOT EXISTS bf_interstate_booking_manifest ON bf_interstate_manifests(booking_id,transfer_id);
    """)
    columns = {r[1] for r in conn.execute('PRAGMA table_info(bf_interstate_bookings)')}
    for field in ('pickup_company', 'delivery_company'):
        if field not in columns:
            conn.execute(f"ALTER TABLE bf_interstate_bookings ADD COLUMN {field} TEXT NOT NULL DEFAULT ''")
    freight.migrate(conn)


def booking_status(row, manifest_status=None):
    if row['bulk_delivered_at']:
        return 'Delivered'
    if row['bulk_dispatched_at']:
        return 'In transit'
    return PROGRESS.get(manifest_status, 'Scheduled' if row['locked_at'] else 'Requested')


def dispatch_colours(conn):
    """Allocate colours in first-booking order so filters and new bookings do not reshuffle them."""
    clients = conn.execute("""SELECT COALESCE(a.name,b.client_name) AS billing_name
        FROM bf_interstate_bookings b LEFT JOIN bf_client_accounts a ON a.id=b.account_id
        GROUP BY COALESCE(a.name,b.client_name) ORDER BY MIN(b.id)""")
    colours = {}
    for client in clients:
        key = ' '.join(client['billing_name'].casefold().split())
        colours.setdefault(key,len(colours))
    return colours


def route_days(origin, destination):
    return LANES.get('-'.join(sorted((PLACES[origin][1], PLACES[destination][1]))))


def parse_booking(form, client=False, previous=None, billing_name=None):
    from app import valid_email
    data = {field:form.get(field,'').strip() for field in FIELDS}
    if billing_name is not None:
        data['client_name'] = billing_name
    if client:
        data['handling']='bulk'
        data['job_type'] = previous['job_type'] if previous is not None else 'internal'
        for field, label in [('pickup_company', 'Pick Up Company Name'), ('delivery_company', 'Delivery Company Name')]:
            if not data[field]:
                raise ValueError(label+' is required.')
    same_legacy_rigid = (previous is not None and previous['rigid_hours_units'] is None
                         and previous['vehicle']==data['vehicle']=='RIGID'
                         and all(PLACES.get(data[k], ('',''))[1]==PLACES[previous[k]][1] for k in ('origin','destination')))
    local = data['vehicle']=='RIGID' and not same_legacy_rigid
    if (client or data['vehicle']) and data['vehicle'] not in ('RIGID', *freight.VEHICLES):
        raise ValueError('Choose RIGID, B DOUBLE or SEMI TRAILER.')
    data['rigid_hours_units'] = None
    if local:
        data['handling'] = 'bulk'
        data['rigid_hours_units'] = freight.requested_rigid_hours(form.get('hours',''))
    if data['job_type'] not in ('internal','external') or data['handling'] not in ('scanned','bulk'):
        raise ValueError('Choose a job type and how the goods will be recorded.')
    origin, destination = data['origin'], data['destination']
    if origin not in PLACES or destination not in PLACES:
        raise ValueError('Select the pickup and delivery states.')
    same_state = PLACES[origin][1] == PLACES[destination][1]
    if local and not same_state:
        raise ValueError('Local RIGID transfers require pickup and delivery in the same state.')
    if not local and same_state:
        raise ValueError('Choose a pickup and delivery in different states.')
    days = 0 if local else route_days(origin, destination)
    try:
        pickup = dated(data['pickup_date']) if data['pickup_date'] else None
        delivery = dated(data['delivery_date']) if data['delivery_date'] else None
        if days is not None and pickup and not delivery:
            delivery = pickup + timedelta(days=days)
        elif days is not None and delivery and not pickup:
            pickup = delivery - timedelta(days=days)
        if not pickup or not delivery or delivery < pickup:
            raise ValueError()
        data['pickup_date'] = dated(pickup.isoformat()).isoformat()
        data['delivery_date'] = dated(delivery.isoformat()).isoformat()
    except ValueError:
        raise ValueError('Check the dates. Enter both dates for a route without a transit estimate. Delivery must be on or after pickup.') from None
    for field,label in [('client_name','Client name'),('pickup_address','Client pickup address'),
                        ('pickup_contact','Client primary contact'),('delivery_address','Delivery address'),
                        ('delivery_contact','Delivery contact name')]:
        if not data[field]:
            raise ValueError(label+' is required.')
    if not valid_email(data['email']) or any(c in data['email'] for c in ',;'):
        raise ValueError('Enter one valid booking confirmation email.')
    if data['handling']=='bulk' and not data['goods']:
        raise ValueError('List the goods and quantities for this bulk movement.')
    for field in FIELDS:
        limit = {'goods':4000,'pickup_address':500,'delivery_address':500,'pickup_phone':50,
                 'delivery_phone':50}.get(field,200)
        if len(data[field]) > limit or ('\n' in data[field] or '\r' in data[field]) and field!='goods':
            raise ValueError('Check the length and format of '+field.replace('_',' ')+'.')
    return data


def confirmation(conn, config, row, now):
    """Claim this locked booking before contacting SMTP. Repeated lock clicks never resend."""
    stamp = now()
    stale = (datetime.fromisoformat(stamp)-timedelta(minutes=10)).isoformat(timespec='seconds')
    claimed = conn.execute("""UPDATE bf_interstate_bookings SET email_status='sending',email_attempt_at=?
        WHERE id=? AND locked_at IS NOT NULL AND (email_status IN ('pending','failed')
        OR (email_status='sending' AND email_attempt_at<?))""",(stamp,row['id'],stale)).rowcount
    conn.commit()
    if not claimed:
        return
    try:
        if not config['SMTP_HOST'] or not config['SMTP_FROM']:
            raise OSError('SMTP unavailable')
        msg = EmailMessage()
        service = 'local RIGID transfer' if row['rigid_hours_units'] is not None else 'interstate transfer'
        msg['Subject'] = f"SB Empire {service} IT-{row['id']:06d} confirmed"
        msg['From'], msg['To'] = config['SMTP_FROM'], row['email']
        freight_details = ''
        if row['vehicle']:
            freight_details = f"Vehicle: {row['vehicle']}\n"
            if row['rigid_hours_units'] is not None:
                billed = max(row['rigid_hours_units']/100, freight.MIN_RIGID_HOURS)
                freight_details += f"Requested hours: {row['rigid_hours_units']/100:g} · Billable hours: {billed:g}\n"
                if row['rigid_hourly_cents'] is not None:
                    freight_details += f"Hourly rate: AUD ${row['rigid_hourly_cents']/100:,.2f} ex GST\n"
            if row['account_id'] is not None:
                price=freight.quote(row['rate_cents'],row['fuel_bps'])
                if price:
                    freight_details += (f"Base freight: AUD ${price['base']/100:,.2f} ex GST\n"
                        f"Fuel surcharge ({price['bps']/100:g}%): AUD ${price['fuel']/100:,.2f} ex GST\n"
                        f"Total: AUD ${price['total']/100:,.2f} ex GST\n")
                else:
                    freight_details += "Freight price to be confirmed\n"
        pickup_company = row['pickup_company']+'\n' if row['pickup_company'] else ''
        delivery_company = row['delivery_company']+'\n' if row['delivery_company'] else ''
        msg.set_content(f"Hi {row['pickup_contact']},\n\nSB Empire has confirmed transfer IT-{row['id']:06d} for {row['client_name']}.\n\n"
            f"Pickup: {dated(row['pickup_date']):%a %d %b %Y} · {PLACES[row['origin']][0]}\n{pickup_company}{row['pickup_address']}\n"
            f"Pickup contact: {row['pickup_contact']} {row['pickup_phone']}\n\n"
            f"Delivery: {dated(row['delivery_date']):%a %d %b %Y} · {PLACES[row['destination']][0]}\n{delivery_company}{row['delivery_address']}\n"
            f"Delivery contact: {row['delivery_contact']} {row['delivery_phone']}\n\n"
            f"{freight_details}Goods: {row['goods'] or 'Bales on a scanned manifest'}\n\nSB Empire Operations")
        send_smtp(config, msg)
    except (OSError, smtplib.SMTPException, ValueError):
        status = 'failed'
    else:
        status = 'sent'
    conn.execute('UPDATE bf_interstate_bookings SET email_status=? WHERE id=? AND email_attempt_at=?',
                 (status,row['id'],stamp))
    conn.commit()


def register(app, db, access, require, render, client_access, now, actor):
    freight.register(app,db,require,render,client_access,now,actor)
    portal.register_site_scope(app,db,require,render,now,actor)

    def page(template, external, **data):
        data.update(places=PLACES,lanes=LANES,regions=STATES,vehicles=freight.VEHICLES,external=external)
        if external:
            return render_template('binflow/'+template+'.html',account=g.bf_client,**data)
        return render(template,section='interstate',**data)

    def booking(bid, external=False):
        row = db().execute('SELECT * FROM bf_interstate_bookings WHERE id=?',(bid,)).fetchone()
        if not row or external and row['account_id']!=g.bf_client['id']:
            abort(404)
        return row

    def details_url(bid, external):
        return url_for('bf_client_interstate_detail' if external else 'bf_interstate_detail',bid=bid)

    def show_calendar(external):
        view = request.args.get('view','month' if external else 'week')
        state = request.args.get('state','')
        if view not in ('week','month') or state not in ('',)+STATES:
            abort(400)
        try:
            chosen = dated(request.args.get('date') or now()[:10])
        except ValueError:
            abort(400)
        start,end,previous,following,title,weeks = calendar_period(chosen,view)
        scope = ' AND b.account_id=?' if external else ''
        args = (g.bf_client['id'],) if external else ()
        if state:
            points = [key for key,(_,region) in PLACES.items() if region==state]
            marks = ','.join('?' for _ in points)
            scope += f' AND (origin IN ({marks}) OR destination IN ({marks}))'
            args += (*points,*points)
        limits = (start.isoformat(),end.isoformat())
        select = """SELECT b.*, COALESCE(a.name,b.client_name) AS billing_name,
                       t.status AS manifest_status, m.expected_bales
                FROM bf_interstate_bookings b LEFT JOIN bf_client_accounts a ON a.id=b.account_id
                LEFT JOIN bf_interstate_manifests m ON m.transfer_id=(
                    SELECT MAX(x.transfer_id) FROM bf_interstate_manifests x WHERE x.booking_id=b.id)
                LEFT JOIN bf_bale_transfers t ON t.id=m.transfer_id"""
        rows = db().execute(select+""" WHERE b.locked_at IS NOT NULL
            AND (pickup_date BETWEEN ? AND ? OR delivery_date BETWEEN ? AND ?)"""+scope+
            ' ORDER BY b.client_name,b.id',(*limits,*limits,*args)).fetchall()
        pending = db().execute(select+' WHERE b.locked_at IS NULL'+scope+
                               ' ORDER BY b.pickup_date,b.id',args).fetchall()
        events = defaultdict(list)
        previews = {}
        legend = {}
        counts = {'Pickup':0,'Delivery':0}
        colours = dispatch_colours(db()) if not external and (rows or pending) else {}
        for booking in (*rows,*pending):
            name = booking['billing_name']
            key = ' '.join(name.casefold().split())
            preview = dict(booking,colour=colours.get(key,0),status=booking_status(booking,booking['manifest_status']))
            previews[booking['id']] = preview
            if not external and booking['locked_at']:
                legend.setdefault(preview['colour'],name)
        for row in rows:
            for field,label in [('pickup_date','Pickup'),('delivery_date','Delivery')]:
                if limits[0] <= row[field] <= limits[1]:
                    events[row[field]].append((label,row))
                    counts[label] += 1
        return page('interstate_calendar',external,view=view,state=state,chosen=chosen,
                    previous=previous,following=following,title=title,weeks=weeks,events=events,pending=pending,
                    previews=previews,legend=legend,counts=counts,today=now()[:10])

    @app.get('/binflow/interstate-transfers')
    @access
    def bf_interstate_calendar():
        return show_calendar(False)

    @app.get('/binflow/client/interstate-transfers')
    @client_access
    def bf_client_interstate():
        return show_calendar(True)

    def form_page(external, row=None):
        values = dict(row) if row else dict(job_type='internal',handling='bulk' if external else 'scanned',client_name=g.bf_client['name'] if external else '')
        aid = g.bf_client['id'] if external else (row['account_id'] if row else None)
        billing_name = None
        if external:
            billing_name = g.bf_client['name']
        elif aid is not None:
            billing_name = db().execute('SELECT name FROM bf_client_accounts WHERE id=?',(aid,)).fetchone()['name']
        if row and row['rigid_hours_units'] is not None:
            values['hours'] = f"{row['rigid_hours_units']/100:g}"

        def rate_card(conn):
            card=freight.rates_for(conn,aid) if aid else {}
            if row and row['rigid_hours_units'] is None and row['vehicle'] and row['rate_cents'] is not None:
                card[f"{PLACES[row['origin']][1]}:{PLACES[row['destination']][1]}:{row['vehicle']}"]=freight.quote(row['rate_cents'],row['fuel_bps'])
            return card

        def show(error=None):
            legacy_route = None
            if row and row['vehicle']=='RIGID' and row['rigid_hours_units'] is None:
                legacy_route = (PLACES[row['origin']][1],PLACES[row['destination']][1])
            hourly, bps = freight.rigid_terms(db(), aid, row)
            return page('interstate_form',external,values=values,key=key,row=row,error=error,rates=rate_card(db()),
                        hourly=hourly,bps=bps,minimum=freight.MIN_RIGID_HOURS,
                        booking_vehicles=('RIGID', *freight.MATRIX_VEHICLES),
                        legacy_route=legacy_route,billing_name=billing_name,
                        priced=aid is not None,bulk_only=external)
        if not row and request.method=='GET' and request.args.get('pickup'):
            try:
                values['pickup_date'] = dated(request.args['pickup']).isoformat()
            except ValueError:
                abort(400)
        key = secrets.token_urlsafe(24)
        if request.method=='POST':
            values = request.form
            key = values.get('request_key','')
            try:
                data = parse_booking(values,client=external,previous=row,billing_name=billing_name)
                conn = db()
                conn.execute('BEGIN IMMEDIATE')
                route = ':'.join((PLACES[data['origin']][1], PLACES[data['destination']][1], data['vehicle']))
                data['rigid_hourly_cents'] = None
                if data['rigid_hours_units'] is not None:
                    hourly, bps = freight.rigid_terms(conn, aid, row)
                    data['rigid_hourly_cents'] = hourly
                    _, price = freight.rigid_quote(hourly, data['rigid_hours_units'], bps)
                else:
                    price = rate_card(conn).get(route) if aid and data['vehicle'] else None
                data['rate_cents'] = price['base'] if price else None
                data['fuel_bps'] = price['bps'] if price else 0
                shown = str(price['base']) if price else ''
                if price and price['bps']:
                    shown += '+'+str(price['bps'])
                if aid and 'shown_rate' in values and values['shown_rate']!=shown:
                    already_saved=not row and conn.execute('SELECT 1 FROM bf_interstate_bookings WHERE request_key=? AND account_id=?',(key,aid)).fetchone()
                    if not already_saved:
                        raise ValueError('The freight rate has changed. Review the updated price before submitting.')
                if row:
                    assignments = ','.join(f'{field}=?' for field in data)
                    if not conn.execute(f'UPDATE bf_interstate_bookings SET {assignments},version=version+1 WHERE id=? AND version=? AND locked_at IS NULL',
                                        (*data.values(),row['id'],values.get('version'))).rowcount:
                        raise ValueError('The booking changed. Reopen it to review its current details.')
                    bid = row['id']
                else:
                    if not re.fullmatch(r'[A-Za-z0-9_-]{20,100}',key):
                        raise ValueError('Reopen the new transfer form before submitting.')
                    aid = g.bf_client['id'] if external else None
                    who = g.bf_client['name'] if external else actor()
                    old = conn.execute('SELECT id,account_id,created_by FROM bf_interstate_bookings WHERE request_key=?',(key,)).fetchone()
                    if old:
                        if old['account_id']!=aid or old['created_by']!=who:
                            abort(409)
                        bid = old['id']
                    else:
                        columns = ','.join(data)
                        marks = ','.join('?' for _ in data)
                        bid = conn.execute(f'INSERT INTO bf_interstate_bookings(request_key,account_id,created_by,created_at,{columns}) VALUES(?,?,?,?,{marks})',
                                           (key,aid,who,now(),*data.values())).lastrowid
                conn.commit()
                return redirect(details_url(bid,external))
            except ValueError as error:
                db().rollback()
                return show(str(error)),400
        return show()

    @app.route('/binflow/interstate-transfers/new',methods=['GET','POST'])
    @access
    def bf_interstate_new():
        return form_page(False)

    @app.route('/binflow/client/interstate-transfers/new',methods=['GET','POST'])
    @client_access
    def bf_client_interstate_new():
        return form_page(True)

    @app.route('/binflow/client/interstate-transfers/<int:bid>/edit',methods=['GET','POST'])
    @client_access
    def bf_client_interstate_edit(bid):
        row=booking(bid,True)
        if row['locked_at']: abort(409)
        return form_page(True,row)

    @app.route('/binflow/interstate-transfers/<int:bid>/edit',methods=['GET','POST'])
    @require('admin')
    def bf_interstate_edit(bid):
        row = booking(bid)
        if row['locked_at']:
            abort(409)
        return form_page(False,row)

    def detail(bid,external):
        row = booking(bid,external)
        manifest = db().execute("""SELECT t.id,t.code,t.status,m.expected_bales FROM bf_interstate_manifests m
            JOIN bf_bale_transfers t ON t.id=m.transfer_id WHERE m.booking_id=? ORDER BY t.id DESC LIMIT 1""",(bid,)).fetchone()
        status = booking_status(row, manifest['status'] if manifest else None)
        return page('interstate_detail',external,b=row,manifest=manifest,status=status,
                    price=freight.quote(row['rate_cents'],row['fuel_bps']), minimum=freight.MIN_RIGID_HOURS)

    @app.get('/binflow/interstate-transfers/<int:bid>')
    @access
    def bf_interstate_detail(bid):
        return detail(bid,False)

    @app.get('/binflow/client/interstate-transfers/<int:bid>')
    @client_access
    def bf_client_interstate_detail(bid):
        return detail(bid,True)

    @app.post('/binflow/interstate-transfers/<int:bid>/lock')
    @require('admin')
    def bf_interstate_lock(bid):
        db().execute('BEGIN IMMEDIATE')
        row = booking(bid)
        if not row['locked_at']:
            if request.form.get('version',type=int)!=row['version']:
                abort(409,'The request has changed. Review the updated dates before confirming.')
            price=freight.quote(row['rate_cents'],row['fuel_bps'])
            hourly = row['rigid_hourly_cents']
            if price is None and row['rigid_hours_units'] is not None:
                hourly, bps = freight.rigid_terms(db(),row['account_id'],row)
                _, price = freight.rigid_quote(hourly,row['rigid_hours_units'],bps)
            elif price is None and row['account_id'] and row['vehicle']:
                key=f"{PLACES[row['origin']][1]}:{PLACES[row['destination']][1]}:{row['vehicle']}"
                price=freight.rates_for(db(),row['account_id']).get(key)
            db().execute('UPDATE bf_interstate_bookings SET locked_at=?,locked_by=?,rate_cents=?,fuel_bps=?,rigid_hourly_cents=?,version=version+1 WHERE id=?',
                         (now(),actor(),price['base'] if price else None,price['bps'] if price else 0,hourly,bid))
            db().commit()
            confirmation(db(),app.config,booking(bid),now)
        else:
            db().rollback()
        return redirect(details_url(bid,False))

    @app.post('/binflow/interstate-transfers/<int:bid>/email')
    @require('admin')
    def bf_interstate_email(bid):
        row = booking(bid)
        if not row['locked_at']:
            abort(409)
        confirmation(db(),app.config,row,now)
        return redirect(details_url(bid,False))

    @app.post('/binflow/interstate-transfers/<int:bid>/bulk')
    @access
    def bf_interstate_bulk(bid):
        row = booking(bid)
        if not row['locked_at'] or row['handling']!='bulk':
            abort(409)
        action = request.form.get('action')
        if action=='dispatch':
            db().execute('UPDATE bf_interstate_bookings SET bulk_dispatched_at=? WHERE id=? AND bulk_dispatched_at IS NULL',(now(),bid))
        elif action=='deliver' and row['bulk_dispatched_at']:
            db().execute('UPDATE bf_interstate_bookings SET bulk_delivered_at=? WHERE id=? AND bulk_delivered_at IS NULL',(now(),bid))
        else:
            abort(409)
        db().commit()
        return redirect(details_url(bid,False))

    @app.route('/binflow/interstate-transfers/access',methods=['GET','POST'])
    @require('admin')
    def bf_interstate_accounts():
        error = None
        attempted = None
        if request.method=='POST':
            try:
                name = request.form.get('name','').strip()
                username = request.form.get('username','').strip().lower()
                password = request.form.get('password','')
                aid = request.form.get('account_id',type=int)
                if not name or len(name)>150 or not re.fullmatch(r'[a-z0-9][a-z0-9@._+-]{2,99}',username):
                    raise ValueError('Enter an organisation name and a unique username of 3–100 characters.')
                if (not aid or password) and not 12<=len(password)<=200:
                    raise ValueError('Use a password between 12 and 200 characters.')
                conn = db()
                conn.execute('BEGIN IMMEDIATE')
                active = int(request.form.get('active')=='yes')
                if aid:
                    old = conn.execute("""SELECT a.password_hash FROM bf_client_accounts a JOIN bf_interstate_accounts i
                        ON i.account_id=a.id WHERE a.id=?""",(aid,)).fetchone()
                    if not old:
                        abort(404)
                    conn.execute("""UPDATE bf_client_accounts SET name=?,username=?,active=?,password_hash=?,
                        auth_version=auth_version+1,updated_at=?,updated_by=? WHERE id=?""",
                        (name,username,active,generate_password_hash(password) if password else old['password_hash'],now(),actor(),aid))
                else:
                    aid = conn.execute("""INSERT INTO bf_client_accounts(name,username,password_hash,active,created_at,updated_at,updated_by)
                        VALUES(?,?,?,?,?,?,?)""",(name,username,generate_password_hash(password),active,now(),now(),actor())).lastrowid
                    conn.execute('INSERT INTO bf_interstate_accounts VALUES(?)',(aid,))
                    portal.bind_named_default(conn)
                    if request.form.get('custom_permissions')=='yes':
                        portal.save_rights(conn,aid,request.form)
                conn.commit()
                flash(f'Booking login “{username}” saved.','success')
                return redirect(url_for('bf_interstate_accounts',_anchor=f'login-{aid}'),code=303)
            except (ValueError,sqlite3.IntegrityError) as exc:
                db().rollback()
                error = str(exc) if isinstance(exc,ValueError) else 'That username is already in use.'
                attempted = dict(id=request.form.get('account_id',type=int),name=name,username=username,active=request.form.get('active')=='yes')
        accounts = db().execute("""SELECT a.id,a.name,a.username,a.active,a.created_at,
            i.account_id IS NOT NULL AS transfer_only FROM bf_client_accounts a
            LEFT JOIN bf_interstate_accounts i ON i.account_id=a.id
            ORDER BY a.created_at DESC,a.id DESC""").fetchall()
        policies={a['id']:portal.rights_for(db(),a['id']) for a in accounts}
        create_rights=portal.defaults(True)
        if attempted and not attempted['id'] and request.form.get('custom_permissions')=='yes':
            create_rights=set(request.form.getlist('permission'))
            if request.form.get('pod_scope')=='national':
                create_rights.add('pods.all')
        return render('interstate_access',policies=policies,modules=portal.MODULES,create_rights=create_rights,
                      revision=portal.revision,section='interstate',accounts=accounts,error=error,attempted=attempted),400 if error else 200

    @app.post('/binflow/interstate-transfers/access/<int:aid>/permissions')
    @require('admin')
    def bf_client_save_permissions(aid):
        conn=db()
        conn.execute('BEGIN IMMEDIATE')
        if not conn.execute('SELECT 1 FROM bf_client_accounts WHERE id=?',(aid,)).fetchone(): abort(404)
        if request.form.get('version')!=portal.revision(portal.rights_for(conn,aid)):
            abort(409,'Permissions changed. Reopen the login before saving.')
        try:
            portal.save_rights(conn,aid,request.form)
        except ValueError as error:
            conn.rollback()
            abort(400,str(error))
        conn.execute('INSERT INTO bf_client_access_history(account_id,action,scopes,actor,created_at) VALUES(?,?,?,?,?)',
                     (aid,'Portal permissions saved',','.join(sorted(portal.rights_for(conn,aid))),actor(),now()))
        conn.commit()
        flash('Portal permissions saved.','success')
        return redirect(url_for('bf_interstate_accounts',_anchor=f'login-{aid}'),code=303)
