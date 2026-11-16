"""Client conversations with the three state warehouse teams."""
import re
import secrets

from flask import abort, g, redirect, render_template, request, url_for

from binflow_permissions import assigned_roles, is_manager, is_warehouse


WAREHOUSES = {
    'NSW': ('Wetherill Park', 'Unit 21, 19-26 Durian Place, Wetherill Park NSW 2164'),
    'QLD': ('North Maclean', 'Lot 5, 4705-4715 Mt Lindsey Highway, North Maclean QLD 4280'),
    'VIC': ('Truganina', 'Unit 3, 3-5 Leader St, Truganina VIC 3029'),
}
PAGE_SIZE = 40


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


def latest_in_states(states):
    placeholders = ','.join('?' for _ in states)
    return f"""SELECT MAX(id) FROM bf_warehouse_messages WHERE state IN ({placeholders})
               GROUP BY state,account_id"""


def register(app, db, client_access, access, render, now):
    @app.context_processor
    def inbox_navigation():
        # Resolve the queue only on staff screens that render its navigation.
        def warehouse_queue():
            if 'bf_warehouse_queue' not in g:
                states = staff_states(db())
                g.bf_warehouse_queue = None
                if states:
                    g.bf_warehouse_queue = db().execute(
                        'SELECT COUNT(*) FROM bf_warehouse_messages WHERE staff_id IS NULL AND id IN (' +
                        latest_in_states(states) + ')', states).fetchone()[0]
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
                               warehouses=WAREHOUSES, latest={row['state']: row for row in rows})

    def conversation(state, account, staff=False):
        conn = db()
        aid = account['id']
        if request.method == 'POST':
            body = request.form.get('body', '').strip()
            key = request.form.get('submission_key', '')
            if not body or len(body) > 4000:
                abort(400, 'Enter a message between 1 and 4,000 characters.')
            if not re.fullmatch(r'[0-9a-f]{32}', key):
                abort(400, 'Reopen the conversation and try again.')
            sender = g.user['id'] if staff else None
            with conn:
                saved = conn.execute("""INSERT INTO bf_warehouse_messages(account_id,state,staff_id,body,created_at,submission_key)
                    VALUES (?,?,?,?,?,?) ON CONFLICT DO NOTHING""", (aid, state, sender, body, now(), key))
                if saved.rowcount == 0:
                    previous = conn.execute("""SELECT body FROM bf_warehouse_messages
                        WHERE account_id=? AND state=? AND staff_id IS ? AND submission_key=?""",
                        (aid, state, sender, key)).fetchone()
                    if previous['body'] != body:
                        abort(409, 'This form has already sent a different message. Reopen the conversation to send another.')
            return redirect(url_for(request.endpoint, **request.view_args, _anchor='latest'), code=303)
        before = request.args.get('before', type=int)
        sql = 'SELECT * FROM bf_warehouse_messages WHERE account_id=? AND state=?'
        args = [aid, state]
        if before is not None:
            sql += ' AND id<?'
            args.append(before)
        rows = conn.execute(sql + ' ORDER BY id DESC LIMIT ?', (*args, PAGE_SIZE+1)).fetchall()
        messages = list(reversed(rows[:PAGE_SIZE]))
        older = url_for(request.endpoint, **request.view_args, before=messages[0]['id']) if len(rows)>PAGE_SIZE else None
        context = dict(account=account, state=state, depot=WAREHOUSES[state], messages=messages,
                       staff=staff, older=older, history=before is not None, submission_key=secrets.token_hex(16))
        if staff:
            return render('warehouse_reply', section='warehouse_messages', **context)
        return render_template('binflow/client_warehouse_chat.html', **context)

    @app.route('/binflow/client/warehouses/<state>', methods=['GET','POST'])
    @client_access
    def bf_client_warehouse_chat(state):
        if state not in WAREHOUSES:
            abort(404)
        return conversation(state, g.bf_client)

    @app.get('/binflow/warehouse/messages')
    @access
    def bf_warehouse_messages():
        states = staff_states(db())
        if not states:
            abort(403)
        selected = request.args.get('state','')
        if selected and selected not in states:
            abort(403)
        visible = (selected,) if selected else states
        before = request.args.get('before', type=int)
        sql = """SELECT m.*,a.name FROM bf_warehouse_messages m JOIN bf_client_accounts a ON a.id=m.account_id
                 WHERE m.id IN (""" + latest_in_states(visible) + ')'
        args = list(visible)
        if before is not None:
            sql += ' AND m.id<?'
            args.append(before)
        rows = db().execute(sql + ' ORDER BY m.id DESC LIMIT ?', (*args, PAGE_SIZE+1)).fetchall()
        return render('warehouse_inbox', section='warehouse_messages', inbox_states=states, selected=selected,
                      threads=rows[:PAGE_SIZE], older=rows[PAGE_SIZE-1]['id'] if len(rows)>PAGE_SIZE else None)

    @app.route('/binflow/warehouse/messages/<state>/<int:aid>', methods=['GET','POST'])
    @access
    def bf_warehouse_reply(state, aid):
        if state not in staff_states(db()):
            abort(403)
        account = db().execute("""SELECT a.id,a.name FROM bf_client_accounts a WHERE a.id=? AND EXISTS
            (SELECT 1 FROM bf_warehouse_messages WHERE account_id=a.id AND state=?)""", (aid, state)).fetchone()
        if not account:
            abort(404)
        return conversation(state, account, staff=True)
