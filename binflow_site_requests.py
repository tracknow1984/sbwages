"""Management review of new collection sites submitted through the BlockTexx portal."""
import json
import sqlite3
from uuid import UUID

from flask import abort, flash, g, redirect, request, url_for

from binflow_client_profiles import FIELDS, create_site, site_account
from binflow_permissions import is_office

STATUSES = {'pending': 'Pending approval', 'approved': 'Approved for scheduling', 'declined': 'Declined'}


def migrate(conn, stamp):
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS bf_client_site_requests (
            id INTEGER PRIMARY KEY, account_id INTEGER NOT NULL REFERENCES bf_client_accounts(id),
            request_key TEXT NOT NULL, existing_client_id INTEGER REFERENCES bf_clients(id),
            business_name TEXT NOT NULL COLLATE NOCASE, site_name TEXT NOT NULL COLLATE NOCASE,
            details TEXT NOT NULL, submitted_at TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','approved','declined')),
            reviewed_at TEXT, reviewed_by TEXT, reason TEXT NOT NULL DEFAULT '',
            profile_id INTEGER REFERENCES bf_client_site_profiles(id),
            UNIQUE(account_id,request_key));
        CREATE UNIQUE INDEX IF NOT EXISTS bf_site_request_pending ON bf_client_site_requests
            (account_id,business_name,site_name) WHERE status='pending';
        CREATE INDEX IF NOT EXISTS bf_site_request_status ON bf_client_site_requests(status,id);
    """)
    # Upgrade a customised existing login once and preserve later permission changes.
    if conn.execute("""INSERT OR IGNORE INTO bf_portal_migrations(id,created_at)
            VALUES('blocktexx1-site-submissions-v1',?)""", (stamp,)).rowcount:
        row = conn.execute("""SELECT p.* FROM bf_client_permissions p JOIN bf_client_named_defaults n
            ON n.account_id=p.account_id WHERE n.name='blocktexx1'""").fetchone()
        if row:
            rights = set(json.loads(row['rights'])) | {'profiles.view', 'profiles.add'}
            conn.execute('UPDATE bf_client_permissions SET rights=? WHERE account_id=?',
                         (json.dumps(sorted(rights)), row['account_id']))


def submit(conn, aid, token, values, details, stamp):
    try:
        token = str(UUID(token))
    except (TypeError, ValueError):
        raise ValueError('Reopen Add client and submit the form again.') from None
    previous = conn.execute('SELECT id FROM bf_client_site_requests WHERE account_id=? AND request_key=?',
                            (aid, token)).fetchone()
    if previous:
        return previous['id']
    cid, business = site_account(conn, values, details)
    site = details['customer_site']
    if cid and conn.execute("""SELECT 1 FROM bf_locations WHERE kind='client' AND client_id=?
            AND name=? COLLATE NOCASE""", (cid, site)).fetchone():
        raise ValueError('This site already exists for the selected client.')
    if conn.execute("""SELECT 1 FROM bf_client_site_requests WHERE account_id=? AND business_name=?
            AND site_name=? AND status='pending'""", (aid, business, site)).fetchone():
        raise ValueError('A request for this client site is already awaiting approval.')
    return conn.execute("""INSERT INTO bf_client_site_requests
        (account_id,request_key,existing_client_id,business_name,site_name,details,submitted_at)
        VALUES(?,?,?,?,?,?,?)""", (aid, token, cid, business, site, json.dumps(details), stamp)).lastrowid


def portal_requests(conn, aid):
    return conn.execute("""SELECT id,site_name,business_name,submitted_at,status,reason,profile_id FROM
        bf_client_site_requests WHERE account_id=? AND (status='pending' OR id IN (
            SELECT id FROM bf_client_site_requests WHERE account_id=? AND status!='pending'
            ORDER BY reviewed_at DESC,id DESC LIMIT 15)) ORDER BY (status='pending') DESC,id DESC""",
        (aid, aid)).fetchall()


def approve(conn, row, stamp, who):
    details = json.loads(row['details'])
    cid = row['existing_client_id']
    if cid is None:
        # A different site for this same business may have been approved since submission.
        existing = conn.execute('SELECT id FROM bf_clients WHERE name=?', (row['business_name'],)).fetchone()
        cid = existing['id'] if existing else None
    if cid and conn.execute("""SELECT 1 FROM bf_locations WHERE kind='client' AND client_id=?
            AND name=? COLLATE NOCASE""", (cid, row['site_name'])).fetchone():
        raise ValueError('This site already exists for the client. Review the existing site before closing this request.')
    pid = create_site(conn, {'client_id': cid or 'new', 'client_name': row['business_name']}, details, stamp, who)
    location = conn.execute("""SELECT l.id,l.client_id FROM bf_client_site_profiles p JOIN bf_locations l
        ON l.id=p.location_id WHERE p.id=?""", (pid,)).fetchone()
    conn.execute('INSERT OR IGNORE INTO bf_client_site_scope(account_id,site_id) VALUES(?,?)',
                 (row['account_id'], location['id']))
    conn.execute('INSERT OR IGNORE INTO bf_client_account_scope(account_id,client_id) VALUES(?,?)',
                 (row['account_id'], location['client_id']))
    return pid


def register(app, db, access, render, now, actor):
    @app.context_processor
    def pending_count():
        if is_office(getattr(g, 'user', None)) and not getattr(g, 'bf_demo_token', None):
            return dict(site_approval_count=db().execute(
                "SELECT COUNT(*) FROM bf_client_site_requests WHERE status='pending'").fetchone()[0])
        return {}

    @app.get('/binflow/client-profiles/approvals')
    @access
    def bf_site_approvals():
        status = request.args.get('status', 'pending')
        if status not in STATUSES:
            abort(400)
        counts = {r['status']:r['n'] for r in db().execute(
            'SELECT status,COUNT(*) AS n FROM bf_client_site_requests GROUP BY status')}
        pages = max(1, (counts.get(status, 0) + 24) // 25)
        page = min(pages, max(1, request.args.get('page', 1, type=int)))
        rows = db().execute("""SELECT r.*,a.username FROM bf_client_site_requests r
            JOIN bf_client_accounts a ON a.id=r.account_id
            WHERE r.status=? ORDER BY r.id DESC LIMIT 25 OFFSET ?""", (status, (page-1)*25))
        return render('site_approvals', section='site_approvals', statuses_by_key=STATUSES,
                      rows=[dict(r, details=json.loads(r['details'])) for r in rows], fields=FIELDS,
                      counts=counts, selected=status, page=page, pages=pages)

    @app.post('/binflow/client-profiles/approvals/<int:rid>')
    @access
    def bf_review_site(rid):
        decision = request.form.get('decision')
        if decision not in ('approved', 'declined'):
            abort(400)
        note = request.form.get('reason', '').strip() if decision=='declined' else ''
        if len(note)>500:
            abort(400, 'Keep the reason to 500 characters.')
        conn = db()
        conn.execute('BEGIN IMMEDIATE')
        row = conn.execute('SELECT * FROM bf_client_site_requests WHERE id=?', (rid,)).fetchone()
        if not row:
            abort(404)
        if row['status'] != 'pending':
            abort(409, 'This client request has already been reviewed.')
        stamp, who = now(), actor()
        try:
            pid = approve(conn, row, stamp, who) if decision=='approved' else None
            conn.execute("""UPDATE bf_client_site_requests SET status=?,reviewed_at=?,reviewed_by=?,reason=?,
                profile_id=? WHERE id=?""", (decision, stamp, who, note, pid, rid))
            conn.commit()
        except (ValueError, sqlite3.IntegrityError) as exc:
            conn.rollback()
            flash(str(exc) if isinstance(exc, ValueError) else 'The site could not be created. Review the client details.', 'error')
            return redirect(url_for('bf_site_approvals', _anchor=f'request-{rid}'), code=303)
        flash(f'{row["site_name"]} · {STATUSES[decision]}.', 'success')
        return redirect(url_for('bf_site_approvals', status=decision, _anchor=f'request-{rid}'), code=303)
