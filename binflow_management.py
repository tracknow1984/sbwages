"""BlockTexx management: scoped collections and permission-gated national views."""
import json

from flask import abort, g, render_template, request
from binflow_schedule import STATES
from binflow_collections import calendar_period, dated


def migrate(conn):
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS bf_client_site_scope (
      account_id INTEGER NOT NULL REFERENCES bf_client_accounts(id),
      site_id INTEGER NOT NULL REFERENCES bf_locations(id),
      PRIMARY KEY(account_id,site_id));
    CREATE TABLE IF NOT EXISTS bf_portal_migrations (id TEXT PRIMARY KEY,created_at TEXT NOT NULL);
    """)


def bootstrap(conn, stamp):
    """One-time credential provisioning; subsequent starts preserve password and scopes."""
    migrate(conn)
    if conn.execute("SELECT 1 FROM bf_portal_migrations WHERE id='blocktexx-management-v1'").fetchone():
        return
    conn.execute('BEGIN IMMEDIATE')
    try:
        if conn.execute("SELECT 1 FROM bf_portal_migrations WHERE id='blocktexx-management-v1'").fetchone():
            conn.rollback()
            return
        conn.execute("UPDATE bf_client_accounts SET active=0,auth_version=auth_version+1,updated_at=?,updated_by='Portal migration'",(stamp,))
        conn.execute("""INSERT INTO bf_client_accounts(username,name,password_hash,active,created_at,updated_at,updated_by)
          VALUES('blocktexx','BlockTexx management',?,1,?,?,'Portal migration')
          ON CONFLICT(username) DO UPDATE SET name=excluded.name,password_hash=excluded.password_hash,
          active=1,updated_at=excluded.updated_at,updated_by=excluded.updated_by""",(BOOTSTRAP_HASH,stamp,stamp))
        aid=conn.execute("SELECT id FROM bf_client_accounts WHERE username='blocktexx'").fetchone()['id']
        sites={r[0] for r in conn.execute("SELECT location_id FROM bf_location_sources WHERE import_id LIKE 'blocktexx-%'")}
        sites.update(r[0] for r in conn.execute("""SELECT p.location_id FROM bf_allocation_workbook_rows w
          JOIN bf_client_site_profiles p ON p.id=w.profile_id WHERE p.location_id IS NOT NULL
          AND (lower(w.import_id) LIKE '%blocktexx%' OR lower(w.source_label) LIKE '%blocktexx%')"""))
        conn.executemany('INSERT OR IGNORE INTO bf_client_site_scope VALUES(?,?)',[(aid,s) for s in sorted(sites)])
        conn.execute('DELETE FROM bf_client_account_scope WHERE account_id=?',(aid,))
        conn.execute("""INSERT INTO bf_client_account_scope SELECT DISTINCT ?,l.client_id FROM bf_locations l
          JOIN bf_client_site_scope s ON s.site_id=l.id WHERE s.account_id=? AND l.client_id IS NOT NULL""",(aid,aid))
        conn.execute("INSERT INTO bf_client_access_history(account_id,action,scopes,actor,created_at) VALUES(?,?,?,?,?)",
          (aid,'BlockTexx management enabled; other client logins disabled',json.dumps(sorted(sites)),'Portal migration',stamp))
        conn.execute("INSERT INTO bf_portal_migrations VALUES('blocktexx-management-v1',?)",(stamp,))
        conn.commit()
    except Exception:
        conn.rollback()
        raise


# One-time hashed credential, never reset automatically after initial migration.
BOOTSTRAP_HASH = 'scrypt:32768:8:1$wPRz9OQKMzV1N0vU$1e562584d5f08b8c607b8c4e1c2ac6f0f2cdc67558d40b743ffac6eb1f81a1b9e756d7010260345bd2055831a9169aeeb197b2a9bbd786c6a94155bb7d5722ed'


def site_guard(alias='j'):
    # Legacy accounts retain client scope; BlockTexx is additionally restricted by site.
    return f""" AND (NOT EXISTS(SELECT 1 FROM bf_client_accounts a WHERE a.id=s.account_id AND (a.username='blocktexx' OR EXISTS(SELECT 1 FROM bf_interstate_accounts i WHERE i.account_id=a.id)))
      OR EXISTS(SELECT 1 FROM bf_client_site_scope ss WHERE ss.account_id=s.account_id AND ss.site_id={alias}.site_id)) """


def pod_scope(account, rights):
    final="""j.status='complete' AND j.pod_signed_at IS NOT NULL AND j.pod_deleted_at IS NULL
        AND NOT EXISTS (SELECT 1 FROM bf_live_cycle x WHERE x.client_id=j.pod_client_id)"""
    if 'pods.view' not in rights:
        return '0',()
    if 'pods.all' in rights:
        return final,()
    return final+""" AND EXISTS(SELECT 1 FROM bf_client_account_scope s
        WHERE s.client_id=j.pod_client_id AND s.account_id=? """+site_guard()+')',(account['id'],)


def permitted_pod(conn, account, job_id, rights):
    scope,params=pod_scope(account,rights)
    return conn.execute('SELECT 1 FROM bf_jobs j WHERE j.id=? AND '+scope,(job_id,*params)).fetchone()


def calendar(conn, aid, grid, state):
    days = {day.isoformat():dict(date=day.isoformat(),label=day.strftime('%a %d %b'),entries=[])
            for week in grid for day in week}
    selected=conn.execute("""SELECT l.id,l.name,l.state FROM bf_locations l
        JOIN bf_client_site_scope s ON s.site_id=l.id WHERE s.account_id=?""",(aid,))
    sites={r['id']:r for r in selected if not state or r['state']==state}
    if not sites:
        return days
    start,end = grid[0][0].isoformat(),grid[-1][0].isoformat()
    weeks=conn.execute("""SELECT id,plan_json FROM bf_schedule_weeks WHERE status='published'
        AND week_start BETWEEN ? AND ?"""+(' AND state=?' if state else ''),
        (start,end,state) if state else (start,end)).fetchall()
    if not weeks:
        return days
    week_ids=[w['id'] for w in weeks]
    marks=','.join('?' for _ in week_ids)
    jobs={r['id']:r for r in conn.execute(f"""SELECT j.*,r.departed_at,
          EXISTS(SELECT 1 FROM bf_job_bins b WHERE b.job_id=j.id AND (b.delivered_at IS NOT NULL OR b.picked_up_at IS NOT NULL)) AS started,
          (SELECT n.id FROM bf_jobs n WHERE n.truck_id=j.truck_id AND n.scheduled_date=j.scheduled_date
           AND n.status='scheduled' AND n.schedule_live=1 AND n.approval_status='approved' ORDER BY n.run_order,n.id LIMIT 1) AS active_job
          FROM bf_jobs j JOIN bf_client_site_scope s ON s.site_id=j.site_id AND s.account_id=?
          LEFT JOIN bf_runs r ON r.truck_id=j.truck_id AND r.run_date=j.scheduled_date
          WHERE j.schedule_week_id IN ({marks}) AND j.schedule_live=1""",(aid,*week_ids))}
    for week in weeks:
        seen=set()
        for day in json.loads(week['plan_json']).get('days',[]):
            if day['date'] not in days: continue
            for item in day.get('items',[]):
                if item.get('kind') not in ('visit','job'):continue
                job=jobs.get(item.get('job_id')) if item['kind']=='job' else None
                sid=job['site_id'] if job else item.get('site_id')
                if sid not in sites or (item['kind']=='job' and not job):continue
                if job and (job['id'] in seen or job['schedule_week_id']!=week['id']):continue
                if job:seen.add(job['id'])
                status='booked'
                if job:
                    if job['status']=='cancelled' or job['approval_status']=='rejected':status='cancelled'
                    elif job['status'] in ('collected','received','complete'):status='completed'
                    elif job['driver_arrived_at'] or job['started'] or (job['departed_at'] and job['id']==job['active_job']):status='on_route'
                days[day['date']]['entries'].append(dict(site=sites[sid]['name'],state=sites[sid]['state'],status=status))
    return days


def register(app, db, client_access, now):
    @app.get('/binflow/client/calendar')
    @client_access
    def bf_client_calendar():
        view = request.args.get('view','week' if 'week' in request.args else 'month')
        state = request.args.get('state','')
        if view not in ('week','month') or (state and state not in STATES):
            abort(400)
        today = now()[:10]
        try:
            chosen = dated(request.args.get('date') or request.args.get('week') or today)
        except ValueError:
            abort(400,'Choose a valid calendar date.')
        _,_,previous,following,title,grid = calendar_period(chosen,view)
        return render_template('binflow/client_calendar.html',account=g.bf_client,states=STATES,state=state,
            view=view,chosen=chosen,today=today,previous=previous,following=following,title=title,grid=grid,
            days=calendar(db(),g.bf_client['id'],grid,state))

    @app.get('/binflow/client/local-pickups')
    @client_access
    def bf_client_national():
        from binflow_collections import collection_calendar_view, STATES as calendar_states
        template, data = collection_calendar_view(db(), request.args, now()[:10],default_view='month')
        return render_template('binflow/'+template+'.html',account=g.bf_client,states=calendar_states,
                               portal_calendar=True, **data)

    @app.get('/binflow/client/local-pickups/progress')
    @client_access
    def bf_client_national_progress():
        from binflow_calendar_edit import calendar_progress_response
        return calendar_progress_response(db())

    @app.get('/binflow/client/local-pickups/weeks')
    @client_access
    def bf_client_national_weeks():
        from binflow_calendar_edit import calendar_weeks_response
        return calendar_weeks_response(db())

    @app.get('/binflow/client/requests')
    @client_access
    def bf_client_requests():
        state=request.args.get('state','')
        if state and state not in STATES:abort(400)
        rows=db().execute("""SELECT r.id,r.bin_code,r.preferred_date,r.notes,r.status,r.created_at,
          l.name AS site_name,l.state,j.scheduled_date,j.approval_status
          FROM bf_collection_requests r JOIN bf_client_site_scope s ON s.site_id=r.site_id
          JOIN bf_locations l ON l.id=r.site_id LEFT JOIN bf_jobs j ON j.id=r.job_id
          WHERE s.account_id=?"""+(' AND l.state=?' if state else '')+' ORDER BY r.created_at DESC,r.id DESC',
          (g.bf_client['id'],state) if state else (g.bf_client['id'],)).fetchall()
        return render_template('binflow/client_requests.html',account=g.bf_client,requests=rows,states=STATES,state=state)
