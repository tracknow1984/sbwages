"""Explicit all-state planning reset; operational records remain intact."""
import hashlib
import json
import uuid

from flask import g, request

from binflow_schedule import STATES, ensure_job_editable
from binflow_route_calendar import START
from binflow_stop_exceptions import closeouts


def preview(conn):
    weeks = [dict(r) for r in conn.execute('SELECT * FROM bf_schedule_weeks ORDER BY state,week_start')]
    starts = [dict(r) for r in conn.execute('SELECT * FROM bf_rollout_states ORDER BY state')]
    jobs = [dict(r) for r in conn.execute("SELECT id,state,scheduled_date,status,truck_id FROM bf_jobs WHERE status!='cancelled' ORDER BY id")]
    locked = set(closeouts(conn, '2000-01-01', '2100-12-31', ''))
    for job in jobs:
        try:
            ensure_job_editable(conn, job)
        except ValueError:
            locked.add((job['state'], job['scheduled_date']))
    counts = {s: dict(state=s, items=0, weeks=0, jobs=sum(j['state']==s for j in jobs)) for s in STATES}
    changes = []
    for row in weeks:
        plan = json.loads(row['plan_json'])
        changed = False
        for day in plan['days']:
            if (row['state'], day['date']) in locked:
                continue
            retained = [i for i in day['items'] if i['kind']=='job']
            removed = len(day['items'])-len(retained)
            if removed or (not retained and day.get('notes')):
                counts[row['state']]['items'] += removed
                day.update(items=retained, estimate_minutes=0, estimate_valid=False, loads=[])
                if not retained:
                    day['notes']=''
                changed = True
        backlog = [b for b in plan.get('backlog',[]) if (row['state'],b.get('date')) in locked]
        if backlog != plan.get('backlog',[]):
            plan['backlog']=backlog
            changed=True
        if changed:
            changes.append((row,plan))
            counts[row['state']]['weeks']+=1
    signature=dict(weeks=weeks,starts=starts,jobs=jobs,locked=sorted(locked))
    token=hashlib.sha256(json.dumps(signature,sort_keys=True).encode()).hexdigest()
    return dict(token=token, states=list(counts.values()), items=sum(c['items'] for c in counts.values()),
                weeks=len(changes), jobs=len(jobs), protected=len(locked), changes=changes, previous_starts=starts)


def apply_reset(conn, summary, stamp, uid, actor):
    archive=dict(manual_states=summary['previous_starts'], weeks=[r for r,_ in summary['changes']])
    for row,plan in summary['changes']:
        before=dict(plan=json.loads(row['plan_json']),state=row['state'],week=row['week_start'],
                    driver_id=row['driver_id'],status=row['status'])
        conn.execute('''INSERT OR IGNORE INTO bf_schedule_history
            (week_id,revision,action,actor_id,created_at,snapshot) VALUES(?,?,?,?,?,?)''',
            (row['id'],row['revision'],'Before all-state planning reset',uid,stamp,json.dumps(before)))
        revision=row['revision']+1
        conn.execute('UPDATE bf_schedule_weeks SET plan_json=?,revision=?,updated_at=?,updated_by=? WHERE id=?',
                     (json.dumps(plan),revision,stamp,uid,row['id']))
        after=dict(before,plan=plan)
        conn.execute('''INSERT INTO bf_schedule_history
            (week_id,revision,action,actor_id,created_at,snapshot) VALUES(?,?,?,?,?,?)''',
            (row['id'],revision,'Cleared planning calendar · all states',uid,stamp,json.dumps(after)))
    for state in STATES:
        conn.execute('''INSERT INTO bf_rollout_states(state,first_week,changed_by) VALUES(?,?,?)
            ON CONFLICT(state) DO UPDATE SET first_week=MIN(first_week,excluded.first_week),changed_by=excluded.changed_by''',
            (state,START.isoformat(),uid))
    audit=dict(actor=actor,user_id=uid,items=summary['items'],weeks=summary['weeks'],jobs_retained=summary['jobs'])
    conn.execute('INSERT INTO bf_calendar_rebuilds VALUES(?,?,?,?,?)',
        ('manual-planning-reset-'+uuid.uuid4().hex,START.isoformat(),json.dumps(archive),json.dumps(audit),stamp))


def register(app,db,require,render,success,now,actor):
    @app.route('/binflow/collection-calendar/clear-planning',methods=['GET','POST'])
    @require('admin')
    def bf_clear_planning_calendar():
        conn=db();error=None;status=200
        if request.method=='POST':
            conn.execute('BEGIN IMMEDIATE')
        summary=preview(conn)
        if request.method=='POST':
            if request.form.get('confirm')!='clear-planning':
                error='Confirm that you want to clear planning across all states.';status=400
            elif request.form.get('token')!=summary['token']:
                error='The calendar changed after this preview. Review the updated counts and confirm again.';status=409
            if error:
                conn.rollback()
            else:
                try:
                    apply_reset(conn,summary,now(),g.user['id'],actor())
                    return success('Planning cleared across all states. Automatic suggestions are off; add work manually. Existing jobs and completed history were retained.',
                                   'bf_collection_calendar',view='week')
                except Exception:
                    conn.rollback()
                    raise
        return render('calendar_reset',section='collection_calendar',summary=summary,error=error),status
