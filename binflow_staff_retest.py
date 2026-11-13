"""Restart the Queensland test employee's BinFlow day from the staff register."""
import base64
import hashlib
import json

from flask import abort, flash, redirect, render_template, request, url_for

DAYS = 'SELECT id FROM bf_workdays WHERE user_id=:uid'
LINKS = f'SELECT work_date FROM bf_workday_entries WHERE workday_id IN ({DAYS})'
INSPECTIONS = 'SELECT prestart_id FROM bf_run_inspections WHERE driver_id=:uid'

# Order children before their parents. Production jobs and allocated bins stay in place.
SCOPE = (
    ('prestart_photos', f'prestart_id IN ({INSPECTIONS})'),
    ('prestarts', f'user_id=:uid AND id IN ({INSPECTIONS})'),
    ('bf_run_inspections', 'driver_id=:uid'),
    ('bf_step_timings', f'workday_id IN ({DAYS})'),
    ('bf_run_acceptance_history', 'user_id=:uid'),
    ('bf_run_depot_arrivals', 'driver_id=:uid'),
    ('bf_runs', 'driver_id=:uid'),
    ('entries', f'user_id=:uid AND work_date IN ({LINKS})'),
    ('bf_workday_events', f'workday_id IN ({DAYS})'),
    ('bf_workday_entries', f'workday_id IN ({DAYS})'),
    ('bf_workdays', 'user_id=:uid'),
    ('bf_driver_resume', 'user_id=:uid'),
)

LABELS = {
    'bf_workdays': 'Driver days', 'bf_runs': 'Vehicle runs', 'bf_run_inspections': 'BinFlow prestarts',
    'entries': 'Linked daily timesheet entries', 'bf_driver_resume': 'Saved driver sessions',
}


def blob(value):
    if isinstance(value, bytes):
        return {'base64': base64.b64encode(value).decode('ascii')}
    raise TypeError(type(value).__name__)


def review(conn, user, day):
    params = {'uid': user['id'], 'actor': 'Test Driver', 'day': day}
    records = {}
    for table, where in SCOPE:
        records[table] = [dict(row) for row in conn.execute(
            f'SELECT * FROM {table} WHERE {where} ORDER BY rowid', params)]
    flags = []
    def check(sql, message):
        if conn.execute(sql, params).fetchone():
            flags.append(message)

    check("""SELECT 1 FROM bf_jobs j WHERE (j.driver_id=:uid OR j.collected_by=:actor)
        AND (j.status IN ('collected','received','complete') OR j.driver_arrived_at IS NOT NULL
        OR EXISTS (SELECT 1 FROM bf_job_bins i WHERE i.job_id=j.id AND
            COALESCE(i.loaded_at,i.delivered_at,i.picked_up_at,i.returned_at,i.weighed_at) IS NOT NULL))""",
          'A docket has recorded bin movements or a POD.')
    check("""SELECT 1 FROM bf_events WHERE actor=:actor UNION ALL
        SELECT 1 FROM bf_bale_events WHERE actor=:actor""", 'A stock or bale event needs reconciliation.')
    check("""SELECT 1 FROM bf_runs r WHERE r.driver_id=:uid AND
        (r.departed_at IS NOT NULL OR r.returned_at IS NOT NULL OR
         (r.accepted_by IS NOT NULL AND r.accepted_by!=:uid) OR EXISTS (
            SELECT 1 FROM bf_jobs j WHERE j.truck_id=r.truck_id AND j.scheduled_date=r.run_date
            AND j.status!='cancelled' AND j.driver_id IS NOT :uid))""",
          'A vehicle run has recorded travel or a shared allocation.')
    check("""SELECT 1 FROM bf_run_inspections i JOIN prestarts p ON p.id=i.prestart_id
        WHERE i.driver_id=:uid AND (p.user_id!=:uid OR i.passed=0)""",
          'A vehicle inspection is shared or records a defect.')
    check(f"""SELECT 1 FROM entries e JOIN sheets s ON s.id=e.sheet_id
        WHERE e.user_id=:uid AND e.work_date IN ({LINKS}) AND
        (s.user_id!=:uid OR s.archived_at IS NOT NULL OR s.status!='draft'
        OR EXISTS(SELECT 1 FROM payments p WHERE p.sheet_id=s.id)
        OR EXISTS(SELECT 1 FROM entry_audit a WHERE a.user_id=e.user_id AND a.work_date=e.work_date)
        OR (e.last_updated_by IS NOT NULL AND e.last_updated_by!=:uid)
        OR (e.activity!='' AND e.activity NOT LIKE 'BINFLOW · %'))""",
          'A linked timesheet has payroll or separately entered information.')
    check(f"""SELECT 1 FROM entries WHERE user_id=:uid AND work_date=:day AND work_date NOT IN ({LINKS})
        AND (COALESCE(start_time,'')!='' OR COALESCE(finish_time,'')!='' OR units!=0 OR committed_at IS NOT NULL)
        """, 'A separately entered timesheet already occupies today.')
    check("""SELECT 1 FROM sheets s WHERE s.user_id=:uid AND :day BETWEEN s.week_start AND s.week_end
        AND (s.status!='draft' OR s.archived_at IS NOT NULL OR EXISTS(SELECT 1 FROM payments p WHERE p.sheet_id=s.id))""",
          'The current timesheet period is locked.')
    counts = {label: len(records[t]) for t, label in LABELS.items()}
    data = json.dumps(records, default=blob, sort_keys=True, separators=(',', ':'))
    version = hashlib.sha256((str(user['auth_version']) + data).encode()).hexdigest()
    return records, counts, flags, data, version


def register(app, db, require, today):
    @app.route('/admin/staff/<int:user_id>/binflow-retest', methods=['GET', 'POST'])
    @require('admin')
    def staff_binflow_retest(user_id):
        conn = db()
        with conn:
            if request.method == 'POST':
                conn.execute('BEGIN IMMEDIATE')
            person = conn.execute("""SELECT * FROM users WHERE id=? AND username='testdriver'
                AND first_name='Test' AND last_name='Driver' AND role='employee' AND business_role='driver'
                AND working_state='QLD' AND NOT EXISTS(SELECT 1 FROM bf_live_cycle WHERE driver_id=users.id)""",
                (user_id,)).fetchone()
            if not person:
                abort(404)
            rows, counts, flags, snapshot, version = review(conn, person, today().isoformat())
            if request.method == 'POST':
                if request.form.get('version') != version or flags:
                    abort(409, 'Reopen the reset review to check the current records.')
                if any(rows.values()):
                    # Keep the exact rows (including photos) for recovery. Saved sample DBs stay on disk.
                    conn.execute("""CREATE TABLE IF NOT EXISTS bf_staff_retest_archive (
                        id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id),
                        saved_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, records TEXT NOT NULL)""")
                    archive = conn.execute('INSERT INTO bf_staff_retest_archive(user_id,records) VALUES(?,?)',
                                           (user_id, snapshot)).lastrowid
                    if conn.execute('SELECT records FROM bf_staff_retest_archive WHERE id=?',(archive,)).fetchone()[0] != snapshot:
                        raise RuntimeError('Could not verify the recovery copy.')
                    # The prestart parent is selected through the inspection row, so delete by the captured keys.
                    for table, where in SCOPE:
                        if table in ('prestarts', 'prestart_photos'):
                            continue
                        conn.execute(f'DELETE FROM {table} WHERE {where}', {'uid': user_id})
                    for prestart in rows['prestarts']:
                        conn.execute('DELETE FROM prestart_photos WHERE prestart_id=?', (prestart['id'],))
                        conn.execute('DELETE FROM prestarts WHERE id=? AND user_id=?', (prestart['id'], user_id))
                    conn.execute('UPDATE bf_lunch_phones SET sent_day=NULL,retry_at=0 WHERE user_id=?',(user_id,))
                    conn.execute('UPDATE users SET auth_version=auth_version+1 WHERE id=?', (user_id,))
                    after, _, after_flags, _, _ = review(conn, person, today().isoformat())
                    if any(after.values()) or after_flags:
                        raise RuntimeError('Reset verification failed.')
                    flash('Queensland testdriver reset. Sign in again and select Start Bin Flow.', 'success')
                return redirect(url_for('staff_binflow_retest', user_id=user_id), 303)
        return render_template('staff_binflow_retest.html', tab='staff', user_id=user_id,
                               counts=counts, flags=flags, version=version, has_activity=any(rows.values()))
