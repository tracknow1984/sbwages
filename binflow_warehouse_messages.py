"""Client conversations with the three state warehouse teams."""
from io import BytesIO
from hashlib import sha256
import re
import secrets

from flask import abort, g, redirect, render_template, request, send_file, url_for

from document_uploads import read_document
from binflow_permissions import assigned_roles, is_manager, is_warehouse


WAREHOUSES = {
    'NSW': ('Wetherill Park', 'Unit 21, 19-26 Durian Place, Wetherill Park NSW 2164'),
    'QLD': ('North Maclean', 'Lot 5, 4705-4715 Mt Lindsey Highway, North Maclean QLD 4280'),
    'VIC': ('Truganina', 'Unit 3, 3-5 Leader St, Truganina VIC 3029'),
}
# Google Maps address matches; centre aerial views without a business listing overlay.
WAREHOUSE_MAP_CENTRES = {
    'NSW': '-33.8452058,150.8863243',
    'QLD': '-27.7708361,153.0130435',
    'VIC': '-37.82668,144.7480477',
}
PAGE_SIZE = 40
ACTIONS = ('ACTIONED', 'REFERRED', 'CLOSED')
STATUSES = ('SENT', 'OPENED', *ACTIONS)


def migrate(conn):
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS bf_warehouse_messages (
            id INTEGER PRIMARY KEY,
            account_id INTEGER NOT NULL REFERENCES bf_client_accounts(id),
            state TEXT NOT NULL,
            staff_id INTEGER REFERENCES users(id),
            body TEXT NOT NULL,
            created_at TEXT NOT NULL,
            submission_key TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS bf_warehouse_threads ON bf_warehouse_messages(state,account_id,id);
        CREATE UNIQUE INDEX IF NOT EXISTS bf_warehouse_submissions
            ON bf_warehouse_messages(account_id,state,COALESCE(staff_id,0),submission_key);
    """)

    columns = {row[1] for row in conn.execute('PRAGMA table_info(bf_warehouse_messages)')}
    if 'reply_to' not in columns:
        conn.execute('ALTER TABLE bf_warehouse_messages ADD COLUMN reply_to INTEGER REFERENCES bf_warehouse_messages(id)')
    conn.executescript("""
        CREATE INDEX IF NOT EXISTS bf_warehouse_replies ON bf_warehouse_messages(reply_to,id);
        CREATE TABLE IF NOT EXISTS bf_warehouse_enquiries (
            message_id INTEGER PRIMARY KEY REFERENCES bf_warehouse_messages(id),
            status TEXT NOT NULL DEFAULT 'SENT', version INTEGER NOT NULL DEFAULT 1, legacy INTEGER NOT NULL DEFAULT 0);
        CREATE TABLE IF NOT EXISTS bf_warehouse_status_history (
            id INTEGER PRIMARY KEY, message_id INTEGER NOT NULL REFERENCES bf_warehouse_messages(id),
            status TEXT NOT NULL, staff_id INTEGER NOT NULL REFERENCES users(id), created_at TEXT NOT NULL);
        CREATE INDEX IF NOT EXISTS bf_warehouse_status_events ON bf_warehouse_status_history(message_id,id);
        CREATE TABLE IF NOT EXISTS bf_warehouse_files (
            message_id INTEGER PRIMARY KEY REFERENCES bf_warehouse_messages(id), name TEXT NOT NULL,
            mime TEXT NOT NULL, digest TEXT NOT NULL, contents BLOB NOT NULL);
        INSERT OR IGNORE INTO bf_warehouse_enquiries(message_id,legacy)
            SELECT id,1 FROM bf_warehouse_messages WHERE staff_id IS NULL AND reply_to IS NULL;
    """)


def staff_states(conn):
    user = g.user
    if not user or g.get('bf_demo_token') or g.get('live_cycle'):
        return ()
    if user['role'] == 'admin':
        return tuple(WAREHOUSES)
    state = user['working_state']
    if state not in WAREHOUSES or not (is_manager(user) or is_warehouse(user)):
        return ()
    assignment = conn.execute("""SELECT l.state FROM bf_bale_managers m
        JOIN bf_locations l ON l.id=m.warehouse_id WHERE m.user_id=?""", (user['id'],)).fetchone()
    if assignment:
        return (state,) if assignment['state'] == state else ()
    return (state,) if is_manager(user) or 'warehouse' in assigned_roles(user) else ()


def enquiry(conn, state, aid, mid):
    row = conn.execute("""SELECT m.*,e.status,e.version,e.legacy FROM bf_warehouse_messages m
        JOIN bf_warehouse_enquiries e ON e.message_id=m.id WHERE m.id=? AND m.state=? AND m.account_id=?""",
        (mid,state,aid)).fetchone()
    if not row:
        abort(404)
    return row


def change_status(conn, mid, status, uid, stamp):
    conn.execute('UPDATE bf_warehouse_enquiries SET status=?,version=version+1 WHERE message_id=?',(status,mid))
    conn.execute('INSERT INTO bf_warehouse_status_history(message_id,status,staff_id,created_at) VALUES(?,?,?,?)',
                 (mid,status,uid,stamp))


def mark_opened(conn, item, uid, stamp):
    if item['status']=='SENT':
        change_status(conn,item['id'],'OPENED',uid,stamp)


def message_list(conn, states, aid=None):
    selected_state = request.args.get('state','')
    status = request.args.get('status','')
    if selected_state and selected_state not in states:
        abort(403)
    if status and status not in STATUSES:
        abort(400,'Choose a listed status.')
    visible = (selected_state,) if selected_state else tuple(states)
    marks = ','.join('?' for _ in visible)
    sql = f"""SELECT m.id,m.account_id,m.state,m.body,m.created_at,a.name,e.status,e.legacy,f.name AS attachment,
        r.body AS reply FROM bf_warehouse_messages m JOIN bf_client_accounts a ON a.id=m.account_id
        JOIN bf_warehouse_enquiries e ON e.message_id=m.id
        LEFT JOIN bf_warehouse_files f ON f.message_id=m.id
        LEFT JOIN bf_warehouse_messages r ON r.id=(SELECT MAX(x.id) FROM bf_warehouse_messages x WHERE x.reply_to=m.id AND x.staff_id IS NOT NULL)
        WHERE m.state IN ({marks})"""
    args = list(visible)
    if aid is not None:
        sql+=' AND m.account_id=?'
        args.append(aid)
    if status:
        sql+=' AND e.status=?'
        args.append(status)
    before=request.args.get('before',type=int)
    if before:
        sql+=' AND m.id<?'
        args.append(before)
    rows=conn.execute(sql+' ORDER BY m.id DESC LIMIT ?',(*args,PAGE_SIZE+1)).fetchall()
    return dict(threads=rows[:PAGE_SIZE],older=rows[PAGE_SIZE-1]['id'] if len(rows)>PAGE_SIZE else None,
                selected=selected_state,selected_status=status,warehouse_statuses=STATUSES,inbox_states=states)


def register(app, db, client_access, access, render, now):
    @app.context_processor
    def inbox_navigation():
        def warehouse_queue():
            if 'bf_warehouse_queue' not in g:
                states=staff_states(db())
                g.bf_warehouse_queue=None
                if states:
                    marks=','.join('?' for _ in states)
                    g.bf_warehouse_queue=db().execute(f"""SELECT COUNT(*) FROM bf_warehouse_enquiries e
                        JOIN bf_warehouse_messages m ON m.id=e.message_id
                        WHERE m.state IN ({marks}) AND e.status IN ('SENT','OPENED','REFERRED')""",states).fetchone()[0]
            return g.bf_warehouse_queue
        return dict(warehouse_queue=warehouse_queue)

    @app.get('/binflow/client/')
    @app.get('/binflow/client/warehouses')
    @client_access
    def bf_client_warehouses():
        rows = db().execute("""SELECT state,staff_id,created_at FROM bf_warehouse_messages WHERE id IN
            (SELECT MAX(id) FROM bf_warehouse_messages WHERE account_id=? GROUP BY state)""",
            (g.bf_client['id'],)).fetchall()
        return render_template('binflow/client_warehouses.html', account=g.bf_client,
                               warehouses=WAREHOUSES, map_centres=WAREHOUSE_MAP_CENTRES,
                               latest={row['state']: row for row in rows})

    def save_message(state, aid, *, staff=False, parent=None):
        conn=db()
        body=request.form.get('body','').strip()
        key=request.form.get('submission_key','')
        if not body or len(body)>4000:
            abort(400,'Enter a message between 1 and 4,000 characters.')
        if not re.fullmatch(r'[0-9a-f]{32}',key):
            abort(400,'Reopen the conversation and try again.')
        upload=request.files.get('attachment')
        file=None
        if upload and upload.filename:
            try:
                name,mime,contents=read_document(upload,app.config['DOCUMENT_MAX_BYTES'],verify_contents=True)
            except ValueError as exc:
                abort(400,str(exc))
            file=(name,mime,sha256(contents).hexdigest(),contents)
        sender=g.user['id'] if staff else None
        stamp=now()
        with conn:
            conn.execute('BEGIN IMMEDIATE')
            previous=conn.execute("""SELECT m.id,m.body,m.reply_to,f.name,f.digest FROM bf_warehouse_messages m
                LEFT JOIN bf_warehouse_files f ON f.message_id=m.id
                WHERE m.account_id=? AND m.state=? AND m.staff_id IS ? AND m.submission_key=?""",
                (aid,state,sender,key)).fetchone()
            if previous:
                if (previous['body']!=body or previous['name']!=(file[0] if file else None)
                        or previous['digest']!=(file[2] if file else None)
                        or parent is not None and previous['reply_to']!=parent):
                    abort(409,'This form already sent a different message. Reopen the conversation to send another.')
                return previous['id']
            if staff:
                if parent is None:
                    parent=conn.execute("""SELECT MAX(id) FROM bf_warehouse_messages
                        WHERE state=? AND account_id=? AND staff_id IS NULL AND reply_to IS NULL""",(state,aid)).fetchone()[0]
                item=enquiry(conn,state,aid,parent)
                mark_opened(conn,item,sender,stamp)
            elif parent is not None and enquiry(conn,state,aid,parent)['status']=='CLOSED':
                abort(409,'This communication is closed. Start a new enquiry for the warehouse.')
            mid=conn.execute("""INSERT INTO bf_warehouse_messages(account_id,state,staff_id,body,created_at,submission_key,reply_to)
                VALUES(?,?,?,?,?,?,?)""",(aid,state,sender,body,stamp,key,parent)).lastrowid
            if not staff and parent is None:
                conn.execute('INSERT INTO bf_warehouse_enquiries(message_id) VALUES(?)',(mid,))
            if file:
                conn.execute('INSERT INTO bf_warehouse_files(message_id,name,mime,digest,contents) VALUES(?,?,?,?,?)',(mid,*file))
        return mid

    @app.route('/binflow/client/communications',methods=['GET','POST'])
    @client_access
    def bf_client_communications():
        account=g.bf_client
        if request.method=='POST':
            state=request.form.get('state','')
            if state not in WAREHOUSES:
                abort(400,'Choose a warehouse.')
            mid=save_message(state,account['id'])
            return redirect(url_for('bf_client_warehouse_chat',state=state,message=mid),303)
        return render_template('binflow/client_communications.html',account=account,warehouses=WAREHOUSES,
            submission_key=secrets.token_hex(16),**message_list(db(),WAREHOUSES,account['id']))

    def conversation(state, account, staff=False):
        conn=db()
        aid=account['id']
        raw=request.args.get('message')
        mid=request.args.get('message',type=int)
        if raw is not None and mid is None:
            abort(404)
        item=enquiry(conn,state,aid,mid) if mid is not None else None
        if request.method=='POST':
            if staff and mid is None and 'reply_to' in request.form:
                mid=request.form.get('reply_to',type=int)
                if mid is None:
                    abort(400,'Choose the enquiry for this reply.')
            sent=save_message(state,aid,staff=staff,parent=mid)
            target=mid if mid is not None or staff else sent
            return redirect(url_for(request.endpoint,**request.view_args,message=target,_anchor='latest'),303)
        if item and staff and request.method=='GET' and g.user['role']!='admin' and item['status']=='SENT':
            with conn:
                conn.execute('BEGIN IMMEDIATE')
                mark_opened(conn,enquiry(conn,state,aid,mid),g.user['id'],now())
            item=enquiry(conn,state,aid,mid)
        before=request.args.get('before',type=int)
        sql="""SELECT m.*,e.status,f.name AS attachment FROM bf_warehouse_messages m
            LEFT JOIN bf_warehouse_enquiries e ON e.message_id=m.id
            LEFT JOIN bf_warehouse_files f ON f.message_id=m.id WHERE m.account_id=? AND m.state=?"""
        args=[aid,state]
        if item:
            sql+=' AND (m.id=? OR m.reply_to=?)'
            args.extend((mid,mid))
        if before is not None:
            sql+=' AND m.id<?'
            args.append(before)
        rows=conn.execute(sql+' ORDER BY m.id DESC LIMIT ?',(*args,PAGE_SIZE+1)).fetchall()
        messages=list(reversed(rows[:PAGE_SIZE]))
        older=url_for(request.endpoint,**request.view_args,message=mid,before=messages[0]['id']) if len(rows)>PAGE_SIZE else None
        events=conn.execute('SELECT status,created_at FROM bf_warehouse_status_history WHERE message_id=? ORDER BY id',(mid,)).fetchall() if item else []
        reply_choices=conn.execute('''SELECT id,body FROM bf_warehouse_messages
            WHERE account_id=? AND state=? AND staff_id IS NULL AND reply_to IS NULL ORDER BY id DESC LIMIT ?''',
            (aid,state,PAGE_SIZE)).fetchall() if staff and not item else []
        context=dict(reply_choices=reply_choices,account=account,state=state,depot=WAREHOUSES[state],messages=messages,item=item,actions=ACTIONS,
                     events=events,staff=staff,older=older,history=before is not None,submission_key=secrets.token_hex(16))
        if staff:
            return render('warehouse_reply',section='warehouse_messages',**context)
        return render_template('binflow/client_warehouse_chat.html',**context)

    @app.route('/binflow/client/warehouses/<state>',methods=['GET','POST'])
    @client_access
    def bf_client_warehouse_chat(state):
        if state not in WAREHOUSES:
            abort(404)
        return conversation(state,g.bf_client)

    @app.get('/binflow/warehouse/messages')
    @access
    def bf_warehouse_messages():
        states=staff_states(db())
        if not states:
            abort(403)
        return render('warehouse_inbox',section='warehouse_messages',**message_list(db(),states))

    @app.route('/binflow/warehouse/messages/<state>/<int:aid>',methods=['GET','POST'])
    @access
    def bf_warehouse_reply(state,aid):
        if state not in staff_states(db()):
            abort(403)
        account=db().execute("""SELECT a.id,a.name FROM bf_client_accounts a WHERE a.id=? AND EXISTS
            (SELECT 1 FROM bf_warehouse_messages WHERE account_id=a.id AND state=?)""",(aid,state)).fetchone()
        if not account:
            abort(404)
        return conversation(state,account,staff=True)

    @app.post('/binflow/warehouse/messages/<state>/<int:aid>/<int:mid>/status')
    @access
    def bf_warehouse_disposition(state,aid,mid):
        conn=db()
        if state not in staff_states(conn):
            abort(403)
        status=request.form.get('status')
        if status not in ACTIONS:
            abort(400,'Choose a listed warehouse action.')
        with conn:
            conn.execute('BEGIN IMMEDIATE')
            item=enquiry(conn,state,aid,mid)
            if status!=item['status']:
                if request.form.get('version',type=int)!=item['version']:
                    abort(409,'The status has changed. Reopen the communication before updating it.')
                mark_opened(conn,item,g.user['id'],now())
                change_status(conn,mid,status,g.user['id'],now())
        return redirect(url_for('bf_warehouse_reply',state=state,aid=aid,message=mid),303)

    def attachment(state,aid,mid):
        row=db().execute("""SELECT f.name,f.mime,f.contents FROM bf_warehouse_messages m
            JOIN bf_warehouse_files f ON f.message_id=m.id WHERE m.id=? AND m.state=? AND m.account_id=?""",
            (mid,state,aid)).fetchone()
        if not row:
            abort(404)
        result=send_file(BytesIO(row['contents']),mimetype=row['mime'],as_attachment=True,download_name=row['name'])
        result.cache_control.no_store=True
        result.headers['X-Content-Type-Options']='nosniff'
        return result

    @app.get('/binflow/client/warehouses/<state>/files/<int:mid>')
    @client_access
    def bf_client_message_attachment(state,mid):
        return attachment(state,g.bf_client['id'],mid)

    @app.get('/binflow/warehouse/messages/<state>/<int:aid>/files/<int:mid>')
    @access
    def bf_warehouse_message_attachment(state,aid,mid):
        if state not in staff_states(db()):
            abort(403)
        return attachment(state,aid,mid)
