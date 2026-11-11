"""Client freight pricing. Monetary amounts are AUD cents ex GST, percentages are basis points."""
import hashlib
import json
from itertools import combinations

from flask import abort, flash, g, redirect, render_template, request, url_for
from binflow_collections import STATES
from binflow_sizes import amount

VEHICLES = ('SEMI TRAILER', 'B-DOUBLE')
MIN_RIGID_HOURS = 4
# Place each return directly under its outbound route, starting with NSW to QLD.
ROUTE_STATES = ('NSW', *(state for state in STATES if state != 'NSW'))
ROUTES = tuple(route for a, b in combinations(ROUTE_STATES, 2) for route in ((a, b), (b, a)))
MATRIX_VEHICLES = ('B-DOUBLE', 'SEMI TRAILER')


def matrix_version(saved, hourly):
    return hashlib.sha256(json.dumps([saved, hourly], sort_keys=True).encode()).hexdigest()


def migrate(conn):
    conn.execute("""CREATE TABLE IF NOT EXISTS bf_interstate_rates (
        account_id INTEGER NOT NULL REFERENCES bf_client_accounts(id), origin TEXT NOT NULL,destination TEXT NOT NULL,
        vehicle TEXT NOT NULL,cents INTEGER NOT NULL CHECK(cents>=0),PRIMARY KEY(account_id,origin,destination,vehicle))""")
    conn.execute("""CREATE TABLE IF NOT EXISTS bf_rigid_hourly_rates (
        account_id INTEGER PRIMARY KEY REFERENCES bf_client_accounts(id), cents INTEGER NOT NULL CHECK(cents>=0))""")
    conn.execute("""CREATE TABLE IF NOT EXISTS bf_fuel_changes (
        id INTEGER PRIMARY KEY, bps INTEGER NOT NULL CHECK(bps>=0), changed_at TEXT NOT NULL, changed_by TEXT NOT NULL)""")
    columns = {row['name'] for row in conn.execute('PRAGMA table_info(bf_interstate_bookings)')}
    if 'vehicle' not in columns:
        conn.execute("ALTER TABLE bf_interstate_bookings ADD COLUMN vehicle TEXT NOT NULL DEFAULT ''")
    if 'rate_cents' not in columns:
        conn.execute('ALTER TABLE bf_interstate_bookings ADD COLUMN rate_cents INTEGER')
    if 'fuel_bps' not in columns:
        conn.execute('ALTER TABLE bf_interstate_bookings ADD COLUMN fuel_bps INTEGER NOT NULL DEFAULT 0 CHECK(fuel_bps>=0)')


def current_fuel(conn):
    row = conn.execute('SELECT * FROM bf_fuel_changes ORDER BY id DESC LIMIT 1').fetchone()
    return row or dict(id=0, bps=0, changed_at=None, changed_by='')


def quote(base, bps):
    if base is None:
        return None
    fuel = (base * bps + 5000) // 10000
    return dict(base=base, bps=bps, fuel=fuel, total=base+fuel)


def hourly_rate(conn, account_id):
    row = conn.execute('SELECT cents FROM bf_rigid_hourly_rates WHERE account_id=?', (account_id,)).fetchone()
    return row['cents'] if row else None


def rigid_quote(hourly, hours_hundredths, bps):
    billed = max(100 * MIN_RIGID_HOURS, hours_hundredths)
    base = (hourly * billed + 50) // 100 if hourly is not None else None
    return billed, quote(base, bps)


def rates_for(conn, account_id):
    rows = conn.execute("""SELECT r.origin,r.destination,r.vehicle,r.cents,
        COALESCE((SELECT bps FROM bf_fuel_changes ORDER BY id DESC LIMIT 1),0) AS bps
        FROM bf_interstate_rates r WHERE r.account_id=? AND r.vehicle IN (?,?)""", (account_id,*VEHICLES))
    return {f"{r['origin']}:{r['destination']}:{r['vehicle']}":quote(r['cents'],r['bps']) for r in rows}


def register(app, db, require, render, client_access, now, actor):
    @app.route('/binflow/interstate-transfers/access/fuel', methods=['GET','POST'])
    @require('admin')
    def bf_weekly_fuel():
        conn = db()
        if request.method == 'POST':
            conn.execute('BEGIN IMMEDIATE')
        setting = current_fuel(conn)
        value = f"{setting['bps']/100:.2f}"
        error = None
        if request.method == 'POST':
            if request.form.get('version', type=int) != setting['id']:
                abort(409, 'The weekly surcharge changed. Reload before saving.')
            value = request.form.get('percent','').strip()
            try:
                bps = amount(value, 'Fuel surcharge (%)', 10000, 100)
                if bps != setting['bps'] or not setting['id']:
                    conn.execute('INSERT INTO bf_fuel_changes(bps,changed_at,changed_by) VALUES(?,?,?)', (bps,now(),actor()))
                conn.commit()
                flash('Weekly fuel surcharge saved for all clients.', 'success')
                return redirect(url_for('bf_weekly_fuel'), code=303)
            except ValueError as exc:
                conn.rollback()
                error = str(exc)
        return render('weekly_fuel', section='interstate', setting=setting, value=value, error=error), 400 if error else 200

    @app.get('/binflow/client/local-rigid/quote')
    @client_access
    def bf_client_rigid_quote():
        hours = request.args.get('hours',str(MIN_RIGID_HOURS))
        try:
            units = amount(hours, 'Hours', 10000, 100)
        except ValueError as exc:
            abort(400,str(exc))
        hourly = hourly_rate(db(), g.bf_client['id'])
        billed, price = rigid_quote(hourly, units, current_fuel(db())['bps'])
        return render_template('binflow/rigid_quote.html', account=g.bf_client, hours=hours,
                               minimum=MIN_RIGID_HOURS, hourly=hourly, billed=billed, price=price)

    @app.route('/binflow/interstate-transfers/access/<int:aid>/rates',methods=['GET','POST'])
    @require('admin')
    def bf_interstate_rates(aid):
        conn = db()
        account = conn.execute('SELECT id,name,username FROM bf_client_accounts WHERE id=?',(aid,)).fetchone()
        origin = request.args.get('origin','QLD')
        if not account:
            abort(404)
        if origin not in STATES:
            abort(400)
        if request.method == 'POST':
            conn.execute('BEGIN IMMEDIATE')
        rows = conn.execute("""SELECT origin,destination,vehicle,cents FROM bf_interstate_rates
            WHERE account_id=? AND vehicle IN (?,?)""", (aid,*MATRIX_VEHICLES))
        saved = {f"{r['origin']}:{r['destination']}:{r['vehicle']}":r['cents'] for r in rows}
        hourly = hourly_rate(conn,aid)
        version = matrix_version(saved,hourly)
        values = {key:f'{cents/100:.2f}' for key,cents in saved.items()}
        hourly_value = '' if hourly is None else f'{hourly/100:.2f}'
        error = None
        if request.method == 'POST':
            keys = [f'{a}:{b}:{v}' for a,b in ROUTES for v in MATRIX_VEHICLES]
            posted_names = {key:key for key in keys}
            expected_version = version
            # A single-state form opened before deployment can still save its original rows.
            if request.form.get('layout') != 'routes' and any(key.count(':')==1 for key in request.form):
                posted_names = {key:key.split(':',1)[1] for key in keys if key.startswith(origin+':')}
                previous = {name:saved[key] for key,name in posted_names.items() if key in saved}
                expected_version = matrix_version(previous,hourly)
            if request.form.get('version') != expected_version:
                abort(409,'This rate card has changed. Reload the matrix before saving.')
            try:
                if not all(name in request.form for name in posted_names.values()):
                    raise ValueError('Some rate fields are missing. Reload the matrix before saving.')
                values.update({key:request.form[name].strip() for key,name in posted_names.items()})
                hourly_value = request.form.get('rigid_hourly',hourly_value).strip()
                updated_hourly = amount(hourly_value,'Local RIGID hourly rate',1000000,100,optional=True)
                upserts, removals = [], []
                for key in posted_names:
                    pickup,delivery,vehicle = key.split(':')
                    cents = amount(values[key],f'{pickup} → {delivery} {vehicle}',1000000,100,optional=True)
                    if cents == saved.get(key):
                        continue
                    route = (aid,pickup,delivery,vehicle)
                    if cents is None:
                        removals.append(route)
                    else:
                        upserts.append((*route,cents))
                conn.executemany("""INSERT INTO bf_interstate_rates(account_id,origin,destination,vehicle,cents)
                    VALUES(?,?,?,?,?) ON CONFLICT(account_id,origin,destination,vehicle)
                    DO UPDATE SET cents=excluded.cents""",upserts)
                conn.executemany("""DELETE FROM bf_interstate_rates
                    WHERE account_id=? AND origin=? AND destination=? AND vehicle=?""",removals)
                if updated_hourly is None:
                    conn.execute('DELETE FROM bf_rigid_hourly_rates WHERE account_id=?',(aid,))
                else:
                    conn.execute("""INSERT INTO bf_rigid_hourly_rates VALUES(?,?) ON CONFLICT(account_id)
                        DO UPDATE SET cents=excluded.cents""",(aid,updated_hourly))
                conn.commit()
                flash(f'Pricing matrix saved for {account["name"]}.','success')
                return redirect(url_for('bf_interstate_rates',aid=aid),code=303)
            except ValueError as exc:
                conn.rollback()
                error = str(exc)
        setting = current_fuel(conn)
        _, minimum_price = rigid_quote(hourly,0,setting['bps'])
        prices = {key:quote(base,setting['bps']) for key,base in saved.items()}
        return render('interstate_rates',section='interstate',client=account,routes=ROUTES,
                      vehicles=MATRIX_VEHICLES,values=values,version=version,error=error,fuel=setting,prices=prices,
                      hourly_value=hourly_value,minimum=MIN_RIGID_HOURS,minimum_price=minimum_price),400 if error else 200
