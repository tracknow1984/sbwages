"""Daily POD filing, exports and administrator email delivery."""
import csv
import io
import re
import secrets
import smtplib
import ssl
from decimal import Decimal
from email.message import EmailMessage

from flask import abort, flash, g, request, send_file, session

from binflow_permissions import can_view_job
from binflow_runs import valid_date


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
        CREATE INDEX IF NOT EXISTS bf_pod_filing ON bf_jobs(scheduled_date,pod_deleted_at,pod_archived_at);
    ''')


def ensure_pod_available(job):
    if job['pod_deleted_at']:
        abort(410, 'This POD is deleted. An administrator can restore it from Deleted PODs.')


def register_pods(app, db, access, require, render, success, failure, now, pdf):
    def selection():
        try: run_date = valid_date(request.args.get('date', now()[:10]))
        except ValueError as error: abort(400, str(error))
        view = request.args.get('view', 'active')
        if view not in ('active', 'archived', 'deleted'): abort(400)
        if view != 'active' and g.user['role'] != 'admin': abort(403)
        return run_date, view

    def daily_jobs(run_date, view):
        clause = {'active': 'j.pod_deleted_at IS NULL AND j.pod_archived_at IS NULL',
                  'archived': 'j.pod_deleted_at IS NULL AND j.pod_archived_at IS NOT NULL',
                  'deleted': 'j.pod_deleted_at IS NOT NULL'}[view]
        rows = db().execute('''SELECT j.*,
            (SELECT SUM(net_g) FROM bf_job_bins i WHERE i.job_id=j.id AND i.direction='collect') AS net,
            (SELECT COUNT(*) FROM bf_job_bins i WHERE i.job_id=j.id AND i.direction='collect' AND i.weighed_at IS NULL) AS unweighed
            FROM bf_jobs j WHERE j.scheduled_date=? AND j.approval_status='approved' AND j.status!='cancelled' AND ''' + clause +
            ' ORDER BY j.client_name,j.site_name,j.id', (run_date,)).fetchall()
        return [j for j in rows if can_view_job(db(), g.user, j)]

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
        return render('daily_pods', section='pods', run_date=run_date, view=view, jobs=jobs,
                      closed=sum(bool(j['pod_signed_at']) for j in jobs))

    @app.get('/binflow/pods/export.csv')
    @require('admin')
    def bf_pods_export():
        run_date, view = selection()
        stream = io.StringIO(); writer = csv.writer(stream)
        writer.writerow(['Docket', 'Date', 'Client', 'Site', 'State', 'POD status', 'Net weight kg', 'Filed under', 'Signed by', 'Signed at', 'Materials'])
        def safe(value):
            value = str(value or '')
            return "'" + value if value.lstrip().startswith(('=', '+', '-', '@')) or value.startswith(('\t', '\r', '\n')) else value
        for j in daily_jobs(run_date, view):
            from binflow_pod_records import status as pod_status
            status = pod_status(j)
            writer.writerow([safe(v) for v in (j['code'], j['scheduled_date'], j['client_name'], j['site_name'],
                j['state'], status, str(Decimal(j['net'] or 0) / 1000), view, j['pod_signed_name'],j['pod_signed_at'], '; '.join(r[0] for r in db().execute("SELECT DISTINCT material_name FROM bf_job_bins WHERE job_id=? AND direction='collect'",(j['id'],))))])
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
                    cls = smtplib.SMTP_SSL if app.config['SMTP_SSL'] else smtplib.SMTP
                    with cls(app.config['SMTP_HOST'], app.config['SMTP_PORT'], timeout=15) as smtp:
                        if not app.config['SMTP_SSL']: smtp.starttls(context=ssl.create_default_context())
                        if app.config['SMTP_USERNAME']: smtp.login(app.config['SMTP_USERNAME'], app.config['SMTP_PASSWORD'])
                        smtp.send_message(msg)
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
