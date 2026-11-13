"""Operational handoffs for approved loads. Driver notices share the approval transaction."""
import smtplib
from datetime import date
from email.message import EmailMessage

from binflow_pods import send_smtp
from binflow_schedule import drivers

FLEET_STATES = ('QLD', 'NSW', 'VIC')
ACTIVE = ('Assigned', 'Updated')


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
            UNIQUE(booking_id,version,event));
        CREATE INDEX IF NOT EXISTS bf_load_dispatch_lookup ON bf_load_dispatches(booking_id,id);
    """)


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


def queue(conn, row, event, dest, actor_id, stamp):
    key, name, email = dest
    title = f"IT-{row['id']:06d} · {event}"
    text = load_details(row) if event in ACTIVE else {
        'On hold':'This load is on hold. Wait for a new assignment before attending.',
        'Cancelled':'This load is cancelled. Do not attend the pickup.'}[event]
    cursor = conn.execute("""INSERT INTO bf_load_dispatches
        (booking_id,version,event,target,recipient,address,title,body,actor_id,status)
        VALUES(?,?,?,?,?,?,?,?,?,?) ON CONFLICT(booking_id,version,event) DO NOTHING""",
        (row['id'],row['version'],event,key,name,email,title,text,actor_id,'queued'))
    if cursor.rowcount and key.startswith('driver:'):
        notice = conn.execute("""INSERT INTO staff_notices(user_id,admin_id,title,body,priority,sent_at)
            VALUES(?,?,?,?,?,?)""",(int(key.split(':')[1]),actor_id,title,text,'normal' if event in ACTIVE else 'urgent',stamp))
        conn.execute("UPDATE bf_load_dispatches SET status='sent', notice_id=? WHERE id=?",(notice.lastrowid,cursor.lastrowid))


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
        claimed = conn.execute("""UPDATE bf_load_dispatches SET status='sending',attempted_at=?
                               WHERE id=? AND status='queued'""",(stamp,item['id'])).rowcount
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
            msg.set_content(item['body'])
            send_smtp(config,msg)
        except (smtplib.SMTPServerDisconnected, TimeoutError):
            status='uncertain'
        except (smtplib.SMTPException,OSError,ValueError):
            status='failed'
        else:
            status='sent'
        conn.execute("UPDATE bf_load_dispatches SET status=? WHERE id=?",(status,item['id']))
        conn.commit()
