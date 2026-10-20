"""Business roles and state boundaries for BINFLOW and staff profiles."""
from flask import abort

BUSINESS_ROLES = {'driver': 'DRIVER', 'labour': 'LABOUR', 'manager': 'MANAGER', 'contractor': 'CONTRACTOR'}
WORKING_STATES = ('QLD', 'NSW', 'VIC', 'SA', 'WA', 'ACT')

# Supporting routes belong to Driver day, Daily schedule or Daily PODs.
# New operations routes remain office-only until explicitly reviewed here.
DRIVER_ENDPOINTS = frozenset({
    'bf_workday', 'bf_start_workday', 'bf_workday_vehicle', 'bf_finish_workday',
    'bf_drivers', 'bf_truck', 'bf_truck_scan', 'bf_run', 'bf_run_prestart',
    'bf_run_action', 'bf_run_scan', 'bf_run_state', 'bf_accept_run', 'bf_load_list',
    'bf_calendar', 'bf_calendar_state', 'bf_daily_pods', 'bf_job', 'bf_job_action',
    'bf_pod', 'bf_pod_pdf', 'bf_evidence', 'bf_qr_staff', 'bf_qr_driver',
})


def is_driver(conn, user):
    return bool(user and user['role'] == 'employee' and user['business_role'] in ('driver', 'contractor') and has_binflow_access(user))


def is_manager(user):
    return bool(user and user['role'] == 'employee' and user['business_role'] == 'manager')


def is_office(user):
    return bool(user and (user['role'] == 'admin' or is_manager(user)))


def has_binflow_access(user):
    return bool(user and (user['role'] == 'admin' or
        (user['business_role'] in ('driver', 'manager', 'contractor') and user['working_state'] in WORKING_STATES)))


def state_scope(user):
    return user['working_state'] if user and user['role'] == 'employee' and user['business_role'] in ('driver', 'contractor') else None


def state_allowed(user, state):
    return has_binflow_access(user) and (state_scope(user) is None or state_scope(user) == state)


def require_state(user, state):
    if not state_allowed(user, state):
        abort(403, 'This information is outside your allocated working state. Ask the office to review your profile.')


def selected_state(user, supplied=''):
    scope = state_scope(user)
    if scope is not None:
        require_state(user, supplied or scope)
        return scope
    return supplied


def eligible_driver(conn, user_id, state):
    user = conn.execute('SELECT * FROM users WHERE id=? AND status=\'active\'', (user_id,)).fetchone()
    return bool(user and state_allowed(user, state))


def available_drivers(conn, state=''):
    users = conn.execute('SELECT * FROM users WHERE status=\'active\' ORDER BY first_name,last_name').fetchall()
    return [u for u in users if has_binflow_access(u) and (not state or state_allowed(u, state))]


def can_view_run(conn, user, truck_id, run_date, include_available=True):
    truck = conn.execute('SELECT state FROM bf_trucks WHERE id=?', (truck_id,)).fetchone()
    if not truck or not state_allowed(user, truck['state']):
        return False
    if is_office(user):
        return True
    run = conn.execute('SELECT driver_id FROM bf_runs WHERE truck_id=? AND run_date=?',
                       (truck_id, run_date)).fetchone()
    assigned = {r['driver_id'] for r in conn.execute('''SELECT driver_id FROM bf_jobs
        WHERE truck_id=? AND scheduled_date=? AND approval_status='approved' AND status!='cancelled'
        AND driver_id IS NOT NULL''', (truck_id, run_date))}
    if run and run['driver_id']:
        assigned.add(run['driver_id'])
    return assigned == {user['id']} or (not assigned and include_available)


def can_view_job(conn, user, job):
    if not user:
        # Client portal PDFs have their own account/scope check before calling the renderer.
        return True
    if not state_allowed(user, job['state']):
        return False
    if is_office(user):
        return True
    if job['approval_status'] != 'approved' or job['status'] == 'cancelled':
        return False
    if job['driver_id'] and job['driver_id'] != user['id']:
        return False
    if job['truck_id']:
        return can_view_run(conn, user, job['truck_id'], job['scheduled_date'],
                            include_available=job['status'] == 'scheduled')
    return job['driver_id'] == user['id']
