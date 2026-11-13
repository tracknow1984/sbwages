"""Operational handoffs for approved loads. Driver notices share the approval transaction."""
import secrets
import smtplib
from datetime import date
from email.message import EmailMessage

from flask import abort, flash, g, redirect, render_template, request, url_for

from binflow_pods import send_smtp
from binflow_schedule import drivers

FLEET_STATES = ('QLD', 'NSW', 'VIC')
ACTIVE = ('Assigned', 'Updated', 'Reassigned')


def migrate(conn):
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS bf_rigid_driver_defaults (
            state TEXT PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id));
        CREATE TABLE IF NOT EXISTS bf_supplier_dispatch_defaults (
            origin TEXT NOT NULL, destination TEXT NOT NULL, vehicle TEXT NOT NULL, job_type TEXT NOT NULL,
            supplier_id INTEGER NOT NULL REFERENCES bf_supplier_site_profiles(id) ON DELETE CASCADE,
            PRIMARY KEY(origin,destination,vehicle,job_type));
        CREATE TABLE IF NOT EXISTS bf_load_dispatches (
            id INTEGER PRIMARY KEY, booking_id INTEGER NOT NULL REFERENCES bf_interstate_bookings(id),
            version INTEGER NOT NULL, event TEXT NOT NULL, target TEXT NOT NULL, recipient TEXT NOT NULL,
            address TEXT NOT NULL, title TEXT NOT NULL, body TEXT NOT NULL, actor_id INTEGER NOT NULL,
            status TEXT NOT NULL, attempted_at TEXT, notice_id INTEGER REFERENCES staff_notices(id) ON DELETE SET NULL,
            attempt INTEGER NOT NULL DEFAULT 1, decision TEXT CHECK(decision IN ('approved','denied')),
            decided_at TEXT, sent_at TEXT, response_token TEXT UNIQUE, UNIQUE(booking_id,version,event,attempt));
        CREATE INDEX IF NOT EXISTS bf_load_dispatch_lookup ON bf_load_dispatches(booking_id,id);
    """)

    if 'attempt' not in {r[1] for r in conn.execute('PRAGMA table_info(bf_load_dispatches)')}:
        # Replace the original uniqueness constraint so a declined assignment can be offered again
        # on the same booking version. Preserve dispatch and Empire Wire notice IDs.
        conn.execute('BEGIN IMMEDIATE')
        conn.execute("""CREATE TABLE bf_load_dispatches_v2 (
            id INTEGER PRIMARY KEY, booking_id INTEGER NOT NULL REFERENCES bf_interstate_bookings(id),
            version INTEGER NOT NULL, event TEXT NOT NULL, target TEXT NOT NULL, recipient TEXT NOT NULL,
            address TEXT NOT NULL, title TEXT NOT NULL, body TEXT NOT NULL, actor_id INTEGER NOT NULL,
            status TEXT NOT NULL, attempted_at TEXT, notice_id INTEGER REFERENCES staff_notices(id) ON DELETE SET NULL,
            attempt INTEGER NOT NULL DEFAULT 1, decision TEXT CHECK(decision IN ('approved','denied')),
            decided_at TEXT, sent_at TEXT, response_token TEXT UNIQUE, UNIQUE(booking_id,version,event,attempt))""")
        conn.execute("""INSERT INTO bf_load_dispatches_v2(id,booking_id,version,event,target,recipient,address,
            title,body,actor_id,status,attempted_at,notice_id,sent_at)
            SELECT d.id,booking_id,version,event,target,recipient,address,title,body,actor_id,status,attempted_at,
            notice_id,CASE WHEN status='sent' THEN COALESCE(n.sent_at,attempted_at) END
            FROM bf_load_dispatches d LEFT JOIN (SELECT id AS nid,sent_at FROM staff_notices) n ON n.nid=d.notice_id""")
        conn.execute('DROP TABLE bf_load_dispatches')
        conn.execute('ALTER TABLE bf_load_dispatches_v2 RENAME TO bf_load_dispatches')
        conn.execute('CREATE INDEX bf_load_dispatch_lookup ON bf_load_dispatches(booking_id,id)')
        conn.commit()
    conn.execute('CREATE INDEX IF NOT EXISTS bf_dispatch_notice ON bf_load_dispatches(notice_id)')


def last(conn, bid):
    return conn.execute('SELECT * FROM bf_load_dispatches WHERE booking_id=? ORDER BY id DESC LIMIT 1',(bid,)).fetchone()


def options(conn, row, state, destination):
    from app import valid_email
    people = drivers(conn,state)
    choices = {'driver:'+str(p['id']): (p['first_name']+' '+p['last_name']).strip() for p in people}
    suppliers = conn.execute("""SELECT id,customer_site,contact_email,state FROM bf_supplier_site_profiles
        WHERE category='industry-suppliers' AND upper(trim(group_name))='TRANSPORT'
        ORDER BY customer_site COLLATE NOCASE,state,id""")
    emails = {}
    for supplier in suppliers:
        address = supplier['contact_email'].strip()
        if valid_email(address) and not any(c in address for c in ',;'):
            key = 'supplier:'+str(supplier['id'])
            choices[key] = supplier['customer_site'] + (' · '+supplier['state'] if supplier['state'] else '')
            emails[key] = address

    rigid = row['vehicle']=='RIGID' and state in FLEET_STATES
    preferred = ''
    if rigid and row['job_type']=='internal':
        default = conn.execute('SELECT user_id FROM bf_rigid_driver_defaults WHERE state=?',(state,)).fetchone()
        if default:
            key = 'driver:'+str(default['user_id'])
            preferred = key if key in choices else ''
        else:
            matches = [p for p in people if p['first_name'].strip().casefold()=='errol'] if state=='QLD' else people
            if len(matches)==1:
                preferred = 'driver:'+str(matches[0]['id'])
    lane = (state, destination, row['vehicle'], row['job_type'])
    saved = conn.execute("""SELECT supplier_id FROM bf_supplier_dispatch_defaults
        WHERE origin=? AND destination=? AND vehicle=? AND job_type=?""",lane).fetchone()
    if saved:
        key = 'supplier:'+str(saved['supplier_id'])
        if key in choices:
            preferred = key
    return dict(choices=choices, emails=emails, preferred=preferred, rigid=rigid, state=state, lane=lane)


def recipient(opts, target):
    key = target if target is not None else opts['preferred']
    if not key:
        return None
    if key not in opts['choices']:
        raise ValueError('Choose an available driver or transport supplier for this load.')
    return (key, opts['choices'][key], opts['emails'].get(key,''))


def load_details(row):
    def stop(label, prefix):
        day = date.fromisoformat(row[prefix+'_date']).strftime('%a %d %b %Y')
        location = ', '.join(filter(None,(row[prefix+'_company'], row[prefix+'_address'])))
        return f'{label} · {day}\n{location}'
    return '\n\n'.join((stop('Pickup','pickup'),stop('Drop-off','delivery'),
                          'Load\n'+(row['goods'] or 'Scanned bales')))


def queue(conn, row, event, dest, actor_id, stamp, attempt=1):
    key, name, email = dest
    title = f"IT-{row['id']:06d} · {event}"
    text = load_details(row) if event in ACTIVE else {
        'On hold':'This load is on hold. Wait for a new assignment before attending.',
        'Cancelled':'This load is cancelled. Do not attend the pickup.'}[event]
    cursor = conn.execute("""INSERT INTO bf_load_dispatches
        (booking_id,version,event,target,recipient,address,title,body,actor_id,status,attempt)
        VALUES(?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(booking_id,version,event,attempt) DO NOTHING""",
        (row['id'],row['version'],event,key,name,email,title,text,actor_id,'queued',attempt))
    if cursor.rowcount and key.startswith('driver:'):
        notice = conn.execute("""INSERT INTO staff_notices(user_id,admin_id,title,body,priority,sent_at)
            VALUES(?,?,?,?,?,?)""",(int(key.split(':')[1]),actor_id,title,text,'normal' if event in ACTIVE else 'urgent',stamp))
        conn.execute("UPDATE bf_load_dispatches SET status='sent', notice_id=?,sent_at=? WHERE id=?",(notice.lastrowid,stamp,cursor.lastrowid))


def update_previous(conn, row, event, stamp):
    old = last(conn,row['id'])
    if not old or old['event']=='Cancelled' or (old['event']=='On hold' and event!='Cancelled'):
        return False
    if old['notice_id']:
        conn.execute('UPDATE staff_notices SET archived_at=COALESCE(archived_at,?) WHERE id=?',(stamp,old['notice_id']))
    # A superseded email must never become a fresh assignment on a retry.
    conn.execute("""UPDATE bf_load_dispatches SET status='superseded'
        WHERE id=? AND status IN ('failed','queued')""",(old['id'],))
    queue(conn,row,event,(old['target'],old['recipient'],old['address']),old['actor_id'],stamp)
    return True


def deliver_mail(conn, bid, config, stamp):
    """Claim each supplier email before sending. Ambiguous attempts require office review."""
    for item in conn.execute("""SELECT * FROM bf_load_dispatches WHERE booking_id=?
            AND status='queued' ORDER BY id""",(bid,)).fetchall():
        token = (item['response_token'] or secrets.token_urlsafe(32)) if item['event'] in ACTIVE else None
        claimed = conn.execute("""UPDATE bf_load_dispatches SET status='sending',attempted_at=?,response_token=?
                               WHERE id=? AND status='queued'""",(stamp,token,item['id'])).rowcount
        conn.commit()
        if not claimed:
            continue
        try:
            if not config['SMTP_HOST'] or not config['SMTP_FROM']:
                raise ValueError('SMTP not configured')
            msg = EmailMessage()
            msg['From'],msg['To'] = config['SMTP_FROM'],item['address']
            msg['Subject'] = 'SB Empire transport · '+item['title']
            msg['Message-ID'] = f"<binflow-dispatch-{item['id']}@{config['SMTP_FROM'].split('@')[-1]}>"
            body=item['body']
            if token:
                base=(config.get('APP_URL') or request.url_root).rstrip('/')
                body+='\n\nApprove or deny this load: '+base+url_for('supplier_load_response',token=token)
            msg.set_content(body)
            send_smtp(config,msg)
        except (smtplib.SMTPServerDisconnected, TimeoutError):
            status='uncertain'
        except (smtplib.SMTPException,OSError,ValueError):
            status='failed'
        else:
            status='sent'
        conn.execute("UPDATE bf_load_dispatches SET status=?,sent_at=CASE WHEN ?='sent' THEN COALESCE(sent_at,?) ELSE sent_at END WHERE id=?",(status,status,stamp,item['id']))
        conn.commit()

def response_label(event,target,status,decision):
    if event not in ACTIVE:
        return ''
    supplier=target.startswith('supplier:')
    actor='supplier' if supplier else 'driver'
    if decision:
        return ('Approved by ' if decision=='approved' else 'Denied by ')+actor
    if supplier and status!='sent':
        return {'failed':'Supplier email failed','queued':'Supplier email queued',
                'sending':'Sending to supplier','uncertain':'Supplier delivery unconfirmed'}.get(status,'')
    return 'Pending external supplier' if supplier else 'Pending driver'


def register_responses(app,db,require,now):
    app.add_template_global(response_label,'dispatch_response_label')

    def current_assignment(where,value,driver_id=None):
        row=db().execute("""SELECT d.*,b.locked_at,b.cancelled_at,b.bulk_delivered_at,
            (SELECT t.status FROM bf_interstate_manifests m JOIN bf_bale_transfers t ON t.id=m.transfer_id
             WHERE m.booking_id=b.id ORDER BY m.transfer_id DESC LIMIT 1) AS manifest_status
            FROM bf_load_dispatches d JOIN bf_interstate_bookings b ON b.id=d.booking_id WHERE """+where,(value,)).fetchone()
        if not row or (driver_id is not None and row['target']!=f'driver:{driver_id}'):
            abort(404)
        if (row['event'] not in ACTIVE or not row['locked_at'] or row['cancelled_at'] or row['bulk_delivered_at']
                or row['manifest_status'] in ('received','handed_over') or last(db(),row['booking_id'])['id']!=row['id']):
            abort(409,'This assignment is closed or has changed.')
        return row

    def decide(row):
        value=request.form.get('decision')
        if value not in ('approved','denied'):
            abort(400)
        if row['decision'] and row['decision']!=value:
            abort(409,'A response has already been recorded.')
        if not row['decision']:
            stamp=now()
            db().execute('UPDATE bf_load_dispatches SET decision=?,decided_at=? WHERE id=?',(value,stamp,row['id']))
            if row['notice_id']:
                db().execute('UPDATE staff_notices SET read_at=COALESCE(read_at,?) WHERE id=?',(stamp,row['notice_id']))
        db().commit()

    @app.post('/employee/hit-me/<int:notice_id>/load-response')
    @require('employee')
    def driver_load_response(notice_id):
        db().execute('BEGIN IMMEDIATE')
        row=current_assignment('d.notice_id=?',notice_id,g.user['id'])
        decide(row)
        flash('Load response saved.','success')
        return redirect(url_for('employee_hit_me',_anchor=f'notice-{notice_id}'))

    @app.route('/transfers/supplier-response/<token>',methods=['GET','POST'])
    def supplier_load_response(token):
        if request.method=='POST':
            db().execute('BEGIN IMMEDIATE')
        row=current_assignment("d.target LIKE 'supplier:%' AND d.response_token=?",token)
        if request.method=='POST':
            decide(row)
            return redirect(url_for('supplier_load_response',token=token))
        return render_template('supplier_load_response.html',load=row),200,{'X-Robots-Tag':'noindex, nofollow, noarchive'}
