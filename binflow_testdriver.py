"""A password-protected driver identity that can operate only on a training database."""
import hashlib
import secrets

from flask import abort, g, redirect, request, session, url_for

from binflow_demo import DEMO_ENDPOINTS, WAREHOUSE_ENDPOINTS


def credential_version(app):
    value = app.config.get('BINFLOW_TESTDRIVER_HASH')
    return hashlib.sha256(value.encode()).hexdigest() if value else None


def restore_run(conn):
    row = conn.execute('SELECT token,state,sample_date FROM bf_testdriver_run WHERE id=1').fetchone()
    if row:
        session['driver_demo'] = dict(token=row['token'], state=row['state'], date=row['sample_date'], uid=1)
    else:
        session.pop('driver_demo', None)


def guard(app, db):
    """Called after CSRF and before any staff view or sample database selection."""
    version = credential_version(app)
    if not version or not secrets.compare_digest(session['testdriver_version'], version):
        session.clear()
        return redirect(url_for('bf_testdriver_login'))
    endpoint = request.endpoint
    if endpoint in ('static', 'logout', 'bf_testdriver_login', 'bf_testdriver_entry', 'login'):
        return None
    if endpoint in ('index', 'employee_dashboard', 'bf_resume'):
        return redirect(url_for('bf_demo_cycle'))
    if endpoint not in ('bf_demo_cycle', 'bf_demo_start'):
        token = ((request.view_args or {}).get('sample_key') if endpoint in WAREHOUSE_ENDPOINTS
                 else request.args.get('demo') if endpoint in DEMO_ENDPOINTS else None)
        saved = session.get('driver_demo', {})
        if not token or token != saved.get('token'):
            abort(403, 'Open the test driver cycle to continue.')
        current = db().execute('SELECT token FROM bf_testdriver_run WHERE id=1').fetchone()
        if not current or token != current['token']:
            abort(410, 'This practice day was restarted. Open the test driver cycle to continue.')
    else:
        restore_run(db())
    # This identity exists in sample databases only. It never authenticates as a staff user.
    g.user = dict(id=1, username='testdriver', first_name='Test', last_name='Driver',
                  role='employee', business_role='driver', extra_business_roles='', working_state='NSW',
                  binflow_workspace='driver')
    return None
