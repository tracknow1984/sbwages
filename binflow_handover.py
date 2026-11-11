"""Warehouse weighing progress for a published truck run or an individual docket."""


def weight_progress(conn, job):
    if job['truck_id'] and job['schedule_live'] and job['approval_status'] == 'approved':
        scope = """j.truck_id=? AND j.scheduled_date=? AND j.schedule_live=1
                   AND j.approval_status='approved' AND j.status!='cancelled'"""
        params = (job['truck_id'], job['scheduled_date'])
    else:
        scope, params = 'j.id=?', (job['id'],)
    return conn.execute(f"""SELECT COUNT(*) AS expected,
            COUNT(CASE WHEN i.weighed_at IS NOT NULL AND i.gross_g IS NOT NULL
                  AND i.tare_g IS NOT NULL AND i.net_g IS NOT NULL THEN 1 END) AS weighed
        FROM bf_jobs j JOIN bf_job_bins i ON i.job_id=j.id AND i.direction='collect'
        WHERE {scope}""", params).fetchone()


def pod_weights_ready(conn, job):
    if job['pod_signed_at']:
        return True
    if job['status'] != 'complete' or not job['collected_at']:
        return False
    progress = weight_progress(conn, job)
    return progress['weighed'] == progress['expected']
