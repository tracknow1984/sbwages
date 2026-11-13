"""Daily POD filing, exports and administrator email delivery."""
import csv
import io
import re
import secrets
import smtplib
import ssl
from datetime import date, datetime, timedelta
from decimal import Decimal
from email.message import EmailMessage

from flask import abort, flash, g, request, send_file, session

from binflow_pickup_pricing import pickup_cents, rate_text
from binflow_permissions import can_view_job, is_office, is_warehouse
from binflow_runs import valid_date


def send_smtp(config, msg):
    cls = smtplib.SMTP_SSL if config['SMTP_SSL'] else smtplib.SMTP
    with cls(config['SMTP_HOST'], config['SMTP_PORT'], timeout=15) as smtp:
        if not config['SMTP_SSL']: smtp.starttls(context=ssl.create_default_context())
        if config['SMTP_USERNAME']: smtp.login(config['SMTP_USERNAME'], config['SMTP_PASSWORD'])
        smtp.send_message(msg)


def migrate_pods(conn):
    columns = {r['name'] for r in conn.execute('PRAGMA table_info(bf_jobs)')}
    for name, definition in [('pod_archived_at', 'TEXT'), ('pod_deleted_at', 'TEXT'),
                             ('pod_version', 'INTEGER NOT NULL DEFAULT 0')]:
        if name not in columns:
            conn.execute(f'ALTER TABLE bf_jobs ADD COLUMN {name} {definition}')
    conn.executescript('''
        CREATE TABLE IF NOT EXISTS bf_pod_history (
            id INTEGER PRIMARY KEY, job_id INTEGER NOT NULL REFERENCES bf_jobs(id),
            action TEXT NOT NULL, user_id INTEGER NOT NULL REFERENCES users(id), created_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS bf_pod_emails (
            id INTEGER PRIMARY KEY, job_id INTEGER NOT NULL REFERENCES bf_jobs(id),
            user_id INTEGER NOT NULL REFERENCES users(id), request_key TEXT NOT NULL UNIQUE,
            recipient TEXT NOT NULL, status TEXT NOT NULL CHECK(status IN ('sending','sent','failed')),
            created_at TEXT NOT NULL, sent_at TEXT);
        CREATE INDEX IF NOT EXISTS bf_pod_collection_items ON bf_job_bins(job_id) WHERE direction='collect';
        CREATE INDEX IF NOT EXISTS bf_pod_filing ON bf_jobs(scheduled_date,pod_deleted_at,pod_archived_at);
    ''')


def ensure_pod_available(job):
    if job['pod_deleted_at']:
        abort(410, 'This POD is deleted. An administrator can restore it from Deleted PODs.')


def warehouse_scope(user):
    if g.get('bf_demo_warehouse'):
        return '1=1', ()
    if g.get('live_cycle'):
        return 'j.pod_client_id=? AND w.id=?',(g.live_cycle['client_id'],g.live_cycle['origin_id'])
    if is_office(user):
        return '1=1', ()
    if is_warehouse(user):
        return """j.state=? AND w.state=? AND NOT EXISTS (
            SELECT 1 FROM bf_bale_managers m WHERE m.user_id=? AND m.warehouse_id!=j.warehouse_id
        )""", (user['working_state'], user['working_state'], user['id'])
    return '0=1', ()


def pending_bins(conn, user, bin_id=None, warehouse_id=None, run=None):
    scope, args = warehouse_scope(user)
    filters = ''
    if run is not None:
        if run['truck_id'] and run['schedule_live']:
            filters += ' AND j.truck_id=? AND j.scheduled_date=? AND j.warehouse_id=? AND j.schedule_live=1'
            args += (run['truck_id'], run['scheduled_date'], run['warehouse_id'])
        else:
            filters += ' AND j.id=?'
            args += (run['id'],)
    if bin_id is not None:
        filters += ' AND b.id=?'
        args += (bin_id,)
    if warehouse_id is not None:
        filters += ' AND w.id=?'
        args += (warehouse_id,)
    return conn.execute(f"""SELECT i.id AS item_id,i.bin_id,i.bin_code,i.size_name,i.tare_g,
            j.id AS job_id,j.code,j.client_name,j.site_name,j.collected_at,
            w.name AS warehouse,w.state,COALESCE(i.returned_at,j.received_at) AS arrived
        FROM bf_job_bins i JOIN bf_jobs j ON j.id=i.job_id JOIN bf_bins b ON b.id=i.bin_id
        JOIN bf_locations w ON w.id=j.warehouse_id
        WHERE {scope} {filters} AND i.direction='collect' AND i.active=1 AND i.weighed_at IS NULL
          AND b.status='received' AND b.truck_id IS NULL AND b.location_id=w.id
          AND j.approval_status='approved' AND j.status IN ('collected','received')
          AND j.pod_deleted_at IS NULL AND (i.returned_at IS NOT NULL OR (j.scan_mode=0 AND j.received_at IS NOT NULL))
        ORDER BY w.state,arrived,i.bin_code,i.id""", args).fetchall()


def monday(day):
    return day - timedelta(days=day.weekday())


def warehouse_progress(conn, user, stamp, selected):
    today = date.fromisoformat(stamp[:10])
    current_week = monday(today)
    selected_week = monday(date.fromisoformat(selected))
    prior_week = current_week - timedelta(days=7)
    states = {}
    for row in pending_bins(conn, user):
        item = dict(row)
        try:
            age = datetime.fromisoformat(stamp).timestamp() - datetime.fromisoformat(item['arrived']).timestamp()
            item['hours'] = max(0, int(age // 3600))
        except (ValueError, TypeError):
            item['hours'] = None
        states.setdefault(item['state'], []).append(item)

    # All weeks use the actual pickup date in BinFlow's Brisbane business clock.
    # Include archived signed PODs in the denominator, as they are still available to clients.
    scope, args = warehouse_scope(user)
    collection_jobs = f"""SELECT j.id,j.code,j.client_name,j.pod_signed_at,j.status,w.state,
            COUNT(*) AS bins, COUNT(i.weighed_at) AS weighed,
            date(substr(j.collected_at,1,10),'weekday 0','-6 days') AS week
        FROM bf_jobs j JOIN bf_locations w ON w.id=j.warehouse_id
        JOIN bf_job_bins i ON i.job_id=j.id AND i.direction='collect'
        WHERE {scope} AND j.approval_status='approved' AND j.status IN ('collected','received','complete')
          AND j.pod_deleted_at IS NULL AND j.collected_at IS NOT NULL
        GROUP BY j.id"""
    weeks = {}
    def week_entry(key):
        start = date.fromisoformat(key)
        return dict(start=key, end=(start+timedelta(days=6)).isoformat(),
                    past=start<current_week, total=0, signed=0, bins=0, weighed=0, on_time=0, outstanding=[])
    rows = conn.execute(f"""WITH jobs AS ({collection_jobs}), totals AS (
        SELECT week,COUNT(*) AS total,COUNT(pod_signed_at) AS signed,SUM(bins) AS bins,
               SUM(weighed) AS weighed,
               COUNT(CASE WHEN substr(pod_signed_at,1,10)<=date(week,'+6 days') THEN 1 END) AS on_time
        FROM jobs WHERE week IS NOT NULL GROUP BY week
        HAVING total>signed OR week IN (?,?,?))
        SELECT t.*,j.id,j.code,j.client_name,j.status,j.state FROM totals t LEFT JOIN jobs j
          ON j.week=t.week AND j.pod_signed_at IS NULL ORDER BY t.week,j.state,j.code""",
        (*args,current_week.isoformat(),prior_week.isoformat(),selected_week.isoformat()))
    for row in rows:
        key = row['week']
        if key not in weeks:
            weeks[key] = week_entry(key)
            weeks[key].update({field: row[field] for field in ('total','signed','bins','weighed','on_time')})
        if row['id'] is not None:
            weeks[key]['outstanding'].append(row)
    for day in (current_week, selected_week):
        if day.isoformat() not in weeks:
            weeks[day.isoformat()] = week_entry(day.isoformat())
    return dict(states=states, pending=sum(map(len,states.values())), weeks=[weeks[k] for k in sorted(weeks)],
                overdue=sum(w['total']-w['signed'] for w in weeks.values() if w['past']))


def register_pods(app, db, access, require, render, success, failure, now, pdf):
    def selection():
        try: run_date = valid_date(request.args.get('date', now()[:10]))
        except ValueError as error: abort(400, str(error))
        view = request.args.get('view', 'active')
        if view not in ('active', 'unfinished', 'archived', 'deleted','signed'): abort(400)
        if view in ('archived','deleted') and g.user['role'] != 'admin': abort(403)
        return run_date, view

    def daily_jobs(run_date, view):
        clause = {'signed':'j.pod_deleted_at IS NULL AND j.pod_signed_at IS NOT NULL', 'active': 'j.pod_deleted_at IS NULL AND j.pod_archived_at IS NULL',
                  'unfinished': 'j.pod_deleted_at IS NULL AND j.pod_archived_at IS NULL AND j.pod_signed_at IS NULL',
                  'archived': 'j.pod_deleted_at IS NULL AND j.pod_archived_at IS NOT NULL',
                  'deleted': 'j.pod_deleted_at IS NOT NULL'}[view]
        rate_sql = 'NULL'
        if g.user['role']=='admin' and not getattr(g,'bf_demo_token',None):
            rate_sql = '''COALESCE((SELECT r.units FROM bf_job_pickup_rates p JOIN bf_pickup_rates r ON r.id=p.rate_id
                WHERE p.job_id=j.id), (SELECT 16000 FROM bf_live_cycle x WHERE x.client_id=j.pod_client_id))'''
        rows = db().execute(f'''SELECT j.*, {rate_sql} AS rate_units,
            (SELECT i.id FROM bf_job_bins i JOIN bf_bins b ON b.id=i.bin_id WHERE i.job_id=j.id AND i.direction='collect'
                AND i.active=1 AND i.weighed_at IS NULL AND (i.returned_at IS NOT NULL OR (j.scan_mode=0 AND j.received_at IS NOT NULL))
                AND b.status='received' AND b.location_id=j.warehouse_id AND b.truck_id IS NULL
                ORDER BY i.bin_code LIMIT 1) AS next_weight_item,
            (SELECT COUNT(*) FROM bf_job_bins i WHERE i.job_id=j.id AND i.direction='collect') AS collection_bins,
            (SELECT SUM(net_g) FROM bf_job_bins i WHERE i.job_id=j.id AND i.direction='collect') AS net,
            (SELECT COUNT(*) FROM bf_job_bins i WHERE i.job_id=j.id AND i.direction='collect' AND i.weighed_at IS NULL) AS unweighed
            FROM bf_jobs j WHERE j.scheduled_date=? AND j.approval_status='approved' AND j.status!='cancelled' AND ''' + clause +
            ' ORDER BY j.client_name,j.site_name,j.id', (run_date,)).fetchall()
        return [j for j in rows if can_view_job(db(), g.user, j) and (not request.args.get('state') or request.args['state']==j['state'])]

    def closed_job(job_id, require_signed=True):
        j = db().execute('SELECT * FROM bf_jobs WHERE id=?', (job_id,)).fetchone()
        if not j: abort(404)
        if j['status'] != 'complete' or (require_signed and not j['pod_signed_at']) or j['approval_status'] != 'approved':
            raise ValueError('Finish the handover, final weights, material classification and signed sign-off before filing or emailing this POD.')
        return j

    @app.get('/binflow/pods')
    @access
    def bf_daily_pods():
        run_date, view = selection()
        jobs = daily_jobs(run_date, view)
        progress = None
        if view == 'active' and not getattr(g, 'bf_demo_token', None) and (is_office(g.user) or is_warehouse(g.user)):
            progress = warehouse_progress(db(), g.user, now(), run_date)
        return render('daily_pods', progress=progress, section='pods', run_date=run_date, view=view, jobs=jobs,
                      closed=sum(bool(j['pod_signed_at']) for j in jobs))

    @app.get('/binflow/pods/export.csv')
    @require('admin')
    def bf_pods_export():
        run_date, view = selection()
        stream = io.StringIO(); writer = csv.writer(stream)
        writer.writerow(['Docket', 'Date', 'Client', 'Site', 'State', 'POD status', 'Net weight kg', 'Filed under', 'Signed by', 'Signed at', 'Materials', 'Pickup rate AUD/kg ex GST', 'Signed pickup value AUD ex GST'])
        def safe(value):
            value = str(value or '')
            return "'" + value if value.lstrip().startswith(('=', '+', '-', '@')) or value.startswith(('\t', '\r', '\n')) else value
        for j in daily_jobs(run_date, view):
            from binflow_pod_records import status as pod_status
            status = pod_status(j)
            cents = pickup_cents(j)
            writer.writerow([safe(v) for v in (j['code'], j['scheduled_date'], j['client_name'], j['site_name'],
                j['state'], status, str(Decimal(j['net'] or 0) / 1000), view, j['pod_signed_name'],j['pod_signed_at'], '; '.join(r[0] for r in db().execute("SELECT DISTINCT material_name FROM bf_job_bins WHERE job_id=? AND direction='collect'",(j['id'],))),
                rate_text(j['rate_units']) if j['collection_bins'] else '',
                f'{Decimal(cents)/100:.2f}' if cents is not None else '')])
        return send_file(io.BytesIO(stream.getvalue().encode('utf-8-sig')), mimetype='text/csv',
                         as_attachment=True, download_name=f'PODs-{run_date}-{view}.csv')

    @app.post('/binflow/pods/<int:job_id>/file')
    @require('admin')
    def bf_file_pod(job_id):
        view = request.form.get('view', 'active')
        if view not in ('active', 'archived', 'deleted'): abort(400)
        run_date = now()[:10]
        try:
            db().execute('BEGIN IMMEDIATE')
            j = closed_job(job_id,require_signed=request.form.get('action')!='restore'); run_date = j['scheduled_date']
            if request.form.get('version', type=int) != j['pod_version']:
                raise ValueError('This POD changed. Refresh the list before trying again.')
            action = request.form.get('action')
            archived, deleted = j['pod_archived_at'], j['pod_deleted_at']
            if action == 'archive' and not deleted: archived = now()
            elif action == 'delete': deleted = now()
            elif action == 'restore': archived = deleted = None
            else: raise ValueError('Choose Archive, Delete or Restore.')
            db().execute('UPDATE bf_jobs SET pod_archived_at=?,pod_deleted_at=?,pod_version=pod_version+1 WHERE id=?',
                         (archived, deleted, job_id))
            db().execute('INSERT INTO bf_pod_history(job_id,action,user_id,created_at) VALUES(?,?,?,?)',
                         (job_id, action, g.user['id'], now()))
            message = {'archive': 'POD archived.', 'delete': 'POD moved to Deleted. You can restore it there.',
                       'restore': 'POD restored to the active list.'}[action]
            return success(message, 'bf_daily_pods', date=run_date, view=view)
        except ValueError as error:
            return failure(error, 'bf_daily_pods', date=run_date, view=view)

    @app.route('/binflow/pods/<int:job_id>/email', methods=['GET', 'POST'])
    @require('admin')
    def bf_email_pod(job_id):
        from app import valid_email
        try: j = closed_job(job_id)
        except ValueError as error: abort(400, str(error))
        ensure_pod_available(j)
        key_name = f'pod_email_key_{job_id}'
        ready = bool(app.config['SMTP_HOST'] and app.config['SMTP_FROM'] and app.config['APP_URL'].startswith('https://'))
        status = 200
        if request.method == 'POST':
            recipient = request.form.get('recipient', '').strip()
            note = request.form.get('message', '').strip()
            key = request.form.get('request_key', '')
            previous = db().execute('SELECT * FROM bf_pod_emails WHERE request_key=? AND job_id=? AND user_id=?',
                                    (key, job_id, g.user['id'])).fetchone()
            if previous:
                if previous['status'] == 'sent':
                    return success('This POD email has already been sent.', 'bf_daily_pods', date=j['scheduled_date'])
                flash('This email attempt is already recorded. Review its status below before starting a new email.', 'error')
                status = 409
            elif not key or not secrets.compare_digest(key, session.get(key_name, '')):
                flash('Refresh the email form and try again.', 'error'); status = 400
            elif not valid_email(recipient) or ',' in recipient or ';' in recipient:
                flash('Enter one valid recipient email address.', 'error'); status = 400
            elif len(note) > 2000:
                flash('Keep the message to 2,000 characters.', 'error'); status = 400
            elif request.form.get('version', type=int) != j['pod_version']:
                flash('This POD changed. Refresh the email form before sending.', 'error'); status = 409
            elif not ready:
                flash('Email is not connected yet. Configure email in Settings before sending.', 'error'); status = 400
            else:
                response = pdf(job_id)
                response.direct_passthrough = False
                attachment = response.get_data(); response.close()
                # Recheck the POD version and reserve this submission before any email is sent.
                cursor = db().execute('''INSERT OR IGNORE INTO bf_pod_emails
                    (job_id,user_id,request_key,recipient,status,created_at)
                    SELECT id,?,?,?,'sending',? FROM bf_jobs
                    WHERE id=? AND pod_version=? AND status='complete' AND pod_signed_at IS NOT NULL AND pod_deleted_at IS NULL''',
                    (g.user['id'], key, recipient, now(), job_id, j['pod_version']))
                db().commit()
                if not cursor.rowcount:
                    return failure(ValueError('This POD changed or the email is already being processed. Refresh the email form.'),
                                   'bf_email_pod', job_id=job_id)
                msg = EmailMessage()
                msg['Subject'] = 'SB EMPIRE POD: ' + j['code']
                msg['From'] = app.config['SMTP_FROM']; msg['To'] = recipient
                msg.set_content(note or 'Please find the attached proof of collection / delivery from SB EMPIRE.')
                msg.add_attachment(attachment, maintype='application', subtype='pdf', filename=j['code'] + '-POD.pdf')
                try:
                    send_smtp(app.config, msg)
                except (OSError, smtplib.SMTPException):
                    db().execute("UPDATE bf_pod_emails SET status='failed' WHERE request_key=?", (key,)); db().commit()
                    flash('The POD could not be emailed. Check the email connection and try again.', 'error'); status = 502
                    session[key_name] = secrets.token_urlsafe(32)
                else:
                    db().execute("UPDATE bf_pod_emails SET status='sent',sent_at=? WHERE request_key=?", (now(), key)); db().commit()
                    session.pop(key_name, None)
                    return success('POD emailed to ' + recipient + '.', 'bf_daily_pods', date=j['scheduled_date'],
                                   view='archived' if j['pod_archived_at'] else 'active')
        else:
            session[key_name] = secrets.token_urlsafe(32)
        session.setdefault(key_name, secrets.token_urlsafe(32))
        contact = db().execute('''SELECT p.contact_email,c.email FROM bf_jobs j
            LEFT JOIN bf_locations l ON l.id=j.site_id
            LEFT JOIN bf_client_site_profiles p ON p.location_id=j.site_id AND l.client_id=j.pod_client_id
            LEFT JOIN bf_clients c ON c.id=j.pod_client_id WHERE j.id=?''', (job_id,)).fetchone()
        recipient = next((value for value in (contact['contact_email'], contact['email'])
                          if value and valid_email(value) and not re.search('[,;]', value)), '')
        sent = db().execute('SELECT recipient,status,created_at,sent_at FROM bf_pod_emails WHERE job_id=? ORDER BY id DESC LIMIT 20',
                            (job_id,)).fetchall()
        return render('pod_email', section='pods', job=j, recipient=recipient, request_key=session[key_name],
                      email_ready=ready, sent=sent), status
