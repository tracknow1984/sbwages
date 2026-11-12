"""Read-only, cross-business figures for the SB Empire owner dashboard."""
import json
from datetime import timedelta

from binflow_collections import occurs, profile_rows
from binflow_interstate import PLACES
from binflow_route_calendar import PRIMARY, START, clients, due, enabled
from binflow_schedule import STATES


def dispatch_outlook(conn, today, selected_date):
    days = {day.isoformat(): dict(date=day, jobs=0, pickups=0)
            for day in (today + timedelta(days=i) for i in range(7))}
    selected = selected_date if selected_date in days else today.isoformat()
    bounds = (today.isoformat(), (today + timedelta(days=6)).isoformat())
    published = "approval_status='approved' AND schedule_live=1 AND status!='cancelled'"
    for row in conn.execute(f"""SELECT scheduled_date, COUNT(*) FROM bf_jobs
            WHERE {published} AND scheduled_date BETWEEN ? AND ? GROUP BY scheduled_date""", bounds):
        days[row[0]]['jobs'] = row[1]
    for row in conn.execute("""SELECT pickup_date, COUNT(*) FROM bf_interstate_bookings
            WHERE locked_at IS NOT NULL AND pickup_date BETWEEN ? AND ? GROUP BY pickup_date""", bounds):
        days[row[0]]['pickups'] = row[1]

    peak = max(1, max(day['jobs'] + day['pickups'] for day in days.values()))
    for day in days.values():
        day['jobs_height'] = 80 * day['jobs'] / peak
        day['pickups_height'] = 80 * day['pickups'] / peak
    jobs = conn.execute(f"""SELECT id, code, site_name, state FROM bf_jobs
            WHERE {published} AND scheduled_date=? ORDER BY run_order, id LIMIT 2""", (selected,)).fetchall()
    bookings = conn.execute("""SELECT id, origin, destination FROM bf_interstate_bookings
            WHERE locked_at IS NOT NULL AND pickup_date=? ORDER BY id LIMIT 2""", (selected,))
    pickups = [dict(id=r['id'], origin=PLACES.get(r['origin'], (r['origin'],))[0],
                    destination=PLACES.get(r['destination'], (r['destination'],))[0]) for r in bookings]
    return dict(days=list(days.values()), selected=days[selected], today=days[today.isoformat()],
                jobs=jobs, pickups=pickups)


def next_week_approvals(conn, today):
    start = today + timedelta(days=7 - today.weekday())
    end = start + timedelta(days=6)
    weeks = {r['state']: r for r in conn.execute("""SELECT w.state,w.status,w.plan_json,w.approved_at,
            u.first_name || ' ' || u.last_name AS approver FROM bf_schedule_weeks w
            LEFT JOIN users u ON u.id=w.approved_by WHERE w.week_start=?""", (start.isoformat(),))}
    jobs = conn.execute("""SELECT id,state,schedule_live FROM bf_jobs WHERE scheduled_date BETWEEN ? AND ?
            AND status!='cancelled' AND approval_status!='rejected'""", (start.isoformat(), end.isoformat()))
    job_ids = {state: set() for state in STATES}
    live_ids = {state: set() for state in STATES}
    for job in jobs:
        if job['state'] in job_ids:
            job_ids[job['state']].add(job['id'])
            if job['schedule_live']:
                live_ids[job['state']].add(job['id'])

    # The calendar generates recurring visits and depot tasks before a week is first saved.
    proposed = set()
    if len(weeks) < len(STATES):
        if enabled(conn):
            if start >= START:
                proposed.update(PRIMARY)  # The planner reserves a baling day in each primary state.
                proposed.update(p['state'] for p in clients(conn) if p['state'] not in weeks and due(p, start))
        else:
            dates = [start + timedelta(days=i) for i in range(7)]
            proposed = {p['state'] for p in profile_rows(conn) if any(occurs(p,day) for day in dates)}

    states = []
    for state in STATES:
        row = weeks.get(state)
        status, detail = 'empty', 'No work scheduled'
        if row:
            items = [item for day in json.loads(row['plan_json']).get('days', []) for item in day['items']]
            planned_jobs = {item['job_id'] for item in items if item['kind']=='job'}
            has_work = bool(planned_jobs & job_ids[state] or any(i['kind']!='job' for i in items))
            additional_jobs = live_ids[state] - planned_jobs
            if row['status']=='published' and additional_jobs:
                status, detail = 'review', 'Additional jobs outside approved plan'
            elif has_work and row['status']=='published':
                status, detail = 'approved', 'Approved'
            elif has_work or additional_jobs:
                status, detail = 'pending', 'Awaiting approval'
        elif state in proposed or job_ids[state]:
            status, detail = 'pending', 'Awaiting approval'
        states.append(dict(state=state, status=status, label=detail,
                           approved_at=row['approved_at'] if row else None,
                           approver=row['approver'] if row else None))
    priority = {'review':0, 'pending':0, 'approved':1, 'empty':2}
    states.sort(key=lambda s: priority[s['status']])
    return dict(start=start, end=end, states=states,
                pending=sum(s['status'] in ('pending','review') for s in states))


def owner_overview(conn, today, selected_date):
    first = today.replace(day=1).isoformat()
    tomorrow = (today + timedelta(days=1)).isoformat()
    counts = dict(conn.execute("""SELECT
        (SELECT COUNT(*) FROM users WHERE role='employee' AND status='active') AS staff,
        (SELECT COUNT(*) FROM documents WHERE archived_at IS NULL) AS documents,
        (SELECT COUNT(*) FROM leave_requests WHERE status='pending' AND archived_at IS NULL) AS leave,
        (SELECT COUNT(*) FROM bf_interstate_bookings WHERE locked_at IS NULL) AS confirmations,
        (SELECT COUNT(*) FROM bf_collection_requests r LEFT JOIN bf_jobs j ON j.id=r.job_id
            WHERE (r.status='new' AND r.review_state!='referred')
               OR (r.status='scheduled' AND COALESCE(j.approval_status,'')!='approved')) AS collections,
        (SELECT COUNT(*) FROM tx_handling WHERE work_date=?) AS handling_today""", (today.isoformat(),)).fetchone())
    holds = conn.execute("""SELECT COUNT(*) AS total, MIN(prestart_id) AS first_id FROM bf_run_inspections
            WHERE passed=0 AND resolved_at IS NULL""").fetchone()
    unpaid = conn.execute("""SELECT COUNT(*) AS total, MIN(s.id) AS first_id FROM sheets s
            WHERE s.status='submitted' AND s.archived_at IS NULL
              AND NOT EXISTS (SELECT 1 FROM payments p WHERE p.sheet_id=s.id)""").fetchone()
    payroll = conn.execute("""SELECT COUNT(*) AS records, COALESCE(SUM(total_cents),0) AS cents
            FROM payments WHERE processed_at>=? AND processed_at<?""", (first, tomorrow)).fetchone()
    handling = conn.execute("""SELECT COUNT(*) AS records, COALESCE(SUM(total_cents),0) AS cents
            FROM tx_handling WHERE work_date>=? AND work_date<?""", (first, tomorrow)).fetchone()
    material = conn.execute("""SELECT COUNT(*) AS bins, COALESCE(SUM(net_g),0) AS grams
            FROM bf_job_bins WHERE direction='collect' AND net_g IS NOT NULL AND weighed_at>=?
            AND weighed_at<?""", (first, tomorrow)).fetchone()
    return dict(counts=counts, holds=holds, unpaid=unpaid, payroll=payroll, handling=handling, material=material,
                approvals=next_week_approvals(conn, today), dispatch=dispatch_outlook(conn, today, selected_date))
