"""Operational invoice drafts and fortnightly proof-of-delivery packs."""
import hashlib
import io
import json
import sqlite3
import threading
import zipfile
from datetime import date, timedelta
from email.message import EmailMessage
from decimal import Decimal
from uuid import uuid4

from flask import abort, flash, redirect, request, send_file, url_for

import binflow_xero as xero
from binflow_pickup_pricing import rate_text
from binflow_pods import send_smtp
from binflow_rental import report
from binflow_sizes import amount

FORTNIGHT_ANCHOR = date(1970, 1, 5)


def fortnight(day):
    start = day - timedelta(days=(day - FORTNIGHT_ANCHOR).days % 14)
    return start.isoformat(), (start + timedelta(days=13)).isoformat()


def migrate(conn):
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS bf_billing_defaults (
            id INTEGER PRIMARY KEY CHECK(id=1), tenant TEXT NOT NULL, account_code TEXT NOT NULL,
            tax_code TEXT NOT NULL, rental_account INTEGER REFERENCES bf_client_accounts(id));
        CREATE TABLE IF NOT EXISTS bf_pod_bill_to (
            client_id INTEGER PRIMARY KEY REFERENCES bf_clients(id), account_id INTEGER NOT NULL REFERENCES bf_client_accounts(id));
        CREATE TABLE IF NOT EXISTS bf_auto_transfer_terms (
            booking_id INTEGER PRIMARY KEY REFERENCES bf_interstate_bookings(id),
            account_id INTEGER NOT NULL REFERENCES bf_client_accounts(id),
            rate_cents INTEGER NOT NULL CHECK(rate_cents>0),fuel_bps INTEGER NOT NULL CHECK(fuel_bps>=0),
            booking_version INTEGER NOT NULL,recorded_at TEXT NOT NULL,recorded_by TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS bf_billing_emails (
            account_id INTEGER PRIMARY KEY REFERENCES bf_client_accounts(id), email TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS bf_auto_drafts (
            id TEXT PRIMARY KEY, group_key TEXT NOT NULL, kind TEXT NOT NULL, start TEXT NOT NULL, end TEXT NOT NULL,
            account_id INTEGER, client TEXT NOT NULL, lines TEXT NOT NULL, issue TEXT NOT NULL DEFAULT '',
            state TEXT NOT NULL DEFAULT 'open', version TEXT NOT NULL, sent_at TEXT, mail_state TEXT);
        CREATE UNIQUE INDEX IF NOT EXISTS bf_auto_open ON bf_auto_drafts(group_key) WHERE state='open';
        CREATE TABLE IF NOT EXISTS bf_auto_claims (
            source TEXT PRIMARY KEY, draft_id TEXT NOT NULL REFERENCES bf_auto_drafts(id));
        CREATE TABLE IF NOT EXISTS bf_auto_billing_clock (id INTEGER PRIMARY KEY CHECK(id=1), last_run TEXT NOT NULL);
    """)
    conn.execute("INSERT OR IGNORE INTO bf_invoice_categories(name) VALUES('POD Collections')")


def charge(source, description, net, **fields):
    return dict(source=source,description=description,net=net,tax=(net+5)//10,**fields)


def totals(lines):
    return sum(i['net'] for i in lines), sum(i['tax'] for i in lines)


def reconcile(conn, stamp, *, pods_only=False, requested_transfer=None):
    """Refresh open drafts within the caller's write transaction. Issued lines are immutable."""
    defaults = conn.execute('SELECT * FROM bf_billing_defaults WHERE id=1').fetchone()
    groups = {}
    def add(key,kind,first,last,aid,client,lines,issue=''):
        grp = groups.setdefault(key,dict(kind=kind,start=first,end=last,account_id=aid,client=client,lines=[],issues=set()))
        grp['lines'].extend(lines)
        if issue:
            grp['issues'].add(issue)

    pods = conn.execute("""SELECT j.id,j.code,j.site_name,j.pod_client_id,j.client_name,j.pod_signed_at,
        j.pod_version, SUM(i.net_g) AS net,COUNT(*) AS bins,COUNT(i.net_g) AS weighed,COALESCE(r.units,CASE WHEN test.id IS NOT NULL THEN 16000 END) AS units,
        test.id AS live_test,
        m.account_id,a.name AS payer FROM bf_jobs j JOIN bf_job_bins i ON i.job_id=j.id AND i.direction='collect'
        LEFT JOIN bf_job_pickup_rates p ON p.job_id=j.id LEFT JOIN bf_pickup_rates r ON r.id=p.rate_id
        LEFT JOIN bf_live_cycle test ON test.client_id=j.pod_client_id
        LEFT JOIN bf_pod_bill_to m ON m.client_id=j.pod_client_id LEFT JOIN bf_client_accounts a ON a.id=m.account_id
        WHERE j.status='complete' AND j.approval_status='approved' AND j.pod_signed_at IS NOT NULL
          AND j.pod_deleted_at IS NULL AND NOT EXISTS(SELECT 1 FROM bf_auto_claims c WHERE c.source='pod:'||j.id)
        GROUP BY j.id ORDER BY j.pod_signed_at,j.id""")
    for j in pods:
        try:
            signed = date.fromisoformat(j['pod_signed_at'][:10])
        except ValueError:
            continue
        first,last = fortnight(signed)
        aid = j['account_id']
        payer = f'a{aid}' if aid else f"c{j['pod_client_id'] or j['client_name']}"
        problem = ('Set the collection billing account in Billing setup.' if not aid else '')
        if j['live_test']:
            payer = 'live-test'
            problem = 'Live test collection. Internal draft only.'
        if j['units'] is None or j['weighed'] != j['bins']:
            problem = 'A signed POD has no recorded collection rate or final weight. Review the source before billing.'
        net = ((j['net'] or 0)*(j['units'] or 0)+50000)//100000
        desc = f"{j['code']} · {j['site_name']} · {signed.isoformat()} · {(j['net'] or 0)/1000:g} kg × ${rate_text(j['units'])}/kg"
        add(f'pods:{payer}:{first}','POD Collections',first,last,aid,j['payer'] or j['client_name'],
            [charge(f"pod:{j['id']}",desc,net,job_id=j['id'],pod_version=j['pod_version'],
                    net_g=j['net'],rate_units=j['units'])],problem)

    if not pods_only:
        add_transport_and_rental(conn, stamp, add, defaults, requested_transfer)
    query = "SELECT * FROM bf_auto_drafts WHERE state='open'"
    if pods_only:
        query += " AND kind='POD Collections'"
    active = {r['group_key']:r for r in conn.execute(query)}
    for key,group in groups.items():
        raw = json.dumps(group['lines'],ensure_ascii=False)
        issue = ' '.join(sorted(group['issues']))
        data = (group['kind'],group['start'],group['end'],group['account_id'],group['client'],raw,issue)
        version = hashlib.sha256(json.dumps(data).encode()).hexdigest()
        previous = active.pop(key,None)
        if previous and previous['version']==version:
            continue
        if previous:
            conn.execute("UPDATE bf_auto_drafts SET kind=?,start=?,end=?,account_id=?,client=?,lines=?,issue=?,version=? WHERE id=?",
                         (*data,version,previous['id']))
        else:
            conn.execute("""INSERT INTO bf_auto_drafts(id,group_key,kind,start,end,account_id,client,lines,issue,version)
                VALUES(?,?,?,?,?,?,?,?,?,?)""",(str(uuid4()),key,*data,version))
    for old in active.values():
        if old['kind']=='Bin Rental' and old['end']<stamp[:10]:
            # New billing settings can resolve a closed rental without changing its quantities.
            aid = defaults['rental_account'] if defaults else None
            if aid and not old['account_id']:
                payer = conn.execute('SELECT name FROM bf_client_accounts WHERE id=?',(aid,)).fetchone()
                conn.execute("UPDATE bf_auto_drafts SET account_id=?,client=?,version=? WHERE id=?",
                             (aid,payer['name'],str(uuid4()),old['id']))
            continue
        conn.execute('DELETE FROM bf_auto_drafts WHERE id=?',(old['id'],))
    if not pods_only:
        conn.execute('INSERT OR REPLACE INTO bf_auto_billing_clock VALUES(1,?)',(stamp,))


def add_transport_and_rental(conn, stamp, add, defaults, requested_transfer):
    from binflow_invoices import transport_lines
    start,end = fortnight(date.fromisoformat(stamp[:10]))
    transfers = conn.execute("""SELECT b.*,a.name AS payer,p.number AS po,
        f.account_id AS billing_account,f.rate_cents AS billing_rate,f.fuel_bps AS billing_fuel,
        COALESCE(b.bulk_delivered_at,t.signed_at,t.received_at,b.pickup_date) AS billing_date,
        (b.bulk_delivered_at IS NOT NULL OR t.id IS NOT NULL) AS delivered
        FROM bf_interstate_bookings b
        LEFT JOIN bf_auto_transfer_terms f ON f.booking_id=b.id AND f.booking_version=b.version
        LEFT JOIN bf_client_accounts a ON a.id=COALESCE(b.account_id,f.account_id)
        LEFT JOIN bf_interstate_purchase_orders p ON p.booking_id=b.id
        LEFT JOIN bf_bale_transfers t ON t.id=(
            SELECT MAX(m.transfer_id) FROM bf_interstate_manifests m JOIN bf_bale_transfers mt ON mt.id=m.transfer_id
            WHERE m.booking_id=b.id AND mt.status IN ('received','handed_over'))
        WHERE b.locked_at IS NOT NULL AND b.cancelled_at IS NULL
          AND (b.bulk_delivered_at IS NOT NULL OR t.id IS NOT NULL OR b.id=? OR EXISTS(
              SELECT 1 FROM bf_auto_drafts d WHERE d.group_key='transfer:'||b.id AND d.state='open'))
        AND NOT EXISTS(SELECT 1 FROM bf_rental_invoices x WHERE x.booking_id=b.id AND x.status!='cancelled')
        AND NOT EXISTS(SELECT 1 FROM bf_auto_claims c WHERE c.source='transfer:'||b.id) ORDER BY b.id""", (requested_transfer,))
    for b in transfers:
        b = dict(b)
        if b['billing_account']:
            b['account_id'] = b['account_id'] or b['billing_account']
            if b['rate_cents'] is None:
                b.update(rate_cents=b['billing_rate'],fuel_bps=b['billing_fuel'])
        entries = transport_lines(b)
        source = f"transfer:{b['id']}"
        lines = [charge(source,desc,rate,delivered=bool(b['delivered'])) for desc,qty,rate in entries]
        issue = '' if entries else 'The transfer has no confirmed transport price.'
        if not b['account_id']:
            issue = 'The transfer needs a billing account.'
        day = b['billing_date'][:10]
        add(source,'Transport',day,day,b['account_id'],b['payer'] or b['client_name'],lines,issue)
    # Keep the established two-week actual rental calculation, using the last observed stock
    # in each period. Never reconstruct a closed fortnight from today's stock.
    rent_key = 'rental:'+start
    covered = conn.execute("""SELECT 1 FROM bf_rental_invoices WHERE category_id=1 AND mode='actual'
        AND status!='cancelled' AND period_start<=? AND period_end>=?""",(end,start)).fetchone()
    data = report(conn)
    rental = data['totals']
    if not covered and (rental['billable_count'] or rental['review_count']):
        aid = defaults['rental_account'] if defaults else None
        account = conn.execute('SELECT name FROM bf_client_accounts WHERE id=?',(aid,)).fetchone()
        lines = [charge(rent_key,f"{data['sizes'][sid]['name']} · {qty} bins × 2 weeks at ${rate/100:.2f}/week",
                        qty*2*rate) for sid,qty in sorted(rental['billable'].items())
                 if qty and (rate:=data['sizes'][sid]['weekly_rental_cents']) is not None]
        issue = '' if rental['actual_cents'] is not None else 'Review the unpriced or flagged bins in Bin Rental Income.'
        add(rent_key,'Bin Rental',start,end,aid,account['name'] if account else 'National bin rental',lines,issue)


def pod_billing(conn, job_id, stamp):
    source = f'pod:{job_id}'
    def find_line():
        return conn.execute("""SELECT d.*,line.value AS pod_line FROM bf_auto_drafts d, json_each(d.lines) line
            WHERE d.kind='POD Collections' AND json_extract(line.value,'$.source')=? LIMIT 1""",(source,)).fetchone()
    draft = find_line()
    if not draft:
        # Include older signed PODs created before immediate draft generation.
        conn.execute('BEGIN IMMEDIATE')
        try:
            reconcile(conn,stamp,pods_only=True)
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        draft = find_line()
    if not draft:
        return None
    line = json.loads(draft['pod_line'])
    lines = json.loads(draft['lines'])
    return dict(draft=draft,line=line,lines=lines,total=sum(x['net']+x['tax'] for x in lines))


def refresh(conn, stamp, always=False):
    conn.execute('BEGIN IMMEDIATE')
    try:
        last=conn.execute('SELECT last_run FROM bf_auto_billing_clock').fetchone()
        if always or not last or last['last_run'][:13]!=stamp[:13]:
            reconcile(conn,stamp)
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def transfer_invoices(conn, booking_ids):
    """Find the active invoice or local draft for each booking in the current view."""
    rows = conn.execute("""
        SELECT b.value AS booking_id, COALESCE(i.id,d.id) AS id,i.status,i.xero_status
        FROM json_each(?) b
        LEFT JOIN bf_rental_invoices i ON i.booking_id=b.value AND i.status!='cancelled'
        LEFT JOIN bf_auto_drafts d ON d.group_key='transfer:'||b.value AND d.state='open'
        WHERE i.id IS NOT NULL OR d.id IS NOT NULL
    """, (json.dumps(booking_ids),))
    return {r['booking_id']:r for r in rows}


def register(app,db,require,render,now,actor):
    from binflow_invoices import issued
    def row(did):
        r=db().execute('SELECT * FROM bf_auto_drafts WHERE id=?',(did,)).fetchone()
        if not r: abort(404)
        return r

    def approval_settings(conn,draft):
        defaults=conn.execute('SELECT * FROM bf_billing_defaults WHERE id=1').fetchone()
        cfg=xero.config(conn)
        if not defaults or not cfg['tenant_id'] or not cfg['token_cipher'] or defaults['tenant']!=cfg['tenant_id']:
            raise ValueError('Connect Xero and save the billing defaults in Billing setup.')
        link=conn.execute("""SELECT l.* FROM bf_invoice_contacts l JOIN bf_client_accounts a ON a.id=l.account_id
            WHERE l.tenant_id=? AND l.account_id=? AND a.active=1""",(cfg['tenant_id'],draft['account_id'])).fetchone()
        if not link: raise ValueError('Link the billing client to its Xero contact in Billing setup.')
        opts=json.loads(cfg['choices_json'])
        account=next((a for a in opts.get('accounts',[]) if a['code']==defaults['account_code']),None)
        tax=next((t for t in opts.get('taxes',[]) if t['code']==defaults['tax_code'] and float(t['rate'])==10),None)
        if not account or not tax: raise ValueError('Review the income account and 10% sales GST code in Billing setup.')
        return cfg,link,account,tax

    @app.route('/binflow/setup/invoice-billing',methods=['GET','POST'])
    @require('admin')
    def bf_auto_billing_setup():
        from app import valid_email
        from binflow_invoices import contacts, categories
        conn=db();cfg=xero.config(conn)
        opts,error=xero.directory_for_page(app,conn,cfg,request.args.get('refresh')=='yes')
        if request.method=='POST':
            try:
                conn.execute('BEGIN IMMEDIATE')
                if cfg['tenant_id']!=request.form.get('tenant') or cfg['tenant_id']!=xero.config(conn)['tenant_id']:
                    raise ValueError('The Xero organisation changed. Refresh Billing setup.')
                action=request.form.get('action')
                aid=request.form.get('account_id',type=int)
                if not conn.execute('SELECT 1 FROM bf_client_accounts WHERE id=? AND active=1',(aid,)).fetchone():
                    raise ValueError('Choose an active client billing account.')
                if action=='defaults':
                    ac=request.form.get('account_code');tx=request.form.get('tax_code')
                    if not cfg['tenant_id'] or not any(a['code']==ac for a in opts.get('accounts',[])) or not any(
                        t['code']==tx and float(t['rate'])==10 for t in opts.get('taxes',[])):
                        raise ValueError('Choose an income account and 10% sales GST code from the connected organisation.')
                    conn.execute('INSERT OR REPLACE INTO bf_billing_defaults VALUES(1,?,?,?,?)',(cfg['tenant_id'],ac,tx,aid))
                elif action=='transfer_terms':
                    transfer = conn.execute('SELECT * FROM bf_interstate_bookings WHERE id=?',(request.form.get('booking',type=int),)).fetchone()
                    if (not transfer or transfer['cancelled_at'] or transfer['version']!=request.form.get('version',type=int)
                            or transfer['account_id'] and transfer['account_id']!=aid
                            or transfer['account_id'] and transfer['rate_cents'] is not None
                            or conn.execute("SELECT 1 FROM bf_rental_invoices WHERE booking_id=? AND status!='cancelled'",(transfer['id'],)).fetchone()):
                        raise ValueError('The transfer or its billing terms changed. Review the current record.')
                    rate=transfer['rate_cents'] if transfer['rate_cents'] is not None else amount(request.form.get('base'),'Agreed freight price',1000000,100)
                    fuel=transfer['fuel_bps'] if transfer['rate_cents'] is not None else amount(request.form.get('fuel'),'Agreed fuel percentage',100,100)
                    if rate<=0: raise ValueError('Enter the agreed positive freight price.')
                    conn.execute('INSERT OR REPLACE INTO bf_auto_transfer_terms VALUES(?,?,?,?,?,?,?)',
                        (transfer['id'],aid,rate,fuel,transfer['version'],now(),actor()))
                elif action=='routing':
                    cid=request.form.get('client_id',type=int)
                    if not conn.execute('SELECT 1 FROM bf_clients WHERE id=?',(cid,)).fetchone(): raise ValueError('Choose a collection client.')
                    conn.execute('INSERT OR REPLACE INTO bf_pod_bill_to VALUES(?,?)',(cid,aid))
                elif action=='email':
                    address=request.form.get('email','').strip()
                    if not valid_email(address): raise ValueError('Enter one valid billing email address.')
                    conn.execute('INSERT OR REPLACE INTO bf_billing_emails VALUES(?,?)',(aid,address))
                else: abort(400)
                conn.commit();flash('Billing setup saved.','success')
                if action=='transfer_terms':
                    draft = transfer_invoices(conn,[transfer['id']]).get(transfer['id'])
                    if draft:
                        return redirect(url_for('bf_auto_review',did=draft['id']))
                return redirect(url_for('bf_auto_billing_setup'))
            except ValueError as exc:
                conn.rollback();flash(str(exc),'error')
        cfg=xero.config(conn)
        return render('auto_billing_setup',section='setup',
            transfer=conn.execute('SELECT * FROM bf_interstate_bookings WHERE id=?',(request.args.get('booking',type=int),)).fetchone(),defaults=conn.execute('SELECT * FROM bf_billing_defaults').fetchone(),
            categories=categories(conn),accounts=contacts(conn,cfg['tenant_id']),options=opts,error=error,tenant=cfg['tenant_id'],
            connected=bool(cfg['tenant_id'] and cfg['token_cipher']),clients=conn.execute("""SELECT c.id,c.name,a.name AS payer
            FROM bf_clients c LEFT JOIN bf_pod_bill_to m ON m.client_id=c.id LEFT JOIN bf_client_accounts a ON a.id=m.account_id
            ORDER BY c.name""").fetchall(),emails=conn.execute('SELECT e.email,a.name FROM bf_billing_emails e JOIN bf_client_accounts a ON a.id=e.account_id').fetchall())

    @app.post('/binflow/interstate-transfers/<int:bid>/invoice-draft')
    @require('admin')
    def bf_transfer_invoice_draft(bid):
        conn = db()
        def existing_or_eligible():
            booking = conn.execute('SELECT locked_at,cancelled_at FROM bf_interstate_bookings WHERE id=?',(bid,)).fetchone()
            if not booking:
                abort(404)
            existing = transfer_invoices(conn,[bid]).get(bid)
            if existing and existing['status']:
                return redirect(url_for('bf_rental_invoice',identifier=existing['id']), code=303)
            if not booking['locked_at'] or booking['cancelled_at']:
                abort(409, 'Only confirmed transfers can be invoiced.')
        existing = existing_or_eligible()
        if existing:
            return existing
        try:
            tenant = xero.verify_draft_connection(app,conn,request.form.get('tenant'))
        except xero.XeroError as exc:
            flash('Invoice was not sent. ' + str(exc),'error')
            return redirect(url_for('bf_xero_settings'),code=303)
        conn.execute('BEGIN IMMEDIATE')
        existing = existing_or_eligible()
        if existing:
            conn.rollback()
            return existing
        reconcile(conn, now(), requested_transfer=bid)
        draft = row(transfer_invoices(conn,[bid])[bid]['id'])
        conn.commit()
        return submit_invoice(draft['id'],True,draft['version'],tenant,from_booking=True)

    @app.get('/binflow/invoices/review/<did>')
    @require('admin')
    def bf_auto_review(did):
        conn=db();refresh(conn,now(),True);d=row(did)
        lines=json.loads(d['lines']);net,tax=totals(lines)
        cfg=link=None
        reason = d['issue']
        if not reason and d['state']=='open':
            try: cfg,link,_,_ = approval_settings(conn,d)
            except ValueError as exc: reason=str(exc)
        return render('auto_invoice',section='invoices',d=d,lines=lines,net=net,tax=tax,reason=reason,link=link,cfg=cfg,
            ready=d['kind']=='Transport' or d['end']<now()[:10],
            email=conn.execute('SELECT email FROM bf_billing_emails WHERE account_id=?',(d['account_id'],)).fetchone(),
            invoice=conn.execute('SELECT * FROM bf_rental_invoices WHERE id=?',(did,)).fetchone())

    def record_remote(conn,did,remote,*,rechecking=False):
        item=conn.execute('SELECT * FROM bf_rental_invoices WHERE id=?',(did,)).fetchone()
        payload=json.loads(item['payload_json'])['Invoices'][0]
        valid_statuses = {'AUTHORISED','PAID'}
        if payload['Status']=='DRAFT':
            valid_statuses = {'DRAFT','AUTHORISED','PAID'} if rechecking else {'DRAFT'}
        if (remote.get('Type')!='ACCREC' or remote.get('Reference')!=item['reference']
                or remote.get('Contact',{}).get('ContactID')!=payload['Contact']['ContactID']
                or remote.get('Status') not in valid_statuses or remote.get('HasErrors')):
            raise ValueError('Xero did not confirm the expected invoice status and billing contact.')
        reviewed = json.loads(item['snapshot_json'])
        if remote.get('CurrencyCode') not in (None,'AUD') or (remote.get('Total') is not None and
                Decimal(str(remote['Total']))*100 != reviewed['subtotal']+reviewed['gst']):
            raise ValueError('The confirmed Xero total differs from the reviewed draft.')
        invoice_id=xero.uuid_text(remote.get('InvoiceID'))
        conn.execute("UPDATE bf_rental_invoices SET status='created',xero_status=?,invoice_id=?,invoice_number=?,error='' WHERE id=?",
                     (remote['Status'],invoice_id,str(remote.get('InvoiceNumber','')),did))
        state = 'drafted' if remote['Status']=='DRAFT' else 'approved'
        conn.execute("UPDATE bf_auto_drafts SET state=? WHERE id=?",(state,did))
        conn.commit()

    @app.post('/binflow/invoices/review/<did>/xero-draft',endpoint='bf_auto_xero_draft')
    @app.post('/binflow/invoices/review/<did>/approve')
    @require('admin')
    def bf_auto_approve(did):
        save_draft = request.endpoint=='bf_auto_xero_draft'
        d = row(did)
        if save_draft and d['kind']!='Transport':
            abort(404)
        if d['state']!='open':
            return redirect(url_for('bf_auto_review',did=did))
        tenant = request.form.get('tenant','')
        if save_draft:
            try:
                xero.verify_draft_connection(app,db(),tenant)
            except xero.XeroError as exc:
                flash('Invoice was not sent. ' + str(exc),'error')
                return redirect(url_for('bf_auto_review',did=did))
        return submit_invoice(did,save_draft,request.form.get('version'),tenant)

    def submit_invoice(did,save_draft,version,tenant,*,from_booking=False):
        conn=db()
        try:
            conn.execute('BEGIN IMMEDIATE');reconcile(conn,now());d=row(did)
            if d['state']!='open' or version!=d['version']:
                raise ValueError('The draft changed. Review its current lines before continuing.')
            if d['kind']!='Transport' and d['end']>=now()[:10]:
                raise ValueError('This fortnight is still collecting charges.')
            if d['issue']: raise ValueError(d['issue'])
            cfg,link,account,tax=approval_settings(conn,d)
            if tenant!=cfg['tenant_id'] or (not from_booking and request.form.get('contact')!=link['contact_id']):
                raise ValueError('The billing contact or organisation changed. Review the draft again.')
            lines=json.loads(d['lines']);net,gst=totals(lines)
            if not lines or net<=0: raise ValueError('There are no positive billable charges.')
            if d['kind']=='Transport' and not save_draft and not lines[0]['delivered']:
                raise ValueError('This transfer can be saved as a Xero draft before delivery.')
            category=conn.execute('SELECT id FROM bf_invoice_categories WHERE name=? COLLATE NOCASE',(d['kind'],)).fetchone()[0]
            bid=int(d['group_key'].split(':')[1]) if d['kind']=='Transport' else None
            if d['kind']=='Bin Rental' and conn.execute("""SELECT 1 FROM bf_rental_invoices WHERE category_id=1 AND
                 status!='cancelled' AND mode='actual' AND period_start<=? AND period_end>=?""",(d['end'],d['start'])).fetchone():
                raise ValueError('This rental fortnight already has an invoice.')
            ref='BF-'+did
            issued_date=now()[:10]
            invoice=dict(Type='ACCREC',Status='DRAFT' if save_draft else 'AUTHORISED',CurrencyCode='AUD',LineAmountTypes='Exclusive',Reference=ref,
                Contact={'ContactID':link['contact_id']},Date=issued_date,DueDate=(date.fromisoformat(issued_date)+timedelta(days=14)).isoformat(),
                LineItems=[dict(Description=i['description'],Quantity=1,UnitAmount=i['net']/100,
                    AccountCode=account['code'],TaxType=tax['code']) for i in lines])
            snapshot=dict(category=d['kind'],contact=dict(id=link['contact_id'],name=link['contact_name']),account=account,tax=tax,
                subtotal=net,gst=gst,lines=[dict(i,quantity=1,rate=i['net']) for i in lines])
            conn.execute("""INSERT INTO bf_rental_invoices(id,tenant_id,tenant_name,period_start,period_end,mode,status,
                reference,snapshot_json,payload_json,created_by,created_at,attempted_at,category_id,portal_account_id,booking_id)
                VALUES(?,?,?,?,?,'actual','sending',?,?,?,?,?,?,?,?,?)""",(did,cfg['tenant_id'],cfg['tenant_name'],d['start'],d['end'],
                ref,json.dumps(snapshot),json.dumps(dict(Invoices=[invoice])),actor(),now(),now(),category,d['account_id'],bid))
            conn.executemany('INSERT INTO bf_auto_claims VALUES(?,?)',[(s,did) for s in {i['source'] for i in lines}])
            conn.execute("UPDATE bf_auto_drafts SET state='sending' WHERE id=?",(did,));conn.commit()
            try:
                result=xero.records(xero.api(app,conn,'Invoices','POST',dict(Invoices=[invoice]),key=did,tenant_id=cfg['tenant_id']),'Invoices')
                if len(result)!=1: raise ValueError('Unexpected invoice response from Xero.')
                record_remote(conn,did,result[0])
            except xero.XeroAuthorisationError as exc:
                # A rejected request is safe to try again after reconnecting.
                conn.execute('BEGIN IMMEDIATE')
                removed = conn.execute("DELETE FROM bf_rental_invoices WHERE id=? AND status='sending'",(did,)).rowcount
                if removed:
                    conn.execute('DELETE FROM bf_auto_claims WHERE draft_id=?',(did,))
                    conn.execute("UPDATE bf_auto_drafts SET state='open' WHERE id=?",(did,))
                conn.commit()
                raise ValueError('Invoice was not sent. ' + str(exc)) from exc
            except (ValueError,OSError) as exc:
                conn.execute("UPDATE bf_rental_invoices SET status='uncertain',error=? WHERE id=? AND status='sending'",(str(exc),did));conn.commit()
                raise ValueError('Check the saved reference in Xero to confirm the result.') from exc
            flash('Draft created in Xero.' if save_draft else 'Invoice approved in Xero.','success')
        except (ValueError,sqlite3.IntegrityError) as exc:
            conn.rollback();flash(str(exc),'error')
        return redirect(url_for('bf_auto_review',did=did))

    @app.post('/binflow/invoices/review/<did>/check')
    @require('admin')
    def bf_auto_check(did):
        from urllib.parse import urlencode
        conn=db();d=row(did)
        if d['state'] not in ('sending','drafted'): abort(409)
        item=conn.execute('SELECT * FROM bf_rental_invoices WHERE id=?',(did,)).fetchone()
        try:
            query=urlencode({'where':'Reference=="'+item['reference']+'"'})
            matches=xero.records(xero.api(app,conn,'Invoices?'+query,tenant_id=item['tenant_id']),'Invoices')
            if len(matches)!=1: raise ValueError('No unique invoice confirmed in Xero. Check the saved reference.')
            record_remote(conn,did,matches[0],rechecking=True);flash('Xero invoice status confirmed.','success')
        except ValueError as exc: conn.rollback();flash(str(exc),'error')
        return redirect(url_for('bf_auto_review',did=did))

    def package(d):
        from binflow_invoices import pdf_bytes
        conn=db()
        if d['state']!='approved' or d['kind']!='POD Collections': abort(409)
        item=conn.execute('SELECT * FROM bf_rental_invoices WHERE id=?',(d['id'],)).fetchone()
        contact=json.loads(item['snapshot_json'])['contact']['id']
        if xero.config(conn)['tenant_id']!=item['tenant_id']: raise ValueError('Reconnect the original Xero organisation.')
        docs=xero.records(xero.api(app,conn,'Invoices/'+item['invoice_id'],tenant_id=item['tenant_id']),'Invoices')
        if len(docs)!=1 or docs[0].get('InvoiceID')!=item['invoice_id'] or not issued(docs[0],contact):
            raise ValueError('This invoice is no longer available for issue to this client.')
        output=io.BytesIO()
        with zipfile.ZipFile(output,'w',compression=zipfile.ZIP_DEFLATED) as z:
            z.writestr('Invoice.pdf',pdf_bytes(app,conn,item['tenant_id'],item['invoice_id']))
            for line in json.loads(d['lines']):
                job=conn.execute('SELECT * FROM bf_jobs WHERE id=?',(line['job_id'],)).fetchone()
                if not job or job['pod_deleted_at'] or not job['pod_signed_at']: raise ValueError('A supporting POD was withdrawn. Review the batch.')
                response=app.view_functions['bf_pod_pdf'](job_id=job['id'])
                response.direct_passthrough=False
                try: z.writestr(f"PODs/{job['code']}.pdf",response.get_data())
                finally: response.close()
        return output.getvalue()

    @app.get('/binflow/invoices/review/<did>/batch.zip')
    @require('admin')
    def bf_auto_download(did):
        d=row(did)
        try: data=package(d)
        except ValueError as exc: flash(str(exc),'error');return redirect(url_for('bf_auto_review',did=did))
        return send_file(io.BytesIO(data),mimetype='application/zip',as_attachment=True,download_name=f"POD-invoice-{d['start']}-{d['end']}.zip")

    @app.post('/binflow/invoices/review/<did>/email')
    @require('admin')
    def bf_auto_email(did):
        import smtplib
        conn=db();d=row(did)
        try:
            dest=conn.execute('SELECT email FROM bf_billing_emails WHERE account_id=?',(d['account_id'],)).fetchone()
            if not dest or dest['email']!=request.form.get('email'): raise ValueError('Review the configured billing email address.')
            if not app.config['SMTP_HOST'] or not app.config['SMTP_FROM']: raise ValueError('Connect outgoing email in Settings.')
            data=package(d)
            message=EmailMessage();message['To']=dest['email'];message['From']=app.config['SMTP_FROM']
            message['Subject']=f"SB Empire · collection invoice and signed PODs · {d['start']} to {d['end']}"
            message.set_content(f"Please find the approved collection invoice and its supporting signed PODs for {d['start']} to {d['end']} attached.")
            message.add_attachment(data,maintype='application',subtype='zip',filename=f"POD-invoice-{d['end']}.zip")
            claim=conn.execute("UPDATE bf_auto_drafts SET mail_state='sending' WHERE id=? AND mail_state IS NULL",(did,));conn.commit()
            if not claim.rowcount: raise ValueError('This email has already been sent or is awaiting delivery confirmation.')
            try: send_smtp(app.config,message)
            except (OSError,smtplib.SMTPException) as exc:
                conn.execute("UPDATE bf_auto_drafts SET mail_state='uncertain' WHERE id=?",(did,));conn.commit()
                raise ValueError('Delivery could not be confirmed. Check the sending mailbox before sending another copy.') from exc
            conn.execute("UPDATE bf_auto_drafts SET mail_state='sent',sent_at=? WHERE id=?",(now(),did));conn.commit()
            flash('Invoice and signed PODs emailed to '+dest['email']+'.','success')
        except ValueError as exc: conn.rollback();flash(str(exc),'error')
        return redirect(url_for('bf_auto_review',did=did))

    if not app.testing:
        def observe():
            pause = threading.Event()
            while True:
                try:
                    with app.app_context():
                        refresh(db(),now())
                except Exception:
                    app.logger.exception('Automatic invoice draft refresh failed')
                pause.wait(300)
        threading.Thread(target=observe,name='binflow-billing',daemon=True).start()
