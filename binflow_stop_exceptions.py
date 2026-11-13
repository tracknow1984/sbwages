"""Missed stops, unused bin custody and collection calendar closeout."""
import json
import secrets

from flask import abort, g, request

from binflow_approvals import allocation_history
from binflow_permissions import is_office
from binflow_qr import reopen_requests, request_history


def migrate(conn):
    conn.execute("""CREATE TABLE IF NOT EXISTS bf_stop_exceptions (
        job_id INTEGER PRIMARY KEY REFERENCES bf_jobs(id), original_id INTEGER NOT NULL REFERENCES bf_jobs(id),
        reason TEXT NOT NULL, reported_at TEXT NOT NULL, reported_by TEXT NOT NULL,
        decision TEXT NOT NULL DEFAULT 'pending' CHECK(decision IN ('pending','approved','rescheduled')),
        reviewed_at TEXT, reviewed_by TEXT, replacement_id INTEGER REFERENCES bf_jobs(id))""")
    conn.execute("""CREATE TABLE IF NOT EXISTS bf_unused_returns (
        item_id INTEGER PRIMARY KEY REFERENCES bf_job_bins(id), returned_at TEXT, received_by TEXT)""")
    conn.execute("CREATE INDEX IF NOT EXISTS bf_stop_exception_queue ON bf_stop_exceptions(decision,reported_at)")
    # Older office cancellations were already approved. Keep their original reason and actor in the register.
    conn.execute("""INSERT OR IGNORE INTO bf_stop_exceptions
        (job_id,original_id,reason,reported_at,reported_by,decision,reviewed_at,reviewed_by)
        SELECT j.id,j.id,COALESCE(NULLIF(ev.notes,''),'Cancelled by operations'),COALESCE(ev.created_at,j.created_at),
            COALESCE(ev.actor,'Operations'),'approved',COALESCE(ev.created_at,j.created_at),COALESCE(ev.actor,'Operations')
        FROM bf_jobs j LEFT JOIN bf_events ev ON ev.id=(SELECT MIN(e.id) FROM bf_events e WHERE e.job_id=j.id
            AND e.event='Job cancelled; reservation released')
        WHERE j.status='cancelled' AND j.approval_status='approved'
          AND NOT EXISTS(SELECT 1 FROM bf_stop_exceptions ex WHERE ex.job_id=j.id)""")


def copy_stop(conn, source, day, actor_id, stamp, cancelled=False):
    jid = conn.execute("SELECT COALESCE(MAX(id),0)+1 FROM bf_jobs").fetchone()[0]
    conn.execute("""INSERT INTO bf_jobs(id,code,request_key,site_id,warehouse_id,scheduled_date,status,
        client_name,site_name,site_address,warehouse_name,state,vehicle,notes,created_at,created_by,
        truck_id,driver_id,scheduled_time,scan_mode,run_order,pod_client_id,schedule_live,approval_status,submitted_at)
        VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (jid,f'BF-{jid:06d}',secrets.token_urlsafe(24),source['site_id'],source['warehouse_id'],day,
         'cancelled' if cancelled else 'scheduled',source['client_name'],source['site_name'],source['site_address'],
         source['warehouse_name'],source['state'],source['vehicle'] if cancelled else '',source['notes'],stamp,actor_id,
         source['truck_id'] if cancelled else None,source['driver_id'] if cancelled else None,source['scheduled_time'],
         1,source['run_order'],source['pod_client_id'],int(cancelled),'approved' if cancelled else 'pending',stamp))
    return jid


def report_stop(conn, data, job_id, reason, user, who, stamp):
    from binflow_acceptance import manifest
    from binflow_journey import complete_scanned_client
    if not reason or len(reason)>500:
        raise ValueError('Give a reason for cancelling this stop (up to 500 characters).')
    job = data['current']
    if (not job or job['id']!=job_id or data['stage'] not in ('navigate','deliver','collect','handover')
            or not data['clock_ready'] or not data['acceptance_ok'] or not data['run']['departed_at']):
        raise ValueError('This stop has changed. Refresh your driver day.')
    rows = data['current_rows']
    missed = [i for i in rows if not (i['delivered_at'] if i['direction']=='deliver' else i['picked_up_at'])]
    if not missed:
        raise ValueError('All movements are recorded. Finish the client handover.')
    partial = len(missed)!=len(rows)
    exc_id = copy_stop(conn,job,job['scheduled_date'],user['id'],stamp,True) if partial else job_id
    conn.execute("""INSERT INTO bf_stop_exceptions(job_id,original_id,reason,reported_at,reported_by)
                    VALUES(?,?,?,?,?)""",(exc_id,job_id,reason,stamp,who))
    for item in missed:
        onboard = item['direction']=='deliver' and item['loaded_at']
        conn.execute('UPDATE bf_job_bins SET job_id=?,active=? WHERE id=?',(exc_id,int(bool(onboard)),item['id']))
        if onboard:
            conn.execute('INSERT INTO bf_unused_returns(item_id) VALUES(?)',(item['id'],))
        if partial and item['direction']=='collect':
            conn.execute("UPDATE bf_collection_requests SET job_id=? WHERE job_id=? AND bin_id=? AND status='scheduled'",
                         (exc_id,job_id,item['bin_id']))
    if partial:
        complete_scanned_client(conn,job,user,stamp)
        if conn.execute('SELECT status FROM bf_jobs WHERE id=?',(job_id,)).fetchone()[0]=='scheduled':
            # Older driver flows still record only the movements actually scanned.
            full = any(i['picked_up_at'] for i in rows)
            conn.execute("""UPDATE bf_jobs SET collected_at=?,collected_by=?,status=?,completed_at=?,
                confirmation='driver_scanned',confirmation_note='Partial client exchange confirmed by bin scans.'
                WHERE id=?""",(stamp,who,'collected' if full else 'complete',None if full else stamp,job_id))
            conn.execute("UPDATE bf_job_bins SET active=0 WHERE job_id=? AND direction='deliver'",(job_id,))
    else:
        conn.execute("UPDATE bf_jobs SET status='cancelled' WHERE id=?",(job_id,))
    allocation_history(conn,exc_id,'Driver cancellation awaiting review',who,user['id'],stamp,reason)
    if data['run']['accepted_at']:
        # Preserve the original signed manifest in acceptance history. The active route now omits the missed work.
        conn.execute('UPDATE bf_runs SET accepted_manifest=? WHERE truck_id=? AND run_date=?',
                     (manifest(conn,job['truck_id'],job['scheduled_date'])[2],job['truck_id'],job['scheduled_date']))


def unused_rows(conn, truck_id, day):
    return conn.execute("""SELECT i.*,j.client_name,j.warehouse_name,j.warehouse_id,j.site_name
        FROM bf_unused_returns u JOIN bf_job_bins i ON i.id=u.item_id JOIN bf_jobs j ON j.id=i.job_id
        WHERE j.truck_id=? AND j.scheduled_date=? AND u.returned_at IS NULL
        ORDER BY j.warehouse_id,j.run_order,i.bin_code""",(truck_id,day)).fetchall()


def receive_empty(conn, item_id, who, stamp, event):
    row=conn.execute("""SELECT u.returned_at,i.bin_id,i.job_id,i.active,j.truck_id,j.warehouse_id,b.truck_load,
        b.truck_id AS actual_truck,b.status FROM bf_unused_returns u JOIN bf_job_bins i ON i.id=u.item_id
        JOIN bf_jobs j ON j.id=i.job_id JOIN bf_bins b ON b.id=i.bin_id WHERE u.item_id=?""",(item_id,)).fetchone()
    if not row:
        raise ValueError('This bin is not an unused return.')
    if row['returned_at']:
        return False
    if not row['active'] or row['status']!='transit' or row['truck_load']!='empty' or row['actual_truck']!=row['truck_id']:
        raise ValueError('The bin is no longer on this truck as an unused empty. Check its record.')
    conn.execute("UPDATE bf_bins SET location_id=?,truck_id=NULL,truck_load=NULL,status='empty' WHERE id=?",
                 (row['warehouse_id'],row['bin_id']))
    conn.execute('UPDATE bf_job_bins SET active=0 WHERE id=?',(item_id,))
    conn.execute('UPDATE bf_unused_returns SET returned_at=?,received_by=? WHERE item_id=?',(stamp,who,item_id))
    # A replacement can be scheduled while the empty is still in transit. Transfer its
    # reservation only after this receipt, inside the same transaction as the custody change.
    conn.execute("""UPDATE bf_job_bins SET active=1 WHERE bin_id=? AND direction='deliver' AND active=0
        AND job_id=(SELECT j.id FROM bf_stop_exceptions e JOIN bf_jobs j ON j.id=e.replacement_id
            WHERE e.job_id=? AND j.status='scheduled' AND j.approval_status='pending')""",
        (row['bin_id'],row['job_id']))
    event(row['bin_id'],'Unused empty returned to warehouse',row['job_id'],target=row['warehouse_id'],from_truck=row['truck_id'])
    return True


def closeouts(conn, first, last, state='', test=False):
    # A training truck can neither lock nor reopen the real state calendar.
    rows=conn.execute("""SELECT j.state,j.scheduled_date,j.status,j.approval_status,j.site_id,
            j.truck_id,r.departed_at,r.returned_at,j.pod_signed_at,j.pod_deleted_at,e.job_id AS missed
        FROM bf_jobs j LEFT JOIN bf_runs r ON r.truck_id=j.truck_id AND r.run_date=j.scheduled_date
        LEFT JOIN bf_stop_exceptions e ON e.job_id=j.id
        WHERE j.scheduled_date BETWEEN ? AND ? AND (?='' OR j.state=?)
          AND EXISTS(SELECT 1 FROM bf_live_cycle t WHERE t.client_id=j.pod_client_id)=?
        ORDER BY j.scheduled_date,j.state""",(first,last,state,state,int(test)))
    result={}
    for row in rows:
        key=(row['state'],row['scheduled_date'])
        day=result.setdefault(key,dict(locked=True,worked=False,pods=0,sites=set()))
        day['sites'].add(row['site_id'])
        terminal = row['status'] in ('received','complete','cancelled')
        if row['departed_at'] and not row['returned_at']:
            terminal=False
        day['locked'] &= terminal
        day['worked'] |= bool(row['returned_at'])
        day['pods'] += int(bool(row['pod_signed_at'] and not row['pod_deleted_at']))
    if not test:
        # A published visit without a corresponding docket still needs to be dealt with.
        for week in conn.execute("""SELECT state,plan_json FROM bf_schedule_weeks WHERE status='published'
                AND week_start<=? AND date(week_start,'+6 days')>=? AND (?='' OR state=?)""",(last,first,state,state)):
            for planned in json.loads(week['plan_json']).get('days',[]):
                day=result.get((week['state'],planned['date']))
                if day and any(i['kind']=='visit' and i.get('site_id') not in day['sites'] for i in planned['items']):
                    day['locked']=False
    return {key:dict(pods=d['pods']) for key,d in result.items() if d['locked'] and d['worked']}


def ensure_day_open(conn,state,day,test=False):
    if (state,day) in closeouts(conn,day,day,state,test):
        raise ValueError(f'{state} {day} is completed and locked. Choose another date.')


def exception_rows(conn,state=''):
    return conn.execute("""SELECT e.*,j.code,j.site_name,j.state,j.scheduled_date,
        n.code AS new_code,n.scheduled_date AS new_date,
        (SELECT COUNT(*) FROM bf_unused_returns u JOIN bf_job_bins i ON i.id=u.item_id
         WHERE i.job_id=j.id AND u.returned_at IS NULL) AS empties_due
        FROM bf_stop_exceptions e JOIN bf_jobs j ON j.id=e.job_id LEFT JOIN bf_jobs n ON n.id=e.replacement_id
        WHERE (?='' OR j.state=?) ORDER BY e.decision!='pending',e.reported_at DESC,j.id""",(state,state)).fetchall()


def rebook(conn, source, new_date, uid, who, stamp):
    from binflow_runs import valid_date
    from binflow_requirements import site_plan
    from binflow_schedule import saved_week,monday,job_item
    new_date=valid_date(new_date)
    if new_date<=source['scheduled_date'] or new_date<stamp[:10]:
        raise ValueError('Choose a later service date, today or in the future.')
    test=bool(conn.execute('SELECT 1 FROM bf_live_cycle WHERE client_id=?',(source['pod_client_id'],)).fetchone())
    ensure_day_open(conn,source['state'],new_date,test)
    planned=conn.execute("""SELECT i.*,b.size_id FROM bf_job_bins i JOIN bf_bins b ON b.id=i.bin_id
        WHERE i.job_id=? ORDER BY i.id""",(source['id'],)).fetchall()
    jid=copy_stop(conn,source,new_date,uid,stamp)
    quantity={'collect':{},'deliver':{}}
    for item in planned:
        reserve_now=True
        if item['direction']=='collect':
            b=conn.execute("""SELECT * FROM bf_bins b WHERE id=? AND location_id=? AND status IN ('at_client','full')
                AND archived=0 AND NOT EXISTS(SELECT 1 FROM bf_job_bins i WHERE i.bin_id=b.id AND i.active=1)""",
                (item['bin_id'],source['site_id'])).fetchone()
        else:
            b=conn.execute("""SELECT b.* FROM bf_bins b WHERE location_id=? AND size_id=? AND status='empty'
                AND archived=0 AND service_status='in_service'
                AND NOT EXISTS(SELECT 1 FROM bf_job_bins i WHERE i.bin_id=b.id AND i.active=1)
                AND (?=0 OR EXISTS(SELECT 1 FROM bf_job_bins i JOIN bf_jobs j ON j.id=i.job_id
                    WHERE i.bin_id=b.id AND j.pod_client_id=?))
                ORDER BY b.id!=?,b.id LIMIT 1""",(source['warehouse_id'],item['size_id'],int(test),
                    source['pod_client_id'],item['bin_id'])).fetchone()
            if not b:
                b=conn.execute("""SELECT b.* FROM bf_unused_returns u JOIN bf_job_bins i ON i.id=u.item_id
                    JOIN bf_bins b ON b.id=i.bin_id JOIN bf_jobs j ON j.id=i.job_id
                    WHERE i.id=? AND i.active=1 AND u.returned_at IS NULL AND b.truck_load='empty'
                        AND b.truck_id=j.truck_id AND b.archived=0 AND b.service_status='in_service'""",
                    (item['id'],)).fetchone()
                reserve_now=False
        if not b:
            raise ValueError(f"No available {item['size_name']} bin for {item['direction']} at this stop. Check the bin location or receive the unused empties, then retry.")
        counts=quantity[item['direction']]
        counts[b['size_id']]=counts.get(b['size_id'],0)+1
        conn.execute("""INSERT INTO bf_job_bins(job_id,bin_id,direction,bin_code,size_name,tare_g,active)
            VALUES(?,?,?,?,?,?,?)""",(jid,b['id'],item['direction'],b['code'],item['size_name'],b['tare_g'],int(reserve_now)))
    _,loading=site_plan(conn,source['site_id'],source['warehouse_id'],quantity['collect'],new_date)
    for row in loading:
        row['selected']=quantity['deliver'].get(row['size_id'],0)
        row['shortfall']=max(0,row['recommended']-row['selected'])
    conn.execute('UPDATE bf_jobs SET bin_plan=? WHERE id=?',(json.dumps(loading),jid))
    # The replacement is a draft allocation. Existing published stops retain their own approvals.
    for req in conn.execute("SELECT id FROM bf_collection_requests WHERE job_id=? AND status='scheduled'",(source['id'],)).fetchall():
        conn.execute("UPDATE bf_collection_requests SET job_id=?,review_state='pending',updated_at=? WHERE id=?",(jid,stamp,req['id']))
        request_history(conn,req['id'],'Rescheduled after missed stop',who,stamp)
    allocation_history(conn,jid,'Replacement allocation awaiting approval',who,uid,stamp,'Original docket '+source['code'])
    week=saved_week(conn,source['state'],monday(new_date))
    if week and not test:
        plan=json.loads(week['plan_json'])
        day=next(d for d in plan['days'] if d['date']==new_date)
        day['items']=[i for i in day['items'] if not(i['kind']=='visit' and i.get('site_id')==source['site_id'])]
        day['items'].append(job_item(conn.execute('SELECT * FROM bf_jobs WHERE id=?',(jid,)).fetchone()))
        day['estimate_valid']=False
        conn.execute('UPDATE bf_jobs SET schedule_week_id=? WHERE id=?',(week['id'],jid))
        rev=week['revision']+1
        conn.execute('UPDATE bf_schedule_weeks SET plan_json=?,revision=?,updated_at=?,updated_by=? WHERE id=?',
                     (json.dumps(plan),rev,stamp,uid,week['id']))
        snapshot=dict(plan=plan,state=source['state'],week=week['week_start'],driver_id=week['driver_id'],status=week['status'])
        conn.execute('INSERT INTO bf_schedule_history(week_id,revision,action,actor_id,created_at,snapshot) VALUES(?,?,?,?,?,?)',
                     (week['id'],rev,'Replacement stop added for approval',uid,stamp,json.dumps(snapshot)))
    return jid


def register(app,db,access,render,now,actor,success,failure,event):
    from binflow_pods import warehouse_scope
    from binflow_runs import mark_returned
    from urllib.parse import urlparse

    @app.get('/binflow/cancelled-jobs')
    @access
    def bf_cancelled_jobs():
        if not is_office(g.user): abort(403)
        state=request.args.get('state','')
        return render('cancelled_jobs',section='collection_calendar',rows=exception_rows(db(),state),selected=state,today=now()[:10])

    @app.post('/binflow/cancelled-jobs/<int:job_id>')
    @access
    def bf_decide_cancel(job_id):
        if not is_office(g.user): abort(403)
        conn=db()
        try:
            conn.execute('BEGIN IMMEDIATE')
            source=conn.execute('SELECT * FROM bf_jobs WHERE id=?',(job_id,)).fetchone()
            exc=conn.execute('SELECT * FROM bf_stop_exceptions WHERE job_id=?',(job_id,)).fetchone()
            if not source or not exc: abort(404)
            if exc['decision']!='pending': raise ValueError('Operations has already reviewed this cancellation.')
            action=request.form.get('decision')
            stamp=now()
            if action=='rescheduled':
                replacement=rebook(conn,source,request.form.get('date'),g.user['id'],actor(),stamp)
            elif action=='approved':
                replacement=None
                reopen_requests(conn,job_id,actor(),stamp)
            else: raise ValueError('Choose reschedule or approve cancellation.')
            conn.execute('UPDATE bf_stop_exceptions SET decision=?,reviewed_at=?,reviewed_by=?,replacement_id=? WHERE job_id=?',
                         (action,stamp,actor(),replacement,job_id))
            allocation_history(conn,job_id,'Cancellation '+action,actor(),g.user['id'],stamp)
            return success('Replacement booking created for scheduling.' if replacement else 'Cancellation approved.',
                           'bf_cancelled_jobs',state=source['state'])
        except ValueError as error:
            return failure(error,'bf_cancelled_jobs')

    @app.post('/binflow/warehouse/unused-empties')
    @access
    def bf_receive_unused():
        conn=db()
        code=request.form.get('code','').strip()[:1000]
        if code.startswith(('https://','http://')): code=urlparse(code).path.rstrip('/').split('/')[-1]
        try:
            conn.execute('BEGIN IMMEDIATE')
            scope,args=warehouse_scope(g.user)
            row=conn.execute(f"""SELECT i.id,j.truck_id,j.scheduled_date,j.state FROM bf_unused_returns u
                JOIN bf_job_bins i ON i.id=u.item_id JOIN bf_jobs j ON j.id=i.job_id JOIN bf_bins b ON b.id=i.bin_id
                JOIN bf_locations w ON w.id=j.warehouse_id JOIN bf_runs r ON r.truck_id=j.truck_id AND r.run_date=j.scheduled_date
                WHERE {scope} AND (b.code=? COLLATE NOCASE OR b.token=?) AND u.returned_at IS NULL AND
                (EXISTS(SELECT 1 FROM bf_run_depot_arrivals a WHERE a.truck_id=j.truck_id AND a.run_date=j.scheduled_date AND a.warehouse_id=w.id)
                 OR (r.departed_at IS NOT NULL AND NOT EXISTS(SELECT 1 FROM bf_jobs s WHERE s.truck_id=j.truck_id AND
                    s.scheduled_date=j.scheduled_date AND s.schedule_live=1 AND s.approval_status='approved' AND s.status='scheduled')))""",
                (*args,code,code)).fetchone()
            if not row: raise ValueError('Scan an outstanding unused empty for a returning truck at your depot.')
            receive_empty(conn,row['id'],actor(),now(),event)
            mark_returned(conn,row['truck_id'],row['scheduled_date'],now())
            return success('Unused empty received into available stock.','bf_warehouse',state=row['state'])
        except ValueError as error:
            return failure(error,'bf_warehouse')
