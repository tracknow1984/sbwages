"""Client-selected recurring planning visits, without creating jobs or reserving bins."""
import hashlib
import json
from datetime import date, timedelta

from flask import abort, g, request

from binflow_collections import occurs, INTERVALS
from binflow_profile_schedule import stored, DAYS, COUNTS


def migrate(conn):
    conn.execute('''CREATE TABLE IF NOT EXISTS bf_calendar_cycles (
        profile_id INTEGER PRIMARY KEY REFERENCES bf_client_site_profiles(id),
        first_date TEXT NOT NULL, requested_schedule TEXT NOT NULL, pattern_json TEXT NOT NULL,
        version INTEGER NOT NULL, active INTEGER NOT NULL DEFAULT 1,
        updated_at TEXT NOT NULL, updated_by INTEGER NOT NULL REFERENCES users(id))''')


def active(conn):
    return {r['profile_id']:dict(r) for r in conn.execute('SELECT * FROM bf_calendar_cycles WHERE active=1')}


def pattern(profile, first, chosen_days=()):
    spec=stored(profile)
    freq=spec.frequency
    if freq in ('unset','review','paused'):
        raise ValueError('Confirm the requested schedule in the client profile before adding it to the calendar.')
    days=list(spec.days)
    if freq in ('twice','three') and not days:
        days=sorted(set(chosen_days))
        if len(days)!=COUNTS[freq] or any(d not in range(7) for d in days):
            raise ValueError(f'Select {COUNTS[freq]} collection weekdays for this requested schedule.')
    if freq=='adhoc':
        return dict(cadence='adhoc',start_date=first.isoformat(),days=[])
    cadence='weekly' if freq in ('once','twice','three','daily','weekdays','custom') else 'monthly' if freq=='last_monthly' else freq
    if cadence not in (*INTERVALS,'monthly'):
        raise ValueError('Confirm a supported collection frequency in the client profile.')
    if cadence in INTERVALS and not days:
        days=[first.weekday()]
    rule=dict(cadence=cadence,start_date=first.isoformat(),days=days,
              monthly_rule='last_weekday' if freq=='last_monthly' else '')
    if not occurs(rule,first):
        raise ValueError('Choose a first date that matches the requested collection weekday or monthly rule.')
    return rule


def due(rule, day):
    return day.isoformat()==rule['start_date'] if rule['cadence']=='adhoc' else occurs(rule,day)


def next_dates(rule, count=6):
    first=date.fromisoformat(rule['start_date']);result=[]
    for offset in range(370):
        day=first+timedelta(days=offset)
        if due(rule,day):result.append(day.isoformat())
        if len(result)==count or rule['cadence']=='adhoc':break
    return result


def add_visits(conn, plan, state):
    from binflow_route_calendar import clients
    from binflow_schedule import visit_item
    from binflow_stop_exceptions import closeouts
    cycles=active(conn)
    if not cycles:return plan
    people={p['profile_id']:p for p in clients(conn) if p['state']==state and p['id'] and p['profile_id'] in cycles}
    seen=set(plan.get('cycle_seen',[]))
    plan['backlog']=[b for b in plan.get('backlog',[]) if not b.get('capacity_blocked')]
    locked=closeouts(conn,plan['days'][0]['date'],plan['days'][-1]['date'],state)
    # Saved plans can contain actual jobs added independently of these planning visits.
    booked={(r['site_id'],r['scheduled_date']) for r in conn.execute("""SELECT site_id,scheduled_date FROM bf_jobs
        WHERE state=? AND scheduled_date BETWEEN ? AND ? AND status!='cancelled' AND approval_status!='rejected'
          AND (schedule_live=1 OR schedule_week_id IS NOT NULL)""",(state,plan['days'][0]['date'],plan['days'][-1]['date']))}
    for day in plan['days']:
        day['items']=[i for i in day['items'] if not (i['id'].startswith('cycle-')
            and ((i.get('site_id'),day['date']) in booked or any(j['kind']=='job' and j.get('site_id')==i.get('site_id') for j in day['items'])))]
    for pid,p in people.items():
        cycle=cycles[pid];rule=json.loads(cycle['pattern_json'])
        for day in plan['days']:
            if not due(rule,date.fromisoformat(day['date'])):continue
            key=f"cycle-{pid}-v{cycle['version']}-{day['date']}"
            if key in seen:continue
            seen.add(key)
            if (state,day['date']) in locked or (p['id'],day['date']) in booked:continue
            if any(i.get('profile_id')==pid or i.get('site_id')==p['id'] for i in day['items']):continue
            candidate=visit_item(p,key)
            from binflow_calendar_limits import check
            try:
                check(conn,state,day['date'],day['items']+[candidate])
            except ValueError as exc:
                plan.setdefault('backlog',[]).append(dict(title=p['name'],profile_id=pid,date=day['date'],reason=str(exc),capacity_blocked=True))
                # Do not mark a blocked occurrence seen: it must be reconsidered when space is freed.
                seen.discard(key)
                continue
            day['items'].append(candidate)
            day.update(estimate_minutes=0,estimate_valid=False,loads=[])
    if seen or 'cycle_seen' in plan:
        plan['cycle_seen']=sorted(seen)
    return plan


def register(app,db,require,render,success,now):
    from binflow_route_calendar import clients, START
    from binflow_stop_exceptions import ensure_day_open

    @app.route('/binflow/collection-calendar/clients/<int:pid>/add',methods=['GET','POST'])
    @require('admin')
    def bf_calendar_add_client(pid):
        conn=db()
        if request.method=='POST':conn.execute('BEGIN IMMEDIATE')
        p=next((p for p in clients(conn) if p['profile_id']==pid),None)
        if not p:abort(404)
        raw=conn.execute('SELECT * FROM bf_client_site_profiles WHERE id=?',(pid,)).fetchone()
        saved=conn.execute('SELECT * FROM bf_calendar_cycles WHERE profile_id=?',(pid,)).fetchone()
        token=hashlib.sha256(json.dumps(dict(profile=dict(raw),cycle=dict(saved) if saved else None),sort_keys=True).encode()).hexdigest()
        first=request.values.get('date') or (saved['first_date'] if saved and saved['active'] else max(now()[:10],START.isoformat()))
        rule=None;error=None;status=200
        try:
            day=date.fromisoformat(first)
            if not START<=day<=date(2100,12,31):raise ValueError('Choose a date from 26 October 2026 to 31 December 2100.')
            if not p['id'] or not p['state']:raise ValueError('Add a state and linked client location in the profile first.')
            selected=[int(d) for d in request.values.getlist('days')]
            rule=pattern(raw,day,selected)
            ensure_day_open(conn,p['state'],first)
            from binflow_calendar_limits import ensure_collection_day
            ensure_collection_day(first)
            if request.method=='POST':
                if request.form.get('token')!=token:
                    status=409;raise ValueError('The client schedule changed. Review it again before adding.')
                if saved and saved['active']:
                    if saved['first_date']!=first or saved['pattern_json']!=json.dumps(rule):
                        raise ValueError('This client already has an active calendar cycle. Its current start date is '+saved['first_date']+'.')
                    return success('This client is already on the calendar. No duplicate visits were added.','bf_collection_calendar',state=p['state'],date=first,view='week')
                from binflow_calendar_limits import check
                from binflow_schedule import get_plan, monday, visit_item
                # Check the coming year and every already-saved future week. Later generated
                # occurrences use the same guard and appear in Needs arrangement if blocked.
                starts={monday((day+timedelta(days=n)).isoformat()) for n in range(367)}
                starts.update(w['week_start'] for w in conn.execute('SELECT week_start FROM bf_schedule_weeks WHERE state=? AND week_start>=?',(p['state'],monday(first))))
                for start in sorted(starts):
                    _,proposal=get_plan(conn,p['state'],start)
                    for target in proposal['days']:
                        if due(rule,date.fromisoformat(target['date'])):
                            ensure_collection_day(target['date'])
                            if not any(i.get('profile_id')==pid or i.get('site_id')==p['id'] for i in target['items']):
                                check(conn,p['state'],target['date'],target['items']+[visit_item(p,'proposed-cycle')])
                version=saved['version']+1 if saved else 1
                conn.execute('''INSERT INTO bf_calendar_cycles VALUES(?,?,?,?,?,1,?,?) ON CONFLICT(profile_id)
                    DO UPDATE SET first_date=excluded.first_date,requested_schedule=excluded.requested_schedule,
                    pattern_json=excluded.pattern_json,version=excluded.version,active=1,updated_at=excluded.updated_at,updated_by=excluded.updated_by''',
                    (pid,first,p['schedule'],json.dumps(rule),version,now(),g.user['id']))
                # Existing published weeks are explicit admin edits; persist their additions
                # and revision so the driver's saved schedule and calendar agree.
                from binflow_schedule import get_plan
                for week in conn.execute("SELECT * FROM bf_schedule_weeks WHERE state=? AND date(week_start,'+6 days')>=? ORDER BY week_start",(p['state'],first)).fetchall():
                    old=json.loads(week['plan_json'])
                    _,plan=get_plan(conn,p['state'],week['week_start'])
                    if old==plan:continue
                    before=dict(plan=old,state=p['state'],week=week['week_start'],driver_id=week['driver_id'],status=week['status'])
                    stamp=now();revision=week['revision']+1
                    conn.execute('''INSERT OR IGNORE INTO bf_schedule_history(week_id,revision,action,actor_id,created_at,snapshot)
                        VALUES(?,?,?,?,?,?)''',(week['id'],week['revision'],'Before client cycle added',g.user['id'],stamp,json.dumps(before)))
                    conn.execute('UPDATE bf_schedule_weeks SET plan_json=?,revision=?,updated_at=?,updated_by=? WHERE id=?',
                        (json.dumps(plan),revision,stamp,g.user['id'],week['id']))
                    conn.execute('''INSERT INTO bf_schedule_history(week_id,revision,action,actor_id,created_at,snapshot)
                        VALUES(?,?,?,?,?,?)''',(week['id'],revision,'Client cycle added · '+p['name'],g.user['id'],stamp,json.dumps(dict(before,plan=plan))))
                return success('Client added to the calendar. Future planning visits follow the requested schedule; allocate bins and approve weeks before dispatch.',
                               'bf_collection_calendar',state=p['state'],date=first,view='week')
        except (ValueError,TypeError) as exc:
            if request.method=='POST':conn.rollback();status=409 if status==409 else 400
            error=str(exc) or 'Choose a valid first date and collection days.'
        spec=stored(raw)
        return render('calendar_add_client',section='collection_calendar',client=p,first=first,error=error,
            token=token,scheduled=saved if saved and saved['active'] else None,dates=next_dates(rule) if rule else [],
            needs_days=COUNTS.get(spec.frequency) if spec.frequency in ('twice','three') and not spec.days else 0,
            weekdays=DAYS,selected_days=request.values.getlist('days'),can_save=bool(rule and not error)),status
