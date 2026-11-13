"""Restore confirmed driver work only after the owner has authenticated."""
from flask import current_app, g, redirect, request, session, url_for
from binflow_permissions import has_binflow_access, workspaces
from binflow_workday import active_workday


SAMPLE_PAGES = {
    'bf_demo_inbound': None, 'bf_demo_docket': 'job_id', 'bf_bales': None,
    'bf_bale_new': None, 'bf_bale': 'bid', 'bf_bale_transfer_new': None,
    'bf_bale_transfer': 'tid',
}


def install_resume(app, db):
    with app.app_context():
        db().execute("""CREATE TABLE IF NOT EXISTS bf_driver_resume (
            user_id INTEGER PRIMARY KEY REFERENCES users(id), token TEXT, state TEXT, sample_date TEXT,
            page TEXT, item_id INTEGER, in_sample INTEGER NOT NULL DEFAULT 0)""")
        db().execute("""CREATE TABLE IF NOT EXISTS bf_testdriver_run (
            id INTEGER PRIMARY KEY CHECK(id=1), token TEXT NOT NULL, state TEXT NOT NULL, sample_date TEXT NOT NULL)""")
        db().commit()

    @app.after_request
    def remember_sample_page(response):
        if session.get('testdriver_version'):
            return response
        saved = session.get('driver_demo')
        if not g.get('bf_demo_token') and saved and request.endpoint in ('bf_workday','bf_run') and response.status_code==200:
            conn = db()
            conn.execute('UPDATE bf_driver_resume SET in_sample=0 WHERE user_id=? AND in_sample=1',(saved['uid'],))
            conn.commit()
        if not g.get('bf_demo_token') or not saved or request.method != 'GET' or response.status_code != 200:
            return response
        endpoint = request.endpoint
        if endpoint in ('bf_run', 'bf_workday'):
            endpoint = 'bf_workday'
        elif endpoint not in SAMPLE_PAGES:
            return response
        item = (request.view_args or {}).get(SAMPLE_PAGES.get(endpoint))
        conn = db()
        conn.execute("""INSERT INTO bf_driver_resume(user_id,token,state,sample_date,page,item_id,in_sample)
            VALUES(?,?,?,?,?,?,1) ON CONFLICT(user_id) DO UPDATE SET page=excluded.page,
            item_id=excluded.item_id, in_sample=1 WHERE token=excluded.token AND
            (page IS NOT excluded.page OR item_id IS NOT excluded.item_id OR in_sample=0)""",
            (saved['uid'], saved['token'], saved['state'], saved['date'], endpoint, item))
        conn.commit()
        return response

    @app.get('/binflow/driver/resume')
    def bf_resume():
        if not g.user:
            return redirect(url_for('login', audience='employee', resume=1))
        target = restore_driver(db(), g.user)
        return redirect(target or url_for('bf_workday' if has_binflow_access(g.user) else 'index'))


def save_sample(conn, saved):
    conn.execute("""INSERT INTO bf_driver_resume(user_id,token,state,sample_date,page,in_sample)
        VALUES(?,?,?,?,'bf_workday',1) ON CONFLICT(user_id) DO UPDATE SET token=excluded.token,
        state=excluded.state,sample_date=excluded.sample_date,page='bf_workday',item_id=NULL,in_sample=1""",
        (saved['uid'], saved['token'], saved['state'], saved['date']))
    conn.commit()


def staff_landing(conn, user, requested=None):
    """Keep drivers on the staff dashboard until they choose a task."""
    if 'driver' in workspaces(user):
        if requested and requested.startswith('/binflow/'):
            session['driver_requested_page'] = requested
        return url_for('employee_dashboard')
    resume = restore_driver(conn, user)
    return requested or resume or url_for('index')


def restore_driver(conn, user):
    from binflow_demo import demo_path
    if not has_binflow_access(user):
        return None
    saved = conn.execute('SELECT * FROM bf_driver_resume WHERE user_id=?', (user['id'],)).fetchone()
    modes = workspaces(user)
    if saved and saved['token'] and demo_path(current_app, saved['token']).is_file():
        session['driver_demo'] = dict(uid=user['id'], token=saved['token'], state=saved['state'], date=saved['sample_date'])
        if saved['in_sample']:
            if 'driver' in modes:
                session['binflow_workspace'] = 'driver'
            page = saved['page'] if saved['page'] in SAMPLE_PAGES else 'bf_workday'
            param = SAMPLE_PAGES.get(page)
            args = {param: saved['item_id']} if param else {}
            if page in ('bf_bale','bf_bale_new','bf_bales','bf_bale_transfer','bf_bale_transfer_new'):
                args['sample_key'] = saved['token']
            else:
                args['demo'] = saved['token']
            return url_for(page, **args)
    if saved and saved['in_sample'] and not session.get('driver_demo'):
        return url_for('bf_demo_cycle')
    if 'driver' in modes:
        day = active_workday(conn, user['id'])
        if day and (not day['state'] or day['state'] == user['working_state']):
            session['binflow_workspace'] = 'driver'
            return url_for('bf_workday')
    return None
