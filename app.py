import hashlib
import io
import json
import os
import re
import secrets
import smtplib
import sqlite3
import ssl
import time
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from email.message import EmailMessage
from functools import wraps
from pathlib import Path
from zoneinfo import ZoneInfo

from flask import Flask, abort, flash, g, redirect, render_template, request, session, url_for, send_file
from werkzeug.utils import secure_filename
from werkzeug.security import check_password_hash, generate_password_hash
from prestart_config import ASSETS, DECLARATION, checklist

SCHEMA = '''
CREATE TABLE IF NOT EXISTS prestarts (
 id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id),
 employee_name TEXT NOT NULL, asset TEXT NOT NULL, asset_type TEXT NOT NULL,
 reading TEXT NOT NULL, reading_unit TEXT NOT NULL, checks_json TEXT NOT NULL,
 failure_count INTEGER NOT NULL, fit_for_duty TEXT NOT NULL CHECK(fit_for_duty IN ('yes','no')),
 notes TEXT NOT NULL, signature TEXT NOT NULL, declaration TEXT NOT NULL,
 submitted_at TEXT NOT NULL, submission_token TEXT NOT NULL UNIQUE
);
CREATE INDEX IF NOT EXISTS prestarts_by_user ON prestarts(user_id,id);
CREATE TABLE IF NOT EXISTS users (
 id INTEGER PRIMARY KEY, role TEXT NOT NULL CHECK(role IN ('admin','employee')),
 first_name TEXT NOT NULL, last_name TEXT NOT NULL DEFAULT '', email TEXT NOT NULL COLLATE NOCASE UNIQUE,
 mobile TEXT NOT NULL DEFAULT '', emergency_name TEXT NOT NULL DEFAULT '', emergency_email TEXT NOT NULL DEFAULT '',
 rate_cents INTEGER NOT NULL DEFAULT 0 CHECK(rate_cents >= 0), username TEXT NOT NULL COLLATE NOCASE UNIQUE,
 password_hash TEXT, status TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active','terminated')),
 week_ending INTEGER NOT NULL DEFAULT 6 CHECK(week_ending IN (4,6)), auth_version INTEGER NOT NULL DEFAULT 0,
 invited_at TEXT
);
CREATE TABLE IF NOT EXISTS sheets (
 id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id), week_end TEXT NOT NULL,
 status TEXT NOT NULL DEFAULT 'draft' CHECK(status IN ('draft','submitted')),
 rate_cents INTEGER, total_units INTEGER, total_cents INTEGER, submitted_at TEXT,
 UNIQUE(user_id, week_end)
);
CREATE TABLE IF NOT EXISTS entries (
 user_id INTEGER NOT NULL REFERENCES users(id), work_date TEXT NOT NULL,
 sheet_id INTEGER NOT NULL REFERENCES sheets(id), units INTEGER NOT NULL CHECK(units BETWEEN 0 AND 2400),
 activity TEXT NOT NULL DEFAULT '', PRIMARY KEY(user_id,work_date)
);
CREATE TABLE IF NOT EXISTS invitations (
 token_hash TEXT PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id), expires_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS login_attempts (key TEXT PRIMARY KEY, count INTEGER NOT NULL, started INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS payments (
 id INTEGER PRIMARY KEY, sheet_id INTEGER NOT NULL UNIQUE REFERENCES sheets(id),
 user_id INTEGER NOT NULL REFERENCES users(id), admin_id INTEGER NOT NULL REFERENCES users(id),
 employee_name TEXT NOT NULL, employee_username TEXT NOT NULL, processed_by TEXT NOT NULL,
 week_start TEXT NOT NULL, week_end TEXT NOT NULL, total_units INTEGER NOT NULL,
 rate_cents INTEGER NOT NULL, total_cents INTEGER NOT NULL CHECK(total_cents>0),
 cash_cents INTEGER NOT NULL CHECK(cash_cents>=0), transfer_cents INTEGER NOT NULL CHECK(transfer_cents>=0),
 processed_at TEXT NOT NULL, CHECK(cash_cents+transfer_cents=total_cents)
);
CREATE INDEX IF NOT EXISTS payments_by_user ON payments(user_id,processed_at);
CREATE TABLE IF NOT EXISTS staff_notices (
 id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id),
 admin_id INTEGER NOT NULL REFERENCES users(id), title TEXT NOT NULL,
 body TEXT NOT NULL, priority TEXT NOT NULL DEFAULT 'normal' CHECK(priority IN ('normal','urgent')),
 sent_at TEXT NOT NULL, read_at TEXT
);
CREATE INDEX IF NOT EXISTS notices_by_user ON staff_notices(user_id,read_at,id);
CREATE TABLE IF NOT EXISTS documents (
 id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id), title TEXT NOT NULL,
 filename TEXT NOT NULL, mime_type TEXT NOT NULL, data BLOB NOT NULL,
 uploaded_at TEXT NOT NULL, archived_at TEXT, archived_by INTEGER REFERENCES users(id),
 viewed_at TEXT, viewed_by INTEGER REFERENCES users(id)
);
CREATE TABLE IF NOT EXISTS document_emails (
 id INTEGER PRIMARY KEY, document_id INTEGER NOT NULL REFERENCES documents(id),
 admin_id INTEGER NOT NULL REFERENCES users(id), recipient TEXT NOT NULL, sent_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS leave_requests (
 id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id),
 start_date TEXT NOT NULL, end_date TEXT NOT NULL, notes TEXT NOT NULL DEFAULT '',
 status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','approved','rejected')),
 requested_at TEXT NOT NULL, decided_at TEXT, decided_by INTEGER REFERENCES users(id),
 admin_notes TEXT NOT NULL DEFAULT '', CHECK(end_date >= start_date)
);
CREATE INDEX IF NOT EXISTS leave_by_user_dates ON leave_requests(user_id,start_date,end_date);
CREATE TABLE IF NOT EXISTS entry_audit (
 id INTEGER PRIMARY KEY, admin_id INTEGER NOT NULL REFERENCES users(id),
 user_id INTEGER NOT NULL REFERENCES users(id), work_date TEXT NOT NULL,
 action TEXT NOT NULL, reason TEXT NOT NULL, before_json TEXT NOT NULL, changed_at TEXT NOT NULL
);
'''


def create_app(test_config=None):
    app = Flask(__name__)
    app.config.update(
        SECRET_KEY=os.environ.get('SECRET_KEY'), DATABASE=os.environ.get('DATABASE_PATH', 'data/sbwages.db'),
        SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE='Lax',
        SESSION_COOKIE_SECURE=os.environ.get('COOKIE_SECURE', 'true').lower() == 'true',
        PERMANENT_SESSION_LIFETIME=timedelta(hours=12), MAX_CONTENT_LENGTH=6 * 1024 * 1024, DOCUMENT_MAX_BYTES=5 * 1024 * 1024,
        APP_URL=os.environ.get('APP_URL', '').rstrip('/'),
        ADMIN_USERNAME=os.environ.get('ADMIN_USERNAME', 'admin'), ADMIN_EMAIL=os.environ.get('ADMIN_EMAIL', ''),
        ADMIN_PASSWORD=os.environ.get('ADMIN_PASSWORD', ''),
        SMTP_HOST=os.environ.get('SMTP_HOST', ''), SMTP_PORT=int(os.environ.get('SMTP_PORT', '587')),
        SMTP_USERNAME=os.environ.get('SMTP_USERNAME', ''), SMTP_PASSWORD=os.environ.get('SMTP_PASSWORD', ''),
        SMTP_FROM=os.environ.get('SMTP_FROM', ''), SMTP_SSL=os.environ.get('SMTP_SSL', 'false').lower() == 'true',
    )
    if test_config:
        app.config.update(test_config)
    if not app.config['SECRET_KEY'] or len(app.config['SECRET_KEY']) < 32:
        raise RuntimeError('Set SECRET_KEY to a random value of at least 32 characters.')
    Path(app.config['DATABASE']).parent.mkdir(parents=True, exist_ok=True)

    def db():
        if 'db' not in g:
            g.db = sqlite3.connect(app.config['DATABASE'], timeout=15)
            g.db.row_factory = sqlite3.Row
            g.db.execute('PRAGMA foreign_keys=ON')
            g.db.execute('PRAGMA journal_mode=WAL')
        return g.db

    @app.teardown_appcontext
    def close_db(error):
        conn = g.pop('db', None)
        if conn:
            conn.close()

    with app.app_context():
        db().executescript(SCHEMA)
        # Additive migration: keep every existing timesheet and password intact.
        db().execute('BEGIN IMMEDIATE')
        db().execute('UPDATE users SET week_ending=6 WHERE week_ending<>6')
        user_columns = {row['name'] for row in db().execute('PRAGMA table_info(users)')}
        for name in ('licence_number', 'licence_state', 'licence_expiry'):
            if name not in user_columns:
                db().execute(f"ALTER TABLE users ADD COLUMN {name} TEXT NOT NULL DEFAULT ''")
        columns = {row['name'] for row in db().execute('PRAGMA table_info(entries)')}
        for name, definition in [('start_time', 'TEXT'), ('finish_time', 'TEXT'),
                                 ('committed_at', 'TEXT')]:
            if name not in columns:
                db().execute(f'ALTER TABLE entries ADD COLUMN {name} {definition}')
        db().execute("UPDATE entries SET committed_at=COALESCE((SELECT submitted_at FROM sheets WHERE id=entries.sheet_id), 'Previously submitted') WHERE committed_at IS NULL AND sheet_id IN (SELECT id FROM sheets WHERE status='submitted')")
        db().commit()
        if not db().execute("SELECT id FROM users WHERE role='admin'").fetchone():
            pw = app.config['ADMIN_PASSWORD']
            email = app.config['ADMIN_EMAIL']
            if len(pw) < 12 or not valid_email(email):
                raise RuntimeError('First launch requires ADMIN_EMAIL and ADMIN_PASSWORD (at least 12 characters).')
            db().execute("INSERT INTO users(role,first_name,email,username,password_hash) VALUES('admin','SB Empire',?,?,?)",
                         (email, app.config['ADMIN_USERNAME'], generate_password_hash(pw)))
            db().commit()

    def today():
        return datetime.now(ZoneInfo('Australia/Brisbane')).date()

    def csrf_token():
        if 'csrf' not in session:
            session['csrf'] = secrets.token_urlsafe(32)
        return session['csrf']

    @app.before_request
    def protect():
        if request.endpoint != 'employee_documents':
            request.max_content_length = 128 * 1024
        g.user = None
        if session.get('uid'):
            user = db().execute('SELECT * FROM users WHERE id=?', (session['uid'],)).fetchone()
            if user and user['status'] == 'active' and user['auth_version'] == session.get('version'):
                g.user = user
            else:
                session.clear()
        if request.method == 'POST':
            if not secrets.compare_digest(session.get('csrf', ''), request.form.get('csrf', '')) or not session.get('csrf'):
                abort(400, 'Your form expired. Refresh the page and try again.')

    @app.after_request
    def headers(response):
        response.headers['Cache-Control'] = 'no-store'
        response.headers['X-Content-Type-Options'] = 'nosniff'
        response.headers['X-Frame-Options'] = 'DENY'
        response.headers['Referrer-Policy'] = 'no-referrer'
        response.headers['Content-Security-Policy'] = "default-src 'self'; style-src 'self'; script-src 'self'; img-src 'self' data:; form-action 'self'; frame-ancestors 'none'; base-uri 'self'"
        if request.endpoint == 'document_file':
            response.headers['Content-Security-Policy'] = "sandbox; default-src 'none'; frame-ancestors 'self'"
            response.headers['X-Frame-Options'] = 'SAMEORIGIN'
        elif request.endpoint == 'view_document':
            response.headers['Content-Security-Policy'] += "; frame-src 'self'"
        if app.config['SESSION_COOKIE_SECURE']:
            response.headers['Strict-Transport-Security'] = 'max-age=31536000'
        return response

    def require(role):
        def decorator(fn):
            @wraps(fn)
            def wrapped(*args, **kwargs):
                if not g.user:
                    return redirect(url_for('login', audience=role))
                if g.user['role'] != role:
                    abort(403)
                return fn(*args, **kwargs)
            return wrapped
        return decorator

    def staff(user_id):
        row = db().execute("SELECT * FROM users WHERE id=? AND role='employee'", (user_id,)).fetchone()
        if not row:
            abort(404)
        return row

    def money(cents):
        return f'${cents / 100:,.2f}'

    def hours(units):
        return f'{units / 100:g}'

    def amount(units, cents):
        return int((Decimal(units) * cents / 100).quantize(Decimal('1'), rounding=ROUND_HALF_UP))

    app.jinja_env.filters.update(money=money, hours=hours, display_date=display_date, display_datetime=display_datetime)
    app.context_processor(lambda: dict(csrf_token=csrf_token, current_user=g.user, today=today(), mail_ready=mail_ready()))

    def mail_ready():
        return bool(app.config['SMTP_HOST'] and app.config['SMTP_FROM'] and app.config['APP_URL'].startswith('https://'))

    def limited(key):
        now = int(time.time())
        row = db().execute('SELECT * FROM login_attempts WHERE key=?', (key,)).fetchone()
        if row and now - row['started'] < 900 and row['count'] >= 10:
            return True
        db().execute('INSERT INTO login_attempts(key,count,started) VALUES(?,1,?) ON CONFLICT(key) DO UPDATE SET count=CASE WHEN started<? THEN 1 ELSE count+1 END, started=CASE WHEN started<? THEN excluded.started ELSE started END', (key, now, now - 900, now - 900))
        db().execute('DELETE FROM login_attempts WHERE started<?', (now - 86400,))
        db().commit()
        return False

    @app.get('/health')
    def health():
        db().execute('SELECT 1')
        return {'status': 'ok'}

    @app.get('/')
    def index():
        if g.user:
            return redirect(url_for('admin_dashboard' if g.user['role'] == 'admin' else 'employee_dashboard'))
        return redirect(url_for('login', audience='employee'))

    @app.route('/<audience>/login', methods=['GET', 'POST'])
    def login(audience):
        if audience not in ('employee', 'admin'):
            abort(404)
        if request.method == 'POST':
            username = request.form.get('username', '').strip().lower()[:100]
            key = hashlib.sha256(f'login:{audience}:{username}'.encode()).hexdigest()
            ipkey = hashlib.sha256(f'ip:{request.remote_addr}'.encode()).hexdigest()
            if limited(key) or limited(ipkey):
                flash('Too many sign-in attempts. Please try again in 15 minutes.', 'error')
                return render_template('login.html', audience=audience), 429
            user = db().execute('SELECT * FROM users WHERE username=? AND role=?', (username, audience)).fetchone()
            # Perform a password hash check even for unknown users.
            hashed = user['password_hash'] if user and user['password_hash'] else app.config['DUMMY_HASH']
            matched = check_password_hash(hashed, request.form.get('password', ''))
            if user and matched and user['status'] == 'active':
                session.clear()
                session.update(uid=user['id'], version=user['auth_version'])
                session.permanent = True
                db().execute('DELETE FROM login_attempts WHERE key IN (?,?)', (key, ipkey))
                db().commit()
                return redirect(url_for('index'))
            flash('Username or password is incorrect, or your account is inactive.', 'error')
        return render_template('login.html', audience=audience)

    app.config['DUMMY_HASH'] = generate_password_hash(secrets.token_urlsafe(32))

    @app.post('/logout')
    def logout():
        session.clear()
        return redirect(url_for('index'))

    def parse_details(admin=False):
        fields = ['first_name', 'last_name', 'email', 'mobile', 'emergency_name', 'emergency_email']
        values = {k: request.form.get(k, '').strip() for k in fields}
        if any(len(v) > 200 for v in values.values()):
            raise ValueError('Please keep each contact field under 200 characters.')
        if not all(values[k] for k in fields):
            raise ValueError('Please complete all staff contact fields.')
        if not valid_email(values['email']) or not valid_email(values['emergency_email']):
            raise ValueError('Enter valid contact and emergency contact email addresses.')
        # Optional fields: omitted keys preserve records from older forms.
        for field in ('licence_number', 'licence_state', 'licence_expiry'):
            if field in request.form:
                values[field] = request.form.get(field, '').strip()
        if len(values.get('licence_number', '')) > 80:
            raise ValueError('Licence number must be 80 characters or fewer.')
        if values.get('licence_state', '') not in ('', 'ACT', 'NSW', 'NT', 'QLD', 'SA', 'TAS', 'VIC', 'WA'):
            raise ValueError('Choose a valid licence state or territory.')
        expiry = values.get('licence_expiry', '')
        if expiry:
            try:
                values['licence_expiry'] = parse_calendar_date(expiry).isoformat()
            except ValueError:
                raise ValueError('Enter a valid licence expiry date.')
        if admin:
            values['username'] = request.form.get('username', '').strip().lower()
            if not re.fullmatch(r'[a-z0-9._@+-]{3,80}', values['username']):
                raise ValueError('Username must be 3–80 letters, numbers, or . _ @ + - characters.')
            values['rate_cents'] = decimal_units(request.form.get('hourly_rate', ''), 'Hourly rate', 100000)
            values['status'] = request.form.get('status')
            if values['status'] not in ('active', 'terminated'):
                raise ValueError('Choose active or terminated.')
            values['week_ending'] = 6
        return values

    @app.get('/admin/staff')
    @require('admin')
    def admin_staff():
        people = db().execute("SELECT * FROM users WHERE role='employee' ORDER BY status,first_name,last_name").fetchall()
        return render_template('staff.html', people=people, active=sum(p['status'] == 'active' for p in people), tab='staff')

    @app.route('/admin/staff/new', methods=['GET', 'POST'])
    @require('admin')
    def add_staff():
        return edit_staff(None)

    @app.route('/admin/staff/<int:user_id>/edit', methods=['GET', 'POST'])
    @require('admin')
    def edit_staff(user_id):
        person = staff(user_id) if user_id else None
        if request.method == 'POST':
            try:
                values = parse_details(admin=True)
                password = request.form.get('password', '')
                if password and len(password) < 12:
                    raise ValueError('Passwords must have at least 12 characters.')
                db().execute('BEGIN IMMEDIATE')
                if person:
                    db().execute('UPDATE users SET ' + ','.join(f'{k}=?' for k in values) + ' WHERE id=?', (*values.values(), user_id))
                    if person['status'] != values['status'] or person['username'] != values['username']:
                        db().execute('UPDATE users SET auth_version=auth_version+1 WHERE id=?', (user_id,))
                else:
                    cursor = db().execute("INSERT INTO users(role," + ','.join(values) + ") VALUES('employee'," + ','.join('?' for _ in values) + ')', tuple(values.values()))
                    user_id = cursor.lastrowid
                if password:
                    db().execute('UPDATE users SET password_hash=?,auth_version=auth_version+1 WHERE id=?', (generate_password_hash(password), user_id))
                if password or (person and (person['email'] != values['email'] or person['status'] != values['status'])):
                    db().execute('DELETE FROM invitations WHERE user_id=?', (user_id,))
                db().commit()
                flash('Staff details saved.', 'success')
                return redirect(url_for('admin_staff'))
            except (ValueError, sqlite3.IntegrityError) as error:
                db().rollback()
                flash(str(error) if isinstance(error, ValueError) else 'That username or email is already in use.', 'error')
                person = dict(request.form)
        return render_template('staff_form.html', person=person, editing=bool(user_id), tab='staff')

    @app.post('/admin/staff/<int:user_id>/invite')
    @require('admin')
    def invite(user_id):
        person = staff(user_id)
        if person['status'] != 'active':
            flash('Only active employees can be invited.', 'error')
            return redirect(url_for('admin_staff'))
        if not mail_ready():
            flash('Email is not connected yet. Configure the email settings listed on the Settings tab.', 'error')
            return redirect(url_for('admin_settings'))
        token = secrets.token_urlsafe(32)
        digest = hashlib.sha256(token.encode()).hexdigest()
        link = app.config['APP_URL'] + url_for('activate', token=token)
        msg = EmailMessage()
        msg['Subject'] = 'Your SB EMPIRE employee account'
        msg['From'] = app.config['SMTP_FROM']
        msg['To'] = person['email']
        msg.set_content(f"Hi {person['first_name']},\n\nYour SB EMPIRE employee account is ready.\nUsername: {person['username']}\nEmail: {person['email']}\n\nSet your password using this single-use link (expires in 48 hours):\n{link}\n\nEmployee login: {app.config['APP_URL']}/employee/login\n\nYou can update your contact details, record daily hours and activities, and submit your weekly timesheet to admin.\n\nSB EMPIRE")
        db().execute('INSERT INTO invitations VALUES(?,?,?)', (digest, user_id, int(time.time()) + 48 * 3600))
        db().commit()
        try:
            cls = smtplib.SMTP_SSL if app.config['SMTP_SSL'] else smtplib.SMTP
            with cls(app.config['SMTP_HOST'], app.config['SMTP_PORT'], timeout=15) as smtp:
                if not app.config['SMTP_SSL']:
                    smtp.starttls(context=ssl.create_default_context())
                if app.config['SMTP_USERNAME']:
                    smtp.login(app.config['SMTP_USERNAME'], app.config['SMTP_PASSWORD'])
                smtp.send_message(msg)
        except (OSError, smtplib.SMTPException):
            db().execute('DELETE FROM invitations WHERE token_hash=?', (digest,))
            db().commit()
            flash('The invitation could not be sent. Check the email connection and try again.', 'error')
        else:
            db().execute('DELETE FROM invitations WHERE user_id=? AND token_hash<>?', (user_id, digest))
            db().execute('UPDATE users SET invited_at=? WHERE id=?', (datetime.now().isoformat(), user_id))
            db().commit()
            flash(f"Invitation emailed to {person['email']}.", 'success')
        return redirect(url_for('admin_staff'))

    @app.route('/activate/<token>', methods=['GET', 'POST'])
    def activate(token):
        digest = hashlib.sha256(token.encode()).hexdigest()
        invitation = db().execute("SELECT i.*,u.first_name,u.username FROM invitations i JOIN users u ON u.id=i.user_id WHERE token_hash=? AND expires_at>? AND u.status='active'", (digest, int(time.time()))).fetchone()
        if not invitation:
            return render_template('error.html', message='This invitation has expired or has already been used. Ask your admin to send a new invitation.'), 400
        if request.method == 'POST':
            pw = request.form.get('password', '')
            if len(pw) < 12 or pw != request.form.get('confirm_password'):
                flash('Use at least 12 characters and make sure both passwords match.', 'error')
            else:
                hashed = generate_password_hash(pw)
                db().execute('BEGIN IMMEDIATE')
                valid = db().execute('SELECT user_id FROM invitations WHERE token_hash=? AND expires_at>?', (digest, int(time.time()))).fetchone()
                if not valid:
                    db().rollback()
                    abort(400, 'Invitation has already been used.')
                db().execute('UPDATE users SET password_hash=?,auth_version=auth_version+1 WHERE id=?', (hashed, invitation['user_id']))
                db().execute('DELETE FROM invitations WHERE user_id=?', (invitation['user_id'],))
                db().commit()
                session.clear()
                flash('Your password is set. You can now sign in.', 'success')
                return redirect(url_for('login', audience='employee'))
        return render_template('activate.html', invitation=invitation)

    @app.route('/employee/details', methods=['GET', 'POST'])
    @require('employee')
    def employee_details():
        person = g.user
        if request.method == 'POST':
            try:
                values = parse_details()
                db().execute('UPDATE users SET ' + ','.join(f'{k}=?' for k in values) + ' WHERE id=?', (*values.values(), g.user['id']))
                if values['email'] != g.user['email']:
                    db().execute('DELETE FROM invitations WHERE user_id=?', (g.user['id'],))
                db().commit()
                flash('Your details have been updated.', 'success')
                return redirect(url_for('employee_details'))
            except (ValueError, sqlite3.IntegrityError) as error:
                db().rollback()
                flash(str(error) if isinstance(error, ValueError) else 'That email is already in use.', 'error')
                person = dict(g.user) | dict(request.form)
        return render_template('details.html', person=person, tab='details')

    def week_end(day, ending):
        return day + timedelta(days=(ending - day.weekday()) % 7)

    def sheet_data(person, ending):
        sheet = db().execute('SELECT * FROM sheets WHERE user_id=? AND week_end=?', (person['id'], ending.isoformat())).fetchone()
        rows = db().execute('SELECT * FROM entries WHERE sheet_id=? ORDER BY work_date', (sheet['id'],)).fetchall() if sheet else []
        by_day = {row['work_date']: row for row in rows}
        days = [ending - timedelta(days=i) for i in range(6, -1, -1)]
        units = sum(r['units'] for r in rows)
        rate = sheet['rate_cents'] if sheet and sheet['status'] == 'submitted' else person['rate_cents']
        total = sheet['total_cents'] if sheet and sheet['status'] == 'submitted' else amount(units, rate)
        # Existing Friday records retain their original totals. Their dates cannot
        # be entered again in a new Monday-Sunday sheet.
        booked = db().execute('SELECT e.work_date,s.week_end FROM entries e JOIN sheets s ON s.id=e.sheet_id WHERE e.user_id=? AND e.work_date BETWEEN ? AND ? AND s.week_end<>?',
            (person['id'], days[0].isoformat(), ending.isoformat(), ending.isoformat())).fetchall()
        return dict(sheet=sheet, days=days, entries=by_day, total_units=units, rate=rate, total_cents=total,
                    booked_days={row['work_date']: row['week_end'] for row in booked}, legacy_week=ending.weekday() != 6,
                    payment=db().execute('SELECT * FROM payments WHERE sheet_id=?',(sheet['id'],)).fetchone() if sheet else None)

    def parse_day():
        start = request.form.get('start_time', '').strip()
        finish = request.form.get('finish_time', '').strip()
        activity = request.form.get('activity', '').strip()
        if len(activity) > 2000:
            raise ValueError('Activity notes must be no more than 2,000 characters per day.')
        if not start and not finish:
            units = 0
        else:
            if not re.fullmatch(r'[0-2][0-9]:[0-5][0-9]', start) or not re.fullmatch(r'[0-2][0-9]:[0-5][0-9]', finish):
                raise ValueError('Enter valid start and finish times.')
            sh, sm = map(int, start.split(':'))
            fh, fm = map(int, finish.split(':'))
            if sh > 23 or fh > 23:
                raise ValueError('Enter valid start and finish times.')
            minutes = fh * 60 + fm - sh * 60 - sm
            if minutes <= 0:
                raise ValueError('Finish time must be after start time. Split overnight work across the two dates.')
            units = int((Decimal(minutes) * 100 / 60).quantize(Decimal('1'), rounding=ROUND_HALF_UP))
            if not activity:
                raise ValueError('Add activity notes for this day.')
        return start or None, finish or None, units, activity

    @app.route('/employee/timesheet', methods=['GET', 'POST'])
    @require('employee')
    def employee_timesheet():
        person = g.user
        try:
            ending = date.fromisoformat(request.values.get('week', week_end(today(), 6).isoformat()))
        except ValueError:
            abort(400, 'Invalid week date.')
        existing = db().execute('SELECT * FROM sheets WHERE user_id=? AND week_end=?', (person['id'], ending.isoformat())).fetchone()
        if not existing and ending.weekday() != 6:
            abort(400, 'Timesheets run Monday to Sunday. Choose a Sunday week ending.')
        if ending > week_end(today(), 6):
            abort(400, 'Future timesheets are not available yet.')
        data = sheet_data(person, ending)
        if request.method == 'POST':
            try:
                action = request.form.get('action')
                if action not in ('save_day', 'commit_day', 'submit'):
                    raise ValueError('Choose Save day, Commit day or Submit to admin.')
                db().execute('BEGIN IMMEDIATE')
                current = db().execute('SELECT * FROM sheets WHERE user_id=? AND week_end=?', (person['id'], ending.isoformat())).fetchone()
                if current and current['status'] == 'submitted':
                    raise ValueError('This timesheet has been submitted and is locked.')
                if action == 'submit':
                    rows = db().execute('SELECT * FROM entries WHERE sheet_id=?', (current['id'],)).fetchall() if current else []
                    units = sum(row['units'] for row in rows)
                    if not units:
                        raise ValueError('Enter some worked hours before submitting.')
                    if any(not row['committed_at'] for row in rows if row['units'] or row['activity']):
                        raise ValueError('Commit each entered day before submitting the week.')
                    rate = db().execute('SELECT rate_cents FROM users WHERE id=?', (person['id'],)).fetchone()['rate_cents']
                    db().execute("UPDATE sheets SET status='submitted',rate_cents=?,total_units=?,total_cents=?,submitted_at=? WHERE id=?",
                                 (rate, units, amount(units, rate), datetime.now(ZoneInfo('Australia/Brisbane')).isoformat(), current['id']))
                else:
                    try:
                        day = date.fromisoformat(request.form.get('work_date', ''))
                    except ValueError:
                        raise ValueError('Choose a valid work date.')
                    if day not in data['days'] or day > today():
                        raise ValueError('Choose a day in this week that is not in the future.')
                    old = db().execute('SELECT * FROM entries WHERE user_id=? AND work_date=?', (person['id'], day.isoformat())).fetchone()
                    if old and (not current or old['sheet_id'] != current['id']):
                        raise ValueError('This date belongs to another timesheet. Ask admin to check the week ending.')
                    if old and old['committed_at']:
                        raise ValueError('This day is committed and locked. Only an administrator can change it.')
                    start, finish, units, activity = parse_day()
                    if old and old['units'] and not old['start_time'] and not start and not finish:
                        raise ValueError('This day has previous hours. Enter start and finish times before saving or committing it.')
                    if not current:
                        sid = db().execute('INSERT INTO sheets(user_id,week_end) VALUES(?,?)', (person['id'], ending.isoformat())).lastrowid
                    else:
                        sid = current['id']
                    committed = datetime.now(ZoneInfo('Australia/Brisbane')).isoformat() if action == 'commit_day' else None
                    db().execute("""INSERT INTO entries(user_id,work_date,sheet_id,units,activity,start_time,finish_time,committed_at)
                        VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(user_id,work_date) DO UPDATE SET
                        units=excluded.units,activity=excluded.activity,start_time=excluded.start_time,
                        finish_time=excluded.finish_time,committed_at=excluded.committed_at""",
                        (person['id'], day.isoformat(), sid, units, activity, start, finish, committed))
                db().commit()
                flash({'save_day': 'Day saved.', 'commit_day': 'Day committed and locked.', 'submit': 'Timesheet submitted to admin.'}[action], 'success')
                return redirect(url_for('employee_timesheet', week=ending.isoformat()))
            except (ValueError, sqlite3.IntegrityError) as error:
                db().rollback()
                flash(str(error) if isinstance(error, ValueError) else 'This date overlaps existing hours. Please contact admin.', 'error')
        return render_template('timesheet.html', **data, person=person, ending=ending, tab='timesheet', admin_view=False,
                               previous=week_end(ending, 6) - timedelta(days=7), following=week_end(ending, 6) + timedelta(days=7), current_ending=week_end(today(), 6))

    @app.get('/employee/history')
    @require('employee')
    def history():
        sheets = db().execute("SELECT s.*,u.first_name,u.last_name,p.id AS payment_id FROM sheets s JOIN users u ON u.id=s.user_id LEFT JOIN payments p ON p.sheet_id=s.id WHERE s.user_id=? ORDER BY week_end DESC", (g.user['id'],)).fetchall()
        sheets = [dict(row) for row in sheets]
        for row in sheets:
            if row['status'] == 'draft':
                row['total_units'] = db().execute('SELECT COALESCE(SUM(units),0) FROM entries WHERE sheet_id=?', (row['id'],)).fetchone()[0]
                row['rate_cents'] = g.user['rate_cents']
                row['total_cents'] = amount(row['total_units'], row['rate_cents'])
        return render_template('submissions.html', sheets=sheets, week_groups=group_timesheets(sheets), tab='history', admin_view=False)

    @app.get('/admin/timesheets')
    @require('admin')
    def admin_timesheets():
        sheets = db().execute("""SELECT s.id,s.user_id,s.week_end,s.status,s.submitted_at,u.first_name,u.last_name,
            CASE WHEN s.status='submitted' THEN s.total_units ELSE COALESCE((SELECT SUM(e.units) FROM entries e WHERE e.sheet_id=s.id),0) END total_units,
            CASE WHEN s.status='submitted' THEN s.rate_cents ELSE u.rate_cents END rate_cents,
            s.total_cents,p.id AS payment_id FROM sheets s JOIN users u ON u.id=s.user_id LEFT JOIN payments p ON p.sheet_id=s.id ORDER BY s.week_end DESC,u.first_name""").fetchall()
        sheets = [dict(row) for row in sheets]
        for row in sheets:
            if row["status"] == "draft":
                row["total_cents"] = amount(row["total_units"], row["rate_cents"])
        return render_template('submissions.html', sheets=sheets, week_groups=group_timesheets(sheets), tab='submissions', admin_view=True)

    @app.get('/admin/timesheets/<int:sheet_id>')
    @require('admin')
    def view_sheet(sheet_id):
        sheet = db().execute("SELECT * FROM sheets WHERE id=?", (sheet_id,)).fetchone()
        if not sheet:
            abort(404)
        person = staff(sheet['user_id'])
        ending = date.fromisoformat(sheet['week_end'])
        return render_template('timesheet.html', **sheet_data(person, ending), person=person, ending=ending, tab='submissions', admin_view=True)

    @app.route('/admin/timesheets/<int:sheet_id>/days/<work_date>', methods=['GET', 'POST'])
    @require('admin')
    def edit_day(sheet_id, work_date):
        sheet = db().execute('SELECT * FROM sheets WHERE id=?', (sheet_id,)).fetchone()
        if not sheet:
            abort(404)
        entry = db().execute('SELECT * FROM entries WHERE sheet_id=? AND work_date=?', (sheet_id, work_date)).fetchone()
        if not entry:
            abort(404)
        person = staff(sheet['user_id'])
        if db().execute('SELECT id FROM payments WHERE sheet_id=?',(sheet_id,)).fetchone():
            flash('Payment has been processed. This timesheet is locked to preserve the payment record.', 'error')
            return redirect(url_for('view_sheet',sheet_id=sheet_id))
        if request.method == 'POST':
            try:
                action = request.form.get('action')
                reason = request.form.get('reason', '').strip()
                if action not in ('correct', 'unlock'):
                    raise ValueError('Choose Save correction or Unlock day.')
                if not reason or len(reason) > 1000:
                    raise ValueError('Enter a reason for the change (up to 1,000 characters).')
                values = parse_day() if action == 'correct' else None
                db().execute('BEGIN IMMEDIATE')
                if db().execute('SELECT id FROM payments WHERE sheet_id=?',(sheet_id,)).fetchone():
                    raise ValueError('Payment has been processed. This timesheet is locked.')
                before = db().execute('SELECT * FROM entries WHERE sheet_id=? AND work_date=?', (sheet_id, work_date)).fetchone()
                current = db().execute('SELECT * FROM sheets WHERE id=?', (sheet_id,)).fetchone()
                now = datetime.now(ZoneInfo('Australia/Brisbane')).isoformat()
                db().execute('INSERT INTO entry_audit(admin_id,user_id,work_date,action,reason,before_json,changed_at) VALUES(?,?,?,?,?,?,?)',
                    (g.user['id'], person['id'], work_date, action, reason, json.dumps({'entry': dict(before), 'sheet': dict(current)}), now))
                if action == 'unlock':
                    db().execute('UPDATE entries SET committed_at=NULL WHERE sheet_id=? AND work_date=?', (sheet_id, work_date))
                    db().execute("UPDATE sheets SET status='draft',rate_cents=NULL,total_units=NULL,total_cents=NULL,submitted_at=NULL WHERE id=?", (sheet_id,))
                else:
                    start, finish, units, activity = values
                    db().execute('UPDATE entries SET start_time=?,finish_time=?,units=?,activity=?,committed_at=? WHERE sheet_id=? AND work_date=?',
                        (start, finish, units, activity, now, sheet_id, work_date))
                    if current['status'] == 'submitted':
                        total = db().execute('SELECT COALESCE(SUM(units),0) AS units FROM entries WHERE sheet_id=?', (sheet_id,)).fetchone()['units']
                        db().execute('UPDATE sheets SET total_units=?,total_cents=? WHERE id=?', (total, amount(total, current['rate_cents']), sheet_id))
                db().commit()
                flash('Day unlocked. The employee must commit it again and resubmit the week.' if action == 'unlock' else 'Correction saved. The day remains locked.', 'success')
                return redirect(url_for('view_sheet', sheet_id=sheet_id))
            except (ValueError, sqlite3.IntegrityError) as error:
                db().rollback()
                flash(str(error) if isinstance(error, ValueError) else 'The correction could not be saved.', 'error')
        audit = db().execute('SELECT a.*,u.first_name,u.last_name FROM entry_audit a JOIN users u ON u.id=a.admin_id WHERE a.user_id=? AND a.work_date=? ORDER BY a.id DESC', (person['id'], work_date)).fetchall()
        return render_template('day_edit.html', sheet=sheet, entry=entry, person=person, audit=audit, tab='submissions')

    @app.route('/admin/timesheets/<int:sheet_id>/payment', methods=['GET','POST'])
    @require('admin')
    def process_payment(sheet_id):
        sheet=db().execute('SELECT * FROM sheets WHERE id=?',(sheet_id,)).fetchone()
        if not sheet:
            abort(404)
        person=staff(sheet['user_id'])
        existing=db().execute('SELECT id FROM payments WHERE sheet_id=?',(sheet_id,)).fetchone()
        if existing:
            flash('Payment has already been processed for this timesheet.', 'error')
            return redirect(url_for('payslip',payment_id=existing['id']))
        if sheet['status'] != 'submitted':
            flash('The staff member must submit this timesheet before payment can be processed.', 'error')
            return redirect(url_for('view_sheet',sheet_id=sheet_id))
        if request.method == 'POST':
            try:
                cash=decimal_units(request.form.get('cash_amount',''), 'Cash amount', 100000000)
                transfer=decimal_units(request.form.get('transfer_amount',''), 'Transfer amount', 100000000)
                db().execute('BEGIN IMMEDIATE')
                current=db().execute('SELECT * FROM sheets WHERE id=?',(sheet_id,)).fetchone()
                if db().execute('SELECT id FROM payments WHERE sheet_id=?',(sheet_id,)).fetchone():
                    raise ValueError('Payment has already been processed for this timesheet.')
                if current['status'] != 'submitted':
                    raise ValueError('This timesheet was reopened. It must be submitted again before payment.')
                if cash+transfer != current['total_cents'] or current['total_cents'] <= 0:
                    raise ValueError('Cash and transfer must add up to the full amount due: ' + money(current['total_cents']) + '.')
                if request.form.get('expected_total') != str(current['total_cents']):
                    raise ValueError('The amount due changed. Reload this payment page and check the new amount.')
                ending=date.fromisoformat(current['week_end'])
                payment_id=db().execute("""INSERT INTO payments(sheet_id,user_id,admin_id,employee_name,employee_username,processed_by,
                    week_start,week_end,total_units,rate_cents,total_cents,cash_cents,transfer_cents,processed_at)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",(sheet_id,person['id'],g.user['id'],person['first_name']+' '+person['last_name'],
                    person['username'],g.user['first_name']+' '+g.user['last_name'],(ending-timedelta(days=6)).isoformat(),
                    current['week_end'],current['total_units'],current['rate_cents'],current['total_cents'],cash,transfer,timestamp())).lastrowid
                db().commit()
                flash('Payment processed. The payment slip is now available to the staff member.', 'success')
                return redirect(url_for('payslip',payment_id=payment_id))
            except (ValueError,sqlite3.IntegrityError) as error:
                db().rollback()
                flash(str(error) if isinstance(error,ValueError) else 'A payment is already recorded for this timesheet.', 'error')
        return render_template('process_payment.html',sheet=sheet,person=person,tab='submissions')

    @app.get('/payments/<int:payment_id>/slip')
    def payslip(payment_id):
        if not g.user:
            abort(403)
        payment=db().execute('SELECT * FROM payments WHERE id=?',(payment_id,)).fetchone()
        if not payment or (g.user['role'] != 'admin' and payment['user_id'] != g.user['id']):
            abort(404)
        return render_template('payslip.html',payment=payment,admin_view=g.user['role']=='admin',tab='submissions' if g.user['role']=='admin' else 'payslips')

    @app.get('/employee/payslips')
    @require('employee')
    def employee_payslips():
        payments=db().execute('SELECT * FROM payments WHERE user_id=? ORDER BY week_end DESC,id DESC',(g.user['id'],)).fetchall()
        return render_template('payslips.html',payments=payments,tab='payslips')

    def timestamp():
        return datetime.now(ZoneInfo('Australia/Brisbane')).isoformat()

    def notice_rows(user_id=None, limit=-1):
        sql = "SELECT n.*,u.first_name,u.last_name,a.first_name AS sender_name FROM staff_notices n JOIN users u ON u.id=n.user_id JOIN users a ON a.id=n.admin_id"
        if user_id is None:
            return db().execute(sql + ' ORDER BY n.id DESC LIMIT ?', (limit,)).fetchall()
        return db().execute(sql + ' WHERE n.user_id=? ORDER BY (n.read_at IS NULL) DESC,n.id DESC LIMIT ?', (user_id,limit)).fetchall()

    def notice_summary():
        return dict(db().execute('SELECT COUNT(*) AS total,COALESCE(SUM(read_at IS NULL),0) AS unread,COALESCE(MAX(id),0) AS latest FROM staff_notices WHERE user_id=?', (g.user['id'],)).fetchone())

    @app.route('/admin/hit-me', methods=['GET','POST'])
    @require('admin')
    def admin_hit_me():
        if request.method == 'POST':
            try:
                try:
                    recipient_id = int(request.form.get('user_id', ''))
                except ValueError:
                    raise ValueError('Choose a staff member.')
                title = request.form.get('title', '').strip() or 'Work update'
                body = request.form.get('body', '').strip()
                priority = request.form.get('priority', 'normal')
                if len(title) > 150 or not body or len(body) > 10000:
                    raise ValueError('Enter job details up to 10,000 characters and a title up to 150 characters.')
                if priority not in ('normal','urgent'):
                    raise ValueError('Choose Normal or Urgent priority.')
                db().execute('BEGIN IMMEDIATE')
                recipient = db().execute("SELECT id FROM users WHERE id=? AND role='employee' AND status='active'", (recipient_id,)).fetchone()
                if not recipient:
                    raise ValueError('Choose an active staff member.')
                db().execute('INSERT INTO staff_notices(user_id,admin_id,title,body,priority,sent_at) VALUES(?,?,?,?,?,?)',
                             (recipient_id,g.user['id'],title,body,priority,timestamp()))
                db().commit()
                flash('Hit Me notice sent to the staff member’s dashboard.', 'success')
                return redirect(url_for('admin_hit_me'))
            except ValueError as error:
                db().rollback()
                flash(str(error),'error')
        people = db().execute("SELECT id,first_name,last_name FROM users WHERE role='employee' AND status='active' ORDER BY first_name,last_name").fetchall()
        return render_template('hit_me.html', people=people, notices=notice_rows(), admin_view=True, tab='hit-me')

    @app.get('/employee/hit-me')
    @require('employee')
    def employee_hit_me():
        return render_template('hit_me.html', notices=notice_rows(g.user['id']), summary=notice_summary(), admin_view=False, tab='hit-me')

    @app.post('/employee/hit-me/<int:notice_id>/read')
    @require('employee')
    def read_notice(notice_id):
        row=db().execute('SELECT id FROM staff_notices WHERE id=? AND user_id=?', (notice_id,g.user['id'])).fetchone()
        if not row:
            abort(404)
        db().execute('UPDATE staff_notices SET read_at=? WHERE id=? AND user_id=? AND read_at IS NULL', (timestamp(),notice_id,g.user['id']))
        db().commit()
        flash('Notice marked as read.', 'success')
        return redirect(url_for('employee_dashboard' if request.form.get('return_to') == 'dashboard' else 'employee_hit_me'))

    @app.get('/employee/hit-me/status')
    @require('employee')
    def hit_me_status():
        return notice_summary()

    @app.get('/employee/dashboard')
    @require('employee')
    def employee_dashboard():
        day=today()
        ending=week_end(day,6)
        entry=db().execute('SELECT e.*,s.status AS sheet_status FROM entries e JOIN sheets s ON s.id=e.sheet_id WHERE e.user_id=? AND e.work_date=?', (g.user['id'],day.isoformat())).fetchone()
        sheet=db().execute('SELECT status FROM sheets WHERE user_id=? AND week_end=?', (g.user['id'],ending.isoformat())).fetchone()
        if entry and (entry['committed_at'] or entry['sheet_status']=='submitted'):
            timesheet_state='committed'
        elif entry:
            timesheet_state='draft'
        elif sheet and sheet['status']=='submitted':
            timesheet_state='submitted'
        else:
            timesheet_state='missing'
        expiry=g.user['licence_expiry']
        try:
            expiry_days=(date.fromisoformat(expiry)-day).days if expiry else None
        except ValueError:
            expiry_days=None
        leave_pending=db().execute("SELECT COUNT(*) FROM leave_requests WHERE user_id=? AND status='pending'", (g.user['id'],)).fetchone()[0]
        return render_template('employee_dashboard.html', tab='home', admin_view=False, summary=notice_summary(),
                               notices=notice_rows(g.user['id'],limit=6), timesheet_state=timesheet_state,
                               expiry_days=expiry_days, expiry=expiry, leave_pending=leave_pending,
                               latest_payment=db().execute('SELECT * FROM payments WHERE user_id=? ORDER BY id DESC LIMIT 1',(g.user['id'],)).fetchone())

    def prestart_rows(user_id=None, failed=False):
        clauses, args = [], []
        if user_id is not None:
            clauses.append('user_id=?')
            args.append(user_id)
        if failed:
            clauses.append("(failure_count>0 OR fit_for_duty='no')")
        return db().execute('SELECT * FROM prestarts' + (' WHERE ' + ' AND '.join(clauses) if clauses else '') + ' ORDER BY id DESC', args).fetchall()

    @app.route('/employee/prestart', methods=['GET', 'POST'])
    @require('employee')
    def employee_prestart():
        asset = request.form.get('asset', '') if request.method == 'POST' else request.args.get('asset', '')
        if asset not in ASSETS:
            asset = ''
        if request.method == 'POST':
            try:
                token = request.form.get('submission_token', '')
                existing = db().execute('SELECT id FROM prestarts WHERE submission_token=? AND user_id=?', (token, g.user['id'])).fetchone()
                if existing:
                    return redirect(url_for('employee_prestart_detail', prestart_id=existing['id']))
                if not token or not secrets.compare_digest(token, session.get('prestart_token', '')):
                    raise ValueError('This prestart form expired. Reopen PRESTART and try again.')
                if not asset:
                    raise ValueError('Choose a machine or vehicle.')
                reading = request.form.get('reading', '').strip()
                if not re.fullmatch(r'\d{1,8}(\.\d{1,2})?', reading):
                    raise ValueError('Enter a valid non-negative meter reading with up to two decimal places.')
                answers = []
                for key, label, allow_na in checklist(asset):
                    value = request.form.get('check_' + key, '')
                    allowed = ('yes', 'no') if key == 'greased' else ('pass', 'fail', 'na') if allow_na else ('pass', 'fail')
                    note = request.form.get('note_' + key, '').strip()
                    if value not in allowed:
                        raise ValueError('Answer every checklist item.')
                    if len(note) > 500 or (value in ('fail', 'no') and not note):
                        raise ValueError('Describe each failed item in up to 500 characters.')
                    answers.append(dict(key=key, label=label, value=value, note=note))
                fit = request.form.get('fit_for_duty', '')
                notes = request.form.get('notes', '').strip()
                signature = request.form.get('signature', '').strip()
                if fit not in ('yes', 'no'):
                    raise ValueError('Select your fit-for-duty status.')
                if len(notes) > 2000 or (fit == 'no' and not notes):
                    raise ValueError('Add notes for a not-fit-for-duty report (up to 2,000 characters).')
                if len(signature) < 3 or len(signature) > 150 or request.form.get('signed') != 'yes':
                    raise ValueError('Type your full name and tick the sign-off declaration.')
                failures = sum(a['value'] in ('fail', 'no') for a in answers)
                try:
                    cursor = db().execute('INSERT INTO prestarts(user_id,employee_name,asset,asset_type,reading,reading_unit,checks_json,failure_count,fit_for_duty,notes,signature,declaration,submitted_at,submission_token) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                        (g.user['id'], (g.user['first_name'] + ' ' + g.user['last_name']).strip(), asset, ASSETS[asset], reading,
                         'hours' if ASSETS[asset] == 'machine' else 'km', json.dumps(answers), failures, fit, notes,
                         signature, DECLARATION, timestamp(), token))
                    db().commit()
                    record_id = cursor.lastrowid
                except sqlite3.IntegrityError:
                    db().rollback()
                    duplicate = db().execute('SELECT id FROM prestarts WHERE submission_token=? AND user_id=?', (token, g.user['id'])).fetchone()
                    if not duplicate:
                        raise
                    record_id = duplicate['id']
                flash('Prestart submitted to admin.' + (' Do not operate until defects or fitness concerns have been addressed with your supervisor.' if failures or fit == 'no' else ''), 'success')
                return redirect(url_for('employee_prestart_detail', prestart_id=record_id))
            except ValueError as error:
                flash(str(error), 'error')
        if request.method == 'GET' or not session.get('prestart_token'):
            session['prestart_token'] = secrets.token_hex(24)
        return render_template('prestart_form.html', tab='prestart', assets=ASSETS, asset=asset,
                               checks=checklist(asset) if asset else [], declaration=DECLARATION,
                               token=session['prestart_token'], records=prestart_rows(g.user['id']), admin_view=False)

    @app.get('/admin/prestart')
    @require('admin')
    def admin_prestart():
        failed = request.args.get('failed') == '1'
        counts = db().execute("SELECT COUNT(*) AS total, COALESCE(SUM(failure_count>0 OR fit_for_duty='no'),0) AS failed FROM prestarts").fetchone()
        return render_template('prestart_admin.html', tab='prestart', admin_view=True, failed=failed,
                               counts=counts, records=prestart_rows(failed=failed))

    def prestart_detail(prestart_id, admin_view):
        row = db().execute('SELECT * FROM prestarts WHERE id=?', (prestart_id,)).fetchone()
        if not row or (not admin_view and row['user_id'] != g.user['id']):
            abort(404)
        return render_template('prestart_detail.html', tab='prestart', admin_view=admin_view,
                               record=row, checks=json.loads(row['checks_json']))

    @app.get('/employee/prestart/<int:prestart_id>')
    @require('employee')
    def employee_prestart_detail(prestart_id):
        return prestart_detail(prestart_id, False)

    @app.get('/admin/prestart/<int:prestart_id>')
    @require('admin')
    def admin_prestart_detail(prestart_id):
        return prestart_detail(prestart_id, True)

    def document_rows(archived=False, user_id=None, limit=-1):
        sql = "SELECT d.id,d.user_id,d.title,d.filename,d.mime_type,length(d.data) AS size,d.uploaded_at,d.archived_at,d.viewed_at,u.first_name,u.last_name FROM documents d JOIN users u ON u.id=d.user_id"
        if user_id is not None:
            return db().execute(sql + ' WHERE d.user_id=? ORDER BY d.id DESC LIMIT ?', (user_id, limit)).fetchall()
        return db().execute(sql + (' WHERE d.archived_at IS NOT NULL' if archived else ' WHERE d.archived_at IS NULL') + ' ORDER BY d.id DESC LIMIT ?', (limit,)).fetchall()

    def leave_rows(user_id=None, pending=False, limit=-1):
        sql = 'SELECT l.*,u.first_name,u.last_name FROM leave_requests l JOIN users u ON u.id=l.user_id'
        if user_id is not None:
            return db().execute(sql + ' WHERE l.user_id=? ORDER BY l.id DESC LIMIT ?', (user_id, limit)).fetchall()
        return db().execute(sql + (" WHERE l.status='pending'" if pending else '') + ' ORDER BY l.id DESC LIMIT ?', (limit,)).fetchall()

    @app.get('/admin/dashboard')
    @require('admin')
    def admin_dashboard():
        counts = dict(documents=db().execute('SELECT COUNT(*) FROM documents WHERE archived_at IS NULL').fetchone()[0],
                      leave=db().execute("SELECT COUNT(*) FROM leave_requests WHERE status='pending'").fetchone()[0],
                      staff=db().execute("SELECT COUNT(*) FROM users WHERE role='employee' AND status='active'").fetchone()[0])
        return render_template('dashboard.html', counts=counts, documents=document_rows(limit=10),
                               requests=leave_rows(pending=True, limit=10), admin_view=True, tab='dashboard')

    @app.route('/employee/documents', methods=['GET', 'POST'])
    @require('employee')
    def employee_documents():
        if request.method == 'POST':
            try:
                title = request.form.get('title', '').strip()
                upload = request.files.get('document')
                if not title or len(title) > 150:
                    raise ValueError('Enter a document title of up to 150 characters.')
                if not upload or not upload.filename:
                    raise ValueError('Choose a PDF, JPG or PNG file.')
                filename = secure_filename(upload.filename)[:180]
                suffix = Path(filename).suffix.lower()
                data = upload.read(app.config['DOCUMENT_MAX_BYTES'] + 1)
                if not data or len(data) > app.config['DOCUMENT_MAX_BYTES']:
                    raise ValueError('Choose a non-empty document no larger than 5 MB.')
                signatures = {'.pdf': ('application/pdf', data.startswith(b'%PDF-')),
                              '.png': ('image/png', data.startswith(b'\x89PNG\r\n\x1a\n')),
                              '.jpg': ('image/jpeg', data.startswith(b'\xff\xd8\xff')),
                              '.jpeg': ('image/jpeg', data.startswith(b'\xff\xd8\xff'))}
                if suffix not in signatures or not signatures[suffix][1]:
                    raise ValueError('Upload a valid PDF, JPG or PNG file.')
                db().execute('INSERT INTO documents(user_id,title,filename,mime_type,data,uploaded_at) VALUES(?,?,?,?,?,?)',
                             (g.user['id'], title, filename, signatures[suffix][0], data, timestamp()))
                db().commit()
                flash('Document uploaded to the admin dashboard.', 'success')
                return redirect(url_for('employee_documents'))
            except ValueError as error:
                flash(str(error), 'error')
        return render_template('documents.html', documents=document_rows(user_id=g.user['id']), admin_view=False, tab='documents')

    @app.get('/admin/documents')
    @require('admin')
    def admin_documents():
        archived = request.args.get('archived') == '1'
        return render_template('documents.html', documents=document_rows(archived=archived), archived=archived, admin_view=True, tab='documents')

    def accessible_document(document_id):
        if not g.user:
            abort(403)
        doc = db().execute('SELECT * FROM documents WHERE id=?', (document_id,)).fetchone()
        if not doc or (g.user['role'] != 'admin' and doc['user_id'] != g.user['id']):
            abort(404)
        return doc

    @app.get('/documents/<int:document_id>/view')
    def view_document(document_id):
        doc = accessible_document(document_id)
        if g.user['role'] == 'admin':
            db().execute('UPDATE documents SET viewed_at=?,viewed_by=? WHERE id=?', (timestamp(), g.user['id'], document_id))
            db().commit()
        return render_template('document_view.html', document=doc, admin_view=g.user['role'] == 'admin', tab='documents')

    @app.get('/documents/<int:document_id>/file')
    def document_file(document_id):
        doc = accessible_document(document_id)
        response = send_file(io.BytesIO(doc['data']), mimetype=doc['mime_type'], download_name=doc['filename'],
                             as_attachment=request.args.get('download') == '1', max_age=0)
        return response

    @app.post('/admin/documents/<int:document_id>/archive')
    @require('admin')
    def archive_document(document_id):
        accessible_document(document_id)
        db().execute('UPDATE documents SET archived_at=?,archived_by=? WHERE id=? AND archived_at IS NULL',
                     (timestamp(), g.user['id'], document_id))
        db().commit()
        flash('Document archived. It remains available in Archived documents.', 'success')
        return redirect(url_for('admin_documents'))

    @app.route('/admin/documents/<int:document_id>/email', methods=['GET', 'POST'])
    @require('admin')
    def email_document(document_id):
        doc = accessible_document(document_id)
        if request.method == 'POST':
            recipient = request.form.get('recipient', '').strip()
            note = request.form.get('message', '').strip()
            if not valid_email(recipient) or ',' in recipient or ';' in recipient:
                flash('Enter one valid recipient email address.', 'error')
            elif len(note) > 2000:
                flash('Keep the message to 2,000 characters.', 'error')
            elif not mail_ready():
                flash('Email is not connected yet. Configure email in Settings before sending.', 'error')
            else:
                msg = EmailMessage()
                msg['Subject'] = 'SB EMPIRE document: ' + doc['filename']
                msg['From'] = app.config['SMTP_FROM']
                msg['To'] = recipient
                msg.set_content(note or 'Please find the attached document from SB EMPIRE.')
                main, sub = doc['mime_type'].split('/')
                msg.add_attachment(doc['data'], maintype=main, subtype=sub, filename=doc['filename'])
                try:
                    cls = smtplib.SMTP_SSL if app.config['SMTP_SSL'] else smtplib.SMTP
                    with cls(app.config['SMTP_HOST'], app.config['SMTP_PORT'], timeout=15) as smtp:
                        if not app.config['SMTP_SSL']:
                            smtp.starttls(context=ssl.create_default_context())
                        if app.config['SMTP_USERNAME']:
                            smtp.login(app.config['SMTP_USERNAME'], app.config['SMTP_PASSWORD'])
                        smtp.send_message(msg)
                except (OSError, smtplib.SMTPException):
                    flash('The document could not be emailed. Check the email connection and try again.', 'error')
                else:
                    db().execute('INSERT INTO document_emails(document_id,admin_id,recipient,sent_at) VALUES(?,?,?,?)',
                                 (document_id, g.user['id'], recipient, timestamp()))
                    db().commit()
                    flash('Document emailed to ' + recipient + '.', 'success')
                    return redirect(url_for('view_document', document_id=document_id))
        sent = db().execute('SELECT recipient,sent_at FROM document_emails WHERE document_id=? ORDER BY id DESC', (document_id,)).fetchall()
        return render_template('document_email.html', document=doc, sent=sent, tab='documents')

    @app.route('/employee/leave', methods=['GET', 'POST'])
    @require('employee')
    def employee_leave():
        if request.method == 'POST':
            try:
                raw_start, raw_end = request.form.get('start_date', ''), request.form.get('end_date', '')
                try:
                    start, end = parse_calendar_date(raw_start), parse_calendar_date(raw_end)
                    raw_start, raw_end = start.isoformat(), end.isoformat()
                except ValueError:
                    raise ValueError('Enter valid dates in DD.MM.YYYY format.')
                if start < today():
                    raise ValueError('Leave must start today or in the future.')
                if end < start:
                    raise ValueError('End date must be on or after the start date.')
                notes = request.form.get('notes', '').strip()
                if len(notes) > 2000:
                    raise ValueError('Keep leave notes to 2,000 characters.')
                db().execute('BEGIN IMMEDIATE')
                overlap = db().execute("SELECT id FROM leave_requests WHERE user_id=? AND status IN ('pending','approved') AND start_date<=? AND end_date>=?", (g.user['id'], raw_end, raw_start)).fetchone()
                if overlap:
                    raise ValueError('These dates overlap a pending or approved leave request.')
                db().execute('INSERT INTO leave_requests(user_id,start_date,end_date,notes,requested_at) VALUES(?,?,?,?,?)',
                             (g.user['id'], raw_start, raw_end, notes, timestamp()))
                db().commit()
                flash('Annual leave request sent to admin for approval.', 'success')
                return redirect(url_for('employee_leave'))
            except ValueError as error:
                db().rollback()
                flash(str(error), 'error')
        return render_template('leave.html', requests=leave_rows(user_id=g.user['id']), admin_view=False, tab='leave')

    @app.get('/admin/leave')
    @require('admin')
    def admin_leave():
        return render_template('leave.html', requests=leave_rows(), admin_view=True, tab='leave')

    @app.post('/admin/leave/<int:leave_id>/decision')
    @require('admin')
    def decide_leave(leave_id):
        decision = request.form.get('decision')
        notes = request.form.get('admin_notes', '').strip()
        if decision not in ('approved', 'rejected') or len(notes) > 2000:
            abort(400, 'Choose Approve or Disapprove and keep comments to 2,000 characters.')
        if not db().execute('SELECT id FROM leave_requests WHERE id=?', (leave_id,)).fetchone():
            abort(404)
        result = db().execute("UPDATE leave_requests SET status=?,admin_notes=?,decided_at=?,decided_by=? WHERE id=? AND status='pending'",
                              (decision, notes, timestamp(), g.user['id'], leave_id))
        db().commit()
        if result.rowcount:
            flash('Annual leave approved.' if decision == 'approved' else 'Annual leave disapproved.', 'success')
        else:
            flash('This request has already been reviewed. Refresh to see the decision.', 'error')
        return redirect(url_for('admin_leave'))

    @app.get('/admin/settings')
    @require('admin')
    def admin_settings():
        return render_template('settings.html', tab='settings')

    @app.errorhandler(400)
    @app.errorhandler(403)
    @app.errorhandler(404)
    @app.errorhandler(413)
    def error_page(error):
        return render_template('error.html', message=error.description), error.code

    return app


def valid_email(value):
    return bool(re.fullmatch(r'[^\s@<>\r\n]+@[^\s@<>\r\n]+\.[^\s@<>\r\n]+', value)) and len(value) <= 200


def decimal_units(raw, label, maximum):
    try:
        value = Decimal(raw)
        if not value.is_finite() or value < 0 or value > maximum or value * 100 != (value * 100).to_integral_value():
            raise ValueError()
        return int(value * 100)
    except (InvalidOperation, ValueError):
        raise ValueError(f'{label} must be between 0 and {maximum:,}, with up to two decimal places.')


def parse_calendar_date(value):
    value = value.strip()
    if re.fullmatch(r'\d{2}\.\d{2}\.\d{4}', value):
        day, month, year = map(int, value.split('.'))
        return date(year, month, day)
    if re.fullmatch(r'\d{4}-\d{2}-\d{2}', value):
        return date.fromisoformat(value)
    raise ValueError('Use DD.MM.YYYY.')


def display_date(value):
    if not value:
        return '—'
    if isinstance(value, date):
        return value.strftime('%d.%m.%Y')
    try:
        return parse_calendar_date(str(value)[:10]).strftime('%d.%m.%Y')
    except ValueError:
        return str(value)


def display_datetime(value):
    if not value:
        return '—'
    try:
        parsed = datetime.fromisoformat(str(value))
        if parsed.tzinfo:
            parsed = parsed.astimezone(ZoneInfo('Australia/Brisbane'))
        return parsed.strftime('%d.%m.%Y · %H:%M')
    except ValueError:
        return display_date(value)


def group_timesheets(sheets):
    groups = {}
    for sheet in sheets:
        end = date.fromisoformat(sheet['week_end'])
        group = groups.setdefault(sheet['week_end'], dict(ending=end, start=end-timedelta(days=6),
                                  legacy=end.weekday()!=6, sheets=[], total_units=0, total_cents=0, submitted=0))
        group['sheets'].append(sheet)
        group['total_units'] += sheet['total_units'] or 0
        group['total_cents'] += sheet['total_cents'] or 0
        group['submitted'] += sheet['status']=='submitted'
    return [groups[key] for key in sorted(groups, reverse=True)]
