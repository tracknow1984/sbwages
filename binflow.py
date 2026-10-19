"""Persistent national bin inventory, exchange dockets and warehouse custody."""
import csv
import io
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

STATES = ('QLD', 'NSW', 'VIC', 'SA', 'WA', 'TAS', 'ACT', 'NT')
STATUSES = {'empty': 'Empty at warehouse', 'at_client': 'At client', 'full': 'Full / pickup requested',
            'transit': 'Returning to warehouse', 'received': 'Awaiting weight', 'weighed': 'Full stock in warehouse'}
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
    with app.app_context():
        db().executescript(SCHEMA)
        for litres in (240, 660, 1100):
            db().execute('INSERT OR IGNORE INTO bf_sizes(name,litres) VALUES(?,?)', (f'{litres} L', litres))
        db().commit()
    app.jinja_env.filters['bfkg'] = kg

    def allowed():
        return bool(g.user and (g.user['role'] == 'admin' or db().execute('SELECT 1 FROM bf_access WHERE user_id=?', (g.user['id'],)).fetchone()))

    app.context_processor(lambda: dict(binflow_allowed=allowed()))

    def access(fn):
        @wraps(fn)
        def wrapped(*args, **kwargs):
            if not g.user:
                session['binflow_next'] = request.path
                return redirect(url_for('bf_signin'))
            if not allowed():
                abort(403, 'Ask an administrator to enable Bin Management / Transport access for your staff login.')
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

    def event(bin_id, label, job_id=None, origin=None, target=None, notes=''):
        db().execute('INSERT INTO bf_events(bin_id,job_id,event,from_location,to_location,actor,notes,created_at) VALUES(?,?,?,?,?,?,?,?)',
                     (bin_id, job_id, label, origin, target, actor(), notes, now()))

    def job(job_id):
        row = db().execute('SELECT j.*,u.first_name||\' \'||u.last_name AS driver_name FROM bf_jobs j LEFT JOIN users u ON u.id=j.driver_id WHERE j.id=?', (job_id,)).fetchone()
        if not row:
            abort(404)
        return row

    def items(job_id):
        return db().execute('SELECT i.*,b.token FROM bf_job_bins i JOIN bf_bins b ON b.id=i.bin_id WHERE i.job_id=? ORDER BY i.direction,i.bin_code', (job_id,)).fetchall()

    def bins(where='1=1', args=()):
        return db().execute('''SELECT b.*,s.name AS size_name,l.name AS location_name,l.kind,c.name AS client_name,
            (SELECT i.job_id FROM bf_job_bins i WHERE i.bin_id=b.id AND i.active=1) AS reserved_job
            FROM bf_bins b JOIN bf_sizes s ON s.id=b.size_id LEFT JOIN bf_locations l ON l.id=b.location_id
            LEFT JOIN bf_clients c ON c.id=l.client_id WHERE ''' + where + ' ORDER BY b.id', args).fetchall()

    def render(name, **values):
        return render_template('binflow/' + name + '.html', tab='binflow', states=STATES, statuses=STATUSES,
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
            return redirect(url_for('bf_dashboard'))
        return render('signin', section='')

    @app.get('/admin/bin-management')
    @access
    def bf_dashboard():
        state = request.args.get('state', '')
        if state and state not in STATES:
            abort(400)
        where, args = ('b.state=?', (state,)) if state else ('1=1', ())
        stock = bins(where, args)
        counts = {key: sum(b['status'] == key for b in stock) for key in STATUSES}
        jobs = db().execute('SELECT * FROM bf_jobs WHERE status NOT IN (\'complete\',\'cancelled\')' + (' AND state=?' if state else '') + ' ORDER BY scheduled_date,id LIMIT 30', args).fetchall()
        sizes = db().execute('SELECT * FROM bf_sizes ORDER BY litres').fetchall()
        state_rows = [dict(state=s, total=sum(b['state'] == s for b in stock), available=sum(b['state'] == s and b['status'] == 'empty' and not b['reserved_job'] for b in stock),
                           on_site=sum(b['state'] == s and b['status'] in ('at_client','full') for b in stock), returning=sum(b['state'] == s and b['status'] == 'transit' for b in stock), warehouse_full=sum(b['state'] == s and b['status'] in ('received','weighed') for b in stock)) for s in STATES if not state or s == state]
        by_size = [dict(name=s['name'], total=sum(b['size_id'] == s['id'] for b in stock), available=sum(b['size_id'] == s['id'] and b['status'] == 'empty' and not b['reserved_job'] for b in stock), at_client=sum(b['size_id'] == s['id'] and b['status'] in ('at_client','full') for b in stock)) for s in sizes]
        net = db().execute('SELECT COALESCE(SUM(i.net_g),0) FROM bf_job_bins i JOIN bf_jobs j ON j.id=i.job_id WHERE i.weighed_at IS NOT NULL' + (' AND j.state=?' if state else ''), args).fetchone()[0]
        return render('dashboard', section='overview', state=state, stock=stock, counts=counts, jobs=jobs, state_rows=state_rows, by_size=by_size, net=net,
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
                    db().execute('INSERT INTO bf_sizes(name,litres,default_tare_g) VALUES(?,?,?)', (text_field('name', 60), integer('litres', 50000), grams(request.form.get('tare', '0'), 'Empty tare')))
                elif kind == 'access':
                    ids = request.form.getlist('users')
                    if len(ids) > 1000 or any(not v.isdigit() for v in ids):
                        raise ValueError('Invalid staff selection.')
                    db().execute('DELETE FROM bf_access')
                    for uid in set(ids):
                        row = db().execute("SELECT id FROM users WHERE id=? AND role='employee' AND status='active'", (uid,)).fetchone()
                        if not row:
                            raise ValueError('Only active employees can receive bin transport access.')
                        db().execute('INSERT INTO bf_access(user_id) VALUES(?)', (uid,))
                else:
                    raise ValueError('Unknown action.')
                return success('Saved.', 'bf_setup')
            except sqlite3.IntegrityError:
                return failure(ValueError('This record already exists. Please use a different name.'), 'bf_setup')
            except ValueError as error:
                return failure(error, 'bf_setup')
        return render('setup', section='setup', clients=db().execute('SELECT * FROM bf_clients ORDER BY name').fetchall(),
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
                return success('Warehouse updated.' if record['kind'] == 'warehouse' else 'Client site updated.', 'bf_setup', _anchor='clients-locations')
            except sqlite3.IntegrityError:
                error = 'A location with this name already exists for this state or client. Please use a different name.'
            except ValueError as exc:
                error = str(exc)
            db().rollback()
        return render('location_edit', section='setup', record=record, values=values, state_locked=state_locked, error=error), 400 if error else 200

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
                tare = grams(request.form.get('tare', '0'), 'Empty tare')
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
        if state:
            clauses.append('b.state=?'); args.append(state)
        if status:
            clauses.append('b.status=?'); args.append(status)
        if client:
            clauses.append('l.client_id=?'); args.append(client)
        if size_id:
            clauses.append('b.size_id=?'); args.append(size_id)
        if query:
            clauses.append('(b.code LIKE ? OR l.name LIKE ? OR c.name LIKE ?)'); args.extend(['%' + query[:100] + '%'] * 3)
        stock = bins(' AND '.join(clauses) or '1=1', args)
        return render('inventory', section='bins', stock=stock, state=state, status=status, client=client, size_id=size_id, query=query, request_key=secrets.token_urlsafe(24),
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
        row = db().execute('SELECT id FROM bf_bins WHERE code=? COLLATE NOCASE OR token=?', (value, value)).fetchone()
        if not row:
            flash('Bin not found. Scan its label or enter the BF number.', 'error')
            return render('scan', section='bins'), 404
        return redirect(url_for('bf_bin', bin_id=row['id']))

    @app.get('/binflow/qr/<token>')
    @access
    def bf_qr_open(token):
        row = db().execute('SELECT id FROM bf_bins WHERE token=?', (token,)).fetchone()
        if not row:
            abort(404)
        return redirect(url_for('bf_bin', bin_id=row['id']))

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
        history = db().execute('SELECT e.*,f.name AS from_name,t.name AS to_name,j.code AS job_code FROM bf_events e LEFT JOIN bf_locations f ON f.id=e.from_location LEFT JOIN bf_locations t ON t.id=e.to_location LEFT JOIN bf_jobs j ON j.id=e.job_id WHERE e.bin_id=? ORDER BY e.id DESC', (bin_id,)).fetchall()
        current_item = db().execute("SELECT i.*,j.code,j.status AS job_status FROM bf_job_bins i JOIN bf_jobs j ON j.id=i.job_id WHERE i.bin_id=? AND i.active=1", (bin_id,)).fetchone()
        return render('bin', section='bins', b=stock[0], history=history, current_item=current_item)

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
        state, status = request.args.get('state',''), request.args.get('status','')
        clauses, args = [], []
        for key, value in (('state',state),('status',status)):
            if value:
                clauses.append('j.' + key + '=?'); args.append(value)
        jobs = db().execute('''SELECT j.*,u.first_name||' '||u.last_name AS driver_name,
            (SELECT COUNT(*) FROM bf_job_bins i WHERE i.job_id=j.id AND direction='collect') AS collections,
            (SELECT COUNT(*) FROM bf_job_bins i WHERE i.job_id=j.id AND direction='deliver') AS deliveries,
            (SELECT SUM(net_g) FROM bf_job_bins i WHERE i.job_id=j.id) AS net_g
            FROM bf_jobs j LEFT JOIN users u ON u.id=j.driver_id''' + (' WHERE ' + ' AND '.join(clauses) if clauses else '') + ' ORDER BY j.scheduled_date DESC,j.id DESC', args).fetchall()
        return render('jobs', section='pickups', jobs=jobs, state=state, status=status)

    @app.route('/binflow/pickups/new', methods=['GET','POST'])
    @access
    def bf_new_job():
        if request.method == 'POST':
            try:
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
                if driver and not db().execute("SELECT 1 FROM users u WHERE u.id=? AND u.status='active' AND (u.role='admin' OR EXISTS(SELECT 1 FROM bf_access a WHERE a.user_id=u.id))", (driver,)).fetchone():
                    raise ValueError('Choose an active staff member with bin transport access.')
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
                for direction,b in selected:
                    db().execute('INSERT INTO bf_job_bins(job_id,bin_id,direction,bin_code,size_name,tare_g) VALUES(?,?,?,?,?,?)', (jid,b['id'],direction,b['code'],b['size_name'],b['tare_g']))
                    event(b['id'], 'Reserved for collection' if direction == 'collect' else 'Reserved for delivery', jid, b['location_id'], notes=f'Due {scheduled.isoformat()}')
                return success('Job saved and bins reserved. Confirm the physical exchange at the client site.', 'bf_job', job_id=jid)
            except (ValueError, sqlite3.IntegrityError) as error:
                return failure(error if isinstance(error,ValueError) else ValueError('One of these bins was just reserved by another user. Please refresh and select available bins.'), 'bf_new_job', site=request.form.get('site_id',''), warehouse=request.form.get('warehouse_id',''))
        sites = db().execute("SELECT l.*,c.name AS client_name FROM bf_locations l JOIN bf_clients c ON c.id=l.client_id WHERE l.kind='client' ORDER BY c.name,l.name").fetchall()
        warehouses = db().execute("SELECT * FROM bf_locations WHERE kind='warehouse' ORDER BY state,name").fetchall()
        site_id, warehouse_id = request.args.get('site', type=int), request.args.get('warehouse', type=int)
        site = next((s for s in sites if s['id'] == site_id), None)
        warehouse = next((w for w in warehouses if w['id'] == warehouse_id), None)
        if site and not warehouse:
            warehouse = next((w for w in warehouses if w['state'] == site['state']), None)
        collect = bins("b.location_id=? AND b.status IN ('at_client','full') AND NOT EXISTS(SELECT 1 FROM bf_job_bins i WHERE i.bin_id=b.id AND i.active=1)", (site['id'],)) if site else []
        deliver = bins("b.location_id=? AND b.status='empty' AND NOT EXISTS(SELECT 1 FROM bf_job_bins i WHERE i.bin_id=b.id AND i.active=1)", (warehouse['id'],)) if warehouse and site and warehouse['state'] == site['state'] else []
        drivers = db().execute("SELECT u.id,u.first_name,u.last_name FROM users u WHERE u.status='active' AND (u.role='admin' OR EXISTS(SELECT 1 FROM bf_access a WHERE a.user_id=u.id)) ORDER BY u.first_name").fetchall()
        return render('new_job', section='pickups', sites=sites, warehouses=warehouses, site=site, warehouse=warehouse, collect=collect, deliver=deliver, drivers=drivers, request_key=secrets.token_urlsafe(24))

    @app.get('/binflow/pickups/<int:job_id>')
    @access
    def bf_job(job_id):
        j = job(job_id)
        rows = items(job_id)
        return render('job', section='pickups', job=j, items=rows, net=sum(i['net_g'] or 0 for i in rows), remaining=sum(i['direction']=='collect' and i['weighed_at'] is None for i in rows))

    @app.post('/binflow/pickups/<int:job_id>/<action>')
    @access
    def bf_job_action(job_id, action):
        if action not in ('exchange','receive','cancel'):
            abort(404)
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
            if action == 'cancel':
                if j['status'] == 'cancelled':
                    return success('Job already cancelled.', 'bf_job', job_id=job_id)
                if j['status'] != 'scheduled':
                    raise ValueError('Only a job that has not been physically completed at the client can be cancelled.')
                reason = text_field('reason', 500)
                db().execute("UPDATE bf_jobs SET status='cancelled' WHERE id=?", (job_id,))
                db().execute('UPDATE bf_job_bins SET active=0 WHERE job_id=?', (job_id,))
                for item in rows:
                    event(item['bin_id'], 'Job cancelled; reservation released', job_id, notes=reason)
                return success('Job cancelled. Bins are available again.', 'bf_job', job_id=job_id)
            if action == 'exchange':
                if j['status'] in ('collected','received','complete'):
                    return success('This exchange has already been recorded.', 'bf_job', job_id=job_id)
                if j['status'] != 'scheduled':
                    raise ValueError('This job is not scheduled.')
                confirmed = set(request.form.getlist('confirmed'))
                if confirmed != {str(i['bin_id']) for i in rows}:
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
                        event(b['id'], 'Full bin collected; returning to warehouse', job_id, j['site_id'], notes=f'Destination: {j["warehouse_name"]}')
                has_collections = any(i['direction'] == 'collect' for i in rows)
                db().execute('UPDATE bf_jobs SET status=?,collected_at=?,collected_by=?,receiver_name=?,confirmation=?,confirmation_note=?,signature=?,photo=?,completed_at=? WHERE id=?',
                             ('collected' if has_collections else 'complete',now(),actor(),receiver,confirmation,note,signature,photo,None if has_collections else now(),job_id))
                return success('Exchange recorded. Collected bins are now in transit; replacement bins are assigned to the client.', 'bf_job', job_id=job_id)
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
            item = db().execute("SELECT * FROM bf_job_bins WHERE id=? AND job_id=? AND direction='collect'", (item_id,job_id)).fetchone()
            if not item:
                abort(404)
            if item['weighed_at']:
                if gross == item['gross_g'] and tare == item['tare_g']:
                    return success('This weight is already saved.', 'bf_job', job_id=job_id)
                raise ValueError('This weight is already finalised. It cannot be overwritten.')
            if j['status'] != 'received':
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
        stock = bins("b.status IN ('transit','received','weighed')")
        return render('warehouse', section='warehouse', stock=stock)

    @app.get('/binflow/pickups/<int:job_id>/pod')
    @access
    def bf_pod(job_id):
        j = job(job_id)
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
        import reportlab
        from reportlab.lib import colors
        from reportlab.lib.styles import getSampleStyleSheet
        from reportlab.lib.units import mm
        from reportlab.pdfbase import pdfmetrics
        from reportlab.pdfbase.ttfonts import TTFont
        from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, Image
        j = job(job_id)
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
        story = [p('SB EMPIRE | BINFLOW', 'Title'), p('PROOF OF COLLECTION / DELIVERY', 'Heading2'), p(j['code'] + (' · FINAL' if j['status']=='complete' else ' · WEIGHT PENDING'), 'Heading2'), Spacer(1,4*mm)]
        for label,value in [('Client',j['client_name']),('Site',j['site_name']),('Address',j['site_address']),('State',j['state']),('Warehouse',j['warehouse_name']),('Vehicle',j['vehicle'] or 'Not recorded'),('Collection / delivery',timestamp(j['collected_at'])),('Recorded by',j['collected_by']),('Warehouse arrival',timestamp(j['received_at']))]:
            story.append(p(f'{label}: {value}'))
        story.append(Spacer(1,5*mm))
        table = [[p(v) for v in ('Bin','Size','Movement','Gross kg','Tare kg','Net kg')]]
        for item in rows:
            table.append([p(item['bin_code']),p(item['size_name']),p('Collected' if item['direction']=='collect' else 'Empty delivered'),p(kg(item['gross_g'])),p(kg(item['tare_g']) if item['direction']=='collect' else '—'),p(kg(item['net_g']))])
        t = Table(table, colWidths=[27*mm,23*mm,44*mm,28*mm,28*mm,28*mm], repeatRows=1, hAlign='LEFT')
        t.setStyle(TableStyle([('BACKGROUND',(0,0),(-1,0),colors.HexColor('#eaf1fa')),('VALIGN',(0,0),(-1,-1),'TOP'),('BOTTOMPADDING',(0,0),(-1,-1),8),('TOPPADDING',(0,0),(-1,-1),8),('LINEBELOW',(0,0),(-1,-1),0.4,colors.HexColor('#d6deeb'))]))
        story += [t,Spacer(1,5*mm),p(f'Recorded net weight: {kg(sum(i["net_g"] or 0 for i in rows))} kg','Heading2')]
        if j['status'] != 'complete':
            story.append(p('Warehouse weighing is incomplete. This is a provisional collection / delivery record.'))
        story += [p('Recipient: ' + (j['receiver_name'] or 'Not present / not recorded')),p('Confirmation: ' + {'signed':'Recipient signed','not_present':'Recipient not present; driver recorded','declined':'Recipient declined signature; driver recorded'}.get(j['confirmation'],j['confirmation']))]
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
        document.build(story); output.seek(0)
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
