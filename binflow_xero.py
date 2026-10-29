"""Admin-only Xero OAuth and reviewed, draft-only fortnightly rental invoices.

Credentials stay encrypted in the existing persistent database. A durable outbox
claims an export before the HTTP request; uncertain outcomes are reconciled by
reference rather than blindly resent. No approval, email or payment API exists.
"""
import base64
import hashlib
import json
import re
import secrets
import sqlite3
import time
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlparse
from urllib.request import Request, build_opener, HTTPRedirectHandler
from uuid import UUID, uuid4

from cryptography.fernet import Fernet, InvalidToken
from flask import abort, flash, g, redirect, request, session, url_for

from binflow_rental import report

SCOPES = 'offline_access accounting.invoices accounting.contacts.read accounting.settings.read'
TOKEN_URL = 'https://identity.xero.com/connect/token'
API_ROOT = 'https://api.xero.com/api.xro/2.0/'


def migrate(conn):
    conn.executescript('''
    CREATE TABLE IF NOT EXISTS bf_xero_config (
      id INTEGER PRIMARY KEY CHECK(id=1), client_id TEXT NOT NULL DEFAULT '',
      secret_cipher BLOB, token_cipher BLOB, expires_at INTEGER NOT NULL DEFAULT 0,
      tenant_id TEXT NOT NULL DEFAULT '', tenant_name TEXT NOT NULL DEFAULT '',
      choices_json TEXT NOT NULL DEFAULT '{}', choices_at INTEGER NOT NULL DEFAULT 0,
      last_test_at TEXT NOT NULL DEFAULT '', updated_at TEXT NOT NULL DEFAULT '',
      updated_by TEXT NOT NULL DEFAULT '', revision INTEGER NOT NULL DEFAULT 0);
    INSERT OR IGNORE INTO bf_xero_config(id) VALUES(1);
    CREATE TABLE IF NOT EXISTS bf_xero_oauth (
      state_hash TEXT PRIMARY KEY, user_id INTEGER NOT NULL, client_id TEXT NOT NULL,
      revision INTEGER NOT NULL, created_at INTEGER NOT NULL);
    CREATE TABLE IF NOT EXISTS bf_rental_invoices (
      id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, tenant_name TEXT NOT NULL,
      period_start TEXT NOT NULL, period_end TEXT NOT NULL,
      mode TEXT NOT NULL CHECK(mode IN ('actual','test')),
      status TEXT NOT NULL CHECK(status IN ('preview','sending','created','uncertain','cancelled')),
      reference TEXT NOT NULL UNIQUE, snapshot_json TEXT NOT NULL, payload_json TEXT NOT NULL,
      created_by TEXT NOT NULL, created_at TEXT NOT NULL, attempted_at TEXT,
      invoice_id TEXT NOT NULL DEFAULT '', invoice_number TEXT NOT NULL DEFAULT '',
      xero_status TEXT NOT NULL DEFAULT '', error TEXT NOT NULL DEFAULT '');
    CREATE UNIQUE INDEX IF NOT EXISTS bf_rental_invoice_period ON bf_rental_invoices(tenant_id,period_start,period_end)
      WHERE mode='actual' AND status!='cancelled';
    ''')


class XeroError(ValueError):
    pass


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def request_json(url, method='GET', headers=None, data=None):
    """Fixed Xero origins only; never expose remote response bodies or secrets."""
    parsed = urlparse(url)
    if parsed.scheme != 'https' or parsed.netloc not in ('api.xero.com', 'identity.xero.com'):
        raise XeroError('Unsupported Xero destination.')
    req = Request(url, method=method, headers={'Accept': 'application/json', **(headers or {})}, data=data)
    try:
        with build_opener(NoRedirect).open(req, timeout=10) as response:
            body = response.read(4 * 1024 * 1024 + 1)
        if len(body) > 4 * 1024 * 1024:
            raise XeroError('Xero returned too much data. Narrow the contact search.')
        return json.loads(body) if body else {}
    except HTTPError as exc:
        messages = {400: 'Xero rejected the request. Check the app settings or invoice contact, account and tax selections.',
                    401: 'Xero authorisation expired or was rejected. Reconnect to Xero.',
                    403: 'Your Xero user does not have the required access. Check permissions and reconnect.',
                    429: 'Xero is temporarily rate limiting requests. Try checking the result shortly.'}
        raise XeroError(messages.get(exc.code, 'Xero could not complete the request. Check the result before retrying.')) from None
    except (URLError, TimeoutError, OSError, json.JSONDecodeError):
        raise XeroError('Xero could not be reached. Check the result before retrying an invoice export.') from None


def uuid_text(value):
    try:
        return str(UUID(str(value)))
    except (ValueError, TypeError, AttributeError):
        raise XeroError('Xero returned an invalid record identifier.') from None


def cipher(app):
    key = hashlib.sha256(b'binflow:xero:v1:' + str(app.config['SECRET_KEY']).encode()).digest()
    return Fernet(base64.urlsafe_b64encode(key))


def decrypt(app, value):
    try:
        return cipher(app).decrypt(value).decode()
    except (InvalidToken, TypeError, UnicodeError):
        raise XeroError('Saved Xero credentials cannot be read. Save the app credentials and reconnect.') from None


def config(conn):
    return dict(conn.execute('SELECT * FROM bf_xero_config WHERE id=1').fetchone())


def callback_url(app):
    origin = app.config.get('APP_URL', '').rstrip('/')
    parsed = urlparse(origin)
    if parsed.scheme != 'https' or not parsed.netloc or parsed.username or parsed.password or parsed.path or parsed.query or parsed.fragment:
        raise XeroError('Set APP_URL to the public HTTPS address of BinFlow before connecting Xero.')
    return origin + '/binflow/xero/callback'


def exchange(app, cfg, values):
    if not cfg['client_id'] or not cfg['secret_cipher']:
        raise XeroError('Save your Xero app credentials first.')
    basic = base64.b64encode((cfg['client_id'] + ':' + decrypt(app, cfg['secret_cipher'])).encode()).decode()
    tokens = request_json(TOKEN_URL, 'POST', {'Authorization': 'Basic ' + basic,
                          'Content-Type': 'application/x-www-form-urlencoded'}, urlencode(values).encode())
    if not isinstance(tokens, dict) or any(not isinstance(tokens.get(k), str) or not tokens[k] for k in ('access_token', 'refresh_token')):
        raise XeroError('Xero did not provide offline access. Reconnect with the requested permissions.')
    try:
        tokens['expires_in'] = min(1800, max(1, int(tokens['expires_in'])))
    except (ValueError, TypeError, KeyError):
        raise XeroError('Xero returned an invalid token expiry.') from None
    granted = set(str(tokens.get('scope', SCOPES)).split())
    if not set(SCOPES.split()).issubset(granted):
        raise XeroError('Xero did not grant the required invoicing permissions. Reconnect to Xero.')
    return tokens


def save_tokens(app, conn, tokens):
    conn.execute('UPDATE bf_xero_config SET token_cipher=?,expires_at=? WHERE id=1',
                 (cipher(app).encrypt(json.dumps(tokens).encode()), int(time.time()) + tokens['expires_in']))


def access_token(app, conn):
    # SQLite serialises refresh-token rotation across threads and workers.
    conn.execute('BEGIN IMMEDIATE')
    try:
        cfg = config(conn)
        if not cfg['token_cipher']:
            raise XeroError('Connect to Xero first.')
        tokens = json.loads(decrypt(app, cfg['token_cipher']))
        if cfg['expires_at'] <= int(time.time()) + 60:
            tokens = exchange(app, cfg, {'grant_type': 'refresh_token', 'refresh_token': tokens['refresh_token']})
            save_tokens(app, conn, tokens)
        conn.commit()
        return tokens['access_token']
    except Exception:
        conn.rollback()
        raise


def api(app, conn, path, method='GET', payload=None, key=None, tenant_id=None):
    token = access_token(app, conn)
    cfg = config(conn)
    tenant = tenant_id or cfg['tenant_id']
    if not tenant:
        raise XeroError('Select the Xero organisation first.')
    if tenant_id and cfg['tenant_id'] and cfg['tenant_id'] != tenant_id:
        raise XeroError('The Xero organisation changed. Prepare a new preview.')
    headers = {'Authorization': 'Bearer ' + token, 'Xero-tenant-id': uuid_text(tenant)}
    if payload is not None:
        headers['Content-Type'] = 'application/json'
    if key:
        headers['Idempotency-Key'] = key
    result = request_json(API_ROOT + path, method, headers,
                          json.dumps(payload).encode() if payload is not None else None)
    if not isinstance(result, dict):
        raise XeroError('Xero returned an unexpected result. Check the result before retrying an invoice export.')
    return result


def connections(app, conn):
    rows = request_json('https://api.xero.com/connections', headers={'Authorization': 'Bearer ' + access_token(app, conn)})
    if not isinstance(rows, list):
        raise XeroError('Xero did not return its organisation connections.')
    return [dict(id=uuid_text(r.get('tenantId')), name=str(r.get('tenantName', 'Unnamed organisation'))[:200])
            for r in rows if isinstance(r, dict) and r.get('tenantType') == 'ORGANISATION']


def records(result, name):
    rows = result.get(name)
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        raise XeroError('Xero returned an unexpected result. Check the result before retrying an invoice export.')
    return rows


def organisation(app, conn, tenant=None):
    rows = records(api(app, conn, 'Organisation', tenant_id=tenant), 'Organisations')
    if len(rows) != 1 or rows[0].get('BaseCurrency') != 'AUD':
        raise XeroError('Choose an Australian-dollar Xero organisation for BinFlow rental invoices.')
    return rows[0]


def choices(app, conn, search='Blocktexx'):
    accounts = records(api(app, conn, 'Accounts'), 'Accounts')
    taxes = records(api(app, conn, 'TaxRates'), 'TaxRates')
    contacts = records(api(app, conn, 'Contacts?' + urlencode({'searchTerm': search[:100], 'page': 1})), 'Contacts')
    result = dict(
        accounts=[dict(code=str(a['Code']), name=a.get('Name', '')) for a in accounts
                  if a.get('Status') == 'ACTIVE' and a.get('Type') in ('REVENUE','SALES','OTHERINCOME') and a.get('Code')],
        taxes=[dict(code=t['TaxType'], name=t.get('Name', ''), rate=str(t.get('EffectiveRate', 0))) for t in taxes
               if t.get('Status') == 'ACTIVE' and t.get('CanApplyToRevenue') and t.get('TaxType')],
        contacts=[dict(id=uuid_text(c.get('ContactID')), name=c.get('Name', '')) for c in contacts
                  if c.get('ContactStatus') == 'ACTIVE'], search=search)
    conn.execute('UPDATE bf_xero_config SET choices_json=?,choices_at=? WHERE id=1', (json.dumps(result), int(time.time())))
    conn.commit()
    return result


def prepare_snapshot(conn, values):
    try:
        start, end = date.fromisoformat(values.get('period_start', '')), date.fromisoformat(values.get('period_end', ''))
    except ValueError:
        raise XeroError('Enter valid rental period dates.') from None
    if (end - start).days != 13:
        raise XeroError('Choose a 14-day rental period, including both dates.')
    mode = values.get('mode', '')
    if mode not in ('actual', 'test'):
        raise XeroError('Choose actual rentals or a labelled test draft.')
    cfg = config(conn)
    opts = json.loads(cfg['choices_json'])
    contact = next((x for x in opts.get('contacts', []) if x['id'] == values.get('contact_id')), None)
    account = next((x for x in opts.get('accounts', []) if x['code'] == values.get('account_code')), None)
    tax = next((x for x in opts.get('taxes', []) if x['code'] == values.get('tax_type')), None)
    if not contact or not account or not tax or not cfg['tenant_id']:
        raise XeroError('Choose a connected organisation, Xero contact, income account and tax rate.')
    if cfg['choices_at'] < int(time.time()) - 3600:
        raise XeroError('Refresh the Xero billing choices before preparing this invoice.')
    data = report(conn)
    counts = data['totals']['billable']
    if mode == 'actual':
        if values.get('reviewed_period') != 'yes':
            raise XeroError('Confirm that current quantities and rates apply for the complete rental period.')
        if data['totals']['actual_cents'] is None:
            raise XeroError('Complete missing actual rental rates and review flagged bins first.')
        if not sum(counts.values()):
            raise XeroError('No actual billable bins are on site. Use a labelled test draft to test the connection.')
    else:
        try:
            size_id, quantity = int(values.get('sample_size', '')), int(values.get('sample_quantity', ''))
            if size_id not in data['sizes'] or not 1 <= quantity <= 10:
                raise ValueError()
        except (ValueError, TypeError):
            raise XeroError('Choose a bin size and a test quantity between 1 and 10.') from None
        counts = {size_id: quantity}
    lines = []
    try:
        tax_rate = Decimal(tax['rate'])
    except (InvalidOperation, TypeError, ValueError):
        raise XeroError('The selected Xero tax rate cannot be used.') from None
    if not tax_rate.is_finite() or not 0 <= tax_rate <= 100:
        raise XeroError('The selected Xero tax rate cannot be used.')
    for sid, quantity in sorted(counts.items()):
        if not quantity:
            continue
        size = data['sizes'][sid]
        rate = size['weekly_rental_cents']
        if rate is None or rate <= 0:
            raise XeroError('Set a positive weekly rental rate for every included bin size.')
        net = quantity * 2 * rate
        gst = int((Decimal(net) * tax_rate / 100).quantize(Decimal('1'), rounding=ROUND_HALF_UP))
        lines.append(dict(size=size['name'], bins=quantity, weeks=2, rate=rate, net=net, tax=gst))
    return dict(start=start.isoformat(), end=end.isoformat(), mode=mode, contact=contact, account=account, tax=tax,
                lines=lines, subtotal=sum(x['net'] for x in lines), gst=sum(x['tax'] for x in lines))


def invoice_payload(snapshot, reference, today):
    return {'Invoices': [dict(Type='ACCREC', Status='DRAFT', CurrencyCode='AUD', LineAmountTypes='Exclusive',
        Contact={'ContactID': snapshot['contact']['id']}, Date=today.isoformat(), DueDate=(today + timedelta(days=14)).isoformat(),
        Reference=reference, LineItems=[dict(
            Description=('TEST ONLY - sample quantities - ' if snapshot['mode'] == 'test' else '') +
                f"Bin rental {line['size']}: {line['bins']} bins x 2 weeks, {snapshot['start']} to {snapshot['end']}",
            Quantity=line['bins'] * 2, UnitAmount=line['rate'] / 100,
            AccountCode=snapshot['account']['code'], TaxType=snapshot['tax']['code']) for line in snapshot['lines']])]}


def register(app, db, require, render, now, actor):
    def go_settings():
        return redirect(url_for('bf_xero_settings'))

    def saved_invoice(identifier):
        row = db().execute('SELECT * FROM bf_rental_invoices WHERE id=?', (identifier,)).fetchone()
        if not row:
            abort(404)
        return dict(row)

    @app.get('/binflow/settings/xero')
    @require('admin')
    def bf_xero_settings():
        cfg = config(db())
        error, orgs, callback = None, [], ''
        try:
            callback = callback_url(app)
            if cfg['token_cipher'] and not cfg['tenant_id']:
                orgs = connections(app, db())
        except XeroError as exc:
            error = str(exc)
        # Only non-secret fields are passed to templates.
        return render('xero_settings', section='setup', configured=bool(cfg['secret_cipher']),
                      client_id=cfg['client_id'], connected=bool(cfg['tenant_id'] and cfg['token_cipher']),
                      authorised=bool(cfg['token_cipher']), tenant_name=cfg['tenant_name'],
                      tested=cfg['last_test_at'], organisations=orgs, callback=callback, error=error)

    @app.post('/binflow/xero/credentials')
    @require('admin')
    def bf_xero_credentials():
        cid, secret = request.form.get('client_id', '').strip(), request.form.get('client_secret', '').strip()
        if not re.fullmatch(r'[A-Za-z0-9._-]{8,128}', cid) or not 8 <= len(secret) <= 512 or any(c in secret for c in '\r\n'):
            flash('Enter a valid Xero Client ID and Client Secret.', 'error')
            return go_settings()
        db().execute('BEGIN IMMEDIATE')
        if config(db())['token_cipher']:
            db().rollback()
            flash('Disconnect BinFlow before replacing its app credentials.', 'error')
            return go_settings()
        db().execute('''UPDATE bf_xero_config SET client_id=?,secret_cipher=?,revision=revision+1,
            updated_at=?,updated_by=? WHERE id=1''', (cid, cipher(app).encrypt(secret.encode()), now(), actor()))
        db().execute('DELETE FROM bf_xero_oauth')
        db().commit()
        flash('Xero app credentials saved securely. Connect to Xero next.', 'success')
        return go_settings()

    @app.post('/binflow/xero/connect')
    @require('admin')
    def bf_xero_connect():
        cfg = config(db())
        try:
            callback = callback_url(app)
            if not cfg['secret_cipher']:
                raise XeroError('Save your Xero app credentials first.')
        except XeroError as exc:
            flash(str(exc), 'error')
            return go_settings()
        state = secrets.token_urlsafe(32)
        session['xero_state'] = state
        db().execute('DELETE FROM bf_xero_oauth WHERE user_id=? OR created_at<?', (g.user['id'], int(time.time()) - 600))
        db().execute('INSERT INTO bf_xero_oauth VALUES(?,?,?,?,?)',
                     (hashlib.sha256(state.encode()).hexdigest(), g.user['id'], cfg['client_id'], cfg['revision'], int(time.time())))
        db().commit()
        authorisation_url = 'https://login.xero.com/identity/connect/authorize?' + urlencode(dict(
            response_type='code', client_id=cfg['client_id'], redirect_uri=callback, scope=SCOPES, state=state))
        # End the protected form submission here. Browsers can otherwise apply
        # form-action to Xero's later cross-origin redirects and leave the
        # submitting page frozen. A new top-level navigation handles that chain.
        return render('xero_connect', section='setup', authorisation_url=authorisation_url)

    @app.get('/binflow/xero/callback')
    @require('admin')
    def bf_xero_callback():
        state = request.args.get('state', '')
        if not state or not secrets.compare_digest(state, session.get('xero_state', '')):
            flash('Xero sign-in could not be verified. Start Connect to Xero again.', 'error')
            return go_settings()
        digest = hashlib.sha256(state.encode()).hexdigest()
        session.pop('xero_state', None)
        db().execute('BEGIN IMMEDIATE')
        pending = db().execute('SELECT * FROM bf_xero_oauth WHERE state_hash=?', (digest,)).fetchone()
        cfg = config(db())
        db().execute('DELETE FROM bf_xero_oauth WHERE state_hash=?', (digest,))
        db().commit()
        if not pending or pending['user_id'] != g.user['id'] or pending['created_at'] < time.time() - 600 or pending['revision'] != cfg['revision']:
            flash('Xero sign-in expired. Start Connect to Xero again.', 'error')
            return go_settings()
        if request.args.get('error') or not request.args.get('code'):
            flash('Xero authorisation was not completed.', 'error')
            return go_settings()
        try:
            tokens = exchange(app, cfg, dict(grant_type='authorization_code', code=request.args['code'], redirect_uri=callback_url(app)))
            db().execute('BEGIN IMMEDIATE')
            if config(db())['revision'] != cfg['revision']:
                raise XeroError('App credentials changed during sign-in. Connect again.')
            save_tokens(app, db(), tokens)
            db().execute("UPDATE bf_xero_config SET tenant_id='',tenant_name='',choices_json='{}',choices_at=0,last_test_at='' WHERE id=1")
            db().commit()
            flash('Xero authorised. Select the organisation for rental invoices.', 'success')
        except XeroError as exc:
            db().rollback()
            flash(str(exc), 'error')
        return go_settings()

    @app.post('/binflow/xero/organisation')
    @require('admin')
    def bf_xero_organisation():
        try:
            selected = next((r for r in connections(app, db()) if r['id'] == request.form.get('tenant_id')), None)
            if not selected:
                raise XeroError('Choose an organisation authorised in Xero.')
            if config(db())['tenant_id']:
                raise XeroError('Disconnect before choosing a different organisation.')
            org = organisation(app, db(), selected['id'])
            db().execute('UPDATE bf_xero_config SET tenant_id=?,tenant_name=?,last_test_at=? WHERE id=1',
                         (selected['id'], str(org.get('Name') or selected['name'])[:200], now()))
            db().commit()
            flash('Xero connection verified. You can prepare a rental draft.', 'success')
        except XeroError as exc:
            flash(str(exc), 'error')
        return go_settings()

    @app.post('/binflow/xero/test')
    @require('admin')
    def bf_xero_test():
        try:
            organisation(app, db())
            db().execute('UPDATE bf_xero_config SET last_test_at=? WHERE id=1', (now(),))
            db().commit()
            flash('Xero connection test passed. Organisation read successfully; no invoice was created.', 'success')
        except XeroError as exc:
            flash(str(exc), 'error')
        return go_settings()

    @app.post('/binflow/xero/disconnect')
    @require('admin')
    def bf_xero_disconnect():
        db().execute("""UPDATE bf_xero_config SET token_cipher=NULL,expires_at=0,tenant_id='',tenant_name='',
            choices_json='{}',choices_at=0,last_test_at='',revision=revision+1 WHERE id=1""")
        db().execute('DELETE FROM bf_xero_oauth')
        db().commit()
        session.pop('xero_state', None)
        flash('BinFlow disconnected. Existing Xero invoices are retained.', 'success')
        return go_settings()

    @app.get('/binflow/rental-invoices')
    @require('admin')
    def bf_rental_invoices():
        cfg = config(db())
        opts, error = json.loads(cfg['choices_json']), None
        if cfg['tenant_id'] and cfg['token_cipher'] and (not opts or request.args.get('refresh') == 'yes'):
            try:
                opts = choices(app, db(), request.args.get('contact_search', 'Blocktexx').strip()[:100])
            except XeroError as exc:
                error = str(exc)
        stamp = date.fromisoformat(now()[:10])
        history = db().execute('SELECT id,reference,period_start,period_end,mode,status,invoice_number FROM bf_rental_invoices ORDER BY created_at DESC,rowid DESC LIMIT 30').fetchall()
        return render('rental_invoices', section='rental', connected=bool(cfg['tenant_id'] and cfg['token_cipher']),
                      tenant_name=cfg['tenant_name'], options=opts, error=error, history=history,
                      period_start=(stamp - timedelta(days=13)).isoformat(), period_end=stamp.isoformat(),
                      sizes=db().execute('SELECT * FROM bf_sizes WHERE weekly_rental_cents>0 ORDER BY litres,id').fetchall(),
                      billable=report(db())['totals']['billable_count'])

    @app.post('/binflow/rental-invoices/preview')
    @require('admin')
    def bf_rental_invoice_preview():
        try:
            db().execute('BEGIN IMMEDIATE')
            cfg = config(db())
            if not cfg['token_cipher']:
                raise XeroError('Connect to Xero first.')
            snapshot = prepare_snapshot(db(), request.form)
            identifier = str(uuid4())
            reference = ('BF-TEST-' if snapshot['mode'] == 'test' else 'BF-RENT-') + identifier
            payload = invoice_payload(snapshot, reference, date.fromisoformat(now()[:10]))
            # Prevent overlapping actual bills, including two simultaneous submissions.
            if snapshot['mode'] == 'actual' and db().execute("""SELECT 1 FROM bf_rental_invoices
                WHERE tenant_id=? AND mode='actual' AND status!='cancelled' AND period_start<=? AND period_end>=?""",
                (cfg['tenant_id'], snapshot['end'], snapshot['start'])).fetchone():
                raise XeroError('An actual rental invoice already covers some or all of this period. Open it from the invoice history.')
            db().execute('''INSERT INTO bf_rental_invoices(id,tenant_id,tenant_name,period_start,period_end,mode,status,
                reference,snapshot_json,payload_json,created_by,created_at) VALUES(?,?,?,?,?,?,'preview',?,?,?,?,?)''',
                (identifier, cfg['tenant_id'], cfg['tenant_name'], snapshot['start'], snapshot['end'], snapshot['mode'],
                 reference, json.dumps(snapshot), json.dumps(payload), actor(), now()))
            db().commit()
            return redirect(url_for('bf_rental_invoice', identifier=identifier))
        except (XeroError, sqlite3.IntegrityError) as exc:
            db().rollback()
            flash(str(exc) if isinstance(exc, XeroError) else 'This rental period already has an invoice preview.', 'error')
            return redirect(url_for('bf_rental_invoices'))

    @app.get('/binflow/rental-invoices/<identifier>')
    @require('admin')
    def bf_rental_invoice(identifier):
        item = saved_invoice(identifier)
        snapshot = json.loads(item['snapshot_json'])
        payload = json.loads(item['payload_json'])['Invoices'][0]
        return render('rental_invoice', section='rental', invoice=item, snapshot=snapshot,
                      invoice_date=payload['Date'], due_date=payload['DueDate'],
                      total=snapshot['subtotal'] + snapshot['gst'])

    @app.post('/binflow/rental-invoices/<identifier>/create')
    @require('admin')
    def bf_rental_invoice_create(identifier):
        item = saved_invoice(identifier)
        try:
            if request.form.get('confirm_draft') != 'yes':
                raise XeroError('Confirm the recipient and amount before creating the Xero draft.')
            if config(db())['tenant_id'] != item['tenant_id']:
                raise XeroError('The Xero organisation changed. Reconnect the original organisation before exporting.')
            db().execute('BEGIN IMMEDIATE')
            claimed = db().execute("UPDATE bf_rental_invoices SET status='sending',attempted_at=? WHERE id=? AND status='preview'",
                                   (now(), identifier)).rowcount
            db().commit()
            if not claimed:
                raise XeroError('This export has already been attempted. Check its saved result below.')
            try:
                result = api(app, db(), 'Invoices', 'POST', json.loads(item['payload_json']), key=identifier, tenant_id=item['tenant_id'])
                invoices = records(result, 'Invoices')
                if len(invoices) != 1 or invoices[0].get('Status') != 'DRAFT' or invoices[0].get('HasErrors'):
                    raise XeroError('Xero did not confirm a draft. Check the Xero result before taking further action.')
                remote = invoices[0]
                iid = uuid_text(remote.get('InvoiceID'))
                db().execute("UPDATE bf_rental_invoices SET status='created',invoice_id=?,invoice_number=?,xero_status='DRAFT',error='' WHERE id=?",
                             (iid, str(remote.get('InvoiceNumber', ''))[:255], identifier))
                db().commit()
                flash('Test draft created in Xero. Keep it as a draft while checking the connection.' if item['mode'] == 'test'
                      else 'Draft created in Xero. Review and approve it in Xero when ready.', 'success')
            except XeroError as exc:
                db().execute("UPDATE bf_rental_invoices SET status='uncertain',error=? WHERE id=?", (str(exc), identifier))
                db().commit()
                raise
        except XeroError as exc:
            flash(str(exc), 'error')
        return redirect(url_for('bf_rental_invoice', identifier=identifier))

    @app.post('/binflow/rental-invoices/<identifier>/check')
    @require('admin')
    def bf_rental_invoice_check(identifier):
        item = saved_invoice(identifier)
        try:
            if item['status'] not in ('sending', 'uncertain', 'created'):
                raise XeroError('This invoice has not been exported.')
            result = api(app, db(), 'Invoices?' + urlencode({'where': 'Reference=="' + item['reference'] + '"'}), tenant_id=item['tenant_id'])
            matches = [x for x in records(result, 'Invoices') if x.get('Reference') == item['reference'] and x.get('Type') == 'ACCREC']
            if len(matches) != 1:
                raise XeroError('No unique Xero invoice was found. Check Xero manually; automatic resending remains blocked to prevent duplicates.')
            remote = matches[0]
            db().execute("UPDATE bf_rental_invoices SET status='created',invoice_id=?,invoice_number=?,xero_status=?,error='' WHERE id=?",
                         (uuid_text(remote.get('InvoiceID')), str(remote.get('InvoiceNumber', ''))[:255], str(remote.get('Status', ''))[:30], identifier))
            db().commit()
            flash('Existing Xero invoice found. Its reference is recorded below.', 'success')
        except XeroError as exc:
            flash(str(exc), 'error')
        return redirect(url_for('bf_rental_invoice', identifier=identifier))

    @app.post('/binflow/rental-invoices/<identifier>/cancel')
    @require('admin')
    def bf_rental_invoice_cancel(identifier):
        saved_invoice(identifier)
        changed = db().execute("UPDATE bf_rental_invoices SET status='cancelled' WHERE id=? AND status='preview'", (identifier,)).rowcount
        db().commit()
        flash('Unsent preview cancelled.' if changed else 'Only an unsent preview can be cancelled here.', 'success' if changed else 'error')
        return redirect(url_for('bf_rental_invoice', identifier=identifier))
