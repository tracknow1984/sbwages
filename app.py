import hashlib
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

from flask import Flask, abort, flash, g, redirect, render_template, request, session, url_for
from werkzeug.security import check_password_hash, generate_password_hash

SCHEMA = '''
CREATE TABLE IF NOT EXISTS users (
 id INTEGER PRIMARY KEY, role TEXT NOT NULL CHECK(role IN ('admin','employee')),
 first_name TEXT NOT NULL, last_name TEXT NOT NULL DEFAULT '', email TEXT NOT NULL COLLATE NOCASE UNIQUE,
 mobile TEXT NOT NULL DEFAULT '', emergency_name TEXT NOT NULL DEFAULT '', emergency_email TEXT NOT NULL DEFAULT '',
 rate_cents INTEGER NOT NULL DEFAULT 0 CHECK(rate_cents >= 0), username TEXT NOT NULL COLLATE NOCASE UNIQUE,
 password_hash TEXT, status TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active','terminated')),
 week_ending INTEGER NOT NULL DEFAULT 4 CHECK(week_ending IN (4,6)), auth_version INTEGER NOT NULL DEFAULT 0,
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
'''


def create_app(test_config=None):
    app = Flask(__name__)
    app.config.update(
        SECRET_KEY=os.environ.get('SECRET_KEY'), DATABASE=os.environ.get('DATABASE_PATH', 'data/sbwages.db'),
        SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE='Lax',
        SESSION_COOKIE_SECURE=os.environ.get('COOKIE_SECURE', 'true').lower() == 'true',
        PERMANENT_SESSION_LIFETIME=timedelta(hours=12), MAX_CONTENT_LENGTH=128 * 1024,
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

    app.jinja_env.filters.update(money=money, hours=hours)
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
            return redirect(url_for('admin_staff' if g.user['role'] == 'admin' else 'employee_timesheet'))
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
        if admin:
            values['username'] = request.form.get('username', '').strip().lower()
            if not re.fullmatch(r'[a-z0-9._@+-]{3,80}', values['username']):
                raise ValueError('Username must be 3–80 letters, numbers, or . _ @ + - characters.')
            values['rate_cents'] = decimal_units(request.form.get('hourly_rate', ''), 'Hourly rate', 100000)
            values['status'] = request.form.get('status')
            if values['status'] not in ('active', 'terminated'):
                raise ValueError('Choose active or terminated.')
            ending = request.form.get('week_ending')
            if ending not in ('4', '6'):
                raise ValueError('Choose Friday or Sunday for the week ending.')
            values['week_ending'] = int(ending)
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
                if person and person['week_ending'] != values['week_ending']:
                    if db().execute("SELECT id FROM sheets WHERE user_id=? AND status='draft'", (user_id,)).fetchone():
                        raise ValueError('Submit existing draft timesheets before changing the week ending.')
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
        return dict(sheet=sheet, days=days, entries=by_day, total_units=units, rate=rate, total_cents=total)

    @app.route('/employee/timesheet', methods=['GET', 'POST'])
    @require('employee')
    def employee_timesheet():
        person = g.user
        try:
            ending = date.fromisoformat(request.values.get('week', week_end(today(), person['week_ending']).isoformat()))
        except ValueError:
            abort(400, 'Invalid week date.')
        existing = db().execute('SELECT * FROM sheets WHERE user_id=? AND week_end=?', (person['id'], ending.isoformat())).fetchone()
        if not existing and ending.weekday() != person['week_ending']:
            abort(400, 'This week does not match your assigned week ending.')
        if ending > week_end(today(), person['week_ending']):
            abort(400, 'Future timesheets are not available yet.')
        data = sheet_data(person, ending)
        if request.method == 'POST':
            try:
                action = request.form.get('action')
                if action not in ('save', 'submit'):
                    raise ValueError('Choose save or submit.')
                if action == 'submit' and today() < ending:
                    raise ValueError('You can submit on or after your week-ending day.')
                rows = []
                for day in data['days']:
                    key = day.isoformat()
                    units = decimal_units(request.form.get('hours_' + key, '') or '0', 'Daily hours', 24)
                    activity = request.form.get('activity_' + key, '').strip()
                    if len(activity) > 2000:
                        raise ValueError('Activity notes must be no more than 2,000 characters per day.')
                    if units and not activity:
                        raise ValueError(f'Add activity notes for {day.strftime("%A %d %b")}.')
                    if day > today() and (units or activity):
                        raise ValueError('Hours and activities cannot be entered for future dates.')
                    rows.append((person['id'], key, units, activity))
                if action == 'submit' and not sum(r[2] for r in rows):
                    raise ValueError('Enter some worked hours before submitting.')
                db().execute('BEGIN IMMEDIATE')
                current = db().execute('SELECT * FROM sheets WHERE user_id=? AND week_end=?', (person['id'], ending.isoformat())).fetchone()
                if current and current['status'] == 'submitted':
                    raise ValueError('This timesheet has been submitted and is locked.')
                overlap = db().execute('SELECT id FROM sheets WHERE user_id=? AND week_end BETWEEN ? AND ? AND week_end<>?', (person['id'], (ending - timedelta(days=6)).isoformat(), (ending + timedelta(days=6)).isoformat(), ending.isoformat())).fetchone()
                if overlap:
                    raise ValueError('This week overlaps an existing timesheet after a week-ending change. Ask admin to check the week-ending setting.')
                if not current:
                    sid = db().execute('INSERT INTO sheets(user_id,week_end) VALUES(?,?)', (person['id'], ending.isoformat())).lastrowid
                else:
                    sid = current['id']
                for uid, key, units, activity in rows:
                    db().execute('INSERT INTO entries VALUES(?,?,?,?,?) ON CONFLICT(user_id,work_date) DO UPDATE SET units=excluded.units,activity=excluded.activity', (uid, key, sid, units, activity))
                if action == 'submit':
                    units = sum(r[2] for r in rows)
                    rate = db().execute('SELECT rate_cents FROM users WHERE id=?', (person['id'],)).fetchone()['rate_cents']
                    db().execute("UPDATE sheets SET status='submitted',rate_cents=?,total_units=?,total_cents=?,submitted_at=? WHERE id=?", (rate, units, amount(units, rate), datetime.now(ZoneInfo('Australia/Brisbane')).isoformat(), sid))
                db().commit()
                flash('Timesheet submitted to admin.' if action == 'submit' else 'Daily hours and activities saved.', 'success')
                return redirect(url_for('employee_timesheet', week=ending.isoformat()))
            except (ValueError, sqlite3.IntegrityError) as error:
                db().rollback()
                flash(str(error) if isinstance(error, ValueError) else 'This week overlaps existing hours. Please contact admin.', 'error')
        return render_template('timesheet.html', **data, person=person, ending=ending, tab='timesheet', admin_view=False,
                               previous=ending - timedelta(days=7), following=ending + timedelta(days=7), current_ending=week_end(today(), person['week_ending']))

    @app.get('/employee/history')
    @require('employee')
    def history():
        sheets = db().execute("SELECT s.*,u.first_name,u.last_name FROM sheets s JOIN users u ON u.id=s.user_id WHERE s.user_id=? AND s.status='submitted' ORDER BY week_end DESC", (g.user['id'],)).fetchall()
        return render_template('submissions.html', sheets=sheets, tab='history', admin_view=False)

    @app.get('/admin/timesheets')
    @require('admin')
    def admin_timesheets():
        sheets = db().execute("SELECT s.*,u.first_name,u.last_name FROM sheets s JOIN users u ON u.id=s.user_id WHERE s.status='submitted' ORDER BY submitted_at DESC").fetchall()
        return render_template('submissions.html', sheets=sheets, tab='submissions', admin_view=True)

    @app.get('/admin/timesheets/<int:sheet_id>')
    @require('admin')
    def view_sheet(sheet_id):
        sheet = db().execute("SELECT * FROM sheets WHERE id=? AND status='submitted'", (sheet_id,)).fetchone()
        if not sheet:
            abort(404)
        person = staff(sheet['user_id'])
        ending = date.fromisoformat(sheet['week_end'])
        return render_template('timesheet.html', **sheet_data(person, ending), person=person, ending=ending, tab='submissions', admin_view=True)

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
