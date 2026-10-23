"""Calendar plan persistence, approval and read-only driver publication."""
import json
import calendar
import hashlib
import re
import sqlite3
import uuid
from datetime import date, timedelta

from flask import abort, g, jsonify, request, redirect, url_for
from binflow_permissions import is_office, state_scope
from binflow_acceptance import ensure_run_unstarted, schedule_changed

STATES = ('QLD', 'NSW', 'VIC', 'SA', 'WA', 'TAS', 'ACT', 'NT')


def migrate(conn):
    conn.executescript('''
    CREATE TABLE IF NOT EXISTS bf_schedule_weeks (
      id INTEGER PRIMARY KEY, state TEXT NOT NULL, week_start TEXT NOT NULL,
      revision INTEGER NOT NULL, status TEXT NOT NULL CHECK(status IN ('draft','published')),
      driver_id INTEGER REFERENCES users(id), plan_json TEXT NOT NULL,
      approved_at TEXT, approved_by INTEGER REFERENCES users(id),
      updated_at TEXT NOT NULL, updated_by INTEGER NOT NULL REFERENCES users(id),
      UNIQUE(state,week_start));
    CREATE TABLE IF NOT EXISTS bf_schedule_templates (
      id INTEGER PRIMARY KEY, state TEXT NOT NULL, name TEXT NOT NULL COLLATE NOCASE,
      version INTEGER NOT NULL, source_week TEXT NOT NULL, plan_json TEXT NOT NULL,
      created_at TEXT NOT NULL, created_by INTEGER NOT NULL REFERENCES users(id),
      UNIQUE(state,name,version));
    CREATE TABLE IF NOT EXISTS bf_schedule_history (
      id INTEGER PRIMARY KEY, week_id INTEGER NOT NULL REFERENCES bf_schedule_weeks(id),
      revision INTEGER NOT NULL, action TEXT NOT NULL, actor_id INTEGER NOT NULL REFERENCES users(id),
      created_at TEXT NOT NULL, snapshot TEXT NOT NULL, UNIQUE(week_id,revision));
    ''')
    columns = {r['name'] for r in conn.execute('PRAGMA table_info(bf_jobs)')}
    for name, definition in (('schedule_week_id', 'INTEGER REFERENCES bf_schedule_weeks(id)'),
                             ('schedule_live', 'INTEGER NOT NULL DEFAULT 1')):
        if name not in columns:
            conn.execute(f'ALTER TABLE bf_jobs ADD COLUMN {name} {definition}')
    conn.execute('CREATE INDEX IF NOT EXISTS bf_jobs_week ON bf_jobs(schedule_week_id)')


def monday(raw):
    try:
        day = date.fromisoformat(raw)
        if not 2000 <= day.year <= 2100: raise ValueError()
        return (day - timedelta(days=day.weekday())).isoformat()
    except (ValueError, TypeError):
        raise ValueError('Choose a valid week.') from None


def drivers(conn, state):
    # Dispatch is for active drivers/contractors in this state, never an office role.
    return [dict(r) for r in conn.execute("""SELECT id,first_name,last_name,working_state FROM users
        WHERE role='employee' AND status='active' AND business_role IN ('driver','contractor')
        AND working_state=? ORDER BY first_name,last_name""", (state,))]


def saved_week(conn, state, start):
    row = conn.execute('''SELECT w.*,u.first_name||' '||u.last_name AS driver_name,
        a.first_name||' '||a.last_name AS editor_name FROM bf_schedule_weeks w
        LEFT JOIN users u ON u.id=w.driver_id JOIN users a ON a.id=w.updated_by
        WHERE w.state=? AND w.week_start=?''', (state, start)).fetchone()
    return dict(row) if row else None


def job_item(row, key=None):
    return dict(id=key or 'job-'+str(row['id']), kind='job', job_id=row['id'], site_id=row['site_id'],
                title=row['code']+' · '+row['site_name'], address=row['site_address'],
                detail='Allocation awaiting approval' if row['approval_status']=='pending' else 'Job / allocation',
                time=row['scheduled_time'], notes='', status=row['status'])


def visit_item(p, key):
    return dict(id=key, kind='visit', profile_id=p['profile_id'], site_id=p['id'], title=p['name'],
                address=', '.join(v for v in (p['address'],p['suburb'],p['state']) if v),
                detail=f'{p["large"]} × 1100L · {p["small"]} × 240L', time='', notes='',
                requested=p['schedule'], status='Planned visit · bin allocation required')


def initial_plan(conn, state, start):
    from binflow_route_calendar import week_plan, clients
    people = clients(conn)
    groups = week_plan(conn, date.fromisoformat(start), state, people=people)
    group = next((x for x in groups if x['state']==state), None)
    days = []
    for offset in range(7):
        key = (date.fromisoformat(start)+timedelta(days=offset)).isoformat()
        source = next((d for d in group['days'] if d['date']==key), None) if group else None
        jobs = conn.execute("""SELECT * FROM bf_jobs WHERE state=? AND scheduled_date=?
            AND status!='cancelled' AND schedule_week_id IS NULL ORDER BY run_order,id""", (state,key)).fetchall()
        items = [job_item(j) for j in jobs]
        job_sites = {j['site_id'] for j in jobs}
        if source:
            for p in source['profiles']:
                if p['id'] and p['id'] not in job_sites:
                    items.append(visit_item(p, f'visit-{p["profile_id"]}-{offset}'))
            if source['baling']:
                items.append(dict(id='baling-'+key,kind='baling',title='Baling & depot work',address='',
                                  detail='Emptying, bin preparation and stock checks',time='06:30',notes=''))
        days.append(dict(date=key,items=items,notes='',estimate_minutes=source['minutes'] if source else 0,
                         estimate_valid=True, loads=[dict(start=t['start'],end=t['end'],spaces=t['spaces'],
                            stops=[dict(title=x['profile']['name'],large=x['large'],small=x['small']) for x in t['stops']])
                            for t in source['trips']] if source else []))
    backlog = []
    if group:
        for day in group['days']:
            for item in day['exceptions']:
                p=item['profile']
                backlog.append(dict(title=p['name'],profile_id=p['profile_id'],reason=item['reason'],
                                    date=day['date'] if item['fixed'] else ''))
    return dict(days=days,resource=group['resource'] if group else dict(depot='Depot to confirm',weekly_hours=0,capacity=0),
                issues=group['issues'] if group else ['Depot and working limits need confirmation.'],backlog=backlog)


def get_plan(conn, state, start):
    row = saved_week(conn,state,start)
    return row, json.loads(row['plan_json']) if row else initial_plan(conn,state,start)


def job_positions(plan):
    return {item['job_id']:(day['date'],index+1,item.get('time',''),item.get('notes',''))
            for day in plan['days'] for index,item in enumerate(day['items']) if item['kind']=='job'}


def clean_plan(conn, raw, original, state, start, week_id):
    from binflow_route_calendar import clients
    if not isinstance(raw,dict) or not isinstance(raw.get('days'),list) or len(raw['days'])!=7:
        raise ValueError('A schedule must contain all seven days.')
    people={p['profile_id']:p for p in clients(conn) if p['id'] and p['state']==state}
    old_items={i['id']:i for d in original['days'] for i in d['items']}
    old_days={d['date']:d for d in original['days']}
    cleaned=dict(original);cleaned['days']=[];keys=set();job_ids=set();count=0
    for offset,day in enumerate(raw['days']):
        expected=(date.fromisoformat(start)+timedelta(days=offset)).isoformat()
        if not isinstance(day,dict) or day.get('date')!=expected or not isinstance(day.get('items'),list):
            raise ValueError('Keep each day inside the selected week. Choose another week to plan it.')
        if len(day['items'])>100: raise ValueError('A day can contain at most 100 stops.')
        note=day.get('notes','')
        if not isinstance(note,str) or len(note)>1000: raise ValueError('Day notes must be under 1,001 characters.')
        output=dict(old_days[expected],items=[],notes=note.strip());visit_ids=set();baling=0
        for item in day['items']:
            if not isinstance(item,dict): raise ValueError('Invalid schedule item.')
            key=item.get('id','');kind=item.get('kind');clock=item.get('time','');notes=item.get('notes','')
            if not isinstance(key,str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,80}',key) or key in keys:
                raise ValueError('Each schedule item needs a unique reference. Refresh the page.')
            keys.add(key);count+=1
            if not isinstance(notes,str) or len(notes)>1000: raise ValueError('Stop notes must be under 1,001 characters.')
            if not isinstance(clock,str) or (clock and (not re.fullmatch(r'\d{2}:\d{2}',clock) or not '06:30'<=clock<='15:30' or int(clock[-2:])>59)):
                raise ValueError('Arrival times must be between 06:30 and 15:30 in local state time.')
            if kind=='job':
                jid=item.get('job_id')
                if type(jid)!=int or jid in job_ids: raise ValueError('Add each job / allocation only once.')
                job_ids.add(jid)
                job=conn.execute('SELECT * FROM bf_jobs WHERE id=? AND state=?',(jid,state)).fetchone()
                if not job or job['status']=='cancelled' or job['approval_status']=='rejected':
                    raise ValueError('A selected job is unavailable. Reload and review the schedule.')
                if job['schedule_week_id'] and job['schedule_week_id']!=week_id:
                    raise ValueError('This job belongs to another saved week. Remove it there before adding it here.')
                value=job_item(job,key)
            elif kind=='visit':
                pid=item.get('profile_id')
                if type(pid)!=int or pid not in people or pid in visit_ids:
                    raise ValueError('Choose a current client in this state, once per day.')
                visit_ids.add(pid);value=visit_item(people[pid],key)
            elif kind=='baling':
                baling+=1
                if baling>1: raise ValueError('Only one baling block is needed per day.')
                value=dict(id=key,kind=kind,title='Baling & depot work',address='',detail='Emptying, bin preparation and stock checks')
            else: raise ValueError('Choose a client visit, job / allocation, or baling block.')
            value.update(time=clock,notes=notes.strip())
            if key in old_items and (old_items[key]['kind']!=kind or any(old_items[key].get(k)!=value.get(k) for k in ('job_id','profile_id'))):
                raise ValueError('A stop reference cannot be changed. Remove the stop and add a new one.')
            output['items'].append(value)
        sites_with_jobs={i['site_id'] for i in output['items'] if i['kind']=='job'}
        if any(i['kind']=='visit' and i['site_id'] in sites_with_jobs for i in output['items']):
            raise ValueError('A day contains a client visit and a job for the same site. Remove the placeholder visit before adding its allocation.')
        # An edit invalidates route estimates; never present a stale optimized route as current.
        fields=lambda d: [(i['id'],i.get('time',''),i.get('notes','')) for i in d['items']]
        output['estimate_valid']=bool(old_days[expected].get('estimate_valid') and fields(output)==fields(old_days[expected]))
        cleaned['days'].append(output)
    if count>400: raise ValueError('A week can contain at most 400 stops.')
    return cleaned


def ensure_job_editable(conn, job):
    if job['status']!='scheduled' or conn.execute('''SELECT 1 FROM bf_job_bins WHERE job_id=? AND
        (loaded_at IS NOT NULL OR delivered_at IS NOT NULL OR picked_up_at IS NOT NULL OR returned_at IS NOT NULL OR weighed_at IS NOT NULL)''',(job['id'],)).fetchone():
        raise ValueError('A job has started or finished. Its date, driver and order are locked; finish that run first.')
    ensure_run_unstarted(conn,job['truck_id'],job['scheduled_date'])


def sync_jobs(conn, old, plan, row, driver_id, publish, uid, actor, stamp, review, scope=None, extra_jobs=(), deferred_runs=None):
    previous=job_positions(old);positions=job_positions(plan)
    assigned=conn.execute('SELECT * FROM bf_jobs WHERE schedule_week_id=?',(row['id'],)).fetchall()
    records={j['id']:j for j in assigned}
    for jid in set(positions)|set(previous)|set(extra_jobs):
        records[jid]=conn.execute('SELECT * FROM bf_jobs WHERE id=?',(jid,)).fetchone()
    changed_runs=set()
    for jid,job in records.items():
        new=positions.get(jid);prior=previous.get(jid)
        if scope and not any(scope[0]<=key<=scope[1] for key in
                ([new[0]] if new else [])+([prior[0]] if prior else [])+([job['scheduled_date']] if jid in extra_jobs else [])):
            continue
        # A no-op save must not disturb an active run, but any actual planning change is locked.
        changed = (new!=prior or (publish and (job['driver_id']!=driver_id or not job['schedule_live']))
                   or (not publish and job['schedule_live']) or (new is None))
        if changed:
            ensure_job_editable(conn,job)
            if job['truck_id']:
                changed_runs.add((job['truck_id'],job['scheduled_date']))
                if new and publish:
                    ensure_run_unstarted(conn,job['truck_id'],new[0])
                    changed_runs.add((job['truck_id'],new[0]))
        if new and publish:
            conn.execute('''UPDATE bf_jobs SET schedule_week_id=?,schedule_live=1,scheduled_date=?,
                run_order=?,scheduled_time=?,driver_id=? WHERE id=?''',
                (row['id'],new[0],new[1],new[2],driver_id,jid))
            if job['approval_status']=='pending': review(jid,'approve','Approved with weekly schedule')
        elif new:
            conn.execute('UPDATE bf_jobs SET schedule_week_id=?,schedule_live=0 WHERE id=?',(row['id'],jid))
        else:
            conn.execute('''UPDATE bf_jobs SET schedule_week_id=NULL,schedule_live=0,driver_id=NULL,
                truck_id=NULL,vehicle='' WHERE id=?''',(jid,))
    if deferred_runs is not None:
        deferred_runs.update(changed_runs)
        return
    refresh_runs(conn, changed_runs, records, uid, actor, stamp)


def refresh_runs(conn, changed_runs, records, uid, actor, stamp):
    for tid,day in changed_runs:
        jobs=conn.execute("""SELECT driver_id FROM bf_jobs WHERE truck_id=? AND scheduled_date=?
            AND schedule_live=1 AND approval_status='approved' AND status!='cancelled'""",(tid,day)).fetchall()
        owners={j['driver_id'] for j in jobs}
        if len(owners)>1 or (jobs and None in owners):
            raise ValueError('This truck has other jobs with a different or unassigned driver. Reconcile its allocation before publishing.')
        owner=next(iter(owners)) if owners else None
        if owner and conn.execute('''SELECT 1 FROM bf_workdays WHERE truck_id=? AND work_date=?
            AND finished_at IS NULL AND user_id!=?''',(tid,day,owner)).fetchone():
            raise ValueError('Another driver has an open workday on this truck. Resolve that assignment before publishing.')
        run=conn.execute('SELECT driver_id FROM bf_runs WHERE truck_id=? AND run_date=?',(tid,day)).fetchone()
        if owner and run and run['driver_id'] and run['driver_id']!=owner and (tid,day) not in {(j['truck_id'],j['scheduled_date']) for j in records.values()}:
            raise ValueError('The destination truck run belongs to another driver.')
        schedule_changed(conn,tid,day,uid,actor,stamp,'Admin saved the weekly schedule.')
        conn.execute('''INSERT INTO bf_runs(truck_id,run_date,driver_id) VALUES(?,?,?) ON CONFLICT(truck_id,run_date)
            DO UPDATE SET driver_id=excluded.driver_id''',(tid,day,owner))


def reusable_plan(conn, plan, state):
    """Templates describe future visits, never duplicate dockets or bin reservations."""
    from binflow_route_calendar import clients
    people={p['id']:p for p in clients(conn) if p['id'] and p['state']==state}
    result=json.loads(json.dumps(plan))
    for day in result['days']:
        visits={};output=[]
        for item in day['items']:
            if item['kind']=='job':
                p=people.get(item['site_id'])
                if not p: raise ValueError('A job site has no current client profile in this state. Update its profile before saving a reusable schedule.')
                value=visit_item(p,'template-'+uuid.uuid4().hex)
                value.update(time=item.get('time',''),notes=item.get('notes',''))
                item=value
            if item['kind']=='visit':
                pid=item['profile_id']
                if pid in visits:
                    notes='\n'.join(dict.fromkeys(v for v in (visits[pid].get('notes',''),item.get('notes','')) if v))
                    if len(notes)>1000: raise ValueError('Consolidate the notes for repeated stops at this site before saving a reusable schedule.')
                    visits[pid]['notes']=notes
                    continue
                visits[pid]=item
            item['id']='template-'+uuid.uuid4().hex
            output.append(item)
        day['items']=output
    return result


def clear_preview(conn, state, period, chosen):
    if state not in STATES or period not in ('week','month'):
        raise ValueError('Choose a state and a week or month to clear.')
    start=monday(chosen)
    day=date.fromisoformat(chosen)
    if period=='week':
        first=date.fromisoformat(start);last=first+timedelta(days=6)
    else:
        first=day.replace(day=1);last=day.replace(day=calendar.monthrange(day.year,day.month)[1])
    first_key,last_key=first.isoformat(),last.isoformat()
    weeks=[];cursor=date.fromisoformat(monday(first_key));total=0
    while cursor<=last:
        row,plan=get_plan(conn,state,cursor.isoformat())
        total+=sum(len(d['items']) for d in plan['days'] if first_key<=d['date']<=last_key)
        weeks.append(dict(start=cursor.isoformat(),row=row,plan=plan))
        cursor+=timedelta(days=7)
    actual=[dict(j) for j in conn.execute('''SELECT id,scheduled_date,status,driver_id,truck_id,schedule_week_id,
        schedule_live,approval_status FROM bf_jobs WHERE state=? AND scheduled_date BETWEEN ? AND ?
        AND schedule_live=1 AND status!='cancelled' ''',(state,first_key,last_key))]
    affected={j['id'] for j in actual}
    affected.update(i['job_id'] for w in weeks for d in w['plan']['days'] if first_key<=d['date']<=last_key
                    for i in d['items'] if i['kind']=='job')
    blocked=0
    for jid in affected:
        job=conn.execute('SELECT * FROM bf_jobs WHERE id=?',(jid,)).fetchone()
        try: ensure_job_editable(conn,job)
        except ValueError: blocked+=1
    fingerprint=dict(state=state,period=period,first=first_key,last=last_key,
        weeks=[dict(start=w['start'],revision=w['row']['revision'] if w['row'] else 0,plan=w['plan']) for w in weeks],jobs=actual)
    token=hashlib.sha256(json.dumps(fingerprint,sort_keys=True).encode()).hexdigest()
    summary=dict(state=state,period=period,first=first_key,last=last_key,weeks=len(weeks),items=total,jobs=len(affected),
                 published=sum(bool(w['row'] and w['row']['status']=='published') for w in weeks),blocked=blocked,token=token)
    return summary,weeks,actual


def register(app,db,access,require,render,now,actor):
    from binflow_calendar_edit import register_calendar_edit
    register_calendar_edit(app, db, require, now, actor)

    def selection():
        from binflow_route_calendar import START
        state=request.args.get('state') or state_scope(g.user) or 'QLD'
        chosen=request.args.get('date')
        if not chosen and not is_office(g.user):
            upcoming=db().execute('''SELECT week_start FROM bf_schedule_weeks WHERE state=? AND driver_id=?
                AND status='published' AND week_start>=? ORDER BY week_start LIMIT 1''',
                (state,g.user['id'],monday(now()[:10]))).fetchone()
            chosen=upcoming['week_start'] if upcoming else now()[:10]
        start=monday(chosen or max(START.isoformat(),now()[:10]))
        if state not in STATES: abort(400)
        if not is_office(g.user) and state!=state_scope(g.user): abort(403)
        return state,start

    def visible(state,start):
        row=saved_week(db(),state,start)
        if not is_office(g.user) and (not row or row['status']!='published' or row['driver_id']!=g.user['id']):
            return None
        return row

    @app.get('/binflow/schedule')
    @access
    def bf_week_schedule():
        try: state,start=selection()
        except ValueError as exc: abort(400,str(exc))
        return redirect(url_for('bf_collection_calendar' if is_office(g.user) else 'bf_calendar',
                                state=state,date=request.args.get('date') or start))

    @app.get('/binflow/schedule/options')
    @require('admin')
    def bf_calendar_options():
        try: state,start=selection()
        except ValueError as exc: abort(400,str(exc))
        row,plan=get_plan(db(),state,start)
        from binflow_route_calendar import clients
        people=[visit_item(p,'') for p in clients(db()) if p['id'] and p['state']==state]
        candidates=[job_item(j) for j in db().execute("""SELECT * FROM bf_jobs WHERE state=? AND status='scheduled'
            AND approval_status!='rejected' AND (schedule_week_id IS NULL OR schedule_week_id=?) ORDER BY scheduled_date,id""",(state,row['id'] if row else 0))]
        history=db().execute('''SELECT h.revision,h.action,h.created_at,u.first_name||' '||u.last_name actor
            FROM bf_schedule_history h JOIN users u ON u.id=h.actor_id WHERE week_id=? ORDER BY h.revision DESC''',
            (row['id'],)).fetchall() if row else []
        templates=db().execute('''SELECT id,name,version,source_week,created_at FROM bf_schedule_templates
            WHERE state=? ORDER BY name,version DESC''',(state,)).fetchall()
        return jsonify(state=state,start=start,plan=plan,revision=row['revision'] if row else 0,
            driver_id=row['driver_id'] if row else None,status=row['status'] if row else 'draft',
            sites=people,jobs=candidates,drivers=[dict(d) for d in drivers(db(),state)],
            history=[dict(h) for h in history],templates=[dict(t) for t in templates])

    @app.get('/binflow/schedule/version')
    @access
    def bf_week_schedule_version():
        try: state,start=selection()
        except ValueError as exc: abort(400,str(exc))
        row=visible(state,start)
        return jsonify(revision=row['revision'] if row else 0)

    @app.get('/binflow/schedule/clear-preview')
    @require('admin')
    def bf_clear_schedule_preview():
        try:
            summary,_,_=clear_preview(db(),request.args.get('state'),request.args.get('period'),request.args.get('date'))
            return jsonify(summary)
        except ValueError as exc: return jsonify(error=str(exc)),400

    @app.post('/binflow/schedule/clear')
    @require('admin')
    def bf_clear_schedule():
        try:
            db().execute('BEGIN IMMEDIATE')
            summary,weeks,actual=clear_preview(db(),request.form.get('state'),request.form.get('period'),request.form.get('date'))
            if request.form.get('token')!=summary['token']:
                db().rollback();return jsonify(error='The schedule changed after the preview. Preview the clear again before continuing.'),409
            if summary['blocked']:
                raise ValueError('This period includes started or completed jobs. Their schedule is protected; choose a period without those jobs.')
            stamp=now();uid=g.user['id'];first,last=summary['first'],summary['last'];state=summary['state']
            for entry in weeks:
                row,old,start=entry['row'],entry['plan'],entry['start']
                plan=json.loads(json.dumps(old))
                for day in plan['days']:
                    if first<=day['date']<=last:
                        day.update(items=[],notes='',estimate_minutes=0,estimate_valid=False,loads=[])
                entire=first<=plan['days'][0]['date'] and plan['days'][-1]['date']<=last
                plan['backlog']=[] if entire else [b for b in plan['backlog'] if not b.get('date') or not first<=b['date']<=last]
                if not row:
                    wid=db().execute('''INSERT INTO bf_schedule_weeks(state,week_start,revision,status,plan_json,updated_at,updated_by)
                        VALUES(?,?,1,'draft',?,?,?)''',(state,start,json.dumps(old),stamp,uid)).lastrowid
                    row=dict(id=wid,revision=1,status='draft',driver_id=None)
                    before=dict(plan=old,state=state,week=start,driver_id=None,status='draft')
                    db().execute('''INSERT INTO bf_schedule_history(week_id,revision,action,actor_id,created_at,snapshot)
                        VALUES(?,1,'Plan before clearing',?,?,?)''',(wid,uid,stamp,json.dumps(before)))
                publish=row['status']=='published'
                extra=[j['id'] for j in actual if monday(j['scheduled_date'])==start]
                sync_jobs(db(),old,plan,row,row['driver_id'],publish,uid,actor(),stamp,
                          app.extensions['bf_review_allocation'],scope=(first,last),extra_jobs=extra)
                revision=row['revision']+1
                db().execute('UPDATE bf_schedule_weeks SET plan_json=?,revision=?,updated_at=?,updated_by=? WHERE id=?',
                    (json.dumps(plan),revision,stamp,uid,row['id']))
                snapshot=dict(plan=plan,state=state,week=start,driver_id=row['driver_id'],status=row['status'])
                db().execute('''INSERT INTO bf_schedule_history(week_id,revision,action,actor_id,created_at,snapshot)
                    VALUES(?,?,?,?,?,?)''',(row['id'],revision,'Cleared '+summary['period']+' · '+first+' to '+last,
                    uid,stamp,json.dumps(snapshot)))
            db().commit()
            return jsonify(message=state+' '+summary['period']+' cleared. Published schedules have been updated; previous versions are kept.')
        except (ValueError,sqlite3.IntegrityError) as exc:
            db().rollback()
            return jsonify(error=str(exc) if isinstance(exc,ValueError) else 'The schedule changed. Preview the clear again.'),400

    @app.post('/binflow/schedule/templates')
    @require('admin')
    def bf_save_schedule_template():
        try:
            state=request.form.get('state');start=monday(request.form.get('week'))
            name=request.form.get('name','').strip()
            if state not in STATES or not name or len(name)>80:
                raise ValueError('Choose a state and a schedule name of 1–80 characters.')
            db().execute('BEGIN IMMEDIATE')
            row,old=get_plan(db(),state,start)
            if str(row['revision'] if row else 0)!=request.form.get('revision'):
                db().rollback();return jsonify(error='This week changed. Reload the latest version before saving a named schedule.'),409
            try: raw=json.loads(request.form.get('plan',''))
            except (TypeError,ValueError): raise ValueError('The schedule could not be read.') from None
            plan=clean_plan(db(),raw,old,state,start,row['id'] if row else None)
            plan=reusable_plan(db(),plan,state)
            version=db().execute('SELECT COALESCE(MAX(version),0)+1 FROM bf_schedule_templates WHERE state=? AND name=?',
                                 (state,name)).fetchone()[0]
            stamp=now()
            tid=db().execute('''INSERT INTO bf_schedule_templates(state,name,version,source_week,plan_json,created_at,created_by)
                VALUES(?,?,?,?,?,?,?)''',(state,name,version,start,json.dumps(plan),stamp,g.user['id'])).lastrowid
            db().commit()
            return jsonify(id=tid,name=name,version=version,message=f'Saved {name} · version {version}. The live week is unchanged.')
        except (ValueError,sqlite3.IntegrityError) as exc:
            db().rollback()
            return jsonify(error=str(exc) if isinstance(exc,ValueError) else 'The saved schedule changed. Please try again.'),400

    @app.get('/binflow/schedule/load')
    @require('admin')
    def bf_load_schedule():
        try:
            state=request.args.get('state');start=monday(request.args.get('week'))
            if state not in STATES: raise ValueError('Choose a state.')
            row,old=get_plan(db(),state,start)
            if request.args.get('template'):
                template=db().execute('SELECT * FROM bf_schedule_templates WHERE id=? AND state=?',
                    (request.args.get('template',type=int),state)).fetchone()
                if not template: abort(404)
                loaded=json.loads(template['plan_json'])
                for offset,day in enumerate(loaded['days']):
                    day['date']=(date.fromisoformat(start)+timedelta(days=offset)).isoformat()
                    day['estimate_valid']=False
                    for item in day['items']: item['id']='loaded-'+uuid.uuid4().hex
                # Source exceptions and depots are date-specific; retain destination checks.
                loaded['backlog']=old['backlog'];loaded['resource']=old['resource'];loaded['issues']=old['issues']
                label=template['name']+' · version '+str(template['version'])
            else:
                version=request.args.get('revision',type=int)
                history=db().execute('SELECT snapshot FROM bf_schedule_history WHERE week_id=? AND revision=?',
                    (row['id'] if row else 0,version)).fetchone()
                if not history: abort(404)
                loaded=json.loads(history['snapshot'])['plan'];label='Saved week version '+str(version)
            # Validate current site/job ownership before showing a loadable preview.
            plan=clean_plan(db(),loaded,old,state,start,row['id'] if row else None)
            if request.args.get('template'):
                for day in plan['days']: day['estimate_valid']=False
            return jsonify(plan=plan,message=label+' loaded for review. Save or approve to apply it to this week.')
        except ValueError as exc:
            return jsonify(error=str(exc)),400

    @app.post('/binflow/schedule/save')
    @require('admin')
    def bf_save_week_schedule():
        try:
            state=request.form.get('state');start=monday(request.form.get('week'));action=request.form.get('action')
            if state not in STATES or start!=request.form.get('week') or action not in ('save','approve'):
                raise ValueError('Choose a state, Monday and save or approval action.')
            db().execute('BEGIN IMMEDIATE')
            row,old=get_plan(db(),state,start)
            revision=row['revision'] if row else 0
            if str(revision)!=request.form.get('revision'):
                db().rollback();return jsonify(error='This week changed in another session. Reload the latest schedule before saving.'),409
            try: raw=json.loads(request.form.get('plan',''))
            except (TypeError,ValueError): raise ValueError('The schedule could not be read. Reload and try again.') from None
            plan=clean_plan(db(),raw,old,state,start,row['id'] if row else None)
            publish=action=='approve' or bool(row and row['status']=='published')
            driver_id=row['driver_id'] if row else None
            if action=='approve':
                driver_id=request.form.get('driver_id',type=int)
                if request.form.get('reviewed')!='yes': raise ValueError('Review the working hours, depot loads and access checks before approving.')
            if publish and driver_id not in {u['id'] for u in drivers(db(),state)}:
                raise ValueError('Choose an active driver or contractor allocated to this state.')
            if action=='approve' and not any(d['items'] for d in plan['days']):
                raise ValueError('Add at least one visit, job / allocation or depot task before approval.')
            stamp=now();uid=g.user['id'];revision+=1
            if not row:
                wid=db().execute('''INSERT INTO bf_schedule_weeks(state,week_start,revision,status,plan_json,updated_at,updated_by)
                    VALUES(?,?,0,'draft','{}',?,?)''',(state,start,stamp,uid)).lastrowid
                row=dict(id=wid,status='draft',driver_id=None)
            sync_jobs(db(),old,plan,row,driver_id,publish,uid,actor(),stamp,app.extensions['bf_review_allocation'])
            status='published' if publish else 'draft'
            db().execute('''UPDATE bf_schedule_weeks SET revision=?,status=?,driver_id=?,plan_json=?,
                approved_at=CASE WHEN ? THEN ? ELSE approved_at END,approved_by=CASE WHEN ? THEN ? ELSE approved_by END,
                updated_at=?,updated_by=? WHERE id=?''',(revision,status,driver_id,json.dumps(plan),action=='approve',stamp,
                    action=='approve',uid,stamp,uid,row['id']))
            label='Week approved and published' if action=='approve' else 'Day changes saved and republished' if publish else 'Draft saved'
            snapshot=dict(plan=plan,state=state,week=start,driver_id=driver_id,status=status)
            db().execute('''INSERT INTO bf_schedule_history(week_id,revision,action,actor_id,created_at,snapshot)
                VALUES(?,?,?,?,?,?)''',(row['id'],revision,label,uid,stamp,json.dumps(snapshot)))
            db().commit()
            return jsonify(revision=revision,message=label,status=status)
        except (ValueError,sqlite3.IntegrityError) as exc:
            db().rollback()
            return jsonify(error=str(exc) if isinstance(exc,ValueError) else 'The schedule changed. Reload and try again.'),400
