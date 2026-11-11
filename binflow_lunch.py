"""Send one lunch reminder per subscribed phone and active driver shift."""
import base64
import logging
import threading
import time
from http.client import HTTPSConnection
from urllib.parse import urlsplit

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from flask import abort, g, jsonify, request
from py_vapid import Vapid02

from binflow_permissions import workspaces


def push_address(endpoint):
    if not isinstance(endpoint, str) or len(endpoint) > 4096 or not endpoint.isascii():
        raise ValueError('Invalid phone subscription.')
    u = urlsplit(endpoint)
    host = u.hostname or ''
    if (u.scheme != 'https' or u.username or u.password or u.port not in (443, None) or u.fragment
            or not u.path.startswith('/') or any(ord(c) < 33 for c in endpoint)
            or not (host == 'fcm.googleapis.com' or host.endswith(('.push.apple.com',
                    '.push.services.mozilla.com', '.notify.windows.com')))):
        raise ValueError('Unsupported phone notification service.')
    return u


def signing_key(conn):
    row = conn.execute('SELECT pem FROM bf_lunch_key WHERE id=1').fetchone()
    if not row:
        key = ec.generate_private_key(ec.SECP256R1())
        pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
        conn.execute('INSERT OR IGNORE INTO bf_lunch_key VALUES(1,?)', (pem,))
        conn.commit()
        row = conn.execute('SELECT pem FROM bf_lunch_key WHERE id=1').fetchone()
    return Vapid02.from_pem(row[0])


def deliver(endpoint, key, contact):
    address = push_address(endpoint)
    headers = key.sign({'sub': contact, 'aud': 'https://' + address.hostname})
    headers.update(TTL='90', Urgency='high', Topic='binflow-lunch')
    channel = HTTPSConnection(address.hostname, timeout=6)
    try:
        target = address.path + ('?' + address.query if address.query else '')
        channel.request('POST', target, body=b'', headers=headers)
        return channel.getresponse().status
    finally:
        channel.close()


def send_due(conn, contact, clock, send=deliver):
    # A short lease handles simultaneous workers and is retried after process restarts.
    for _ in range(40):
        conn.execute('BEGIN IMMEDIATE')
        row = conn.execute("""SELECT p.endpoint,p.user_id,d.id FROM bf_lunch_phones p
            JOIN bf_workdays d ON d.user_id=p.user_id JOIN users u ON u.id=p.user_id
            WHERE u.status='active' AND d.finished_at IS NULL AND d.break_started_at IS NULL
            AND p.sent_day IS NOT d.id AND p.retry_at<=? AND unixepoch(d.started_at)+14400<=?
            ORDER BY d.started_at LIMIT 1""", (clock, clock)).fetchone()
        if not row:
            conn.commit()
            break
        endpoint, uid, day = row
        conn.execute('UPDATE bf_lunch_phones SET retry_at=? WHERE endpoint=?',(clock+90,endpoint))
        conn.commit()
        try:
            status = send(endpoint, signing_key(conn), contact)
        except (OSError, ValueError):
            continue
        if status in (404,410):
            conn.execute('DELETE FROM bf_lunch_phones WHERE endpoint=? AND user_id=?',(endpoint,uid))
        elif 200 <= status < 300:
            conn.execute('UPDATE bf_lunch_phones SET sent_day=?,retry_at=0 WHERE endpoint=? AND user_id=?',(day,endpoint,uid))
        conn.commit()


def register_lunch(app, db):
    with app.app_context():
        db().executescript("""
            CREATE TABLE IF NOT EXISTS bf_lunch_key(id INTEGER PRIMARY KEY CHECK(id=1),pem BLOB NOT NULL);
            CREATE TABLE IF NOT EXISTS bf_lunch_phones(endpoint TEXT PRIMARY KEY,user_id INTEGER NOT NULL
                REFERENCES users(id),sent_day INTEGER,retry_at INTEGER NOT NULL DEFAULT 0);
        """)

    @app.get('/binflow/driver/lunch-worker.js')
    def bf_lunch_worker():
        return app.send_static_file('binflow-lunch-worker.js')

    @app.route('/binflow/driver/lunch-alerts',methods=['GET','POST'])
    def bf_lunch_alerts():
        if not g.user or ('driver' not in workspaces(g.user) and g.user['role']!='admin'):
            abort(403)
        if request.method=='GET':
            key = signing_key(db()).public_key.public_bytes(serialization.Encoding.X962,serialization.PublicFormat.UncompressedPoint)
            return jsonify(key=base64.urlsafe_b64encode(key).decode().rstrip('='))
        endpoint = request.form.get('endpoint','')
        try:
            push_address(endpoint)
        except ValueError:
            abort(400)
        db().execute("""INSERT INTO bf_lunch_phones(endpoint,user_id) VALUES(?,?)
            ON CONFLICT(endpoint) DO UPDATE SET user_id=excluded.user_id,sent_day=NULL,retry_at=0
            WHERE user_id!=excluded.user_id""",(endpoint,g.user['id']))
        db().commit()
        return jsonify(ok=True)

    def dispatch():
        contact = 'mailto:'+app.config['ADMIN_EMAIL'] if app.config['ADMIN_EMAIL'] else app.config['APP_URL']
        while True:
            try:
                with app.app_context():
                    send_due(db(),contact,int(time.time()))
            except Exception:
                logging.getLogger(__name__).warning('Lunch reminder delivery interrupted; retrying.')
            time.sleep(15)

    if not app.testing:
        threading.Thread(target=dispatch, name='binflow-lunch', daemon=True).start()
