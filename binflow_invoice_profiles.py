"""Saved invoice settings and commercial terms used by the booking and invoice screens."""
from binflow_freight_rates import VEHICLES, current_fuel, hourly_rate, rigid_quote
from binflow_sizes import amount


def migrate(conn):
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS bf_invoice_defaults (
            tenant TEXT PRIMARY KEY, account_code TEXT NOT NULL, tax_code TEXT NOT NULL,
            rental_account INTEGER REFERENCES bf_client_accounts(id));
        INSERT OR IGNORE INTO bf_invoice_defaults(tenant,account_code,tax_code,rental_account)
            SELECT tenant,account_code,tax_code,rental_account FROM bf_billing_defaults WHERE tenant!='';
        CREATE TABLE IF NOT EXISTS bf_office_billing_clients (
            portal TEXT NOT NULL, booking_name TEXT NOT NULL COLLATE NOCASE,
            account_id INTEGER NOT NULL REFERENCES bf_client_accounts(id), PRIMARY KEY(portal,booking_name));
        CREATE TABLE IF NOT EXISTS bf_recurring_freight_terms (
            account_id INTEGER NOT NULL REFERENCES bf_client_accounts(id), portal TEXT NOT NULL,
            origin TEXT NOT NULL, destination TEXT NOT NULL, vehicle TEXT NOT NULL, job_type TEXT NOT NULL,
            unit_cents INTEGER NOT NULL CHECK(unit_cents>0), fuel_bps INTEGER NOT NULL CHECK(fuel_bps>=0),
            PRIMARY KEY(account_id,portal,origin,destination,vehicle,job_type));
    """)


def defaults(conn, tenant):
    return conn.execute('SELECT * FROM bf_invoice_defaults WHERE tenant=?', (tenant,)).fetchone()


def ledger(options, saved):
    code = saved['account_code'] if saved else None
    tax_code = saved['tax_code'] if saved else None
    account = next((a for a in options.get('accounts', []) if a['code'] == code), None)
    tax = next((t for t in options.get('taxes', []) if t['code'] == tax_code and float(t['rate']) == 10), None)
    return account, tax


def save_defaults(conn, tenant, options, account_code, tax_code, rental_account):
    account, tax = ledger(options, dict(account_code=account_code, tax_code=tax_code))
    if not tenant or not account or not tax:
        raise ValueError('Choose a current sales account and 10% sales GST code for this Xero organisation.')
    conn.execute("""INSERT INTO bf_invoice_defaults VALUES(?,?,?,?) ON CONFLICT(tenant) DO UPDATE SET
        account_code=excluded.account_code,tax_code=excluded.tax_code,rental_account=excluded.rental_account""",
        (tenant, account_code, tax_code, rental_account))


def linked_contact(conn, tenant, aid):
    return conn.execute("""SELECT c.* FROM bf_invoice_contacts c JOIN bf_client_accounts a ON a.id=c.account_id
        WHERE c.tenant_id=? AND c.account_id=? AND a.active=1""", (tenant, aid)).fetchone()


def save_contact(conn, tenant, aid, options, cid):
    contact = next((c for c in options.get('contacts', []) if c['id'] == cid), None)
    if not contact:
        raise ValueError('Choose a billing contact from the current Xero directory.')
    conn.execute("""INSERT INTO bf_invoice_contacts VALUES(?,?,?,?) ON CONFLICT(tenant_id,account_id)
        DO UPDATE SET contact_id=excluded.contact_id,contact_name=excluded.contact_name""",
        (tenant, aid, contact['id'], contact['name']))


def lane(b):
    from binflow_interstate import PLACES
    origin = PLACES.get(b['origin'])
    destination = PLACES.get(b['destination'])
    hourly = b['vehicle'] == 'RIGID' and b['rigid_hours_units'] is not None
    if origin and destination and (hourly or b['vehicle'] in VEHICLES):
        return origin[1], destination[1], b['vehicle'], b['job_type']
    return None


def price_from_profile(conn, b, aid):
    route = lane(b)
    if not aid or not route:
        return None
    origin, dest, vehicle, _ = route
    hourly = vehicle == 'RIGID'
    if hourly:
        unit = hourly_rate(conn, aid)
    else:
        matrix = conn.execute("""SELECT cents FROM bf_interstate_rates WHERE account_id=? AND origin=?
            AND destination=? AND vehicle=?""", (aid, origin, dest, vehicle)).fetchone()
        unit = matrix['cents'] if matrix else None
    if unit is not None:
        fuel = current_fuel(conn)['bps']
    else:
        terms = conn.execute("""SELECT unit_cents,fuel_bps FROM bf_recurring_freight_terms
            WHERE account_id=? AND portal=? AND origin=? AND destination=? AND vehicle=? AND job_type=?""",
            (aid, b['portal'], *route)).fetchone()
        if not terms:
            return None
        unit, fuel = terms['unit_cents'], terms['fuel_bps']
    if hourly:
        _, quote = rigid_quote(unit, b['rigid_hours_units'], fuel)
        unit = quote['base']
    return unit, fuel


def billing_terms(conn, b, selected=None):
    aid, rate, fuel = b['account_id'], b['rate_cents'], b['fuel_bps']
    # Most client bookings already carry their approved payer and price.
    if aid is None or rate is None:
        frozen = conn.execute("""SELECT f.* FROM bf_auto_transfer_terms f WHERE booking_id=? AND booking_version=?
            AND (? IS NULL OR f.account_id=?)""", (b['id'], b['version'], aid, aid)).fetchone()
        if frozen:
            aid = aid or frozen['account_id']
            if rate is None:
                rate, fuel = frozen['rate_cents'], frozen['fuel_bps']
    if aid is None:
        routing = conn.execute("""SELECT m.account_id FROM bf_office_billing_clients m JOIN bf_client_accounts a
            ON a.id=m.account_id AND a.active=1 WHERE m.portal=? AND m.booking_name=?""",
            (b['portal'], b['client_name'].strip())).fetchone()
        aid = routing['account_id'] if routing else selected
    if rate is None:
        profile = price_from_profile(conn, b, aid)
        if profile:
            rate, fuel = profile
    return aid, rate, fuel


def save_terms(conn, b, aid, form, stamp, actor, *, edit=False):
    if b['account_id'] is not None and b['account_id'] != aid:
        raise ValueError('The booking already has a different billing client.')
    if b['cancelled_at'] or not b['locked_at'] or conn.execute(
            "SELECT 1 FROM bf_rental_invoices WHERE booking_id=? AND status!='cancelled'", (b['id'],)).fetchone():
        raise ValueError('Only a confirmed, unbilled transfer can be updated.')
    resolved, rate, fuel = billing_terms(conn, b, aid)
    if resolved != aid:
        if not edit:
            raise ValueError('The billing client changed. Reopen this booking’s billing details.')
        agreed = price_from_profile(conn,b,aid) if b['rate_cents'] is None else None
        rate, fuel = agreed if agreed else (b['rate_cents'], b['fuel_bps'])
    if rate is None or (edit and b['rate_cents'] is None and form.get('base')):
        unit = amount(form.get('base'), 'Agreed freight price', 1000000, 100)
        fuel = amount(form.get('fuel'), 'Agreed fuel percentage', 100, 100)
        if not unit:
            raise ValueError('Enter a positive freight price.')
        route = lane(b)
        hourly = form.get('price_basis') == 'hourly'
        if hourly and (not route or route[2] != 'RIGID'):
            raise ValueError('Hourly pricing requires a local RIGID booking with requested hours.')
        rate = rigid_quote(unit, b['rigid_hours_units'], fuel)[1]['base'] if hourly else unit
        if form.get('remember_rate') == 'yes':
            if not route or route[2]=='RIGID' and not hourly:
                raise ValueError('Choose a valid route and vehicle to save recurring freight terms.')
            conn.execute("""INSERT INTO bf_recurring_freight_terms VALUES(?,?,?,?,?,?,?,?)
                ON CONFLICT(account_id,portal,origin,destination,vehicle,job_type) DO UPDATE SET
                unit_cents=excluded.unit_cents, fuel_bps=excluded.fuel_bps""", (aid,b['portal'],*route,unit,fuel))
    if not rate:
        raise ValueError('Review the confirmed freight price before billing this load.')
    if b['account_id'] is None and form.get('remember_client') == 'yes':
        conn.execute("""INSERT INTO bf_office_billing_clients VALUES(?,?,?) ON CONFLICT(portal,booking_name)
            DO UPDATE SET account_id=excluded.account_id""", (b['portal'],b['client_name'].strip(),aid))
    conn.execute("""INSERT OR REPLACE INTO bf_auto_transfer_terms VALUES(?,?,?,?,?,?,?)""",
                 (b['id'], aid, rate, fuel, b['version'], stamp, actor))


def previous_manual_choices(conn, tenant):
    """Reuse selections from saved drafts only for the same payer, contact and Xero organisation."""
    rows = conn.execute("""SELECT portal_account_id,
        json_extract(snapshot_json,'$.account.code') AS account_code,
        json_extract(snapshot_json,'$.tax.code') AS tax_code FROM (
            SELECT i.portal_account_id,i.snapshot_json,ROW_NUMBER() OVER (PARTITION BY i.portal_account_id ORDER BY i.created_at DESC,i.rowid DESC) AS pos
            FROM bf_rental_invoices i JOIN bf_invoice_contacts c
              ON c.account_id=i.portal_account_id AND c.tenant_id=i.tenant_id
              AND c.contact_id=json_extract(i.snapshot_json,'$.contact.id')
            WHERE i.tenant_id=? AND i.mode='actual' AND i.status IN ('preview','created') AND i.category_id!=1
              AND NOT EXISTS (SELECT 1 FROM bf_auto_drafts d WHERE d.id=i.id)
        ) WHERE pos=1""", (tenant,))
    return {r['portal_account_id']:dict(r) for r in rows}
