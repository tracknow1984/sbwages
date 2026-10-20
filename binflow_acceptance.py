"""Driver acceptance of a dated, approved truck manifest before warehouse loading."""
import hashlib
import json
from collections import Counter, defaultdict
from datetime import date, timedelta

from flask import abort, g, request


def migrate_acceptance(conn):
    columns={r['name'] for r in conn.execute('PRAGMA table_info(bf_runs)')}
    first='acceptance_required' not in columns
    for name,definition in (
        ('acceptance_required','INTEGER NOT NULL DEFAULT 1'),('accepted_at','TEXT'),
        ('accepted_by','INTEGER REFERENCES users(id)'),('accepted_name',"TEXT NOT NULL DEFAULT ''"),
        ('accepted_manifest',"TEXT NOT NULL DEFAULT ''")):
        if name not in columns: conn.execute(f'ALTER TABLE bf_runs ADD COLUMN {name} {definition}')
    conn.execute('''CREATE TABLE IF NOT EXISTS bf_run_acceptance_history (
        id INTEGER PRIMARY KEY, truck_id INTEGER NOT NULL REFERENCES bf_trucks(id),run_date TEXT NOT NULL,
        action TEXT NOT NULL, user_id INTEGER NOT NULL REFERENCES users(id), actor TEXT NOT NULL,
        manifest TEXT NOT NULL DEFAULT '',notes TEXT NOT NULL DEFAULT '',created_at TEXT NOT NULL)''')
    if first:
        # Grandfather physical runs already underway, without inventing driver acceptance records.
        conn.execute('''UPDATE bf_runs SET acceptance_required=0 WHERE departed_at IS NOT NULL OR returned_at IS NOT NULL
            OR EXISTS(SELECT 1 FROM bf_jobs j JOIN bf_job_bins i ON i.job_id=j.id
                WHERE j.truck_id=bf_runs.truck_id AND j.scheduled_date=bf_runs.run_date
                AND (i.loaded_at IS NOT NULL OR i.delivered_at IS NOT NULL OR i.picked_up_at IS NOT NULL OR i.returned_at IS NOT NULL))''')


def manifest(conn,truck_id,run_date):
    jobs=[dict(r) for r in conn.execute('''SELECT id,code,site_id,warehouse_id,state,client_name,site_name,
        warehouse_name,scheduled_date,scheduled_time,run_order,driver_id,notes FROM bf_jobs
        WHERE truck_id=? AND scheduled_date=? AND approval_status='approved' AND status!='cancelled' ORDER BY run_order,id''',(truck_id,run_date))]
    for j in jobs:
        j['bins']=[dict(r) for r in conn.execute('''SELECT bin_id,bin_code,direction,size_name
            FROM bf_job_bins WHERE job_id=? ORDER BY direction,bin_id''',(j['id'],))]
    snapshot=json.dumps(jobs,sort_keys=True,separators=(',',':'))
    return jobs,snapshot,hashlib.sha256(snapshot.encode()).hexdigest()


def acceptance_ok(conn,run,truck_id,run_date):
    if not run['acceptance_required']: return True
    return bool(run['accepted_at'] and run['accepted_manifest']==manifest(conn,truck_id,run_date)[2])


def ensure_run_unstarted(conn,truck_id,run_date):
    if not truck_id: return
    run=conn.execute('SELECT * FROM bf_runs WHERE truck_id=? AND run_date=?',(truck_id,run_date)).fetchone()
    if (run and (run['departed_at'] or run['returned_at'])) or conn.execute('''SELECT 1 FROM bf_jobs j
        JOIN bf_job_bins i ON i.job_id=j.id WHERE j.truck_id=? AND j.scheduled_date=? AND j.status!='cancelled'
        AND (i.loaded_at IS NOT NULL OR i.delivered_at IS NOT NULL OR i.picked_up_at IS NOT NULL OR i.returned_at IS NOT NULL)''',
        (truck_id,run_date)).fetchone():
        raise ValueError('This run has started. Unload unused empties before changing its schedule; completed client movements must finish their run.')


def schedule_changed(conn,truck_id,run_date,uid,actor,stamp,notes):
    if not truck_id: return
    run=conn.execute('SELECT * FROM bf_runs WHERE truck_id=? AND run_date=?',(truck_id,run_date)).fetchone()
    if run and run['accepted_at']:
        conn.execute('''INSERT INTO bf_run_acceptance_history(truck_id,run_date,action,user_id,actor,manifest,notes,created_at)
            VALUES(?,?,'Schedule changed; accept again',?,?,?,?,?)''',(truck_id,run_date,uid,actor,run['accepted_manifest'],notes,stamp))
        conn.execute("UPDATE bf_runs SET acceptance_required=1,accepted_at=NULL,accepted_by=NULL,accepted_name='',accepted_manifest='' WHERE truck_id=? AND run_date=?",(truck_id,run_date))


def schedule_cards(conn,state,run_date,today,user):
    """Only actual approved jobs with a truck can be accepted; planning profiles are separate."""
    from binflow_permissions import is_office
    first=min(run_date,today);last=max(run_date,(date.fromisoformat(today)+timedelta(days=60)).isoformat())
    jobs=conn.execute('''SELECT j.*,t.name AS truck_name,r.driver_id AS run_driver,r.accepted_at,r.accepted_by,r.accepted_name,r.returned_at,
        r.acceptance_required,u.first_name||' '||u.last_name AS assigned_name
        FROM bf_jobs j JOIN bf_trucks t ON t.id=j.truck_id
        LEFT JOIN bf_runs r ON r.truck_id=j.truck_id AND r.run_date=j.scheduled_date
        LEFT JOIN users u ON u.id=COALESCE(r.driver_id,j.driver_id)
        WHERE j.approval_status='approved' AND j.status!='cancelled' AND j.scheduled_date BETWEEN ? AND ?
        ORDER BY j.scheduled_date,t.name,j.run_order,j.id''',(first,last)).fetchall()
    grouped=defaultdict(list)
    for j in jobs: grouped[(j['truck_id'],j['scheduled_date'])].append(j)
    cards=[];state_counts=Counter();days=Counter()
    for (tid,day),rows in grouped.items():
        assigned={r['driver_id'] for r in rows if r['driver_id']}|{r['run_driver'] for r in rows if r['run_driver']}
        mine=not assigned or assigned=={user['id']}
        if not is_office(user) and not mine: continue
        active=any(r['status'] in ('scheduled','collected') for r in rows)
        if active and day>=today: state_counts[rows[0]['state']]+=1
        if rows[0]['state']!=state: continue
        if active or day==run_date: days[day]+=1
        if day!=run_date: continue
        r=rows[0];run=conn.execute('SELECT * FROM bf_runs WHERE truck_id=? AND run_date=?',(tid,day)).fetchone()
        accepted=bool(run and acceptance_ok(conn,run,tid,day))
        values,snapshot,version=manifest(conn,tid,day)
        sizes=Counter();warehouses=set()
        for job in values:
            warehouses.add(job['warehouse_name'])
            job['empties']=Counter(b['size_name'] for b in job['bins'] if b['direction']=='deliver')
            sizes.update(job['empties'])
        cards.append(dict(truck_id=tid,name=r['truck_name'],jobs=values,accepted=accepted,
            accepted_at=r['accepted_at'],accepted_name=r['accepted_name'],assigned_name=r['assigned_name'],
            can_accept=mine and not accepted and all(j['status']=='scheduled' for j in rows),
            mine=mine,manifest_version=version,totals=sizes,warehouses=sorted(warehouses),returned=bool(r['returned_at'])))
    days.setdefault(run_date,0)
    visible=sorted(days.items())[:28]
    if run_date not in dict(visible): visible=sorted(visible[:27]+[(run_date,days[run_date])])
    return dict(schedules=cards,state_counts=state_counts,schedule_days=visible)


def register_acceptance(app,db,access,success,failure,now,actor):
    from binflow_runs import run_jobs,valid_date,run_data
    from binflow_workday import run_workday,driver_today

    @app.post('/binflow/driver/<int:truck_id>/accept')
    @access
    def bf_accept_run(truck_id):
        from binflow_permissions import require_state
        run_date=request.form.get('date','')
        truck=db().execute('SELECT * FROM bf_trucks WHERE id=?',(truck_id,)).fetchone()
        if not truck: abort(404)
        require_state(g.user,truck['state'])
        try:
            valid_date(run_date)
            db().execute('BEGIN IMMEDIATE')
            day=run_workday(db(),g.user['id'],truck_id,run_date)
            if g.user['role']=='employee' or day:
                data=run_data(db(),truck_id,run_date,g.user['id'],driver_today(db(),g.user,now()))
                if not day or day['finished_at'] or not data['passed'] or data['hold']:
                    raise ValueError('Start your driver day, select this vehicle and pass the prestart before accepting the schedule.')
            jobs=run_jobs(db(),truck_id,run_date)
            if not jobs: raise ValueError('There are no approved jobs on this truck for the selected day.')
            run=db().execute('SELECT * FROM bf_runs WHERE truck_id=? AND run_date=?',(truck_id,run_date)).fetchone()
            assigned={j['driver_id'] for j in jobs if j['driver_id']}
            if run and run['driver_id']: assigned.add(run['driver_id'])
            if assigned and assigned!={g.user['id']}:
                raise ValueError('This schedule is assigned to another driver. Ask the office to reassign it.')
            if run and acceptance_ok(db(),run,truck_id,run_date):
                return success('Schedule already accepted. Continue your day.','bf_run',truck_id=truck_id,date=run_date)
            ensure_run_unstarted(db(),truck_id,run_date)
            if any(j['status']!='scheduled' or not j['scan_mode'] for j in jobs):
                raise ValueError('Only an unstarted scan-based schedule can be accepted.')
            values,snapshot,version=manifest(db(),truck_id,run_date)
            if request.form.get('manifest_version')!=version:
                raise ValueError('The schedule changed. Review the updated clients and bins, then accept again.')
            if any(not j['bins'] for j in values): raise ValueError('The schedule needs a bin manifest before acceptance.')
            if db().execute('''SELECT 1 FROM bf_runs r WHERE r.run_date=? AND r.truck_id!=? AND r.driver_id=?
                AND r.accepted_at IS NOT NULL AND r.returned_at IS NULL
                AND EXISTS(SELECT 1 FROM bf_jobs j WHERE j.truck_id=r.truck_id AND j.scheduled_date=r.run_date AND j.status!='cancelled')''',
                (run_date,truck_id,g.user['id'])).fetchone():
                raise ValueError('You have already accepted another truck for this day. Finish that run or ask the office to reassign it.')
            stamp=now();person=actor()
            db().execute('INSERT INTO bf_runs(truck_id,run_date,driver_id) VALUES(?,?,?) ON CONFLICT(truck_id,run_date) DO UPDATE SET driver_id=excluded.driver_id',
                (truck_id,run_date,g.user['id']))
            db().execute("UPDATE bf_jobs SET driver_id=? WHERE truck_id=? AND scheduled_date=? AND approval_status='approved' AND status!='cancelled'",(g.user['id'],truck_id,run_date))
            values,snapshot,version=manifest(db(),truck_id,run_date)
            db().execute('''UPDATE bf_runs SET acceptance_required=1,accepted_at=?,accepted_by=?,accepted_name=?,accepted_manifest=?
                WHERE truck_id=? AND run_date=?''',(stamp,g.user['id'],person,version,truck_id,run_date))
            db().execute('''INSERT INTO bf_run_acceptance_history(truck_id,run_date,action,user_id,actor,manifest,created_at)
                VALUES(?,?,'Schedule accepted',?,?,?,?)''',(truck_id,run_date,g.user['id'],person,snapshot,stamp))
            return success('Schedule accepted. Scan the allocated empty bins onto your truck.' if day else 'Schedule accepted. Complete today’s prestart, then scan your allocated empty bins onto the truck.',
                'bf_run',truck_id=truck_id,date=run_date)
        except ValueError as error:
            return failure(error,'bf_drivers',state=truck['state'],date=run_date or now()[:10])
