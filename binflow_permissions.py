"""Driver portal permissions shared by navigation and server-side routes."""

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
    return bool(user and user['role'] == 'employee' and conn.execute(
        'SELECT 1 FROM bf_access WHERE user_id=?', (user['id'],)).fetchone())


def can_view_run(conn, user, truck_id, run_date, include_available=True):
    if user['role'] == 'admin':
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
    if not user or user['role'] == 'admin':
        # Client portal PDFs have their own account/scope check before calling the renderer.
        return True
    if job['approval_status'] != 'approved' or job['status'] == 'cancelled':
        return False
    if job['driver_id'] and job['driver_id'] != user['id']:
        return False
    if job['truck_id']:
        return can_view_run(conn, user, job['truck_id'], job['scheduled_date'],
                            include_available=job['status'] == 'scheduled')
    return job['driver_id'] == user['id']
