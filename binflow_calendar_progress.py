"""Read-only calendar progress from live custody records and saved planning."""
from collections import defaultdict
from datetime import date, timedelta

STATES = ('QLD','NSW','VIC','SA','WA','TAS','ACT','NT')


def progress(conn, first, last, state=''):
    from binflow_schedule import get_plan
    from binflow_route_calendar import START
    result = {}
    jobs = [dict(r) for r in conn.execute("""
        SELECT j.*,r.departed_at,
          EXISTS(SELECT 1 FROM bf_job_bins i WHERE i.job_id=j.id
            AND (i.delivered_at IS NOT NULL OR i.picked_up_at IS NOT NULL)) AS started,
          (SELECT next.id FROM bf_jobs next WHERE next.truck_id=j.truck_id
            AND next.scheduled_date=j.scheduled_date AND next.status='scheduled'
            AND next.schedule_live=1 AND next.approval_status='approved'
            ORDER BY next.run_order,next.id LIMIT 1) AS active_job
        FROM bf_jobs j LEFT JOIN bf_runs r ON r.truck_id=j.truck_id AND r.run_date=j.scheduled_date
        WHERE j.scheduled_date BETWEEN ? AND ?""" + (" AND j.state=?" if state else ""),
        (first.isoformat(),last.isoformat(),state) if state else (first.isoformat(),last.isoformat()))]
    actual = defaultdict(list)
    for job in jobs:
        actual[job['state'],job['scheduled_date']].append(job)
    start = first-timedelta(days=first.weekday())
    while start <= last:
        for region in (state,) if state else STATES:
            plan = get_plan(conn,region,start.isoformat())[1] if start>=START else {'days':[]}
            days = {d['date']:d for d in plan['days']}
            for offset in range(7):
                day = start+timedelta(days=offset)
                if not first<=day<=last:
                    continue
                key=day.isoformat()
                rows=actual[region,key]
                sites={j['site_id'] for j in rows}
                entries=[]
                for item in days.get(key,{}).get('items',[]):
                    if item['kind']=='visit' and item.get('site_id') not in sites:
                        entries.append(dict(id=item['id'],title=item['title'],status='booked',job_id=None,
                                            note='Planned visit; bin allocation required'))
                for job in sorted(rows,key=lambda j:(j['run_order'],j['id'])):
                    if job['status']=='cancelled' or job['approval_status']=='rejected':
                        status='cancelled'
                    elif job['status'] in ('collected','received','complete'):
                        status='completed'
                    elif job['driver_arrived_at'] or job['started'] or (
                        job['departed_at'] and job['id']==job['active_job']):
                        status='on_route'
                    else:
                        status='booked'
                    entries.append(dict(id='job-'+str(job['id']),title=job['site_name'],status=status,
                                        job_id=job['id'],note=job['code']))
                if entries:
                    result[region+':'+key]=dict(state=region,date=key,entries=entries,
                        counts={s:sum(e['status']==s for e in entries) for s in ('booked','on_route','completed','cancelled')})
        start+=timedelta(days=7)
    return result
