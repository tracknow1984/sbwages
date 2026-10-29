"""Persistent national bin inventory, exchange dockets and warehouse custody."""
import csv
import io
import json
import os
import re
import secrets
import sqlite3
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from functools import wraps
from urllib.parse import urlparse
from xml.sax.saxutils import escape
from zoneinfo import ZoneInfo

from flask import abort, flash, g, redirect, render_template, request, send_file, session, url_for
from prestart_media import photo_jpeg, signature_png
from binflow_driver import migrate_driver, register_driver
from binflow_permissions import (DRIVER_ENDPOINTS, is_driver, is_manager, is_office, can_view_job,
    has_binflow_access, state_scope, eligible_driver, available_drivers)
from binflow_runs import migrate_runs, register_runs, guard_movement, check_owner
from binflow_gps import migrate_gps, register_gps
from binflow_portal import migrate_portal, register_portal
from binflow_pods import ensure_pod_available
from binflow_qr import migrate_qr, register_qr, link_request, reopen_requests, pickup_requests, approve_requests, request_queue
from binflow_workday import migrate_workdays, register_workdays, driver_today
from binflow_tares import apply_standard_tares
from binflow_sizes import migrate as migrate_sizes, save as save_size, register as register_sizes
from binflow_approvals import migrate_approvals, register_approvals, ensure_approved, allocation_history
from binflow_collections import migrate_collections, populate_collection_profiles, register_collections, planning_resources
from binflow_acceptance import migrate_acceptance, register_acceptance, ensure_run_unstarted, schedule_changed
from binflow_requirements import migrate_requirements, populate_requirements, site_plan, register_requirements
from binflow_allocation_import import migrate as migrate_allocation_import, replace_from_workbook, replace_pallecons
from binflow_route_calendar import migrate as migrate_route_calendar, rebuild as rebuild_calendar
from binflow_schedule import migrate as migrate_schedule, register as register_schedule
from binflow_client_profiles import (migrate as migrate_client_profiles, import_profiles, seed_locations, register_counts,
    register_profiles, for_location as client_profile_for_location, sync_location as sync_client_profile)
from binflow_import import SCHEMA as IMPORT_SCHEMA, IMPORT_ID, import_blocktexx, profiles as source_profiles

STATES = ('QLD', 'NSW', 'VIC', 'SA', 'WA', 'TAS', 'ACT', 'NT')
STATUSES = {'empty': 'Empty at warehouse', 'at_client': 'At client', 'full': 'Full / pickup requested',
            'transit': 'On truck / in transit', 'received': 'TO BE WEIGHED', 'weighed': 'Full stock in warehouse'}
JOB_STATUSES = {'scheduled': 'Scheduled', 'collected': 'On the road', 'received': 'Weighing', 'complete': 'Complete', 'cancelled': 'Cancelled'}
SCHEMA = '''
CREATE TABLE IF NOT EXISTS bf_clients (
 id INTEGER PRIMARY KEY, name TEXT NOT NULL COLLATE NOCASE UNIQUE, contact TEXT NOT NULL DEFAULT '', email TEXT NOT NULL DEFAULT '', phone TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS bf_locations (
 id INTEGER PRIMARY KEY, kind TEXT NOT NULL CHECK(kind IN ('warehouse','client')), client_id INTEGER REFERENCES bf_clients(id),
 name TEXT NOT NULL, state TEXT NOT NULL, address TEXT NOT NULL, created_at TEXT NOT NULL,
 CHECK((kind='warehouse' AND client_id IS NULL) OR (kind='client' AND client_id IS NOT NULL)), UNIQUE(kind,client_id,name)
);
CREATE TABLE IF NOT EXISTS bf_sizes (
 id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE, litres INTEGER NOT NULL CHECK(litres>0), default_tare_g INTEGER NOT NULL DEFAULT 0 CHECK(default_tare_g>=0)
);
CREATE UNIQUE INDEX IF NOT EXISTS bf_warehouse_name ON bf_locations(state,name) WHERE kind='warehouse';
CREATE TABLE IF NOT EXISTS bf_batches (
 request_key TEXT PRIMARY KEY, quantity INTEGER NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS bf_bins (
 id INTEGER PRIMARY KEY, code TEXT NOT NULL UNIQUE, token TEXT NOT NULL UNIQUE, size_id INTEGER NOT NULL REFERENCES bf_sizes(id),
 state TEXT NOT NULL, location_id INTEGER REFERENCES bf_locations(id), status TEXT NOT NULL CHECK(status IN ('empty','at_client','full','transit','received','weighed')),
 tare_g INTEGER NOT NULL CHECK(tare_g>=0), created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS bf_access (user_id INTEGER PRIMARY KEY REFERENCES users(id));
CREATE TABLE IF NOT EXISTS bf_jobs (
 id INTEGER PRIMARY KEY, code TEXT NOT NULL UNIQUE, request_key TEXT NOT NULL UNIQUE, site_id INTEGER NOT NULL REFERENCES bf_locations(id), warehouse_id INTEGER NOT NULL REFERENCES bf_locations(id),
 driver_id INTEGER REFERENCES users(id), scheduled_date TEXT NOT NULL, status TEXT NOT NULL CHECK(status IN ('scheduled','collected','received','complete','cancelled')),
 client_name TEXT NOT NULL, site_name TEXT NOT NULL, site_address TEXT NOT NULL, warehouse_name TEXT NOT NULL, state TEXT NOT NULL,
 vehicle TEXT NOT NULL DEFAULT '', notes TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL, created_by INTEGER NOT NULL REFERENCES users(id),
 collected_at TEXT, collected_by TEXT, received_at TEXT, completed_at TEXT, receiver_name TEXT NOT NULL DEFAULT '', confirmation TEXT NOT NULL DEFAULT '',
 confirmation_note TEXT NOT NULL DEFAULT '', signature BLOB, photo BLOB
);
CREATE TABLE IF NOT EXISTS bf_job_bins (
 id INTEGER PRIMARY KEY, job_id INTEGER NOT NULL REFERENCES bf_jobs(id), bin_id INTEGER NOT NULL REFERENCES bf_bins(id), direction TEXT NOT NULL CHECK(direction IN ('collect','deliver')),
 bin_code TEXT NOT NULL, size_name TEXT NOT NULL, tare_g INTEGER NOT NULL CHECK(tare_g>=0), gross_g INTEGER, net_g INTEGER,
 weighed_at TEXT, weighed_by TEXT, active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1)),
 UNIQUE(job_id,bin_id), CHECK(gross_g IS NULL OR gross_g>=tare_g), CHECK(net_g IS NULL OR net_g=gross_g-tare_g)
);
CREATE UNIQUE INDEX IF NOT EXISTS bf_one_reservation_per_bin ON bf_job_bins(bin_id) WHERE active=1;
CREATE INDEX IF NOT EXISTS bf_bins_location ON bf_bins(location_id,status);
CREATE INDEX IF NOT EXISTS bf_jobs_schedule ON bf_jobs(scheduled_date,status);
CREATE TABLE IF NOT EXISTS bf_events (
 id INTEGER PRIMARY KEY, bin_id INTEGER NOT NULL REFERENCES bf_bins(id), job_id INTEGER REFERENCES bf_jobs(id), event TEXT NOT NULL,
 from_location INTEGER REFERENCES bf_locations(id), to_location INTEGER REFERENCES bf_locations(id), actor TEXT NOT NULL, notes TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS bf_events_bin ON bf_events(bin_id,id);
'''


def now():
    return datetime.now(ZoneInfo('Australia/Brisbane')).isoformat(timespec='seconds')


def kg(value):
    return '—' if value is None else f'{value / 1000:,.3f}'.rstrip('0').rstrip('.')


def grams(raw, label='Weight'):
    try:
        value = Decimal(str(raw))
        if not value.is_finite() or value < 0 or value > 5000 or value * 1000 != (value * 1000).to_integral_value():
            raise ValueError()
        return int(value * 1000)
    except (InvalidOperation, ValueError, TypeError):
        raise ValueError(f'{label} must be 0–5,000 kg, with up to three decimal places.') from None


def register_binflow(app, db, require):
    from binflow_demo import install_demo_database
    db = install_demo_database(app, db, now)
    with app.app_context():
        db().executescript(SCHEMA)
        migrate_sizes(db())
        from binflow_xero import migrate as migrate_xero
        migrate_xero(db())
        migrate_driver(db())
        from binflow_bin_condition import migrate as migrate_bin_condition
        migrate_bin_condition(db())
        db().executescript(IMPORT_SCHEMA)
        migrate_requirements(db())
        migrate_runs(db())
        migrate_acceptance(db())
        migrate_workdays(db())
        migrate_gps(db())
        migrate_portal(db())
        migrate_qr(db())
        migrate_approvals(db())
        migrate_collections(db())
        migrate_client_profiles(db())
        from binflow_supplier_profiles import migrate as migrate_supplier_profiles
        migrate_supplier_profiles(db())
        migrate_allocation_import(db())
        migrate_route_calendar(db())
        migrate_schedule(db())
        from binflow_journey import migrate_journey
        migrate_journey(db())
        from binflow_reports import migrate_timings
        migrate_timings(db())
        from binflow_pod_records import migrate as migrate_pod_records
        migrate_pod_records(db())
        from binflow_bales import migrate as migrate_bales
        migrate_bales(db())
        db().commit()
        tare_update = apply_standard_tares(db(), now())
        if tare_update:
            app.logger.info('BinFlow standard empty weights applied: %s', json.dumps(tare_update))
        if os.environ.get('BINFLOW_IMPORT_BLOCKTEXX') == IMPORT_ID:
            summary, changed = import_blocktexx(db(), json.loads(os.environ.get('BINFLOW_BLOCKTEXX_GROUPS_JSON', '[]')), now())
            if summary:
                app.logger.warning('BinFlow Blocktexx register import %s: %s', 'completed' if changed else 'already complete', json.dumps(summary, sort_keys=True))
            else:
                app.logger.warning('BinFlow Blocktexx register import waiting: no populated saved source model.')
            plan_summary, changed = populate_requirements(db(), now())
            if plan_summary:
                app.logger.warning('BinFlow national bin allocation profiles %s: %s', 'populated' if changed else 'already populated', json.dumps(plan_summary, sort_keys=True))
        profile_payload = os.environ.get('BINFLOW_CLIENT_PROFILES_GZIP_B64', '')
        if profile_payload:
            profile_summary, changed = import_profiles(db(), profile_payload, now())
            app.logger.warning('BinFlow client site profiles %s: %s', 'imported' if changed else 'already imported', json.dumps(profile_summary))
        collection_summary=populate_collection_profiles(db(),now())
        if collection_summary:
            frequency_counts=[dict(r) for r in db().execute('''SELECT l.state,p.cadence,COUNT(*) AS sites,
                SUM(CASE WHEN p.start_date!='' AND (p.cadence='monthly' OR (p.cadence IN ('weekly','fortnightly','four_weekly','eight_weekly') AND p.service_days!='[]')) THEN 1 ELSE 0 END) AS dated
                FROM bf_collection_profiles p JOIN bf_locations l ON l.id=p.location_id GROUP BY l.state,p.cadence''')]
            app.logger.warning('BinFlow collection profiles imported: %s; calendar coverage: %s',json.dumps(collection_summary),json.dumps(frequency_counts))
        allocation_payload = os.environ.get('BINFLOW_ALLOCATION_WORKBOOK_GZIP_B64', '')
        if allocation_payload:
            allocation_summary, changed = replace_from_workbook(db(), allocation_payload, now())
            app.logger.warning('BinFlow workbook allocations %s: %s', 'replaced' if changed else 'already replaced', json.dumps(allocation_summary))
            app.logger.warning('BinFlow client register reconciliation: %s', json.dumps(register_counts(db())))
            app.logger.warning('BinFlow calendar operating settings: %s', json.dumps(planning_resources(db())))
            app.logger.warning('BinFlow future calendar jobs: %s', json.dumps([dict(r) for r in db().execute(
                "SELECT state,status,approval_status,COUNT(*) count FROM bf_jobs WHERE scheduled_date>='2026-10-26' AND status!='cancelled' GROUP BY state,status,approval_status")]))
            replacement, changed = replace_pallecons(db(), now())
            app.logger.warning('BinFlow pallecon replacement %s: %s', 'applied' if changed else 'already applied', json.dumps(replacement))
            calendar_summary, changed = rebuild_calendar(db(), now())
            app.logger.warning('BinFlow current client calendar %s: %s', 'rebuilt' if changed else 'already rebuilt', json.dumps(calendar_summary))
        from binflow_management import migrate as migrate_management, bootstrap
        migrate_management(db())
        db().commit()
        if not app.testing:
            bootstrap(db(), now())
    app.jinja_env.filters['bfkg'] = kg

    def allowed():
        return has_binflow_access(g.user)

    app.context_processor(lambda: dict(driver_demo=getattr(g, 'bf_demo_token', ''), driver_server_now=now(), binflow_allowed=allowed(), binflow_driver=is_driver(db(), g.user),
                                      binflow_manager=is_manager(g.user), binflow_state=state_scope(g.user)))

    def access(fn):
        @wraps(fn)
        def wrapped(*args, **kwargs):
            if not g.user:
                session['binflow_next'] = request.path
                return redirect(url_for('bf_signin'))
            if not allowed():
                abort(403, 'BINFLOW requires a DRIVER, MANAGER or CONTRACTOR profile with an allocated working state. Ask the office to update Staff details.')
            if not is_office(g.user):
                if fn.__name__ == 'bf_dashboard':
                    return redirect(url_for('bf_workday'))
                if fn.__name__ not in DRIVER_ENDPOINTS:
                    abort(403, 'This area is managed by the office. Use your driver menu to continue.')
            return fn(*args, **kwargs)
        return wrapped

    def actor():
        return (g.user['first_name'] + ' ' + g.user['last_name']).strip()

    def text_field(name, maximum=200, required=True):
        value = request.form.get(name, '').strip()
        if (required and not value) or len(value) > maximum:
            raise ValueError(f'{name.replace("_", " ").capitalize()} is required and must be under {maximum + 1} characters.' if required else f'{name} is too long.')
        return value

    def integer(name, maximum=1000000000):
        try:
            number = int(request.form.get(name, ''))
            if not 1 <= number <= maximum:
                raise ValueError()
            return number
        except (ValueError, TypeError):
            raise ValueError(f'Choose a valid {name.replace("_id", "").replace("_", " ")}.') from None

    def location(location_id, kind=None):
        row = db().execute('SELECT l.*,c.name AS client_name FROM bf_locations l LEFT JOIN bf_clients c ON c.id=l.client_id WHERE l.id=?', (location_id,)).fetchone()
        if not row or (kind and row['kind'] != kind):
            raise ValueError('Choose a valid client site or warehouse.')
        return row

    def event(bin_id, label, job_id=None, origin=None, target=None, notes='', from_truck=None, to_truck=None):
        from binflow_pod_records import label as custody_label
        db().execute('INSERT INTO bf_events(bin_id,job_id,event,from_location,to_location,actor,notes,created_at,from_truck,to_truck,from_label,to_label) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
                     (bin_id, job_id, label, origin, target, actor(), notes, now(), from_truck, to_truck, custody_label(db(),origin,from_truck), custody_label(db(),target,to_truck)))

    def job(job_id):
        row = db().execute('SELECT j.*,u.first_name||\' \'||u.last_name AS driver_name FROM bf_jobs j LEFT JOIN users u ON u.id=j.driver_id WHERE j.id=?', (job_id,)).fetchone()
        if not row:
            abort(404)
        if not can_view_job(db(), g.user, row):
            abort(403, 'This docket is not assigned or available to you.')
        return row

    def items(job_id):
        return db().execute('SELECT i.*,b.token FROM bf_job_bins i JOIN bf_bins b ON b.id=i.bin_id WHERE i.job_id=? ORDER BY i.direction,i.bin_code', (job_id,)).fetchall()

    def bins(where='1=1', args=()):
        scope = state_scope(g.user)
        if scope is not None:
            where, args = '(' + where + ') AND b.state=?', (*args, scope)
        return db().execute('''SELECT b.*,s.name AS size_name,COALESCE(t.name,l.name) AS location_name,l.kind,c.name AS client_name,
            t.name AS truck_name,COALESCE(ac.name,c.name) AS allocated_client_name,
            COALESCE(al.name,CASE WHEN l.kind='client' THEN l.name END) AS allocated_site_name,
            aj.approval_status AS allocation_approval,
            (SELECT i.job_id FROM bf_job_bins i WHERE i.bin_id=b.id AND i.active=1) AS reserved_job
            FROM bf_bins b JOIN bf_sizes s ON s.id=b.size_id LEFT JOIN bf_locations l ON l.id=b.location_id
            LEFT JOIN bf_clients c ON c.id=l.client_id LEFT JOIN bf_trucks t ON t.id=b.truck_id
            LEFT JOIN bf_job_bins ai ON ai.bin_id=b.id AND ai.active=1 AND ai.direction='deliver'
            LEFT JOIN bf_jobs aj ON aj.id=ai.job_id AND aj.status NOT IN ('cancelled','complete')
                AND aj.approval_status IN ('pending','approved')
            LEFT JOIN bf_locations al ON al.id=aj.site_id
            LEFT JOIN bf_clients ac ON ac.id=al.client_id WHERE ''' + where + ' ORDER BY b.id', args).fetchall()

    from binflow_guides import GUIDES
    app.context_processor(lambda: dict(bf_public_guide=GUIDES.get({
        "bf_client_login":"client_login", "bf_client_pods":"client_pods",
        "bf_qr_open":"qr_actions", "bf_qr_collection":"qr_collection"}.get(request.endpoint))))

    def render(name, **values):
        values.setdefault("bf_page_guide", GUIDES.get(name))
        if getattr(g,'bf_demo_token',None):
            values.setdefault('driver_mode',True)
            values.setdefault('journey_mode',True)
        scope = state_scope(g.user)
        return render_template('binflow/' + name + '.html', tab='binflow', states=(scope,) if scope else STATES, statuses=STATUSES,
                               job_statuses=JOB_STATUSES, **values)

    def success(message, endpoint, **values):
        db().commit()
        flash(message, 'success')
        return redirect(url_for(endpoint, **values))

    def failure(error, endpoint, **values):
        db().rollback()
        flash(str(error), 'error')
        return redirect(url_for(endpoint, **values))

    @app.get('/binflow/sign-in')
    def bf_signin():
        if g.user:
            return redirect(url_for('bf_workday' if is_driver(db(), g.user) else 'bf_dashboard'))
        return render('signin', section='')

    @app.get('/admin/bin-management')
    @access
    def bf_dashboard():
        if is_manager(g.user) and db().execute('SELECT 1 FROM bf_bale_managers WHERE user_id=?',(g.user['id'],)).fetchone():
            return redirect(url_for('bf_bales'))
        state = request.args.get('state', '')
        if state and state not in STATES:
            abort(400)
        where, args = ('b.state=?', (state,)) if state else ('1=1', ())
        stock = bins(where, args)
        counts = {key: sum(b['status'] == key for b in stock) for key in STATUSES}
        sizes = db().execute('SELECT * FROM bf_sizes ORDER BY litres').fetchall()
        state_rows=[]
        for area in STATES:
            if state and state!=area: continue
            for size in sizes:
                group=[b for b in stock if b['state']==area and b['size_id']==size['id']]
                state_rows.append(dict(state=area,size_id=size['id'],size_name=size['name'],total=len(group),
                    available=sum(b['status']=='empty' and not b['reserved_job'] for b in group),
                    reserved=sum(b['status']=='empty' and bool(b['reserved_job']) for b in group),
                    on_site=sum(b['status'] in ('at_client','full') for b in group),
                    returning=sum(b['status']=='transit' for b in group),
                    warehouse_full=sum(b['status'] in ('received','weighed') for b in group)))
        by_size = [dict(name=s['name'], total=sum(b['size_id'] == s['id'] for b in stock), available=sum(b['size_id'] == s['id'] and b['status'] == 'empty' and not b['reserved_job'] for b in stock), at_client=sum(b['size_id'] == s['id'] and b['status'] in ('at_client','full') for b in stock)) for s in sizes]
        net = db().execute('SELECT COALESCE(SUM(i.net_g),0) FROM bf_job_bins i JOIN bf_jobs j ON j.id=i.job_id WHERE i.weighed_at IS NOT NULL' + (' AND j.state=?' if state else ''), args).fetchone()[0]
        return render('dashboard', section='overview', state=state, stock=stock, counts=counts, state_rows=state_rows, by_size=by_size, net=net,
                      request_dashboard=True,**request_queue(db(),state,request.args.get('requests','all'),limit=12),
                      client_count=db().execute('SELECT COUNT(*) FROM bf_clients').fetchone()[0], warehouse_count=db().execute("SELECT COUNT(*) FROM bf_locations WHERE kind='warehouse'").fetchone()[0])

    @app.route('/binflow/setup', methods=['GET', 'POST'])
    @require('admin')
    def bf_setup():
        if request.method == 'POST':
            try:
                kind = text_field('action')
                db().execute('BEGIN IMMEDIATE')
                if kind == 'client':
                    email = text_field('email', required=False)
                    if email and not re.fullmatch(r'[^\s@<>\r\n]+@[^\s@<>\r\n]+\.[^\s@<>\r\n]+', email):
                        raise ValueError('Enter a valid client email address.')
                    db().execute('INSERT INTO bf_clients(name,contact,email,phone,created_at) VALUES(?,?,?,?,?)', (text_field('name'), text_field('contact', required=False), email, text_field('phone', required=False), now()))
                elif kind in ('warehouse', 'site'):
                    state = text_field('state')
                    if state not in STATES:
                        raise ValueError('Choose an Australian state or territory.')
                    client_id = integer('client_id') if kind == 'site' else None
                    if client_id and not db().execute('SELECT 1 FROM bf_clients WHERE id=?', (client_id,)).fetchone():
                        raise ValueError('Choose a valid client.')
                    db().execute('INSERT INTO bf_locations(kind,client_id,name,state,address,created_at) VALUES(?,?,?,?,?,?)', ('client' if kind == 'site' else kind, client_id, text_field('name'), state, text_field('address', 500), now()))
                elif kind == 'size':
                    save_size(db(), request.form, now(), actor())
                    return success('Bin size and pricing added.', 'bf_setup', _anchor='bin-sizes')
                elif kind == 'access':
                    raise ValueError('Set each employee’s business role and working state in Staff details. The old access checkbox is no longer used.')
                else:
                    raise ValueError('Unknown action.')
                return success('Saved.', 'bf_setup')
            except sqlite3.IntegrityError:
                return failure(ValueError('This record already exists. Please use a different name.'), 'bf_setup')
            except ValueError as error:
                return failure(error, 'bf_setup')
        seed_locations(db(), now())
        db().commit()
        imported = db().execute('SELECT * FROM bf_import_runs WHERE id=?', (IMPORT_ID,)).fetchone()
        return render('setup', section='setup', import_summary=json.loads(imported['summary']) if imported else None,
                      source_locations={row['location_id']: row['kind'] for row in db().execute('SELECT location_id,kind FROM bf_location_sources')},
                      client_profiles={r['location_id']: r['id'] for r in db().execute('SELECT id,location_id FROM bf_client_site_profiles WHERE location_id IS NOT NULL')},
                      clients=db().execute('SELECT * FROM bf_clients ORDER BY name').fetchall(),
                      locations=db().execute('SELECT l.*,c.name AS client_name FROM bf_locations l LEFT JOIN bf_clients c ON c.id=l.client_id ORDER BY l.kind DESC,l.state,l.name').fetchall(),
                      sizes=db().execute('SELECT * FROM bf_sizes ORDER BY litres').fetchall(),
                      staff=db().execute("SELECT u.*,a.user_id AS access FROM users u LEFT JOIN bf_access a ON a.user_id=u.id WHERE u.role='employee' AND u.status='active' ORDER BY first_name,last_name").fetchall())

    @app.route('/binflow/locations/<int:location_id>/edit', methods=['GET', 'POST'])
    @require('admin')
    def bf_edit_location(location_id):
        if request.method == 'POST':
            db().execute('BEGIN IMMEDIATE')
        record = db().execute('SELECT l.*,c.name AS client_name FROM bf_locations l LEFT JOIN bf_clients c ON c.id=l.client_id WHERE l.id=?', (location_id,)).fetchone()
        if not record:
            abort(404)
        state_locked = bool(db().execute('''SELECT EXISTS(SELECT 1 FROM bf_bins WHERE location_id=?)
            OR EXISTS(SELECT 1 FROM bf_jobs WHERE site_id=? OR warehouse_id=?)''', (location_id, location_id, location_id)).fetchone()[0])
        values = dict(record)
        error = None
        if request.method == 'POST':
            values.update({key: request.form.get(key, '').strip() for key in ('name', 'state', 'address')})
            try:
                name, state, address = text_field('name'), text_field('state'), text_field('address', 500)
                if state not in STATES:
                    raise ValueError('Choose an Australian state or territory.')
                if state != record['state'] and state_locked:
                    raise ValueError('This location has linked bins or jobs, so its state cannot be changed. Add a new location for another state.')
                db().execute('UPDATE bf_locations SET name=?,state=?,address=? WHERE id=?', (name, state, address, location_id))
                if record['kind'] == 'client':
                    sync_client_profile(db(), dict(id=location_id,name=name,state=state,address=address), now(), actor())
                return success('Warehouse updated.' if record['kind'] == 'warehouse' else 'Client site updated.',
                               'bf_setup' if record['kind'] == 'warehouse' else 'bf_client_profiles')
            except sqlite3.IntegrityError:
                error = 'A location with this name already exists for this state or client. Please use a different name.'
            except ValueError as exc:
                error = str(exc)
            db().rollback()
        return render('location_edit', section='setup', record=record, values=values, state_locked=state_locked, error=error,
                      client_profile=client_profile_for_location(db(),location_id),
                      source_profiles=source_profiles(db(), location_id)), 400 if error else 200

    @app.route('/binflow/bins', methods=['GET', 'POST'])
    @access
    def bf_inventory():
        if request.method == 'POST':
            if g.user['role'] != 'admin':
                abort(403)
            try:
                key = text_field('request_key', 80)
                if not re.fullmatch(r'[A-Za-z0-9_-]{20,80}', key):
                    raise ValueError('Refresh the bin registration form before saving.')
                count = integer('quantity', 500)
                warehouse = location(integer('warehouse_id'), 'warehouse')
                size = db().execute('SELECT * FROM bf_sizes WHERE id=?', (integer('size_id'),)).fetchone()
                if not size:
                    raise ValueError('Choose a bin size.')
                supplied_tare = request.form.get('tare', '').strip()
                tare = grams(supplied_tare, 'Empty tare') if supplied_tare else size['default_tare_g']
                db().execute('BEGIN IMMEDIATE')
                if db().execute('SELECT 1 FROM bf_batches WHERE request_key=?', (key,)).fetchone():
                    return success('This batch has already been registered.', 'bf_inventory')
                start = db().execute('SELECT COALESCE(MAX(id),0)+1 FROM bf_bins').fetchone()[0]
                for bid in range(start, start + count):
                    db().execute('INSERT INTO bf_bins(id,code,token,size_id,state,location_id,status,tare_g,created_at) VALUES(?,?,?,?,?,?,\'empty\',?,?)',
                                 (bid, f'BF{bid:06d}', secrets.token_urlsafe(24), size['id'], warehouse['state'], warehouse['id'], tare, now()))
                    event(bid, 'Bin registered', target=warehouse['id'])
                db().execute('INSERT INTO bf_batches(request_key,quantity,created_at) VALUES(?,?,?)', (key,count,now()))
                return success(f'{count} bins registered. Each has its own permanent QR code.', 'bf_inventory', state=warehouse['state'])
            except ValueError as error:
                return failure(error, 'bf_inventory')
        clauses, args = [], []
        state, status, client, size_id, query = [request.args.get(k, '').strip() for k in ('state','status','client','size','q')]
        condition=request.args.get('condition','').strip()
        if condition:
            clauses.append('b.service_status=?');args.append(condition)
        if state:
            clauses.append('b.state=?'); args.append(state)
        if status:
            clauses.append('b.status=?'); args.append(status)
        if client:
            clauses.append('COALESCE(al.client_id,l.client_id)=CAST(? AS INTEGER)'); args.append(client)
        if size_id:
            clauses.append('b.size_id=?'); args.append(size_id)
        if query:
            clauses.append('(b.code LIKE ? OR l.name LIKE ? OR c.name LIKE ? OR al.name LIKE ? OR ac.name LIKE ?)'); args.extend(['%' + query[:100] + '%'] * 5)
        stock = bins(' AND '.join(clauses) or '1=1', args)
        return render('inventory', section='bins', stock=stock, state=state, status=status, client=client, size_id=size_id, condition=condition, query=query, request_key=secrets.token_urlsafe(24),
                      sizes=db().execute('SELECT * FROM bf_sizes ORDER BY litres').fetchall(), clients=db().execute('SELECT * FROM bf_clients ORDER BY name').fetchall(),
                      warehouses=db().execute("SELECT * FROM bf_locations WHERE kind='warehouse' ORDER BY state,name").fetchall())

    @app.get('/binflow/scan')
    @access
    def bf_scan_search():
        value = request.args.get('code', '').strip()
        if not value:
            return render('scan', section='bins')
        if value.startswith(('https://','http://')):
            value = urlparse(value).path.rstrip('/').split('/')[-1]
        row = db().execute('SELECT id,token FROM bf_bins WHERE code=? COLLATE NOCASE OR token=?', (value, value)).fetchone()
        if not row:
            flash('Bin not found. Scan its label or enter the BF number.', 'error')
            return render('scan', section='bins'), 404
        return redirect(url_for('bf_qr_open', token=row['token']))

    @app.route('/binflow/bins/<int:bin_id>', methods=['GET','POST'])
    @access
    def bf_bin(bin_id):
        stock = bins('b.id=?', (bin_id,))
        if not stock:
            abort(404)
        if request.method == 'POST':
            try:
                db().execute('BEGIN IMMEDIATE')
                b = bins('b.id=?', (bin_id,))[0]
                action = text_field('action')
                if action == 'full':
                    if b['status'] != 'at_client':
                        raise ValueError('Only a bin currently at a client can be marked full.')
                    db().execute("UPDATE bf_bins SET status='full' WHERE id=?", (bin_id,))
                    event(bin_id, 'Marked full / pickup requested', origin=b['location_id'], target=b['location_id'])
                elif action == 'empty':
                    if b['status'] != 'weighed' or b['reserved_job']:
                        raise ValueError('Weigh this bin before marking its contents processed and the bin empty.')
                    note = text_field('notes', 500)
                    db().execute("UPDATE bf_bins SET status='empty' WHERE id=?", (bin_id,))
                    event(bin_id, 'Contents processed; bin empty', origin=b['location_id'], target=b['location_id'], notes=note)
                elif action == 'tare':
                    if b['status'] != 'empty' or b['reserved_job']:
                        raise ValueError('Update tare only while the unreserved bin is empty at the warehouse.')
                    tare = grams(request.form.get('tare'), 'Empty tare')
                    db().execute('UPDATE bf_bins SET tare_g=? WHERE id=?', (tare, bin_id))
                    event(bin_id, 'Empty tare updated', notes=f'{kg(tare)} kg')
                else:
                    raise ValueError('Unknown bin action.')
                return success('Bin updated.', 'bf_bin', bin_id=bin_id)
            except ValueError as error:
                return failure(error, 'bf_bin', bin_id=bin_id)
        history = db().execute('SELECT e.*,COALESCE(ft.name,f.name) AS from_name,COALESCE(tt.name,t.name) AS to_name,j.code AS job_code FROM bf_events e LEFT JOIN bf_locations f ON f.id=e.from_location LEFT JOIN bf_locations t ON t.id=e.to_location LEFT JOIN bf_jobs j ON j.id=e.job_id LEFT JOIN bf_trucks ft ON ft.id=e.from_truck LEFT JOIN bf_trucks tt ON tt.id=e.to_truck WHERE e.bin_id=? ORDER BY e.id DESC', (bin_id,)).fetchall()
        current_item = db().execute("SELECT i.*,j.code,j.status AS job_status FROM bf_job_bins i JOIN bf_jobs j ON j.id=i.job_id WHERE i.bin_id=? AND i.active=1", (bin_id,)).fetchone()
        return render('bin', section='bins', b=stock[0], history=history, current_item=current_item)

    @app.route('/binflow/bins/<int:bin_id>/edit', methods=['GET', 'POST'])
    @require('admin')
    def bf_edit_bin(bin_id):
        if request.method == 'POST':
            db().execute('BEGIN IMMEDIATE')
        stock = bins('b.id=?', (bin_id,))
        if not stock:
            abort(404)
        b = stock[0]
        version = db().execute('SELECT COALESCE(MAX(id),0) FROM bf_events WHERE bin_id=?', (bin_id,)).fetchone()[0]
        blocked = bool(b['reserved_job'] or b['status'] in ('transit', 'received'))
        selected = request.form.get('location_id', str(b['location_id'] or ''))
        notes = request.form.get('notes', '')
        error = None
        if request.method == 'POST':
            try:
                if blocked:
                    raise ValueError('This bin is linked to an active movement. Complete or cancel its docket before editing its location.')
                target = location(integer('location_id'))
                if target['state'] != b['state']:
                    raise ValueError('Choose a location in this bin\'s state. Bin pools stay within each state.')
                if target['id'] == b['location_id']:
                    return success('This bin is already at the selected location.', 'bf_bin', bin_id=bin_id)
                if request.form.get('version') != str(version):
                    raise ValueError('This bin changed after you opened the form. Review its current location and status before saving again.')
                if target['kind'] == 'warehouse':
                    if b['status'] == 'full':
                        raise ValueError('Use a pickup docket to return full bins to a warehouse for weighing.')
                    if b['status'] == 'at_client' and request.form.get('confirm_empty') != 'yes':
                        raise ValueError('Confirm the bin is empty before returning it to warehouse empty stock. Use a pickup docket for full bins.')
                    status = 'weighed' if b['status'] == 'weighed' else 'empty'
                else:
                    if b['status'] == 'weighed':
                        raise ValueError('Record the contents as processed before allocating this bin to a client.')
                    status = 'full' if b['status'] == 'full' else 'at_client'
                note = text_field('notes', 500, False)
                db().execute('UPDATE bf_bins SET location_id=?,status=? WHERE id=?', (target['id'], status, bin_id))
                audit = f'{STATUSES[b["status"]]} → {STATUSES[status]}. {note}'.strip()
                if b['status'] == 'at_client' and target['kind'] == 'warehouse':
                    audit += ' Administrator confirmed the bin is empty.'
                event(bin_id, 'Location updated in physical bin register', origin=b['location_id'], target=target['id'], notes=audit)
                return success('Bin location updated.', 'bf_bin', bin_id=bin_id)
            except ValueError as exc:
                error = str(exc)
                db().rollback()
        choices = db().execute('''SELECT l.*,c.name AS client_name FROM bf_locations l
            LEFT JOIN bf_clients c ON c.id=l.client_id WHERE l.state=? ORDER BY l.kind DESC,c.name,l.name''', (b['state'],)).fetchall()
        return render('bin_edit', section='bins', b=b, version=version, blocked=blocked,
                      locations=choices, selected=selected, notes=notes, error=error), 400 if error else 200

    def qr_url(token):
        base = app.config['APP_URL'] or request.url_root.rstrip('/')
        return base + url_for('bf_qr_open', token=token)

    @app.get('/binflow/bins/<int:bin_id>/qr.png')
    @access
    def bf_qr_image(bin_id):
        import qrcode
        from qrcode.image.pil import PilImage
        b = db().execute('SELECT token FROM bf_bins WHERE id=?', (bin_id,)).fetchone()
        if not b:
            abort(404)
        qr = qrcode.QRCode(error_correction=qrcode.constants.ERROR_CORRECT_Q, box_size=8, border=4)
        qr.add_data(qr_url(b['token'])); qr.make(fit=True)
        output = io.BytesIO(); qr.make_image(image_factory=PilImage).save(output, format='PNG'); output.seek(0)
        return send_file(output, mimetype='image/png')

    @app.get('/binflow/labels')
    @access
    def bf_labels():
        raw = request.args.getlist('bin')
        if not raw or len(raw) > 200 or any(not v.isdigit() for v in raw):
            abort(400, 'Select 1–200 bins to print labels.')
        stock = bins('b.id IN (' + ','.join('?' for _ in raw) + ')', raw)
        return render('labels', section='bins', stock=stock)

    @app.get('/binflow/pickups')
    @access
    def bf_jobs():
        return redirect(url_for('bf_collection_calendar',state=request.args.get('state','')))

    @app.route('/binflow/pickups/new', methods=['GET','POST'])
    @access
    def bf_new_job():
        if request.method == 'POST':
            approve_schedule=request.form.get('approve_schedule')=='yes'
            if approve_schedule and g.user['role']!='admin': abort(403)
            try:
                if approve_schedule and not request.form.get('collection_request',type=int):
                    raise ValueError('Choose a collection request before approving its schedule.')
                key = text_field('request_key', 80)
                if not re.fullmatch(r'[A-Za-z0-9_-]{20,80}', key):
                    raise ValueError('Refresh this form before saving.')
                db().execute('BEGIN IMMEDIATE')
                previous = db().execute('SELECT id FROM bf_jobs WHERE request_key=?', (key,)).fetchone()
                if previous:
                    db().rollback()
                    return redirect(url_for('bf_job', job_id=previous['id']))
                site = location(integer('site_id'), 'client')
                warehouse = location(integer('warehouse_id'), 'warehouse')
                if site['state'] != warehouse['state']:
                    raise ValueError('Choose a warehouse in the same state as the client site. Bin pools stay within their state.')
                scheduled = date.fromisoformat(text_field('scheduled_date'))
                driver = request.form.get('driver_id', '')
                if driver and not eligible_driver(db(), driver, site['state']):
                    raise ValueError('Choose an active driver or contractor allocated to this state, or a manager.')
                selected = []
                for direction in ('collect','deliver'):
                    ids = request.form.getlist(direction)
                    if len(ids) > 200 or any(not v.isdigit() for v in ids) or len(set(ids)) != len(ids):
                        raise ValueError('Choose each bin only once, up to 200 per direction.')
                    for bid in ids:
                        rows = bins('b.id=?', (bid,))
                        if not rows:
                            raise ValueError('A selected bin no longer exists.')
                        b = rows[0]
                        if b['reserved_job']:
                            raise ValueError(f'{b["code"]} is already allocated to another job.')
                        if b['state'] != site['state']:
                            raise ValueError('All bins must belong to the same state pool as this site.')
                        if direction == 'collect' and (b['location_id'] != site['id'] or b['status'] not in ('at_client','full')):
                            raise ValueError(f'{b["code"]} is not available at this client site.')
                        if direction == 'deliver' and (b['location_id'] != warehouse['id'] or b['status'] != 'empty'):
                            raise ValueError(f'{b["code"]} is not an available empty bin at this warehouse.')
                        selected.append((direction,b))
                if not selected or len({b['id'] for _,b in selected}) != len(selected):
                    raise ValueError('Choose at least one bin. A bin cannot be collected and delivered on the same job.')
                jid = db().execute('SELECT COALESCE(MAX(id),0)+1 FROM bf_jobs').fetchone()[0]
                db().execute('''INSERT INTO bf_jobs(id,code,request_key,site_id,warehouse_id,driver_id,scheduled_date,status,client_name,site_name,site_address,warehouse_name,state,vehicle,notes,created_at,created_by)
                    VALUES(?,?,?,?,?,?,?,'scheduled',?,?,?,?,?,?,?,?,?)''', (jid,f'BF-{jid:06d}',key,site['id'],warehouse['id'],driver or None,scheduled.isoformat(),site['client_name'],site['name'],site['address'],warehouse['name'],site['state'],text_field('vehicle',80,False),text_field('notes',1000,False),now(),g.user['id']))
                selected_collections = {}
                selected_deliveries = {}
                for direction,b in selected:
                    counts = selected_collections if direction=='collect' else selected_deliveries
                    counts[b['size_id']] = counts.get(b['size_id'],0)+1
                profile, loading = site_plan(db(),site['id'],warehouse['id'],selected_collections,scheduled.isoformat())
                for row in loading:
                    row['selected'] = selected_deliveries.get(row['size_id'],0)
                    row['shortfall'] = max(0,row['recommended']-row['selected'])
                stamp=now()
                db().execute("UPDATE bf_jobs SET scan_mode=1,bin_plan=?,pod_client_id=?,approval_status='pending',submitted_at=? WHERE id=?", (json.dumps(loading),site['client_id'],stamp,jid))
                allocation_history(db(),jid,'Allocation submitted',actor(),g.user['id'],stamp)
                for direction,b in selected:
                    db().execute('INSERT INTO bf_job_bins(job_id,bin_id,direction,bin_code,size_name,tare_g) VALUES(?,?,?,?,?,?)', (jid,b['id'],direction,b['code'],b['size_name'],b['tare_g']))
                    event(b['id'], 'Reserved for collection' if direction == 'collect' else 'Reserved for delivery', jid, b['location_id'], notes=f'Due {scheduled.isoformat()}')
                rid=request.form.get('collection_request',type=int)
                if rid:
                    link_request(db(),rid,jid,site['id'],{b['id'] for d,b in selected if d=='collect'},actor(),now())
                if approve_schedule:
                    app.extensions['bf_review_allocation'](jid,'approve')
                    return success('Collection schedule approved. Choose a truck to add it to a driver run.','bf_job',job_id=jid)
                return success('Job saved as an allocation awaiting approval. Bins are reserved until an administrator approves or rejects it.', 'bf_job', job_id=jid)
            except (ValueError, sqlite3.IntegrityError) as error:
                return failure(error if isinstance(error,ValueError) else ValueError('One of these bins was just reserved by another user. Please refresh and select available bins.'), 'bf_new_job', site=request.form.get('site_id',''), warehouse=request.form.get('warehouse_id',''),collection_request=request.form.get('collection_request',''))
        try:
            plan_date = date.fromisoformat(request.args.get('date') or now()[:10]).isoformat()
        except ValueError:
            abort(400,'Choose a valid run date.')
        sites = db().execute("SELECT l.*,c.name AS client_name FROM bf_locations l JOIN bf_clients c ON c.id=l.client_id WHERE l.kind='client' ORDER BY c.name,l.name").fetchall()
        warehouses = db().execute("SELECT * FROM bf_locations WHERE kind='warehouse' ORDER BY state,name").fetchall()
        requested_id=request.args.get('collection_request',type=int)
        collection_request=db().execute("SELECT * FROM bf_collection_requests WHERE id=? AND status='new'",(requested_id,)).fetchone() if requested_id else None
        if requested_id and not collection_request:
            flash('That request has already been handled. Open its docket from Collection requests.','error')
            return redirect(url_for('bf_collection_requests'))
        site_id, warehouse_id = request.args.get('site', type=int), request.args.get('warehouse', type=int)
        if collection_request:
            site_id=collection_request['site_id']
        site = next((s for s in sites if s['id'] == site_id), None)
        warehouse = next((w for w in warehouses if w['id'] == warehouse_id), None)
        if site and not warehouse:
            warehouse = next((w for w in warehouses if w['state'] == site['state']), None)
        collect = bins("b.location_id=? AND b.status IN ('at_client','full') AND NOT EXISTS(SELECT 1 FROM bf_job_bins i WHERE i.bin_id=b.id AND i.active=1)", (site['id'],)) if site else []
        deliver = bins("b.location_id=? AND b.status='empty' AND NOT EXISTS(SELECT 1 FROM bf_job_bins i WHERE i.bin_id=b.id AND i.active=1)", (warehouse['id'],)) if warehouse and site and warehouse['state'] == site['state'] else []
        drivers = available_drivers(db(), site['state'] if site else '')
        profile, loading = site_plan(db(),site['id'],warehouse['id'],run_date=plan_date) if site and warehouse and site['state']==warehouse['state'] else (None,[])
        defaults = {'collect':set(), 'deliver':set()}
        for direction, available in (('collect',collect),('deliver',deliver)):
            for plan in loading:
                matching = sorted((b for b in available if b['size_id']==plan['size_id']),key=lambda b:(b['status']!='full',b['id']))
                quantity = plan['collect'] if direction=='collect' else plan['selected']
                defaults[direction].update(b['id'] for b in matching[:quantity])
        if collection_request and any(b['id']==collection_request['bin_id'] for b in collect):
            defaults['collect'].add(collection_request['bin_id'])
        return render('new_job', section='pickups',collection_request=collection_request, sites=sites, warehouses=warehouses, site=site, warehouse=warehouse, collect=collect, deliver=deliver, drivers=drivers, request_key=secrets.token_urlsafe(24), profile=profile, loading=loading, defaults=defaults, plan_date=plan_date,
                      source_profiles=source_profiles(db(), site['id']) if site else [])

    @app.get('/binflow/pickups/<int:job_id>')
    @access
    def bf_job(job_id):
        j = job(job_id)
        rows = items(job_id)
        if not is_office(g.user) or getattr(g,'bf_demo_token',None):
            return render('driver_job', section='pods' if j['collected_at'] else 'calendar',
                          job=j, items=rows, net=sum(i['net_g'] or 0 for i in rows))
        return render('job', section='pickups', job=j, items=rows, net=sum(i['net_g'] or 0 for i in rows), remaining=sum(i['direction']=='collect' and i['weighed_at'] is None for i in rows),
                      approval_history=db().execute('SELECT * FROM bf_allocation_history WHERE job_id=? ORDER BY id',(job_id,)).fetchall(),
                      trucks=db().execute('SELECT * FROM bf_trucks WHERE state=? ORDER BY name', (j['state'],)).fetchall(), loading=json.loads(j['bin_plan']),
                      scan_ready=all(i['delivered_at'] if i['direction']=='deliver' else i['picked_up_at'] for i in rows),
                      scans_started=any(i['loaded_at'] or i['delivered_at'] or i['picked_up_at'] for i in rows))

    @app.post('/binflow/pickups/<int:job_id>/<action>')
    @access
    def bf_job_action(job_id, action):
        if action == 'cancel' and g.user['role'] != 'admin':
            abort(403, 'Only an administrator can cancel scheduled work.')
        if action not in ('exchange','receive','cancel'):
            abort(404)
        if not is_office(g.user):
            j = job(job_id)
            if action != 'exchange' or not j['scan_mode'] or not j['truck_id']:
                abort(403, 'Use your driver day to scan bins and complete the client handover.')
        try:
            photo = None; signature = None
            if action == 'exchange':
                upload = request.files.get('photo')
                if upload and upload.filename:
                    photo = photo_jpeg(upload)
                raw_signature = request.form.get('signature','')
                if raw_signature and raw_signature != '[]':
                    signature = signature_png(raw_signature)
            db().execute('BEGIN IMMEDIATE')
            j = job(job_id); rows = items(job_id)
            ensure_approved(j)
            if action == 'cancel':
                if j['schedule_week_id']:
                    raise ValueError('Remove this job from its weekly calendar and save before cancelling it.')
                if j['status'] == 'cancelled':
                    return success('Job already cancelled.', 'bf_job', job_id=job_id)
                if j['status'] != 'scheduled':
                    raise ValueError('Only a job that has not been physically completed at the client can be cancelled.')
                if j['scan_mode'] and any(i['loaded_at'] or i['delivered_at'] or i['picked_up_at'] for i in rows):
                    raise ValueError('Bins have already moved. Unload unused empties before cancelling; finish any client movements on this docket.')
                reason = text_field('reason', 500)
                if j['truck_id']:
                    check_owner(db(),j['truck_id'],j['scheduled_date'],g.user)
                    ensure_run_unstarted(db(),j['truck_id'],j['scheduled_date'])
                    schedule_changed(db(),j['truck_id'],j['scheduled_date'],g.user['id'],actor(),now(),'Client job cancelled: '+j['code'])
                db().execute("UPDATE bf_jobs SET status='cancelled' WHERE id=?", (job_id,))
                db().execute('UPDATE bf_job_bins SET active=0 WHERE job_id=?', (job_id,))
                reopen_requests(db(),job_id,actor(),now())
                for item in rows:
                    event(item['bin_id'], 'Job cancelled; reservation released', job_id, notes=reason)
                return success('Job cancelled. Bins are available again.', 'bf_job', job_id=job_id)
            if action == 'exchange':
                if j['status'] in ('collected','received','complete'):
                    return success('This exchange has already been recorded.', 'bf_job', job_id=job_id)
                if j['status'] != 'scheduled':
                    raise ValueError('This job is not scheduled.')
                if j['scan_mode']:
                    if j['truck_id']:
                        guard_movement(db(),j,'exchange',g.user,driver_today(db(),g.user,now()))
                    if not j['truck_id'] or not all(i['delivered_at'] if i['direction']=='deliver' else i['picked_up_at'] for i in rows):
                        raise ValueError('Scan every delivery and pickup at the client before finishing this handover.')
                elif set(request.form.getlist('confirmed')) != {str(i['bin_id']) for i in rows}:
                    raise ValueError('Check or scan every physical bin to confirm the complete exchange.')
                confirmation = text_field('confirmation')
                receiver = text_field('receiver_name', 150, False)
                note = text_field('confirmation_note', 500, False)
                if confirmation not in ('signed','not_present','declined'):
                    raise ValueError('Choose how this handover was confirmed.')
                if confirmation == 'signed' and (not signature or not receiver):
                    raise ValueError('Enter the recipient name and capture their signature, or choose an unavailable-recipient option.')
                if confirmation != 'signed' and not note:
                    raise ValueError('Record why the recipient did not sign.')
                if confirmation != 'signed':
                    signature = None
                for item in rows:
                    b = bins('b.id=?', (item['bin_id'],))[0]
                    if b['reserved_job'] != job_id:
                        raise ValueError('A bin reservation changed. Refresh this job.')
                    if j['scan_mode']:
                        if item['direction'] == 'deliver':
                            if b['location_id'] != j['site_id'] or b['status'] not in ('at_client','full') or b['truck_id']:
                                raise ValueError('A delivered bin has changed. Review this docket.')
                            db().execute('UPDATE bf_job_bins SET active=0 WHERE id=?', (item['id'],))
                        elif b['truck_id'] != j['truck_id'] or b['truck_load'] != 'full':
                            raise ValueError('A collection bin is no longer on the assigned truck.')
                    else:
                        if item['direction'] == 'deliver':
                            if b['status'] != 'empty' or b['location_id'] != j['warehouse_id']:
                                raise ValueError('A replacement bin is no longer empty at the warehouse.')
                            db().execute("UPDATE bf_bins SET location_id=?,status='at_client' WHERE id=?", (j['site_id'], b['id']))
                            db().execute('UPDATE bf_job_bins SET active=0 WHERE id=?', (item['id'],))
                            event(b['id'], 'Empty bin delivered to client', job_id, j['warehouse_id'], j['site_id'])
                        else:
                            if b['status'] not in ('at_client','full') or b['location_id'] != j['site_id']:
                                raise ValueError('A collection bin is no longer at this client site.')
                            db().execute("UPDATE bf_bins SET location_id=NULL,status='transit' WHERE id=?", (b['id'],))
                            pickup_requests(db(),job_id,b['id'],actor(),now())
                            event(b['id'], 'Full bin collected; returning to warehouse', job_id, j['site_id'], notes=f'Destination: {j["warehouse_name"]}')
                has_collections = any(i['direction'] == 'collect' for i in rows)
                db().execute('UPDATE bf_jobs SET status=?,collected_at=?,collected_by=?,receiver_name=?,confirmation=?,confirmation_note=?,signature=?,photo=?,completed_at=? WHERE id=?',
                             ('collected' if has_collections else 'complete',now(),actor(),receiver,confirmation,note,signature,photo,None if has_collections else now(),job_id))
                if j['scan_mode']:
                    return success('Client handover saved. Continue to the next step.', 'bf_run', truck_id=j['truck_id'], date=j['scheduled_date'])
                return success('Exchange recorded. Collected bins are now in transit; replacement bins are assigned to the client.', 'bf_job', job_id=job_id)
            if j['scan_mode']:
                raise ValueError('Use Driver / truck to scan each returning bin into the warehouse.')
            if j['status'] in ('received','complete'):
                return success('This warehouse return has already been recorded.', 'bf_job', job_id=job_id)
            if j['status'] != 'collected':
                raise ValueError('Confirm the collection before receiving bins at the warehouse.')
            expected = {str(i['bin_id']) for i in rows if i['direction'] == 'collect'}
            if set(request.form.getlist('confirmed')) != expected:
                raise ValueError('Confirm every returning bin before recording this warehouse arrival.')
            for item in rows:
                if item['direction'] == 'collect':
                    b = bins('b.id=?', (item['bin_id'],))[0]
                    if b['status'] != 'transit' or b['reserved_job'] != job_id:
                        raise ValueError('A returning bin has changed. Refresh this docket.')
                    db().execute("UPDATE bf_bins SET location_id=?,status='received' WHERE id=?", (j['warehouse_id'], item['bin_id']))
                    event(item['bin_id'], 'Received at warehouse; weight pending', job_id, target=j['warehouse_id'])
            db().execute("UPDATE bf_jobs SET status='received',received_at=? WHERE id=?", (now(),job_id))
            return success('Warehouse arrival recorded. Enter gross and tare weights for each collected bin.', 'bf_job', job_id=job_id)
        except ValueError as error:
            return failure(error, 'bf_job', job_id=job_id)

    @app.post('/binflow/pickups/<int:job_id>/weigh/<int:item_id>')
    @access
    def bf_weigh(job_id, item_id):
        try:
            gross = grams(request.form.get('gross'), 'Gross weight')
            tare = grams(request.form.get('tare'), 'Tare weight')
            if gross < tare:
                raise ValueError('Gross weight cannot be less than the empty-bin tare.')
            if request.form.get('confirm_tare') != 'yes':
                raise ValueError('Confirm that the tare is the actual empty-bin weight.')
            db().execute('BEGIN IMMEDIATE')
            j = job(job_id)
            ensure_approved(j)
            item = db().execute("SELECT * FROM bf_job_bins WHERE id=? AND job_id=? AND direction='collect'", (item_id,job_id)).fetchone()
            if not item:
                abort(404)
            if item['weighed_at']:
                if gross == item['gross_g'] and tare == item['tare_g']:
                    return success('This weight is already saved.', 'bf_job', job_id=job_id)
                raise ValueError('This weight is already finalised. It cannot be overwritten.')
            if j['status'] != 'received' and not (j['scan_mode'] and j['status']=='collected' and item['returned_at']):
                raise ValueError('Receive the bins at the warehouse before weighing.')
            b = bins('b.id=?', (item['bin_id'],))[0]
            if b['status'] != 'received' or b['location_id'] != j['warehouse_id'] or b['reserved_job'] != job_id:
                raise ValueError('This bin is not waiting for weighing on this docket.')
            db().execute('UPDATE bf_job_bins SET gross_g=?,tare_g=?,net_g=?,weighed_at=?,weighed_by=?,active=0 WHERE id=?', (gross,tare,gross-tare,now(),actor(),item_id))
            db().execute("UPDATE bf_bins SET status='weighed',tare_g=? WHERE id=?", (tare,item['bin_id']))
            event(item['bin_id'], 'Warehouse weight recorded', job_id, j['warehouse_id'], j['warehouse_id'], f'Gross {kg(gross)} kg; tare {kg(tare)} kg; net {kg(gross-tare)} kg')
            if not db().execute("SELECT 1 FROM bf_job_bins WHERE job_id=? AND direction='collect' AND weighed_at IS NULL", (job_id,)).fetchone():
                db().execute("UPDATE bf_jobs SET status='complete',completed_at=? WHERE id=?", (now(),job_id))
            return success('Weight saved. The POD reflects the recorded net weight.', 'bf_job', job_id=job_id)
        except ValueError as error:
            return failure(error, 'bf_job', job_id=job_id)

    @app.get('/binflow/warehouse')
    @access
    def bf_warehouse():
        stock = bins("b.status IN ('transit','received','weighed') AND (b.truck_load IS NULL OR b.truck_load='full')")
        return render('warehouse', section='warehouse', stock=stock)

    @app.get('/binflow/pickups/<int:job_id>/pod')
    @access
    def bf_pod(job_id):
        j = job(job_id)
        ensure_pod_available(j)
        if j['status'] in ('scheduled','cancelled'):
            abort(400, 'Confirm the physical handover before creating a POD.')
        rows = items(job_id)
        return render('pod', section='pods', job=j, items=rows, net=sum(i['net_g'] or 0 for i in rows))

    @app.get('/binflow/pickups/<int:job_id>/evidence/<kind>')
    @access
    def bf_evidence(job_id, kind):
        j = job(job_id)
        if kind not in ('photo','signature') or not j[kind]:
            abort(404)
        return send_file(io.BytesIO(j[kind]), mimetype='image/jpeg' if kind == 'photo' else 'image/png')

    @app.get('/binflow/pickups/<int:job_id>/pod.pdf')
    @access
    def bf_pod_pdf(job_id):
        from pathlib import Path
        from binflow_pod_records import status as pod_status, qr_bytes
        import reportlab
        from reportlab.lib import colors
        from reportlab.lib.styles import getSampleStyleSheet
        from reportlab.lib.units import mm
        from reportlab.pdfbase import pdfmetrics
        from reportlab.pdfbase.ttfonts import TTFont
        from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, Image, KeepTogether
        j = job(job_id)
        ensure_pod_available(j)
        if j['status'] in ('scheduled','cancelled'):
            abort(400, 'Confirm the physical handover before creating a POD.')
        rows = items(job_id); output = io.BytesIO(); styles = getSampleStyleSheet()
        # Embed fonts so client PDFs render consistently on phones and printers.
        font_dir = Path(reportlab.__file__).parent / 'fonts'
        for name, filename in [('BinFlow-Regular', 'Vera.ttf'), ('BinFlow-Bold', 'VeraBd.ttf')]:
            if name not in pdfmetrics.getRegisteredFontNames():
                pdfmetrics.registerFont(TTFont(name, str(font_dir / filename)))
        for name, style in styles.byName.items():
            style.fontName = 'BinFlow-Bold' if name.startswith('Heading') or name == 'Title' else 'BinFlow-Regular'
            style.textColor = colors.HexColor('#183552')
        styles['BodyText'].fontSize = 9
        styles['BodyText'].leading = 14
        styles['Title'].fontSize = 20
        styles['Heading2'].fontSize = 13
        def timestamp(value):
            if not value:
                return 'Pending / not required'
            return datetime.fromisoformat(value).astimezone(ZoneInfo('Australia/Brisbane')).strftime('%d/%m/%Y %H:%M')
        document = SimpleDocTemplate(output, pagesize=(210*mm,297*mm), rightMargin=16*mm, leftMargin=16*mm, topMargin=16*mm, bottomMargin=16*mm, title=j['code'] + ' Proof of collection / delivery')
        p = lambda text, style='BodyText': Paragraph(escape(str(text)), styles[style])
        story = [p('SB EMPIRE | BINFLOW', 'Title'), p('PROOF OF COLLECTION / DELIVERY', 'Heading2'), p(j['code'] + (' · '+pod_status(j)), 'Heading2'), Spacer(1,4*mm)]
        record_url=app.config['APP_URL'].rstrip('/')+url_for('bf_pod_record',token=j['pod_token'])
        details = [('Client',j['client_name']),('Site',j['site_name']),('Address',j['site_address']),('State',j['state']),('Warehouse',j['warehouse_name']),('Vehicle',j['vehicle'] or 'Not recorded'),('Collection / delivery',timestamp(j['collected_at'])),('Recorded by',j['collected_by']),('Warehouse arrival',timestamp(j['received_at']))]
        details_table=Table([[[p(f'{label}: {value}') for label,value in details[:5]],
                             [p(f'{label}: {value}') for label,value in details[5:]],
                             [Image(io.BytesIO(qr_bytes(record_url)),width=28*mm,height=28*mm),
                              Paragraph('<link href="'+escape(record_url, {'"':'&quot;'})+'">Scan / open this POD</link>',styles['BodyText'])]]],
                            colWidths=[70*mm,70*mm,38*mm])
        details_table.setStyle(TableStyle([('VALIGN',(0,0),(-1,-1),'TOP'),('LEFTPADDING',(0,0),(-1,-1),0),('RIGHTPADDING',(0,0),(-1,-1),8)]))
        story.append(details_table)
        story.append(Spacer(1,5*mm))
        table = [[p(v) for v in ('Bin / journey','Size','Movement','Material','Gross kg','Tare kg','Net kg')]]
        for item in rows:
            journey_url=app.config['APP_URL'].rstrip('/')+url_for('bf_pod_journey',token=j['pod_token'],item_id=item['id'])
            table.append([Paragraph('<link href="'+escape(journey_url, {'"':'&quot;'})+'">'+escape(item['bin_code'])+'</link>',styles['BodyText']),p(item['size_name']),p('Collected' if item['direction']=='collect' else 'Empty delivered'),p(item['material_name'] or ('Unclassified' if item['direction']=='collect' else 'Not applicable')),p(kg(item['gross_g'])),p(kg(item['tare_g']) if item['direction']=='collect' else '—'),p(kg(item['net_g']))])
        t = Table(table, colWidths=[24*mm,18*mm,25*mm,39*mm,24*mm,24*mm,24*mm], repeatRows=1, hAlign='LEFT')
        t.setStyle(TableStyle([('BACKGROUND',(0,0),(-1,0),colors.HexColor('#eaf1fa')),('VALIGN',(0,0),(-1,-1),'TOP'),('BOTTOMPADDING',(0,0),(-1,-1),8),('TOPPADDING',(0,0),(-1,-1),8),('LINEBELOW',(0,0),(-1,-1),0.4,colors.HexColor('#d6deeb'))]))
        story += [t,Spacer(1,5*mm),p(f'Recorded net weight: {kg(sum(i["net_g"] or 0 for i in rows))} kg','Heading2')]
        if not j['pod_signed_at']:
            story.append(p('PROVISIONAL: warehouse weighing and a separate signed final approval are required. '+pod_status(j)))
        else:
            story += [KeepTogether([p('Warehouse final sign-off','Heading2'),p('Signed by: '+j['pod_signed_name']),
                      p('Signed at: '+timestamp(j['pod_signed_at'])),
                      Image(io.BytesIO(j['pod_signature']),width=75*mm,height=25*mm)])]
        story += [p('Recipient: ' + (j['receiver_name'] or 'Not present / not recorded')),p('Confirmation: ' + {'signed':'Recipient signed','not_present':'Recipient not present; driver recorded','declined':'Recipient declined signature; driver recorded','driver_scanned':'Driver QR scans completed; no client signature'}.get(j['confirmation'],j['confirmation']))]
        if j['confirmation_note']:
            story.append(p(j['confirmation_note']))
        if j['signature']:
            story += [Spacer(1,3*mm), Image(io.BytesIO(j['signature']), width=90*mm,height=30*mm)]
        if j['photo']:
            from PIL import Image as PILImage
            image = PILImage.open(io.BytesIO(j['photo'])); width,height=image.size
            scale = min(150*mm/width,80*mm/height)
            story += [Spacer(1,4*mm),p('Collection evidence','Heading3'),Image(io.BytesIO(j['photo']),width=width*scale,height=height*scale)]
        if j['notes']:
            story += [Spacer(1,3*mm),p('Notes: ' + j['notes'])]
        story += [Spacer(1,5*mm),p('Times are recorded in Australia/Brisbane time. Net weight = gross weight less empty-bin tare. A driver-only confirmation is not a client signature.')]
        def pod_page(canvas, doc):
            canvas.saveState();canvas.setFont('BinFlow-Regular',8)
            canvas.setFillColor(colors.HexColor('#526675'))
            canvas.drawString(16*mm,9*mm,j['code']+' | '+pod_status(j))
            canvas.drawRightString(194*mm,9*mm,'Page '+str(doc.page))
            canvas.restoreState()
        document.build(story,onFirstPage=pod_page,onLaterPages=pod_page); output.seek(0)
        return send_file(output,mimetype='application/pdf',as_attachment=True,download_name=j['code']+'-POD.pdf')

    @app.get('/binflow/export.csv')
    @access
    def bf_export():
        stream=io.StringIO(); writer=csv.writer(stream)
        writer.writerow(['Bin ID','Size','State','Status','Client','Location','Tare kg','Reserved docket'])
        def safe(value):
            value = '' if value is None else str(value)
            return "'"+value if value.startswith(('=','+','-','@','\t','\r')) else value
        for b in bins():
            writer.writerow([safe(v) for v in (b['code'],b['size_name'],b['state'],STATUSES[b['status']],b['client_name'],b['location_name'] or 'In transit',kg(b['tare_g']),f'BF-{b["reserved_job"]:06d}' if b['reserved_job'] else '')])
        return send_file(io.BytesIO(stream.getvalue().encode('utf-8-sig')),mimetype='text/csv',as_attachment=True,download_name='binflow-inventory.csv')

    from binflow_pod_records import register as register_pod_records
    register_pod_records(app,db,access,require,render,job,items,success,failure,now,actor)

    register_driver(app, db, access, require, bins, job, items, render, event, success, failure, text_field, integer, now, STATES)

    register_requirements(app, db, require, access, location, render, success, actor, now, source_profiles)

    from binflow_bin_condition import register as register_bin_condition
    register_bin_condition(app,db,require,success,failure,event)
    register_runs(app, db, access, require, render, success, failure, now, actor)

    register_gps(app, db, access, require, render, success, failure, now, actor)
    register_portal(app, db, access, require, render, success, failure, now, actor, bf_pod_pdf.__wrapped__)
    from binflow_bales import register as register_bales
    register_bales(app,db,access,require,render,success,failure,now,actor)

    register_qr(app, db, access, require, allowed, render, success, failure, now, actor)
    register_approvals(app,db,access,require,allowed,render,job,items,bins,event,success,failure,now,actor,reopen_requests,approve_requests)
    register_collections(app,db,access,require,render,success,now,actor)
    register_acceptance(app,db,access,success,failure,now,actor)
    register_workdays(app,db,access,require,render,success,failure,now)
    from binflow_journey import register_journey
    register_journey(app,db,access,render,success,failure,now)
    from binflow_reports import register_reports
    register_reports(app,db,require,render,now)
    from binflow_demo import register_demo
    register_demo(app,db,access,render,now)
    from binflow_rental import register as register_rental
    register_rental(app,db,require,render,now)
    from binflow_xero import register as register_xero
    register_xero(app,db,require,render,now,actor)
    register_sizes(app,db,require,render,now,actor)
    register_profiles(app,db,require,render,now,actor)
    from binflow_supplier_profiles import register_suppliers
    register_suppliers(app,db,require,render,now,actor)

    register_schedule(app,db,access,require,render,now,actor)
