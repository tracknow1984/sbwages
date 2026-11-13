"""Business roles and state boundaries for BINFLOW and staff profiles."""
from flask import abort

BUSINESS_ROLES = {'driver': 'DRIVER', 'labour': 'LABOUR', 'manager': 'MANAGER', 'contractor': 'CONTRACTOR', 'warehouse': 'WAREHOUSE'}
WORKING_STATES = ('QLD', 'NSW', 'VIC', 'SA', 'WA', 'ACT')

# Supporting routes belong to Driver day or Daily PODs.
# New operations routes remain office-only until explicitly reviewed here.
DRIVER_ENDPOINTS = frozenset({
    'bf_live_restart','bf_live_labels','bf_live_label_qr','bf_week_schedule', 'bf_week_schedule_version',
    'bf_accept_overview', 'bf_overview_state', 'bf_workday_break', 'bf_clock_state', 'bf_test_prestart',
    'bf_demo_cycle', 'bf_demo_docket', 'bf_demo_start', 'bf_demo_labels', 'bf_demo_qr', 'bf_demo_inbound',
    'bf_workday', 'bf_start_workday', 'bf_workday_vehicle', 'bf_finish_workday',
    'bf_drivers', 'bf_truck', 'bf_truck_scan', 'bf_run', 'bf_run_prestart',
    'bf_run_action', 'bf_run_scan', 'bf_run_state', 'bf_accept_run', 'bf_load_list',
    'bf_calendar', 'bf_workday_plan_state', 'bf_daily_pods', 'bf_job', 'bf_job_action',
    'bf_pod', 'bf_pod_pdf', 'bf_evidence', 'bf_qr_staff', 'bf_qr_driver',
})


WAREHOUSE_ENDPOINTS = frozenset({
    'bf_live_restart','bf_live_labels','bf_live_label_qr','bf_warehouse', 'bf_warehouse_scan', 'bf_job', 'bf_weigh', 'bf_pod', 'bf_pod_pdf',
    'bf_pod_materials', 'bf_pod_sign', 'bf_daily_pods', 'bf_pod_record', 'bf_pod_qr',
    'bf_pod_signature', 'bf_pod_journey', 'bf_evidence', 'bf_qr_image',
})


def assigned_roles(user):
    if not user:
        return ()
    keys = user.keys()
    primary = user['business_role'] if 'business_role' in keys else ''
    extra = user['extra_business_roles'] if 'extra_business_roles' in keys else ''
    selected = {primary, *extra.split()}
    return tuple(role for role in BUSINESS_ROLES if role in selected)


def role_fields(selected):
    primary = next((r for r in ('manager', 'driver', 'contractor', 'labour') if r in selected), '')
    return dict(business_role=primary,
                extra_business_roles=' '.join(r for r in BUSINESS_ROLES if r in selected and r != primary))


def workspaces(user):
    if not user or user['role'] != 'employee' or user['working_state'] not in WORKING_STATES:
        return ()
    roles = set(assigned_roles(user))
    return tuple(mode for mode, enabled in (
        ('manager', 'manager' in roles), ('driver', bool(roles & {'driver', 'contractor'})),
        ('warehouse', bool(roles & {'driver', 'warehouse'}))) if enabled)


def current_workspace(user):
    if user and 'binflow_workspace' in user.keys():
        return user['binflow_workspace']
    return next(iter(workspaces(user)), None)


def is_driver(conn, user):
    return current_workspace(user) == 'driver'


def is_manager(user):
    return current_workspace(user) == 'manager'


def is_warehouse(user):
    return current_workspace(user) == 'warehouse'


def is_office(user):
    return bool(user and (user['role'] == 'admin' or is_manager(user)))


def has_binflow_access(user):
    return bool(user and (user['role'] == 'admin' or workspaces(user)))


def state_scope(user):
    return user['working_state'] if current_workspace(user) in ('driver', 'warehouse') else None


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
    return bool(user and driver_available(user, state))


def driver_available(user, state):
    modes = workspaces(user)
    if 'driver' in modes:
        return not state or user['working_state'] == state
    return user['role'] == 'admin' or 'manager' in modes


def available_drivers(conn, state=''):
    users = conn.execute('SELECT * FROM users WHERE status=\'active\' ORDER BY first_name,last_name').fetchall()
    return [u for u in users if driver_available(u, state)]


def run_operating_state(conn, truck_id, run_date, home_state):
    states = conn.execute("""SELECT state FROM bf_workdays WHERE truck_id=? AND work_date=? AND state!=''
        UNION SELECT state FROM bf_jobs WHERE truck_id=? AND scheduled_date=? AND schedule_live=1
        AND approval_status='approved' AND status!='cancelled'""", (truck_id, run_date, truck_id, run_date)).fetchall()
    if not states:
        return home_state
    return states[0]['state'] if len(states) == 1 else None


def can_view_run(conn, user, truck_id, run_date, include_available=True):
    truck = conn.execute('SELECT state FROM bf_trucks WHERE id=?', (truck_id,)).fetchone()
    if not truck or not state_allowed(user, run_operating_state(conn, truck_id, run_date, truck['state'])):
        return False
    if is_office(user):
        return True
    run = conn.execute('SELECT driver_id FROM bf_runs WHERE truck_id=? AND run_date=?',
                       (truck_id, run_date)).fetchone()
    assigned = {r['driver_id'] for r in conn.execute('''SELECT driver_id FROM bf_jobs
        WHERE truck_id=? AND scheduled_date=? AND approval_status='approved' AND status!='cancelled'
        AND schedule_live=1 AND driver_id IS NOT NULL''', (truck_id, run_date))}
    if run and run['driver_id']:
        assigned.add(run['driver_id'])
    return assigned == {user['id']} or (not assigned and include_available)


def can_view_job(conn, user, job):
    if not user:
        # Client portal PDFs have their own account/scope check before calling the renderer.
        return True
    if not state_allowed(user, job['state']):
        return False
    from flask import g, has_request_context
    if has_request_context() and g.get('live_cycle') and job['pod_client_id']!=g.live_cycle['client_id']:
        return False
    if is_office(user):
        return True
    if is_warehouse(user):
        assigned = conn.execute('SELECT warehouse_id FROM bf_bale_managers WHERE user_id=?',
                                (user['id'],)).fetchone()
        return (job['approval_status'] == 'approved' and job['status'] in ('collected','received','complete')
                and (not assigned or assigned['warehouse_id'] == job['warehouse_id']))
    if not job['schedule_live'] or job['approval_status'] != 'approved' or job['status'] == 'cancelled':
        return False
    if job['driver_id'] and job['driver_id'] != user['id']:
        return False
    if job['truck_id']:
        return can_view_run(conn, user, job['truck_id'], job['scheduled_date'],
                            include_available=job['status'] == 'scheduled')
    return job['driver_id'] == user['id']
