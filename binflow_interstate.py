"""Dated interstate bookings and the link to the warehouse's scanned manifest."""
import io
import re
import json
import secrets
import smtplib
import sqlite3
from calendar import monthrange
from collections import defaultdict
from datetime import datetime, timedelta
from email.message import EmailMessage
from urllib.parse import quote, urlencode

from flask import abort, flash, g, redirect, render_template, request, send_file, url_for
from werkzeug.security import generate_password_hash

from binflow_auto_invoices import transfer_invoices
from binflow_collections import STATES, calendar_period, dated
from binflow_pods import send_smtp
from document_uploads import read_document
import binflow_client_access as portal
import binflow_freight_rates as freight
import binflow_transfer_dispatch as dispatch
import binflow_transfer_pods as transfer_pods
import sbtx

PLACES = {
    'MEL': ('Melbourne', 'VIC'), 'SYD': ('Sydney', 'NSW'), 'BNE': ('Brisbane', 'QLD'),
    'ADL': ('Adelaide', 'SA'), 'WA': ('Western Australia', 'WA'),
    **{state: (state, state) for state in STATES if state != 'WA'},
}
LANES = {'QLD-VIC': 3, 'NSW-QLD': 2, 'SA-VIC': 2, 'QLD-SA': 4, 'QLD-WA': 5}
FIELDS = ('job_type','handling','origin','destination','pickup_date','delivery_date','client_name',
          'pickup_address','pickup_contact','pickup_phone','delivery_address','delivery_contact',
          'delivery_phone','email','goods','vehicle','pickup_company','delivery_company')
PROGRESS = {'loading':'Loading','transit':'In transit','received':'Delivered','handed_over':'Delivered'}


def awaiting_confirmation(conn, state=''):
    """All open BinFlow confirmations, including dates outside the calendar view."""
    scope, args = '', []
    if state:
        points = [code for code, (_, area) in PLACES.items() if area == state]
        marks = ','.join('?' for _ in points)
        scope = f' AND (b.origin IN ({marks}) OR b.destination IN ({marks}))'
        args = points + points
    rows = conn.execute("""SELECT b.id, b.pickup_date, b.delivery_date, b.origin, b.destination,
            b.vehicle, COALESCE(a.name, b.client_name) AS customer, po.number AS po_number,
            EXISTS(SELECT 1 FROM bf_interstate_date_changes c WHERE c.booking_id=b.id
                AND c.decision IS NULL AND c.superseded_at IS NULL) AS date_change
        FROM bf_interstate_bookings b
        LEFT JOIN bf_client_accounts a ON a.id=b.account_id
        LEFT JOIN bf_interstate_purchase_orders po ON po.booking_id=b.id
        WHERE b.portal='binflow' AND b.locked_at IS NULL AND b.cancelled_at IS NULL"""
        + scope + ' ORDER BY b.pickup_date, b.id', args).fetchall()
    return [dict(row, origin_name=PLACES[row['origin']][0],
                 destination_name=PLACES[row['destination']][0]) for row in rows]


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
        CREATE TABLE IF NOT EXISTS bf_interstate_date_changes (
            id INTEGER PRIMARY KEY, booking_id INTEGER NOT NULL REFERENCES bf_interstate_bookings(id),
            original_pickup TEXT NOT NULL, original_delivery TEXT NOT NULL,
            proposed_pickup TEXT NOT NULL, proposed_delivery TEXT NOT NULL,
            requested_at TEXT NOT NULL, requested_by TEXT NOT NULL,
            decision TEXT CHECK(decision IN ('approved','original')), reviewed_at TEXT, reviewed_by TEXT);
        CREATE UNIQUE INDEX IF NOT EXISTS bf_interstate_open_change
            ON bf_interstate_date_changes(booking_id) WHERE decision IS NULL;
        CREATE TABLE IF NOT EXISTS bf_interstate_purchase_orders (
            booking_id INTEGER PRIMARY KEY REFERENCES bf_interstate_bookings(id),
            number TEXT NOT NULL DEFAULT '', filename TEXT, mime_type TEXT, document BLOB,
            revision INTEGER NOT NULL, updated_at TEXT NOT NULL, updated_by TEXT NOT NULL,
            CHECK(length(trim(number))>0 OR document IS NOT NULL),
            CHECK((document IS NULL AND filename IS NULL AND mime_type IS NULL)
               OR (length(document)>0 AND filename IS NOT NULL AND mime_type IS NOT NULL)));
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
    if 'portal' not in columns:
        conn.execute("ALTER TABLE bf_interstate_bookings ADD COLUMN portal TEXT NOT NULL DEFAULT 'binflow' CHECK(portal IN ('binflow','sbtx'))")
    conn.execute('CREATE INDEX IF NOT EXISTS bf_transfers_by_portal ON bf_interstate_bookings(portal,account_id,pickup_date)')
    for field in ('pickup_company', 'delivery_company'):
        if field not in columns:
            conn.execute(f"ALTER TABLE bf_interstate_bookings ADD COLUMN {field} TEXT NOT NULL DEFAULT ''")
    if 'cancelled_at' not in columns:
        conn.execute('ALTER TABLE bf_interstate_bookings ADD COLUMN cancelled_at TEXT')
    date_columns = {r[1] for r in conn.execute('PRAGMA table_info(bf_interstate_date_changes)')}
    if 'superseded_at' not in date_columns:
        conn.execute('ALTER TABLE bf_interstate_date_changes ADD COLUMN superseded_at TEXT')
        conn.execute('DROP INDEX IF EXISTS bf_interstate_open_change')
        conn.execute("""CREATE UNIQUE INDEX bf_interstate_open_change ON bf_interstate_date_changes(booking_id)
            WHERE decision IS NULL AND superseded_at IS NULL""")
    conn.execute("""CREATE TABLE IF NOT EXISTS bf_load_changes(
        id INTEGER PRIMARY KEY,booking_id INTEGER NOT NULL REFERENCES bf_interstate_bookings(id),
        action TEXT NOT NULL,reason TEXT NOT NULL,changed_at TEXT NOT NULL,changed_by TEXT NOT NULL,
        before_json TEXT NOT NULL,after_json TEXT NOT NULL)""")
    freight.migrate(conn)
    dispatch.migrate(conn)
    transfer_pods.migrate(conn)


def booking_status(row, manifest_status=None, date_change=False):
    if row['cancelled_at']:
        return 'Cancelled'
    if date_change:
        return 'Awaiting confirmation'
    if row['bulk_delivered_at']:
        return 'Delivered'
    if row['bulk_dispatched_at']:
        return 'In transit'
    return PROGRESS.get(manifest_status, 'Scheduled' if row['locked_at'] else 'Requested')


def share_dates_link(row):
    reference = f"IT-{row['id']:06d}"
    status = 'Confirmed' if row['locked_at'] else 'Awaiting confirmation from SB Empire'
    if not row['delivery_date']:
        status = 'Scheduled pickup · Delivery to confirm'
    lines = [f"Transfer {reference}", status, '']
    for stop, place in (('pickup', 'origin'), ('delivery', 'destination')):
        day = row[stop + '_date']
        label = dated(day).strftime('%A %d %B %Y') if day else 'Date to confirm'
        city, state = PLACES.get(row[place], ('', ''))
        location = ', '.join(filter(None, (row[stop + '_company'] or city, state)))
        lines.append(' · '.join(filter(None, (f'{stop.title()}: {label}', location))))
    return 'mailto:?' + urlencode({'subject': f'{reference} · Booking dates · {status}',
                                   'body': '\r\n'.join(lines)}, quote_via=quote)


def can_move_dates(row, manifest_status):
    return not (row['cancelled_at'] or row['bulk_dispatched_at'] or row['bulk_delivered_at']
                or manifest_status in ('transit', 'received', 'handed_over'))


def dispatch_colours(conn, portal_name='binflow'):
    """Allocate colours in first-booking order so filters and new bookings do not reshuffle them."""
    clients = conn.execute("""SELECT COALESCE(a.name,b.client_name) AS billing_name
        FROM bf_interstate_bookings b LEFT JOIN bf_client_accounts a ON a.id=b.account_id
        WHERE b.portal=? GROUP BY COALESCE(a.name,b.client_name) ORDER BY MIN(b.id)""", (portal_name,))
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
        WHERE id=? AND version=? AND locked_at IS NOT NULL AND (email_status IN ('pending','failed')
        OR (email_status='sending' AND email_attempt_at<?))""",(stamp,row['id'],row['version'],stale)).rowcount
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
        latest = conn.execute("""SELECT decision FROM bf_interstate_date_changes WHERE booking_id=?
            AND decision IS NOT NULL ORDER BY id DESC LIMIT 1""", (row['id'],)).fetchone()
        review_note = ''
        if latest:
            review_note = ('SB Empire approved your requested date change.' if latest['decision'] == 'approved'
                           else 'SB Empire has kept your originally confirmed dates.')
            review_note += '\n\n'
        pickup_company = row['pickup_company']+'\n' if row['pickup_company'] else ''
        delivery_company = row['delivery_company']+'\n' if row['delivery_company'] else ''
        msg.set_content(f"Hi {row['pickup_contact']},\n\nSB Empire has confirmed transfer IT-{row['id']:06d} for {row['client_name']}.\n\n"
            f"{review_note}Pickup: {dated(row['pickup_date']):%a %d %b %Y} · {PLACES[row['origin']][0]}\n{pickup_company}{row['pickup_address']}\n"
            f"Pickup contact: {row['pickup_contact']} {row['pickup_phone']}\n\n"
            f"Delivery: {dated(row['delivery_date']):%a %d %b %Y} · {PLACES[row['destination']][0]}\n{delivery_company}{row['delivery_address']}\n"
            f"Delivery contact: {row['delivery_contact']} {row['delivery_phone']}\n\n"
            f"{freight_details}Goods: {row['goods'] or 'Bales on a scanned manifest'}\n\nSB Empire Operations")
        send_smtp(config, msg)
    except (OSError, smtplib.SMTPException, ValueError):
        status = 'failed'
    else:
        status = 'sent'
    conn.execute('UPDATE bf_interstate_bookings SET email_status=? WHERE id=? AND version=? AND email_attempt_at=?',
                 (status,row['id'],row['version'],stamp))
    conn.commit()


def register(app, db, access, require, render, client_access, now, actor):
    sbtx_office, sbtx_client = sbtx.blueprints(db, client_access)
    app.add_template_global(sbtx.transfer_url, 'transfer_url')

    def office_transfer_link(bid, **values):
        return sbtx.office_detail_url(db(), bid, **values)

    app.add_template_global(office_transfer_link, 'office_transfer_link')

    def portal_name():
        return 'sbtx' if sbtx.is_sbtx() else 'binflow'

    def booking_right(action):
        return ('sbtx.' if sbtx.is_sbtx() else 'bookings.') + action

    dispatch.register_responses(app,db,require,now)
    app.add_template_global(share_dates_link, 'bf_share_dates_link')
    freight.register(app,db,require,render,client_access,now,actor)
    portal.register_site_scope(app,db,require,render,now,actor)

    def page(template, external, **data):
        data.update(sbtx_portal=sbtx.is_sbtx(), right=booking_right,
                    places={**PLACES, '': ('To confirm', '')} if sbtx.is_sbtx() else PLACES,lanes=LANES,regions=STATES,vehicles=freight.VEHICLES,external=external)
        if external:
            return render_template('binflow/'+template+'.html',account=g.bf_client,**data)
        if template in ('interstate_calendar','interstate_detail') and g.user['role']=='admin':
            connection = db().execute('SELECT tenant_id FROM bf_xero_config WHERE id=1 AND token_cipher IS NOT NULL').fetchone()
            data['billing_tenant'] = connection['tenant_id'] if connection else None
        if sbtx.is_sbtx():
            return render_template('binflow/'+template+'.html', tab='sbtx', **data)
        return render(template,section='interstate',**data)

    def booking(bid, external=False):
        row = db().execute('SELECT * FROM bf_interstate_bookings WHERE id=? AND portal=?',(bid,portal_name())).fetchone()
        if not row or external and row['account_id']!=g.bf_client['id']:
            abort(404)
        return row

    def details_url(bid, external):
        return sbtx.transfer_url('detail',external,bid=bid)

    def show_calendar(external):
        if external and request.args.get('responses')=='1':
            abort(403)
        view = request.args.get('view','month')
        state = request.args.get('state','')
        vehicle_filter = request.args.get('vehicle','')
        calendar_vehicles = ('RIGID', *freight.VEHICLES)
        if vehicle_filter not in ('', 'unassigned', *calendar_vehicles):
            abort(400)
        if view not in ('week','month') or state not in ('',)+STATES:
            abort(400)
        try:
            chosen = dated(request.args.get('date') or now()[:10])
        except ValueError:
            abort(400)
        scope = ' AND b.portal=?'
        args = (portal_name(),)
        if external:
            scope += ' AND b.account_id=?'
            args += (g.bf_client['id'],)
        dispatch_fields = dispatch_join = ''
        if not external:
            dispatch_fields = """,d.event AS handoff_event,d.target AS handoff_target,d.recipient AS handoff_recipient,
                d.status AS send_status,d.sent_at AS handed_at,d.decision AS dispatch_decision,d.decided_at"""
            dispatch_join = """LEFT JOIN bf_load_dispatches d ON d.id=(
                SELECT MAX(k.id) FROM bf_load_dispatches k WHERE k.booking_id=b.id)"""
        select = f"""SELECT b.*, COALESCE(a.name,b.client_name) AS billing_name,
                       t.status AS manifest_status, t.code AS manifest_code, m.expected_bales, changes.id AS date_change_id,
                       po.booking_id IS NOT NULL AS po_received, po.number AS po_number, po.filename AS po_filename, po.mime_type AS po_mime_type
                       {dispatch_fields}
                FROM bf_interstate_bookings b LEFT JOIN bf_client_accounts a ON a.id=b.account_id
                {dispatch_join}
                LEFT JOIN bf_interstate_manifests m ON m.transfer_id=(
                    SELECT MAX(x.transfer_id) FROM bf_interstate_manifests x WHERE x.booking_id=b.id)
                LEFT JOIN bf_bale_transfers t ON t.id=m.transfer_id
                LEFT JOIN bf_interstate_purchase_orders po ON po.booking_id=b.id
                LEFT JOIN bf_interstate_date_changes changes ON changes.booking_id=b.id AND changes.decision IS NULL AND changes.superseded_at IS NULL"""
        if request.args.get('export') == 'month':
            from binflow_transfer_export import monthly_workbook
            first = chosen.replace(day=1).isoformat()
            last = chosen.replace(day=monthrange(chosen.year, chosen.month)[1]).isoformat()
            bookings = db().execute(select + """ WHERE
                (b.pickup_date BETWEEN ? AND ? OR b.delivery_date BETWEEN ? AND ?)""" + scope +
                ' ORDER BY b.pickup_date,b.delivery_date,b.id', (first,last,first,last,*args))
            records = (dict(b, status=booking_status(b, b['manifest_status'], b['date_change_id'])) for b in bookings)
            brand = 'SBTX' if sbtx.is_sbtx() else 'BinFlow'
            response = send_file(monthly_workbook(records, chosen, PLACES, external, brand),
                                 download_name=f"{brand}-All-Transfers-{chosen:%Y-%m}.xlsx",
                                 mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
                                 as_attachment=True)
            response.cache_control.no_store = True
            return response
        start,end,previous,following,title,weeks = calendar_period(chosen,view)
        if state:
            points = [key for key,(_,region) in PLACES.items() if region==state]
            marks = ','.join('?' for _ in points)
            scope += f' AND (origin IN ({marks}) OR destination IN ({marks}))'
            args += (*points,*points)
        if vehicle_filter:
            scope += " AND COALESCE(b.vehicle,'')=?"
            args += ('' if vehicle_filter == 'unassigned' else vehicle_filter,)
        vehicle_order = "CASE b.vehicle WHEN 'RIGID' THEN 1 WHEN 'SEMI TRAILER' THEN 2 WHEN 'B-DOUBLE' THEN 3 ELSE 4 END"
        limits = (start.isoformat(),end.isoformat())
        rows = db().execute(select+""" WHERE b.locked_at IS NOT NULL
            AND b.cancelled_at IS NULL AND b.pickup_date BETWEEN ? AND ?"""+scope+
            f' ORDER BY {vehicle_order},b.client_name,b.id',(*limits,*args)).fetchall()
        if request.args.get('responses')=='1':
            from app import display_datetime
            invoices = transfer_invoices(db(),[b['id'] for b in rows])
            return {'bookings': {str(b['id']): dict(
                invoiced=bool(b['id'] in invoices and invoices[b['id']]['invoiced']),
                label=dispatch.response_label(b['handoff_event'],b['handoff_target'],b['send_status'],b['dispatch_decision']),
                decision=b['dispatch_decision'] or 'pending',recipient=b['handoff_recipient'] or '',
                sent=display_datetime(b['handed_at']) if b['handed_at'] else '',
                responded=display_datetime(b['decided_at']) if b['decided_at'] else '') for b in rows}}
        pending = db().execute(select+' WHERE b.locked_at IS NULL AND b.cancelled_at IS NULL'+scope+
                               f' ORDER BY b.pickup_date,{vehicle_order},b.id',args).fetchall()
        cancelled = db().execute(select+""" WHERE b.cancelled_at IS NOT NULL
            AND (pickup_date BETWEEN ? AND ? OR delivery_date BETWEEN ? AND ?)"""+scope+
            ' ORDER BY b.pickup_date,b.id',(*limits,*limits,*args)).fetchall()
        events = defaultdict(list)
        previews = {}
        legend = {}
        colours = dispatch_colours(db(), portal_name()) if not external and (rows or pending) else {}
        for booking in (*rows,*pending):
            name = booking['billing_name']
            key = ' '.join(name.casefold().split())
            preview = dict(booking, colour=colours.get(key,0),
                           movable=bool(booking['delivery_date']) and can_move_dates(booking, booking['manifest_status']),
                           status=booking_status(booking, booking['manifest_status'], booking['date_change_id']))
            previews[booking['id']] = preview
            if not external and booking['locked_at']:
                legend.setdefault(preview['colour'],name)
        for row in rows:
            events[row['pickup_date']].append(previews[row['id']])
        invoices = transfer_invoices(db(), list(previews)) if not external else {}
        return page('interstate_calendar',external,invoices=invoices,view=view,state=state,chosen=chosen,
                    previous=previous,following=following,title=title,weeks=weeks,events=events,pending=pending,cancelled=cancelled,
                    previews=previews,legend=legend,vehicle_filter=vehicle_filter,calendar_vehicles=calendar_vehicles,booking_count=len(rows),today=now()[:10],can_update_booking=can_update_booking(external))

    @sbtx_office.get('/', endpoint='calendar')
    @app.get('/binflow/interstate-transfers')
    @access
    def bf_interstate_calendar():
        return show_calendar(False)

    @sbtx_client.get('/', endpoint='calendar')
    @app.get('/binflow/client/interstate-transfers')
    @client_access
    def bf_client_interstate():
        return show_calendar(True)

    def office_change_reason():
        reason = request.form.get('reason','').strip()
        if not reason or len(reason)>1000:
            raise ValueError('Enter a reason of up to 1,000 characters.')
        return reason

    def has_delivered(row, manifest):
        return bool(row['bulk_delivered_at'] or (manifest and manifest['status'] in ('received','handed_over')))

    def record_change(before, action, reason):
        after = booking(before['id'])
        db().execute("""INSERT INTO bf_load_changes(booking_id,action,reason,changed_at,changed_by,before_json,after_json)
            VALUES(?,?,?,?,?,?,?)""",(before['id'],action,reason,now(),actor(),json.dumps(dict(before)),json.dumps(dict(after))))
        db().execute("""UPDATE bf_interstate_date_changes SET superseded_at=?,reviewed_by=?
            WHERE booking_id=? AND decision IS NULL AND superseded_at IS NULL""",(now(),actor(),before['id']))
        db().execute("""UPDATE bf_rental_invoices SET status='cancelled',error='Booking changed before issue'
            WHERE booking_id=? AND status='preview'""",(before['id'],))

    def form_page(external, row=None):
        values = dict(row) if row else dict(job_type='internal',handling='bulk' if external else 'scanned',client_name=g.bf_client['name'] if external else '')
        aid = g.bf_client['id'] if external else (row['account_id'] if row else None)
        payers = []
        can_assign = not aid and g.user and g.user['role']=='admin'
        if sbtx.is_sbtx():
            can_assign = not row or not row['locked_at'] or not row['origin']
        if not external and can_assign:
            account_scope = ""
            if sbtx.is_sbtx():
                account_scope = ''' AND EXISTS (SELECT 1 FROM bf_client_permissions p, json_each(p.rights) r
                    WHERE p.account_id=bf_client_accounts.id AND r.value='sbtx.view')'''
            payers = db().execute('SELECT id,name FROM bf_client_accounts WHERE active=1' +
                                  account_scope + ' ORDER BY name,id').fetchall()
            selected = request.args.get('bill_to',type=int)
            if selected is not None:
                if not any(a['id']==selected for a in payers): abort(400,'Choose an active billing client.')
                aid = selected
        billing_name = None
        if external:
            billing_name = g.bf_client['name']
        elif aid is not None:
            billing_name = db().execute('SELECT name FROM bf_client_accounts WHERE id=?',(aid,)).fetchone()['name']
        if row and row['rigid_hours_units'] is not None:
            values['hours'] = f"{row['rigid_hours_units']/100:g}"

        previous_terms = row if row and row['account_id'] == aid else None

        def rate_card(conn):
            card=freight.rates_for(conn,aid) if aid else {}
            if previous_terms and row['rigid_hours_units'] is None and row['vehicle'] and row['rate_cents'] is not None:
                card[f"{PLACES[row['origin']][1]}:{PLACES[row['destination']][1]}:{row['vehicle']}"]=freight.quote(row['rate_cents'],row['fuel_bps'])
            return card

        def show(error=None):
            legacy_route = None
            if row and row['vehicle']=='RIGID' and row['rigid_hours_units'] is None:
                legacy_route = (PLACES[row['origin']][1],PLACES[row['destination']][1])
            hourly, bps = freight.rigid_terms(db(), aid, previous_terms)
            return page('interstate_form',external,values=values,key=key,row=row,error=error,rates=rate_card(db()),
                        hourly=hourly,bps=bps,minimum=freight.MIN_RIGID_HOURS,
                        booking_vehicles=('RIGID', *freight.MATRIX_VEHICLES),
                        legacy_route=legacy_route,billing_name=billing_name,payers=payers,payer_id=aid,
                        priced=aid is not None,bulk_only=external or sbtx.is_sbtx())
        if not row and request.method=='GET' and request.args.get('pickup'):
            try:
                values['pickup_date'] = dated(request.args['pickup']).isoformat()
            except ValueError:
                abort(400)
        key = secrets.token_urlsafe(24)
        if request.method=='POST':
            values = request.form.copy()
            if sbtx.is_sbtx() and not external:
                values['handling'] = 'bulk'
            key = values.get('request_key','')
            try:
                data = parse_booking(values,client=external,previous=row,billing_name=billing_name)
                conn = db()
                conn.execute('BEGIN IMMEDIATE')
                route = ':'.join((PLACES[data['origin']][1], PLACES[data['destination']][1], data['vehicle']))
                data['rigid_hourly_cents'] = None
                if data['rigid_hours_units'] is not None:
                    hourly, bps = freight.rigid_terms(conn, aid, previous_terms)
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
                    already_saved=not row and conn.execute('SELECT 1 FROM bf_interstate_bookings WHERE request_key=? AND account_id=? AND portal=?',(key,aid,portal_name())).fetchone()
                    if not already_saved:
                        raise ValueError('The freight rate has changed. Review the updated price before submitting.')
                if row:
                    current = booking(row['id'],external)
                    manifest = current_manifest(row['id'])
                    if current['cancelled_at'] or has_delivered(current,manifest) or values.get('version',type=int)!=current['version']:
                        raise ValueError('The load changed or is closed. Reopen it before editing.')
                    if external and (current['locked_at'] and current['origin'] or pending_change(row['id'])):
                        abort(409)
                    if sbtx.is_sbtx() and not current['origin']:
                        data.update(locked_at=None, locked_by=None, email_status='pending', email_attempt_at=None)
                    if not external:
                        reason = office_change_reason()
                        if aid and (not current['account_id'] or sbtx.is_sbtx() and aid != current['account_id']):
                            if sbtx.is_sbtx() and current['account_id'] and po_metadata(current['id']):
                                raise ValueError('This booking already has a customer purchase order. Keep its billing client.')
                            data['account_id'] = aid
                        changed = {key for key,val in data.items() if current[key]!=val}
                        if not changed: raise ValueError('Change a load detail before saving.')
                        moving = bool(current['bulk_dispatched_at'] or manifest and manifest['status']=='transit')
                        if moving and changed - {'delivery_address','delivery_contact','delivery_phone','delivery_company','goods','email'}:
                            raise ValueError('This load is in transit. Update the delivery contact, delivery address or load notes.')
                        if manifest and manifest['status']=='loading' and changed & {'origin','destination','handling','vehicle'}:
                            raise ValueError('Close the loading manifest before changing its route, vehicle or handling.')
                        if not moving and current['locked_at']:
                            data.update(locked_at=None,locked_by=None,email_status='pending',email_attempt_at=None)
                    assignments = ','.join(f'{field}=?' for field in data)
                    conn.execute(f'UPDATE bf_interstate_bookings SET {assignments},version=version+1 WHERE id=?',
                                 (*data.values(),row['id']))
                    if not external:
                        record_change(current,'Modified',reason)
                        updated = booking(row['id'])
                        dispatch.update_previous(conn,updated,'Updated' if updated['locked_at'] else 'On hold',now())
                    bid = row['id']
                else:
                    if not re.fullmatch(r'[A-Za-z0-9_-]{20,100}',key):
                        raise ValueError('Reopen the new transfer form before submitting.')
                    who = g.bf_client['name'] if external else actor()
                    old = conn.execute('SELECT id,account_id,created_by,portal FROM bf_interstate_bookings WHERE request_key=?',(key,)).fetchone()
                    if old:
                        if old['portal']!=portal_name() or old['account_id']!=aid or old['created_by']!=who:
                            abort(409)
                        bid = old['id']
                    else:
                        data['portal'] = portal_name()
                        columns = ','.join(data)
                        marks = ','.join('?' for _ in data)
                        bid = conn.execute(f'INSERT INTO bf_interstate_bookings(request_key,account_id,created_by,created_at,{columns}) VALUES(?,?,?,?,{marks})',
                                           (key,aid,who,now(),*data.values())).lastrowid
                conn.commit()
                if row and not external:
                    dispatch.deliver_mail(conn,bid,app.config,now())
                return redirect(details_url(bid,external))
            except ValueError as error:
                db().rollback()
                return show(str(error)),400
        return show()

    @sbtx_office.route('/jobs/new', endpoint='new_job', methods=['GET','POST'])
    @app.route('/binflow/interstate-transfers/new',methods=['GET','POST'])
    @access
    def bf_interstate_new():
        return form_page(False)

    @sbtx_client.route('/jobs/new', endpoint='new_job', methods=['GET','POST'])
    @app.route('/binflow/client/interstate-transfers/new',methods=['GET','POST'])
    @client_access
    def bf_client_interstate_new():
        return form_page(True)

    @sbtx_client.route('/transfers/<int:bid>/edit', endpoint='edit', methods=['GET','POST'])
    @app.route('/binflow/client/interstate-transfers/<int:bid>/edit',methods=['GET','POST'])
    @client_access
    def bf_client_interstate_edit(bid):
        row=booking(bid,True)
        if row['cancelled_at'] or (row['locked_at'] and row['origin']) or pending_change(bid):
            abort(409)
        return form_page(True,row)

    @sbtx_office.route('/transfers/<int:bid>/edit', endpoint='edit', methods=['GET','POST'])
    @app.route('/binflow/interstate-transfers/<int:bid>/edit',methods=['GET','POST'])
    @require('admin')
    def bf_interstate_edit(bid):
        row = booking(bid)
        if row['cancelled_at'] or has_delivered(row,current_manifest(bid)):
            abort(409,'This load is closed.')
        return form_page(False,row)

    @sbtx_office.post('/transfers/<int:bid>/cancel', endpoint='cancel')
    @app.post('/binflow/interstate-transfers/<int:bid>/cancel')
    @require('admin')
    def bf_interstate_cancel(bid):
        conn=db()
        conn.execute('BEGIN IMMEDIATE')
        row=booking(bid)
        if row['cancelled_at']: return redirect(details_url(bid,False))
        if request.form.get('version',type=int)!=row['version'] or has_delivered(row,current_manifest(bid)):
            abort(409,'This load has changed or been delivered. Review the latest record.')
        try: reason=office_change_reason()
        except ValueError as error:
            conn.rollback()
            return detail(bid,False,change_error=str(error)),400
        conn.execute('UPDATE bf_interstate_bookings SET cancelled_at=?,locked_at=NULL,locked_by=NULL,version=version+1 WHERE id=?',(now(),bid))
        record_change(row,'Cancelled',reason)
        dispatch.update_previous(conn,booking(bid),'Cancelled',now())
        conn.commit()
        dispatch.deliver_mail(conn,bid,app.config,now())
        return redirect(details_url(bid,False))

    def po_metadata(bid):
        return db().execute("""SELECT number,filename,mime_type,revision FROM bf_interstate_purchase_orders
                               WHERE booking_id=?""",(bid,)).fetchone()

    def can_update_booking(external):
        return bool(g.client_rights & {booking_right('add'),booking_right('edit')}) if external else g.user['role']=='admin'

    def pending_change(bid):
        return db().execute('SELECT * FROM bf_interstate_date_changes WHERE booking_id=? AND decision IS NULL AND superseded_at IS NULL',
                            (bid,)).fetchone()

    def current_manifest(bid):
        return db().execute("""SELECT t.id,t.code,t.status,m.expected_bales FROM bf_interstate_manifests m
            JOIN bf_bale_transfers t ON t.id=m.transfer_id WHERE m.booking_id=? ORDER BY t.id DESC LIMIT 1""",(bid,)).fetchone()

    def detail(bid,external,po_error=None,date_error=None,change_error=None):
        row = booking(bid,external)
        manifest = current_manifest(bid)
        change = pending_change(bid)
        manifest_status = manifest['status'] if manifest else None
        status = booking_status(row, manifest_status, change)
        history = db().execute('SELECT action,reason,changed_at,changed_by FROM bf_load_changes WHERE booking_id=? ORDER BY id DESC',(bid,)).fetchall()
        office = not external and g.user['role']=='admin'
        invoice = transfer_invoices(db(), [bid]).get(bid) if office else None
        billed = invoice and invoice['status'] in ('sending','created','uncertain')
        handoff = dispatch.last(db(),bid) if office else None
        route_options = dispatch.options(db(),row,PLACES[row['origin']][1],PLACES[row['destination']][1]) if office and row['origin'] and not row['cancelled_at'] and not has_delivered(row,manifest) else None
        if route_options and handoff and handoff['event']=='On hold' and handoff['target'] in route_options['choices']:
            route_options['preferred'] = handoff['target']
        return page('interstate_detail',external,transfer_pod=transfer_pods.record(db(),bid),handoff=handoff,route_options=route_options,history=history,invoice=invoice,billed=billed,change_error=change_error,
                    editable=not row['cancelled_at'] and not has_delivered(row,manifest),b=row,manifest=manifest,status=status,po=po_metadata(bid),
                    po_error=po_error,can_update_booking=can_update_booking(external), change=change,
                    movable=bool(row['delivery_date']) and can_move_dates(row, manifest_status), date_error=date_error,
                    price=freight.quote(row['rate_cents'],row['fuel_bps']), minimum=freight.MIN_RIGID_HOURS)

    @sbtx_office.get('/transfers/<int:bid>', endpoint='detail')
    @app.get('/binflow/interstate-transfers/<int:bid>')
    @access
    def bf_interstate_detail(bid):
        return detail(bid,False)

    @sbtx_client.get('/transfers/<int:bid>', endpoint='detail')
    @app.get('/binflow/client/interstate-transfers/<int:bid>')
    @client_access
    def bf_client_interstate_detail(bid):
        return detail(bid,True)

    @sbtx_client.post('/transfers/<int:bid>/reschedule', endpoint='reschedule')
    @app.post('/binflow/client/interstate-transfers/<int:bid>/reschedule')
    @client_access
    def bf_client_interstate_reschedule(bid):
        if not can_update_booking(True):
            abort(403)
        conn = db()
        conn.execute('BEGIN IMMEDIATE')
        row = booking(bid, True)
        manifest = current_manifest(bid)
        if row['cancelled_at']:
            abort(409, 'This load was cancelled.')
        if not row['delivery_date']:
            abort(409,'Complete the transfer details before requesting a date change.')
        if not can_move_dates(row, manifest['status'] if manifest else None):
            abort(409, 'This transfer has already been dispatched.')
        if request.form.get('version', type=int) != row['version']:
            abort(409, 'This booking changed. Reopen it before requesting new dates.')
        try:
            pickup = dated(request.form.get('pickup_date', ''))
            delivery_value = request.form.get('delivery_date', '')
            duration = dated(row['delivery_date']) - dated(row['pickup_date'])
            delivery = dated(delivery_value) if delivery_value else dated((pickup + duration).isoformat())
            if delivery < pickup:
                raise ValueError('Delivery must be on or after pickup.')
            dates = (pickup.isoformat(), delivery.isoformat())
            if dates == (row['pickup_date'], row['delivery_date']):
                raise ValueError('Choose a different pickup or delivery date.')
        except (ValueError, OverflowError) as error:
            conn.rollback()
            return detail(bid, True, date_error=str(error)),400

        change = pending_change(bid)
        who = g.bf_client['username']
        if change:
            conn.execute("""UPDATE bf_interstate_date_changes SET proposed_pickup=?,proposed_delivery=?,
                requested_at=?,requested_by=? WHERE id=?""", (*dates, now(), who, change['id']))
        elif row['locked_at']:
            conn.execute("""INSERT INTO bf_interstate_date_changes
                (booking_id,original_pickup,original_delivery,proposed_pickup,proposed_delivery,requested_at,requested_by)
                VALUES(?,?,?,?,?,?,?)""", (bid,row['pickup_date'],row['delivery_date'],*dates,now(),who))
        conn.execute("""UPDATE bf_interstate_bookings SET pickup_date=?,delivery_date=?,locked_at=NULL,
            locked_by=NULL,email_status='pending',email_attempt_at=NULL,version=version+1 WHERE id=?""",
            (*dates,bid))
        dispatch.update_previous(conn,booking(bid),'On hold',now())
        conn.commit()
        dispatch.deliver_mail(conn,bid,app.config,now())
        return redirect(details_url(bid, True))

    def save_po(bid, external):
        row = booking(bid,external)
        if not can_update_booking(external):
            abort(403)
        if not row['locked_at']:
            abort(409,'A purchase order can be supplied once the booking dates are confirmed.')
        number = request.form.get('po_number','').strip()
        upload = request.files.get('po_file')
        try:
            if len(number)>100 or (number and not number.isprintable()):
                raise ValueError('Enter a PO number of up to 100 characters on one line.')
            if not number and not upload:
                raise ValueError('Enter a PO number or choose a PO file.')
            filename,mime,document = read_document(upload,app.config['DOCUMENT_MAX_BYTES'],verify_contents=True) if upload else (None,None,None)
        except ValueError as error:
            return detail(bid,external,str(error)),400

        conn=db()
        conn.execute('BEGIN IMMEDIATE')
        if not booking(bid,external)['locked_at']:
            abort(409,'The dates have changed. Review the booking before updating its purchase order.')
        previous=po_metadata(bid)
        revision=previous['revision'] if previous else 0
        if request.form.get('po_revision',type=int)!=revision:
            conn.rollback()
            return detail(bid,external,'This PO changed. Review the saved details before submitting again.'),409
        who = 'Client login '+g.bf_client['username'] if external else actor()
        conn.execute("""INSERT INTO bf_interstate_purchase_orders
            (booking_id,number,filename,mime_type,document,revision,updated_at,updated_by)
            VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(booking_id) DO UPDATE SET
            number=COALESCE(NULLIF(excluded.number,''),number),
            filename=COALESCE(excluded.filename,filename),mime_type=COALESCE(excluded.mime_type,mime_type),
            document=COALESCE(excluded.document,document),revision=excluded.revision,
            updated_at=excluded.updated_at,updated_by=excluded.updated_by""",
            (bid,number,filename,mime,document,revision+1,now(),who))
        conn.commit()
        return redirect(details_url(bid,external)+'#purchase-order')

    def download_po(bid, external):
        booking(bid,external)
        po=db().execute("""SELECT filename,mime_type,document FROM bf_interstate_purchase_orders
                           WHERE booking_id=? AND filename IS NOT NULL""",(bid,)).fetchone()
        if not po:
            abort(404)
        inline = request.args.get('preview') == '1' and po['mime_type'] in ('application/pdf','image/jpeg','image/png')
        return send_file(io.BytesIO(po['document']),mimetype=po['mime_type'],
                         download_name=po['filename'],as_attachment=not inline)

    @sbtx_client.post('/transfers/<int:bid>/purchase-order', endpoint='po')
    @app.post('/binflow/client/interstate-transfers/<int:bid>/purchase-order')
    @client_access
    def bf_client_interstate_po(bid):
        return save_po(bid,True)

    @sbtx_office.post('/transfers/<int:bid>/purchase-order', endpoint='po')
    @app.post('/binflow/interstate-transfers/<int:bid>/purchase-order')
    @require('admin')
    def bf_interstate_po(bid):
        return save_po(bid,False)

    @sbtx_client.get('/transfers/<int:bid>/purchase-order/file', endpoint='po_file')
    @app.get('/binflow/client/interstate-transfers/<int:bid>/purchase-order/file')
    @client_access
    def bf_client_interstate_po_file(bid):
        return download_po(bid,True)

    @sbtx_office.get('/transfers/<int:bid>/purchase-order/file', endpoint='po_file')
    @app.get('/binflow/interstate-transfers/<int:bid>/purchase-order/file')
    @access
    def bf_interstate_po_file(bid):
        return download_po(bid,False)

    def dispatch_recipient(row):
        opts = dispatch.options(db(),row,PLACES[row['origin']][1],PLACES[row['destination']][1])
        try:
            selection = request.form.get('dispatch_to')
            prior = dispatch.last(db(),row['id'])
            if selection is None and prior and prior['event']=='On hold' and prior['target'] in opts['choices']:
                selection = prior['target']
            dest = dispatch.recipient(opts,selection)
            if not dest and opts['choices']:
                raise ValueError('Choose the driver or transport supplier before approving this load.')
            if request.form.get('remember_driver') and dest:
                if dest[0].startswith('supplier:'):
                    db().execute("""INSERT INTO bf_supplier_dispatch_defaults VALUES(?,?,?,?,?)
                        ON CONFLICT(origin,destination,vehicle,job_type) DO UPDATE SET supplier_id=excluded.supplier_id""",
                        (*opts['lane'],int(dest[0].split(':')[1])))
                elif opts['rigid']:
                    db().execute("""INSERT INTO bf_rigid_driver_defaults VALUES(?,?) ON CONFLICT(state)
                        DO UPDATE SET user_id=excluded.user_id""",(opts['state'],int(dest[0].split(':')[1])))
                    db().execute("""DELETE FROM bf_supplier_dispatch_defaults
                        WHERE origin=? AND destination=? AND vehicle=? AND job_type=?""",opts['lane'])
                else:
                    raise ValueError('Driver defaults are available for the QLD, NSW and VIC rigid fleet.')
            return dest
        except ValueError as error:
            abort(400,str(error))

    @sbtx_office.post('/transfers/<int:bid>/dispatch-recipient', endpoint='dispatch_recipient')
    @app.post('/binflow/interstate-transfers/<int:bid>/dispatch-recipient')
    @require('admin')
    def bf_interstate_dispatch_recipient(bid):
        conn=db()
        conn.execute('BEGIN IMMEDIATE')
        row=booking(bid)
        if not row['origin']:
            abort(409, 'Complete the imported transfer details before dispatch or confirmation.')
        handoff=dispatch.last(conn,bid)
        if request.form.get('version',type=int)!=row['version']:
            abort(409,'The booking changed. Reopen it before dispatching.')
        if request.form.get('retry'):
            legacy_link = bool(handoff and handoff['target'].startswith('supplier:') and handoff['status']=='sent'
                               and not handoff['response_token'] and handoff['event'] in dispatch.ACTIVE)
            if not handoff or handoff['decision'] or request.form.get('retry',type=int)!=handoff['id'] or not legacy_link and handoff['status'] not in ('queued','failed','uncertain','sending'):
                abort(409,'Check the latest dispatch status.')
            if handoff['status']=='sending' and (datetime.fromisoformat(now())-datetime.fromisoformat(handoff['attempted_at']))<timedelta(minutes=10):
                abort(409,'A delivery attempt is still in progress.')
            conn.execute("UPDATE bf_load_dispatches SET status='queued' WHERE id=?",(handoff['id'],))
        else:
            if not row['locked_at'] or row['cancelled_at'] or has_delivered(row,current_manifest(bid)) or handoff and handoff['event'] in dispatch.ACTIVE and handoff['decision']!='denied':
                abort(409,'Review the current transfer assignment.')
            dest=dispatch_recipient(row)
            if not dest:
                abort(400,'Choose a driver or supplier.')
            event='Assigned'
            attempt=1
            if handoff and handoff['decision']=='denied':
                if request.form.get('declined_id',type=int)!=handoff['id']:
                    abort(409,'Reopen the declined load before reassigning.')
                event='Reassigned'
                attempt=handoff['attempt']+1
            dispatch.queue(conn,row,event,dest,g.user['id'],now(),attempt)
        conn.commit()
        dispatch.deliver_mail(conn,bid,app.config,now())
        return redirect(details_url(bid,False))

    @sbtx_office.post('/transfers/<int:bid>/lock', endpoint='lock')
    @app.post('/binflow/interstate-transfers/<int:bid>/lock')
    @require('admin')
    def bf_interstate_lock(bid):
        db().execute('BEGIN IMMEDIATE')
        row = booking(bid)
        if not row['origin']:
            abort(409, 'Complete the imported transfer details before dispatch or confirmation.')
        if row['cancelled_at']: abort(409,'This load was cancelled.')
        if not row['locked_at']:
            if request.form.get('version',type=int)!=row['version']:
                abort(409,'The request has changed. Review the updated dates before confirming.')
            change = pending_change(bid)
            decision = request.form.get('decision', 'approved')
            if decision not in ('approved', 'original') or (decision == 'original' and not change):
                abort(400)
            if change:
                if decision == 'original':
                    db().execute('UPDATE bf_interstate_bookings SET pickup_date=?,delivery_date=? WHERE id=?',
                                 (change['original_pickup'],change['original_delivery'],bid))
                db().execute('UPDATE bf_interstate_date_changes SET decision=?,reviewed_at=?,reviewed_by=? WHERE id=?',
                             (decision,now(),actor(),change['id']))
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
            approved = booking(bid)
            dest = dispatch_recipient(approved)
            if dest:
                dispatch.queue(db(),approved,'Assigned',dest,g.user['id'],now())
            else:
                flash('Dates approved. Choose a driver or transport supplier to send this load.', 'warning')
            db().commit()
            dispatch.deliver_mail(db(),bid,app.config,now())
            confirmation(db(),app.config,approved,now)
        else:
            db().rollback()
        return redirect(details_url(bid,False))

    @sbtx_office.post('/transfers/<int:bid>/email', endpoint='email')
    @app.post('/binflow/interstate-transfers/<int:bid>/email')
    @require('admin')
    def bf_interstate_email(bid):
        row = booking(bid)
        if not row['origin']:
            abort(409, 'Complete the imported transfer details before dispatch or confirmation.')
        if not row['locked_at']:
            abort(409)
        confirmation(db(),app.config,row,now)
        return redirect(details_url(bid,False))

    @sbtx_office.post('/transfers/<int:bid>/bulk', endpoint='bulk')
    @app.post('/binflow/interstate-transfers/<int:bid>/bulk')
    @access
    def bf_interstate_bulk(bid):
        db().execute('BEGIN IMMEDIATE')
        row = booking(bid)
        if not row['origin']:
            abort(409, 'Complete the imported transfer details before dispatch or confirmation.')
        if not row['locked_at'] or row['cancelled_at'] or row['handling']!='bulk':
            abort(409)
        action = request.form.get('action')
        if action=='dispatch' and not row['bulk_delivered_at']:
            db().execute('UPDATE bf_interstate_bookings SET bulk_dispatched_at=? WHERE id=? AND bulk_dispatched_at IS NULL',(now(),bid))
        elif action=='deliver':
            db().execute('UPDATE bf_interstate_bookings SET bulk_delivered_at=? WHERE id=? AND bulk_delivered_at IS NULL',(now(),bid))
        else:
            abort(409)
        db().commit()
        return redirect(details_url(bid,False))

    @app.route('/sbtx/customers',endpoint='sbtx_accounts',methods=['GET','POST'])
    @app.route('/binflow/interstate-transfers/access',methods=['GET','POST'])
    @require('admin')
    def bf_interstate_accounts():
        sbtx_setup = request.endpoint == 'sbtx_accounts'
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
                    elif sbtx_setup:
                        conn.execute('INSERT INTO bf_client_permissions(account_id,rights) VALUES(?,?)',
                                     (aid, json.dumps(sorted(portal.SBTX_RIGHTS))))
                conn.commit()
                flash(f'Booking login “{username}” saved.','success')
                return redirect(url_for(request.endpoint,_anchor=f'login-{aid}'),code=303)
            except (ValueError,sqlite3.IntegrityError) as exc:
                db().rollback()
                error = str(exc) if isinstance(exc,ValueError) else 'That username is already in use.'
                attempted = dict(id=request.form.get('account_id',type=int),name=name,username=username,active=request.form.get('active')=='yes')
        accounts = db().execute("""SELECT a.id,a.name,a.username,a.active,a.created_at,
            i.account_id IS NOT NULL AS transfer_only FROM bf_client_accounts a
            LEFT JOIN bf_interstate_accounts i ON i.account_id=a.id
            ORDER BY a.created_at DESC,a.id DESC""").fetchall()
        policies={a['id']:portal.rights_for(db(),a['id']) for a in accounts}
        create_rights=portal.SBTX_RIGHTS if sbtx_setup else portal.defaults(True)
        if attempted and not attempted['id'] and request.form.get('custom_permissions')=='yes':
            create_rights=set(request.form.getlist('permission'))
            if request.form.get('pod_scope')=='national':
                create_rights.add('pods.all')
        values = dict(policies=policies, modules=portal.MODULES, create_rights=create_rights, revision=portal.revision,
                      accounts=accounts, error=error, attempted=attempted)
        status = 400 if error else 200
        if sbtx_setup:
            values['accounts'] = [a for a in accounts if 'sbtx.view' in policies[a['id']]]
            return render_template('binflow/interstate_access.html', tab='sbtx', sbtx_setup=True, **values), status
        return render('interstate_access',section='interstate',**values), status

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
        return redirect(url_for('sbtx_accounts' if request.args.get('area') == 'sbtx' else 'bf_interstate_accounts',_anchor=f'login-{aid}'),code=303)

    transfer_pods.register(app,db,access,client_access,page,booking,sbtx_office,sbtx_client,now,actor)
    app.register_blueprint(sbtx_office)
    app.register_blueprint(sbtx_client)
