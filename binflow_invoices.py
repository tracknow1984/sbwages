"""Client invoice categories and billed-to access through the existing Xero connection."""
import io
import json
import sqlite3
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from urllib.parse import urlencode
from urllib.request import Request, build_opener
from uuid import uuid4

from werkzeug.utils import secure_filename
from flask import abort, flash, g, redirect, render_template, request, send_file, url_for

import binflow_xero as xero
from binflow_freight_rates import quote
from binflow_sizes import amount


def migrate(conn):
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS bf_invoice_categories (
            id INTEGER PRIMARY KEY, name TEXT NOT NULL COLLATE NOCASE UNIQUE);
        INSERT OR IGNORE INTO bf_invoice_categories VALUES (1,'Bin Rental'),(2,'Transport');
        CREATE TABLE IF NOT EXISTS bf_invoice_contacts (
            tenant_id TEXT NOT NULL, account_id INTEGER NOT NULL REFERENCES bf_client_accounts(id),
            contact_id TEXT NOT NULL, contact_name TEXT NOT NULL, PRIMARY KEY(tenant_id,account_id));
    """)
    columns = {row[1] for row in conn.execute('PRAGMA table_info(bf_rental_invoices)')}
    if 'category_id' not in columns:
        conn.execute('DROP INDEX IF EXISTS bf_rental_invoice_period')
        conn.execute('ALTER TABLE bf_rental_invoices ADD COLUMN category_id INTEGER NOT NULL DEFAULT 1')
    if 'portal_account_id' not in columns:
        conn.execute('ALTER TABLE bf_rental_invoices ADD COLUMN portal_account_id INTEGER REFERENCES bf_client_accounts(id)')
    if 'booking_id' not in columns:
        conn.execute('ALTER TABLE bf_rental_invoices ADD COLUMN booking_id INTEGER REFERENCES bf_interstate_bookings(id)')
    conn.execute("""CREATE UNIQUE INDEX IF NOT EXISTS bf_rental_invoice_period ON bf_rental_invoices(tenant_id,period_start,period_end)
        WHERE category_id=1 AND mode='actual' AND status!='cancelled'""")
    conn.execute("""CREATE UNIQUE INDEX IF NOT EXISTS bf_invoice_transfer ON bf_rental_invoices(booking_id)
        WHERE booking_id IS NOT NULL AND status!='cancelled'""")


def categories(conn):
    return conn.execute('SELECT * FROM bf_invoice_categories ORDER BY id').fetchall()


def contacts(conn, tenant):
    return conn.execute("""SELECT a.id,a.name,a.username,c.contact_id,c.contact_name FROM bf_client_accounts a
        LEFT JOIN bf_invoice_contacts c ON c.account_id=a.id AND c.tenant_id=? WHERE a.active=1 ORDER BY a.name,a.username""",
                        (tenant,)).fetchall()


def booked_transfer(conn, bid):
    row = conn.execute("""SELECT b.*,p.number AS po FROM bf_interstate_bookings b
        LEFT JOIN bf_interstate_purchase_orders p ON p.booking_id=b.id
        WHERE b.id=? AND b.locked_at IS NOT NULL AND b.account_id IS NOT NULL""",(bid,)).fetchone()
    if not row:
        abort(404)
    return row


def transport_lines(b):
    from binflow_interstate import PLACES
    price = quote(b['rate_cents'], b['fuel_bps'])
    if not price or not price['total']:
        return []
    ref = f"IT-{b['id']:06d}"
    description = f"{ref} · {b['vehicle']} · {PLACES[b['origin']][0]} to {PLACES[b['destination']][0]} · {b['pickup_date']}"
    if b['po']:
        description += ' · PO ' + b['po']
    lines = [(description, '1', price['base'])]
    if price['fuel']:
        lines.append((f"{ref} · Fuel surcharge {b['fuel_bps']/100:g}%", '1', price['fuel']))
    return lines


def manual_lines(form):
    descriptions, quantities, rates = [form.getlist(k) for k in ('description','quantity','unit_rate')]
    if not (1 <= len(descriptions) == len(quantities) == len(rates) <= 50):
        raise ValueError('Enter between one and 50 invoice lines.')
    lines = []
    for desc, qty, rate in zip(descriptions, quantities, rates):
        if not any((desc.strip(),qty.strip(),rate.strip())):
            continue
        units = amount(qty,'Quantity',1000000,100)
        cents = amount(rate,'Unit rate',10000000,100)
        if not desc.strip() or len(desc.strip())>1000 or units<=0 or cents<=0:
            raise ValueError('Each line needs a description, a positive quantity and an ex GST rate.')
        lines.append((desc.strip(), format(Decimal(units)/100,'f'), cents))
    if not lines:
        raise ValueError('Enter at least one invoice line.')
    return lines


def draft_values(conn, form, booking):
    cfg = xero.config(conn)
    if not cfg['token_cipher'] or not cfg['tenant_id']:
        raise ValueError('Connect Xero before preparing a draft.')
    aid = booking['account_id'] if booking else form.get('client',type=int)
    link = conn.execute("""SELECT c.* FROM bf_invoice_contacts c JOIN bf_client_accounts a ON a.id=c.account_id
        WHERE a.active=1 AND c.tenant_id=? AND c.account_id=?""",(cfg['tenant_id'],aid)).fetchone()
    if not link:
        raise ValueError('Link this client login to its Xero billing contact under Invoices first.')
    cat = conn.execute('SELECT * FROM bf_invoice_categories WHERE id=? AND id!=1',
                       (2 if booking else form.get('category',type=int),)).fetchone()
    if not cat:
        raise ValueError('Choose Transport or another invoice category.')
    contact, account, tax = xero.billing_choices(conn, form, dict(id=link['contact_id'],name=link['contact_name']))
    try:
        start, due = (date.fromisoformat(form.get(k,'')) for k in ('invoice_date','due_date'))
    except ValueError:
        raise ValueError('Enter a valid invoice date and due date.') from None
    if due < start:
        raise ValueError('The due date must be on or after the invoice date.')
    try:
        pct = Decimal(str(tax['rate']))
        if not pct.is_finite() or not 0 <= pct <= 100:
            raise ValueError()
    except (InvalidOperation, ValueError):
        raise ValueError('Refresh the Xero tax rates before continuing.') from None
    entries = transport_lines(booking) if booking else []
    if not entries:
        entries = manual_lines(form)
        if booking:
            ref = f"IT-{booking['id']:06d}" + (' · PO '+booking['po'] if booking['po'] else '')
            entries = [(ref+' · '+description,qty,rate) for description,qty,rate in entries]
    lines, items = [], []
    for description, quantity, rate in entries:
        qty = Decimal(quantity)
        net = int((qty*rate).quantize(Decimal(1),rounding=ROUND_HALF_UP))
        gst = int((Decimal(net)*pct/100).quantize(Decimal(1),rounding=ROUND_HALF_UP))
        if net <= 0 or net+gst>999999999999:
            raise ValueError('Check the quantity and rate on this line.')
        lines.append(dict(description=description,quantity=quantity,rate=rate,net=net,tax=gst))
        items.append(dict(Description=description,Quantity=float(qty),UnitAmount=rate/100,
                          AccountCode=account['code'],TaxType=tax['code']))
    snapshot = dict(category=cat['name'],contact=contact,account=account,tax=tax,lines=lines,
                    subtotal=sum(x['net'] for x in lines),gst=sum(x['tax'] for x in lines))
    return aid,cat['id'],snapshot,dict(Type='ACCREC',Status='DRAFT',CurrencyCode='AUD',LineAmountTypes='Exclusive',
        Contact={'ContactID':contact['id']},Date=start.isoformat(),DueDate=due.isoformat(),LineItems=items)


def client_records(conn, tenant, aid, contact):
    return conn.execute("""SELECT id,category_id,invoice_id FROM bf_rental_invoices
        WHERE tenant_id=? AND (portal_account_id IS NULL OR portal_account_id=?)
          AND json_extract(snapshot_json,'$.contact.id')=? AND status='created' AND mode='actual' AND invoice_id!=''
        ORDER BY created_at DESC,rowid DESC""",(tenant,aid,contact)).fetchall()


def issued(row, contact):
    return (row.get('Type')=='ACCREC' and row.get('Status') in ('AUTHORISED','PAID')
            and (row.get('Contact') or {}).get('ContactID')==contact)


def pdf_bytes(app, conn, tenant, iid):
    token = xero.access_token(app,conn)
    if xero.config(conn)['tenant_id']!=tenant:
        raise xero.XeroError('The billing organisation changed. Refresh the page.')
    req = Request(xero.API_ROOT+'Invoices/'+xero.uuid_text(iid),headers={'Authorization':'Bearer '+token,
                  'Xero-tenant-id':xero.uuid_text(tenant),'Accept':'application/pdf'})
    try:
        with build_opener(xero.NoRedirect).open(req,timeout=12) as result:
            data = result.read(16*1024*1024+1)
    except (OSError,TimeoutError):
        raise xero.XeroError('The invoice PDF is temporarily unavailable.') from None
    if len(data)>16*1024*1024 or not data.startswith(b'%PDF-'):
        raise xero.XeroError('The invoice PDF is temporarily unavailable.')
    return data


def register(app, db, require, render, client_access, now, actor):
    @app.get('/binflow/invoices')
    @require('admin')
    def bf_invoices():
        conn = db()
        cfg = xero.config(conn)
        opts = json.loads(cfg['choices_json'])
        error = None
        connected = bool(cfg['tenant_id'] and cfg['token_cipher'])
        if connected and (not opts or request.args.get('refresh')=='yes'):
            try:
                opts = xero.choices(app,conn,request.args.get('search','').strip()[:100])
            except xero.XeroError as exc:
                error = str(exc)
        page = max(1,request.args.get('page',1,type=int))
        history = conn.execute("""SELECT i.id,i.invoice_number,i.mode,i.status,i.xero_status,i.created_at,
            json_extract(i.snapshot_json,'$.contact.name') AS contact,c.name AS category FROM bf_rental_invoices i
            JOIN bf_invoice_categories c ON c.id=i.category_id ORDER BY i.created_at DESC,i.rowid DESC LIMIT 31 OFFSET ?""",
                               ((page-1)*30,)).fetchall()
        return render('invoices_admin',section='invoices',categories=categories(conn),accounts=contacts(conn,cfg['tenant_id']),
                      history=history[:30],has_next=len(history)>30,page=page,options=opts,connected=connected,error=error,tenant=cfg['tenant_id'])

    @app.post('/binflow/invoices/categories')
    @require('admin')
    def bf_invoice_category():
        name = ' '.join(request.form.get('name','').split())
        if not 1<=len(name)<=64 or not name.isprintable():
            flash('Enter a category name of up to 64 characters.','error')
        else:
            try:
                db().execute('INSERT INTO bf_invoice_categories(name) VALUES(?)',(name,))
                db().commit()
                flash('Invoice category added.','success')
            except sqlite3.IntegrityError:
                db().rollback()
                flash('That invoice category already exists.','error')
        return redirect(url_for('bf_invoices',_anchor='categories'))

    @app.post('/binflow/invoices/contact')
    @require('admin')
    def bf_invoice_contact():
        conn = db()
        conn.execute('BEGIN IMMEDIATE')
        cfg = xero.config(conn)
        aid = request.form.get('client',type=int)
        if not cfg['token_cipher'] or not cfg['tenant_id'] or request.form.get('tenant')!=cfg['tenant_id']:
            abort(409,'Reload the current Xero organisation before linking a contact.')
        if not conn.execute('SELECT 1 FROM bf_client_accounts WHERE id=? AND active=1',(aid,)).fetchone():
            abort(400)
        cid = request.form.get('contact','')
        if not cid:
            abort(400,'Choose a billing contact or select Remove existing link.')
        if cid!='unlink':
            options = json.loads(cfg['choices_json']).get('contacts',[])
            contact = next((c for c in options if c['id']==cid),None)
            if not contact:
                abort(400,'Choose a contact from the current Xero search results.')
            conn.execute("""INSERT INTO bf_invoice_contacts VALUES(?,?,?,?) ON CONFLICT(tenant_id,account_id)
                DO UPDATE SET contact_id=excluded.contact_id,contact_name=excluded.contact_name""",
                         (cfg['tenant_id'],aid,contact['id'],contact['name']))
        else:
            conn.execute('DELETE FROM bf_invoice_contacts WHERE tenant_id=? AND account_id=?',(cfg['tenant_id'],aid))
        conn.commit()
        flash('Billing contact saved.','success')
        return redirect(url_for('bf_invoices',_anchor='contacts'))

    @app.route('/binflow/invoices/new',methods=['GET','POST'])
    @require('admin')
    def bf_invoice_compose():
        conn = db()
        values = request.form if request.method=='POST' else request.args
        raw_bid = values.get('booking','')
        booking = booked_transfer(conn,raw_bid) if raw_bid else None
        error = None
        if request.method=='POST':
            try:
                conn.execute('BEGIN IMMEDIATE')
                key = xero.uuid_text(values.get('draft_key'))
                if conn.execute('SELECT 1 FROM bf_rental_invoices WHERE id=?',(key,)).fetchone():
                    conn.rollback()
                    return redirect(url_for('bf_rental_invoice',identifier=key))
                aid,cat,snapshot,invoice = draft_values(conn,values,booking)
                cfg = xero.config(conn)
                invoice['Reference']='BF-INV-'+key
                conn.execute("""INSERT INTO bf_rental_invoices(id,tenant_id,tenant_name,period_start,period_end,
                  mode,status,reference,snapshot_json,payload_json,created_by,created_at,category_id,portal_account_id,booking_id)
                  VALUES(?,?,?,?,?,'actual','preview',?,?,?,?,?,?,?,?)""",(key,cfg['tenant_id'],cfg['tenant_name'],
                    invoice['Date'],invoice['Date'],invoice['Reference'],json.dumps(snapshot),json.dumps({'Invoices':[invoice]}),
                    actor(),now(),cat,aid,booking['id'] if booking else None))
                conn.commit()
                return redirect(url_for('bf_rental_invoice',identifier=key))
            except sqlite3.IntegrityError:
                conn.rollback()
                if not booking:
                    raise
                existing = conn.execute("SELECT id FROM bf_rental_invoices WHERE booking_id=? AND status!='cancelled'",(booking['id'],)).fetchone()
                if not existing:
                    raise
                flash('This transfer already has an invoice.','success')
                return redirect(url_for('bf_rental_invoice',identifier=existing['id']))
            except ValueError as exc:
                conn.rollback()
                error = str(exc)
        cfg = xero.config(conn)
        stamp = date.fromisoformat(now()[:10])
        return render('invoice_compose',section='invoices',categories=categories(conn),accounts=contacts(conn,cfg['tenant_id']),
            options=json.loads(cfg['choices_json']),connected=bool(cfg['tenant_id'] and cfg['token_cipher']),
            booking=booking,booked_lines=transport_lines(booking) if booking else [],error=error,
            key=values.get('draft_key') or str(uuid4()),stamp=stamp,due=stamp+timedelta(days=14),
            entries=list(zip(*(request.form.getlist(n) for n in ('description','quantity','unit_rate')))) or [('','1','')])

    def client_link(conn):
        cfg = xero.config(conn)
        return conn.execute('SELECT * FROM bf_invoice_contacts WHERE tenant_id=? AND account_id=?',
                            (cfg['tenant_id'],g.bf_client['id'])).fetchone()

    @app.get('/binflow/client/invoices')
    @client_access
    def bf_client_invoices():
        conn = db()
        link = client_link(conn)
        groups = {c['id']:dict(name=c['name'],rows=[]) for c in categories(conn)}
        error = None
        if not link:
            error = ('Your billing contact is awaiting setup. Contact SB Empire for invoice access.'
                     if xero.config(conn)['tenant_id'] else 'Invoice access is awaiting setup with SB Empire.')
        if link:
            local = client_records(conn,link['tenant_id'],link['account_id'],link['contact_id'])
            by_id = {r['invoice_id']:r for r in local}
            ids = list(by_id)
            try:
                for offset in range(0,len(ids),40):
                    params = urlencode(dict(IDs=','.join(ids[offset:offset+40]),ContactIDs=link['contact_id'],Statuses='AUTHORISED,PAID',page=1))
                    result = xero.api(app,conn,'Invoices?'+params,tenant_id=link['tenant_id'])
                    for row in xero.records(result,'Invoices'):
                        record = by_id.get(row.get('InvoiceID'))
                        if record and issued(row,link['contact_id']):
                            groups[record['category_id']]['rows'].append(dict(key=record['id'],
                                 number=row.get('InvoiceNumber') or 'Invoice',date=(row.get('DateString') or '')[:10],
                                 due=(row.get('DueDateString') or '')[:10],status='Paid' if row['Status']=='PAID' else 'Awaiting payment',
                                 total=row['Total'],outstanding=row['AmountDue'],currency=row.get('CurrencyCode','AUD')))
            except (xero.XeroError,KeyError):
                error = 'Invoice details are temporarily unavailable. Refresh this page to try again.'
        for group in groups.values():
            group['rows'].sort(key=lambda row:(row['date'],row['number']),reverse=True)
        return render_template('binflow/client_invoices.html',account=g.bf_client,groups=groups.values(),error=error)

    @app.get('/binflow/client/invoices/<identifier>/pdf')
    @client_access
    def bf_client_invoice_pdf(identifier):
        conn = db()
        link = client_link(conn)
        if not link:
            abort(404)
        record = conn.execute("""SELECT invoice_id FROM bf_rental_invoices WHERE id=? AND tenant_id=?
            AND (portal_account_id IS NULL OR portal_account_id=?) AND json_extract(snapshot_json,'$.contact.id')=?
            AND status='created' AND mode='actual' AND invoice_id!=''""",
            (identifier,link['tenant_id'],link['account_id'],link['contact_id'])).fetchone()
        if not record:
            abort(404)
        iid = xero.uuid_text(record['invoice_id'])
        try:
            rows = xero.records(xero.api(app,conn,'Invoices/'+iid,tenant_id=link['tenant_id']),'Invoices')
            if len(rows)!=1 or rows[0].get('InvoiceID')!=iid or not issued(rows[0],link['contact_id']):
                abort(404)
            data = pdf_bytes(app,conn,link['tenant_id'],iid)
        except xero.XeroError:
            abort(503,'The invoice PDF is temporarily unavailable. Please try again.')
        response = send_file(io.BytesIO(data),mimetype='application/pdf',as_attachment=True,
                             download_name=(secure_filename(str(rows[0].get('InvoiceNumber') or 'Invoice')) or 'Invoice')+'.pdf')
        response.cache_control.no_store=True
        return response
