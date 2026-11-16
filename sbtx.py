"""General transport bookings for the SB Empire office and individual customers."""
from collections import defaultdict
from io import BytesIO
import secrets

from flask import Blueprint, abort, flash, g, redirect, render_template, request, send_file, url_for

from binflow_collections import calendar_period, dated
from binflow_permissions import is_office
from document_uploads import read_document


SCHEMA = """
CREATE TABLE IF NOT EXISTS sbtx_jobs (
    id INTEGER PRIMARY KEY,
    request_key TEXT NOT NULL UNIQUE,
    account_id INTEGER REFERENCES bf_client_accounts(id),
    description TEXT NOT NULL,
    pickup_date TEXT NOT NULL,
    customer_name TEXT NOT NULL,
    contact_number TEXT NOT NULL,
    po_number TEXT NOT NULL DEFAULT '',
    filename TEXT,
    mime_type TEXT,
    document BLOB,
    version INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS sbtx_pickups ON sbtx_jobs(pickup_date);
CREATE INDEX IF NOT EXISTS sbtx_customer_pickups ON sbtx_jobs(account_id, pickup_date);
"""
JOB_FIELDS = 'id,account_id,description,pickup_date,customer_name,contact_number,po_number,filename,version'


def read_job_fields(max_bytes):
    fields = {}
    for key, label, limit in [('description', 'job description', 2000), ('customer_name', 'customer name', 150),
                              ('contact_number', 'customer contact number', 50), ('po_number', 'PO number', 100)]:
        value = request.form.get(key, '').strip()
        if not value and key != 'po_number':
            raise ValueError(f'Enter the {label}.')
        if len(value) > limit:
            raise ValueError(f'Keep the {label} to {limit} characters or fewer.')
        if key == 'contact_number' and not any(c.isdigit() for c in value):
            raise ValueError('Enter a contact number with its digits.')
        fields[key] = value
    try:
        fields['pickup_date'] = dated(request.form.get('pickup_date', '')).isoformat()
    except ValueError:
        raise ValueError('Choose a valid pickup date.') from None
    upload = request.files.get('po_file')
    file_values = (None, None, None)
    if upload:
        file_values = read_document(upload, max_bytes, verify_contents=True)
    fields['filename'], fields['mime_type'], fields['document'] = file_values
    return fields


def register(app, db, client_access, now):
    with app.app_context():
        db().executescript(SCHEMA)
    bp = Blueprint('sbtx', __name__)

    @client_access
    def check_customer():
        return None

    @bp.before_request
    def check_access():
        g.sbtx_external = request.blueprint == 'sbtx_client'
        if g.sbtx_external:
            return check_customer()
        if not g.user:
            return redirect(url_for('login', audience='admin', area='sbtx'))
        if not is_office(g.user):
            abort(403)

    def can(action):
        return not g.sbtx_external or 'sbtx.' + action in g.client_rights

    def scope():
        if g.sbtx_external:
            return ' AND account_id=?', (g.bf_client['id'],)
        return '', ()

    def get_job(jid, fields=JOB_FIELDS):
        clause, params = scope()
        row = db().execute(f'SELECT {fields} FROM sbtx_jobs WHERE id=?' + clause, (jid, *params)).fetchone()
        if row is None:
            abort(404)
        return row

    def page(template, **values):
        return render_template('sbtx/' + template, tab='sbtx', external=g.sbtx_external, **values)

    @bp.get('/')
    def calendar():
        view = request.args.get('view', 'month')
        today = now()[:10]
        if view not in ('week', 'month'):
            abort(400)
        try:
            chosen = dated(request.args.get('date') or today)
        except ValueError:
            abort(400, 'Choose a valid calendar date.')
        start, end, previous, following, title, weeks = calendar_period(chosen, view)
        clause, params = scope()
        rows = db().execute(f'SELECT {JOB_FIELDS} FROM sbtx_jobs WHERE pickup_date BETWEEN ? AND ?' +
                            clause + ' ORDER BY pickup_date, customer_name COLLATE NOCASE, id',
                            (start.isoformat(), end.isoformat(), *params))
        events = defaultdict(list)
        for row in rows:
            events[row['pickup_date']].append(row)
        return page('calendar.html', view=view, chosen=chosen, previous=previous, following=following,
                    title=title, weeks=weeks, events=events, today=today, can_add=can('add'))

    def customer_logins(current_id):
        # SBTX is opt-in. A disabled login remains identifiable on its existing jobs.
        return db().execute("""
            SELECT a.id,a.name,a.username FROM bf_client_accounts a
            WHERE a.id=? OR (a.active=1 AND EXISTS (
                SELECT 1 FROM bf_client_permissions p, json_each(p.rights) r
                WHERE p.account_id=a.id AND r.value='sbtx.view'))
            ORDER BY a.name COLLATE NOCASE,a.username
        """, (current_id,)).fetchall()

    def job_form(job=None):
        editing = job is not None
        can_save = can('edit' if editing else 'add')
        if request.method == 'POST' and not can_save:
            abort(403)
        current_id = job['account_id'] if editing else None
        accounts = [] if g.sbtx_external else customer_logins(current_id)
        if editing:
            values = dict(job)
        else:
            try:
                pickup = dated(request.args.get('pickup') or now()[:10]).isoformat()
            except ValueError:
                abort(400, 'Choose a valid pickup date.')
            values = dict(pickup_date=pickup, customer_name=g.bf_client['name'] if g.sbtx_external else '',
                          request_key=secrets.token_hex(16))

        error = None
        if request.method == 'POST':
            values.update(request.form)
            try:
                fields = read_job_fields(app.config['DOCUMENT_MAX_BYTES'])
                conn = db()
                conn.execute('BEGIN IMMEDIATE')
                if g.sbtx_external:
                    fields['account_id'] = g.bf_client['id']
                else:
                    selected = request.form.get('account_id', '')
                    if selected and selected not in {str(a['id']) for a in customer_logins(current_id)}:
                        raise ValueError('Choose a customer login with SBTX access.')
                    fields['account_id'] = int(selected) if selected else None
                if editing:
                    get_job(job['id'])
                    fields.update(id=job['id'], version=request.form.get('version', type=int))
                    changed = conn.execute("""
                        UPDATE sbtx_jobs SET description=:description, pickup_date=:pickup_date,
                        customer_name=:customer_name, contact_number=:contact_number, account_id=:account_id,
                        po_number=:po_number, filename=COALESCE(:filename,filename),
                        mime_type=COALESCE(:mime_type,mime_type), document=COALESCE(:document,document),
                        version=version+1 WHERE id=:id AND version=:version
                    """, fields)
                    if not changed.rowcount:
                        conn.rollback()
                        abort(409, 'This job has changed. Reopen it from the calendar before saving.')
                else:
                    key = request.form.get('request_key', '')
                    if len(key) != 32 or any(c not in '0123456789abcdef' for c in key):
                        raise ValueError('Reopen New job and try again.')
                    who = 'customer-' + str(g.bf_client['id']) if g.sbtx_external else 'staff-' + str(g.user['id'])
                    fields['key'] = who + '-' + key
                    conn.execute("""
                        INSERT INTO sbtx_jobs(request_key,account_id,description,pickup_date,customer_name,
                                              contact_number,po_number,filename,mime_type,document)
                        VALUES(:key,:account_id,:description,:pickup_date,:customer_name,
                               :contact_number,:po_number,:filename,:mime_type,:document)
                        ON CONFLICT(request_key) DO NOTHING
                    """, fields)
                conn.commit()
                flash('Job saved.', 'success')
                return redirect(url_for('.calendar', date=fields['pickup_date']), code=303)
            except ValueError as exc:
                db().rollback()
                error = str(exc)
        return page('job.html', job=job, values=values, can_save=can_save, accounts=accounts,
                    error=error), 400 if error else 200

    @bp.route('/jobs/new', methods=['GET', 'POST'])
    def new_job():
        return job_form()

    @bp.route('/jobs/<int:jid>', methods=['GET', 'POST'])
    def job(jid):
        return job_form(get_job(jid))

    @bp.get('/jobs/<int:jid>/purchase-order')
    def po_file(jid):
        row = get_job(jid, 'filename,mime_type,document')
        if row['document'] is None:
            abort(404)
        return send_file(BytesIO(row['document']), mimetype=row['mime_type'],
                         download_name=row['filename'], as_attachment=True)

    app.register_blueprint(bp, url_prefix='/sbtx')
    app.register_blueprint(bp, name='sbtx_client', url_prefix='/sbtx/client')
