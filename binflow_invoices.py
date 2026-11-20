"""Client invoice categories and billed-to access through the existing Xero connection."""
import io
import json
import re
import sqlite3
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from urllib.parse import urlencode
from urllib.request import Request, build_opener
from uuid import uuid4

from werkzeug.utils import secure_filename
from flask import abort, flash, g, redirect, render_template, request, send_file, url_for

import binflow_auto_invoices as auto
import binflow_invoice_reports as invoice_reports
import binflow_invoice_pods as invoice_pods
import binflow_period_reports as reports
import binflow_xero as xero
import binflow_invoice_profiles as profiles
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

    auto.migrate(conn)
    invoice_pods.migrate(conn)


def categories(conn):
    return conn.execute('SELECT * FROM bf_invoice_categories ORDER BY id').fetchall()


def contacts(conn, tenant):
    return conn.execute("""SELECT a.id,a.name,a.username,c.contact_id,c.contact_name FROM bf_client_accounts a
        LEFT JOIN bf_invoice_contacts c ON c.account_id=a.id AND c.tenant_id=? WHERE a.active=1 ORDER BY a.name,a.username""",
                        (tenant,)).fetchall()


def booked_transfer(conn, bid):
    row = conn.execute("""SELECT b.*,p.number AS po FROM bf_interstate_bookings b
        LEFT JOIN bf_interstate_purchase_orders p ON p.booking_id=b.id
        WHERE b.id=? AND b.locked_at IS NOT NULL AND b.cancelled_at IS NULL AND b.account_id IS NOT NULL""",(bid,)).fetchone()
    if not row:
        abort(404)
    return row


def transport_lines(b):
    from binflow_interstate import PLACES
    price = quote(b['rate_cents'], b['fuel_bps'])
    if not price or not price['total']:
        return []
    ref = f"IT-{b['id']:06d}"
    stops = [PLACES[code][0] for code in (b['origin'],b['destination']) if code in PLACES]
    description = ' · '.join(filter(None,(ref,b['vehicle'],' to '.join(stops),b['pickup_date'])))
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
    if not conn.execute('SELECT 1 FROM bf_client_accounts WHERE id=? AND active=1',(aid,)).fetchone():
        raise ValueError('Choose an active billing client.')
    link = profiles.linked_contact(conn,cfg['tenant_id'],aid)
    chosen_contact = form.get('new_contact')
    if not link:
        profiles.save_contact(conn,cfg['tenant_id'],aid,json.loads(cfg['choices_json']),chosen_contact)
        link = profiles.linked_contact(conn,cfg['tenant_id'],aid)
    elif chosen_contact and chosen_contact != link['contact_id']:
        raise ValueError('The saved billing contact changed. Reopen the draft before continuing.')
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


def client_records(conn, tenant, aid, contact, selected=None):
    sql = """SELECT id,category_id,invoice_id FROM bf_rental_invoices
        WHERE tenant_id=? AND (portal_account_id IS NULL OR portal_account_id=?)
          AND json_extract(snapshot_json,'$.contact.id')=? AND status='created' AND mode='actual' AND invoice_id!=''"""
    args = [tenant,aid,contact]
    if selected is not None:
        sql += ' AND id IN (SELECT value FROM json_each(?))'
        args.append(json.dumps(sorted(selected)))
    return conn.execute(sql+' ORDER BY created_at DESC,rowid DESC',args).fetchall()


def invoice_period(params,today):
    grouping=params.get('period','all')
    if grouping not in ('all',*reports.PERIODS):
        abort(400,'Choose weekly, fortnightly, monthly or all dates.')
    chosen=params.get('date') or today
    period=reports.period_view('' if grouping=='all' else chosen,'day' if grouping=='all' else grouping)
    period['key']=grouping
    return period,chosen


def xero_invoice_date(row,field):
    value=row.get(field+'String') or row.get(field)
    if not value:
        return ''
    epoch=re.fullmatch(r'/Date\((-?\d+)(?:[+-]\d{4})?\)/',value)
    if epoch:
        return datetime.fromtimestamp(int(epoch[1])/1000,timezone.utc).date().isoformat()
    return date.fromisoformat(value[:10]).isoformat()


def client_invoice_rows(app,conn,link,period,sort,selected=None):
    records=client_records(conn,link['tenant_id'],link['account_id'],link['contact_id'],selected)
    if selected is not None and {r['id'] for r in records}!=selected:
        abort(404)
    by_id={r['invoice_id']:r for r in records}
    ids=list(by_id)
    invoices={}
    for offset in range(0,len(ids),40):
        batch=ids[offset:offset+40]
        params=urlencode(dict(IDs=','.join(batch),ContactIDs=link['contact_id'],Statuses='AUTHORISED,PAID',page=1))
        result=xero.api(app,conn,'Invoices?'+params,tenant_id=link['tenant_id'])
        for row in xero.records(result,'Invoices'):
            iid=row.get('InvoiceID')
            if iid not in batch or not issued(row,link['contact_id']):
                continue
            record=by_id[iid]
            day=xero_invoice_date(row,'Date')
            if period['first'] and not period['first']<=day<=period['last']:
                continue
            invoices[record['id']]=dict(key=record['id'],iid=iid,category=record['category_id'],
                number=row.get('InvoiceNumber') or 'Invoice',date=day,due=xero_invoice_date(row,'DueDate'),
                status='Paid' if row['Status']=='PAID' else 'Awaiting payment',
                total=float(row['Total']),outstanding=float(row['AmountDue']),currency=row.get('CurrencyCode') or 'AUD')
    if selected is not None and set(invoices)!=selected:
        abort(409,'A selected invoice is no longer available for this period. Refresh the list and select again.')
    return sorted(invoices.values(),key=lambda row:(row['date'],row['number'],row['key']),reverse=sort=='newest')


def client_document(app,conn,link,row,pdf):
    source=pdf_bytes(app,conn,link['tenant_id'],row['iid'])
    return invoice_pods.bundle(source,(
        invoice_pods.file_content(conn,row['key'],p['id']) if p['kind']=='file' else reports.pdf_data(pdf(p['id']))
        for p in row['pods']))


def client_invoice_download(app,conn,link,rows,groups,period,file_format,pdf):
    filename='BinFlow-client-invoices-'+(period['first'] or 'selected')
    if file_format=='xls':
        return reports.xls('Invoices',['Invoice','Category','Issued','Due','Status','Currency','Total incl. GST','Balance owing','Supporting PODs'],
            ((r['number'],groups[r['category']]['name'],date.fromisoformat(r['date']) if r['date'] else None,
              date.fromisoformat(r['due']) if r['due'] else None,r['status'],r['currency'],r['total'],r['outstanding'],', '.join(p['label'] for p in r['pods']))
             for r in rows),filename)
    def entries():
        for index,r in enumerate(rows,1):
            # The selection number keeps duplicate or sanitised invoice numbers unique in the archive.
            name=secure_filename(r['number']) or 'Invoice'
            yield f'{index:03d}-{name}.pdf',client_document(app,conn,link,r,pdf)
    return reports.zip_download(entries(),filename)


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


def register(app, db, require, render, client_access, now, actor,pdf):
    auto.register(app,db,require,render,now,actor)
    invoice_reports.register(app,db,require)
    invoice_pods.register(app,db,require,render,now,actor)
    @app.get('/binflow/invoices')
    @require('admin')
    def bf_invoices():
        conn = db()
        cfg = xero.config(conn)
        connected = bool(cfg['tenant_id'] and cfg['token_cipher'])
        auto.refresh(conn,now(),True)
        period,chosen=invoice_period(request.args,now()[:10])
        grouping=period['key']
        clause='' if grouping=='all' else ' WHERE '+invoice_reports.INVOICE_DATE+' BETWEEN ? AND ?'
        params=() if grouping=='all' else (period['first'],period['last'])
        drafts = conn.execute(f"""SELECT d.*,i.id AS saved_id,i.invoice_number,({auto.INVOICED_SQL}) AS invoiced,
            COALESCE((SELECT SUM(json_extract(x.value,'$.net')+json_extract(x.value,'$.tax'))
              FROM json_each(d.lines) x),0) AS total FROM bf_auto_drafts d LEFT JOIN bf_rental_invoices i ON i.id=d.id {clause}
            ORDER BY CASE d.state WHEN 'sending' THEN 0 WHEN 'open' THEN 1 ELSE 2 END,d.end DESC,d.client""",params).fetchall()
        page = max(1,request.args.get('page',1,type=int))
        history = conn.execute(f"""SELECT i.id,i.invoice_number,i.mode,i.status,i.xero_status,i.created_at,({auto.INVOICED_SQL}) AS invoiced,
            json_extract(i.snapshot_json,'$.contact.name') AS contact,c.name AS category FROM bf_rental_invoices i
            JOIN bf_invoice_categories c ON c.id=i.category_id LEFT JOIN bf_auto_drafts d ON d.id=i.id {clause} ORDER BY i.created_at DESC,i.rowid DESC LIMIT 31 OFFSET ?""",
                               (*params,(page-1)*30,)).fetchall()
        supporting=invoice_pods.documents(conn,{r['id'] for r in history[:30]}|{d['saved_id'] for d in drafts if d['saved_id']})
        return render('invoices_admin',section='invoices',supporting=supporting,categories=categories(conn),accounts=contacts(conn,cfg['tenant_id']),
                      history=history[:30],has_next=len(history)>30,page=page,drafts=drafts,today=now()[:10],period=period,chosen=chosen)

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
        return redirect(url_for('bf_auto_billing_setup',_anchor='categories'))

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
            try:
                profiles.save_contact(conn,cfg['tenant_id'],aid,json.loads(cfg['choices_json']),cid)
            except ValueError as exc:
                abort(400,str(exc))
        else:
            conn.execute('DELETE FROM bf_invoice_contacts WHERE tenant_id=? AND account_id=?',(cfg['tenant_id'],aid))
        conn.commit()
        flash('Billing contact saved.','success')
        return redirect(url_for('bf_auto_billing_setup',billing_client=aid,_anchor='contacts'))

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
                key = xero.uuid_text(values.get('draft_key'))
                if conn.execute('SELECT 1 FROM bf_rental_invoices WHERE id=?',(key,)).fetchone():
                    return redirect(url_for('bf_rental_invoice',identifier=key))
                tenant = xero.verify_draft_connection(app,conn,values.get('tenant'))
                xero.directory_for_page(app,conn,xero.config(conn))
                conn.execute('BEGIN IMMEDIATE')
                if conn.execute('SELECT 1 FROM bf_rental_invoices WHERE id=?',(key,)).fetchone():
                    conn.rollback()
                    return redirect(url_for('bf_rental_invoice',identifier=key))
                booking = booked_transfer(conn,raw_bid) if raw_bid else None
                aid,cat,snapshot,invoice = draft_values(conn,values,booking)
                cfg = xero.config(conn)
                if tenant != cfg['tenant_id']:
                    raise ValueError('The Xero organisation changed. Review the invoice again.')
                xero.set_invoice_po(app,invoice,booking['po'] if booking else values.get('po_number'),key)
                conn.execute("""INSERT INTO bf_rental_invoices(id,tenant_id,tenant_name,period_start,period_end,
                  mode,status,reference,snapshot_json,payload_json,created_by,created_at,category_id,portal_account_id,booking_id)
                  VALUES(?,?,?,?,?,'actual','preview',?,?,?,?,?,?,?,?)""",(key,cfg['tenant_id'],cfg['tenant_name'],
                    invoice['Date'],invoice['Date'],'BF-INV-'+key,json.dumps(snapshot),json.dumps({'Invoices':[invoice]}),
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
        opts, directory_error = xero.directory_for_page(app,conn,cfg)
        defaults = profiles.defaults(conn,cfg['tenant_id'])
        previous = profiles.previous_manual_choices(conn,cfg['tenant_id'])
        initial_aid = booking['account_id'] if booking else values.get('client',type=int)
        initial = previous.get(initial_aid) or defaults
        return render('invoice_compose',defaults=defaults,initial=initial,previous=previous,tenant=cfg['tenant_id'],section='invoices',categories=categories(conn),accounts=contacts(conn,cfg['tenant_id']),
            options=opts,connected=bool(cfg['tenant_id'] and cfg['token_cipher']),
            booking=booking,booked_lines=transport_lines(booking) if booking else [],error=error or directory_error,
            key=values.get('draft_key') or str(uuid4()),stamp=stamp,due=stamp+timedelta(days=14),
            entries=list(zip(*(request.form.getlist(n) for n in ('description','quantity','unit_rate')))) or [('','1','')])

    def client_link(conn):
        cfg = xero.config(conn)
        return conn.execute('SELECT * FROM bf_invoice_contacts WHERE tenant_id=? AND account_id=?',
                            (cfg['tenant_id'],g.bf_client['id'])).fetchone()

    @app.route('/binflow/client/invoices',methods=['GET','POST'])
    @client_access
    def bf_client_invoices():
        conn=db()
        link=client_link(conn)
        groups={c['id']:dict(name=c['name'],rows=[]) for c in categories(conn)}
        period,chosen=invoice_period(request.args,now()[:10])
        sort=request.args.get('sort','newest')
        if sort not in ('newest','oldest'):
            abort(400,'Choose an invoice date sort order.')
        selected=None
        if request.method=='POST':
            if not link:
                abort(404)
            file_format=request.form.get('format')
            if file_format not in ('xls','zip'):
                abort(400,'Choose Excel or ZIP.')
            selected=reports.selected_ids()
        error=None
        if not link:
            error=('Your billing contact is awaiting setup. Contact SB Empire for invoice access.'
                   if xero.config(conn)['tenant_id'] else 'Invoice access is awaiting setup with SB Empire.')
        else:
            try:
                rows=client_invoice_rows(app,conn,link,period,sort,selected)
                supporting=invoice_pods.documents(conn,[r['key'] for r in rows],g.bf_client)
                for row in rows:
                    row['pods']=supporting.get(row['key'],[])
                if selected is not None:
                    return client_invoice_download(app,conn,link,rows,groups,period,file_format,pdf)
                for row in rows:
                    groups[row['category']]['rows'].append(row)
            except (xero.XeroError,KeyError,TypeError,ValueError):
                if selected is not None:
                    abort(503,'The selected invoices could not be downloaded. Please try again.')
                error='Invoice details are temporarily unavailable. Refresh this page to try again.'
        return render_template('binflow/client_invoices.html',account=g.bf_client,groups=groups.values(),
            error=error,period=period,chosen=chosen,sort=sort,count=sum(len(c['rows']) for c in groups.values()))

    def issued_invoice(identifier):
        conn = db()
        link = client_link(conn)
        if not link:
            abort(404)
        records=client_records(conn,link['tenant_id'],link['account_id'],link['contact_id'],{identifier})
        if not records:
            abort(404)
        iid=xero.uuid_text(records[0]['invoice_id'])
        try:
            rows = xero.records(xero.api(app,conn,'Invoices/'+iid,tenant_id=link['tenant_id']),'Invoices')
            if len(rows)!=1 or rows[0].get('InvoiceID')!=iid or not issued(rows[0],link['contact_id']):
                abort(404)
        except xero.XeroError:
            abort(503,'The invoice PDF is temporarily unavailable. Please try again.')
        return link,dict(key=identifier,iid=iid,number=rows[0].get('InvoiceNumber') or 'Invoice',
                         pods=invoice_pods.documents(conn,[identifier],g.bf_client).get(identifier,[]))

    @app.get('/binflow/client/invoices/<identifier>/pods/<any(job,file):kind>/<int:doc_id>')
    @client_access
    def bf_client_invoice_pod(identifier,kind,doc_id):
        _,row=issued_invoice(identifier)
        doc=next((p for p in row['pods'] if p['id']==doc_id and p['kind']==kind),None)
        if not doc: abort(404)
        if kind=='file':
            return invoice_pods.document_response(invoice_pods.file_content(db(),identifier,doc_id),doc['label'])
        return invoice_pods.document_response(reports.pdf_data(pdf(doc_id)),doc['label']+'.pdf')

    @app.get('/binflow/client/invoices/<identifier>/pdf')
    @client_access
    def bf_client_invoice_pdf(identifier):
        link,row=issued_invoice(identifier)
        try:
            data=client_document(app,db(),link,row,pdf)
        except (ValueError,xero.XeroError):
            abort(503,'The invoice PDF is temporarily unavailable. Please try again.')
        response = send_file(io.BytesIO(data),mimetype='application/pdf',as_attachment=True,
                             download_name=(secure_filename(str(row['number'])) or 'Invoice')+'.pdf')
        response.cache_control.no_store=True
        return response
