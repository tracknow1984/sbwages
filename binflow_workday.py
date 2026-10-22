"""Driver clock records, phone timestamps and daily timesheet commits."""
import sqlite3
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from binflow_permissions import selected_state, state_scope, require_state

from flask import abort, g, redirect, request, url_for
from lunch import deduct_lunch
from entry_recording import stamp_entry
from work_weeks import profile_end, profile_start, saved_period

STATES=('QLD','NSW','VIC','SA','WA','TAS','ACT','NT')


def migrate_workdays(conn):
    conn.executescript('''
    CREATE TABLE IF NOT EXISTS bf_workdays (
        id INTEGER PRIMARY KEY,user_id INTEGER NOT NULL REFERENCES users(id),work_date TEXT NOT NULL,
        timezone TEXT NOT NULL,state TEXT NOT NULL DEFAULT '',truck_id INTEGER REFERENCES bf_trucks(id),
        started_at TEXT NOT NULL,start_received_at TEXT NOT NULL,phone_started_at TEXT NOT NULL DEFAULT '',
        finished_at TEXT,finish_received_at TEXT,phone_finished_at TEXT NOT NULL DEFAULT '',
        notes TEXT NOT NULL DEFAULT '',UNIQUE(user_id,work_date));
    CREATE UNIQUE INDEX IF NOT EXISTS bf_one_open_workday ON bf_workdays(user_id) WHERE finished_at IS NULL;
    CREATE UNIQUE INDEX IF NOT EXISTS bf_one_workday_truck ON bf_workdays(truck_id) WHERE finished_at IS NULL AND truck_id IS NOT NULL;
    CREATE TABLE IF NOT EXISTS bf_workday_entries (
        workday_id INTEGER NOT NULL REFERENCES bf_workdays(id),work_date TEXT NOT NULL,
        sheet_id INTEGER REFERENCES sheets(id) ON DELETE SET NULL,start_time TEXT NOT NULL,
        PRIMARY KEY(workday_id,work_date));
    CREATE TABLE IF NOT EXISTS bf_workday_events (
        id INTEGER PRIMARY KEY,workday_id INTEGER NOT NULL REFERENCES bf_workdays(id),
        actor_id INTEGER NOT NULL REFERENCES users(id),action TEXT NOT NULL,notes TEXT NOT NULL DEFAULT '',created_at TEXT NOT NULL);
    ''')
    columns={r['name'] for r in conn.execute('PRAGMA table_info(bf_runs)')}
    if 'clock_required' not in columns:
        conn.execute('ALTER TABLE bf_runs ADD COLUMN clock_required INTEGER NOT NULL DEFAULT 1')
        # Existing physical runs can finish without invented clock or payroll records.
        conn.execute('''UPDATE bf_runs SET clock_required=0 WHERE departed_at IS NOT NULL OR returned_at IS NOT NULL
            OR EXISTS(SELECT 1 FROM bf_jobs j JOIN bf_job_bins i ON i.job_id=j.id
                WHERE j.truck_id=bf_runs.truck_id AND j.scheduled_date=bf_runs.run_date
                AND (i.loaded_at IS NOT NULL OR i.picked_up_at IS NOT NULL OR i.returned_at IS NOT NULL))''')


def active_workday(conn,uid):
    return conn.execute('SELECT * FROM bf_workdays WHERE user_id=? AND finished_at IS NULL',(uid,)).fetchone()


def run_workday(conn,uid,truck_id,run_date):
    return conn.execute('SELECT * FROM bf_workdays WHERE user_id=? AND truck_id=? AND work_date=?',
                        (uid,truck_id,run_date)).fetchone()


def driver_today(conn,user,stamp):
    day=active_workday(conn,user['id'])
    return datetime.fromisoformat(stamp).astimezone(ZoneInfo(day['timezone'])).date().isoformat() if day else stamp[:10]


def clock_stamp(form,received,zone=None):
    """Keep the phone's event timestamp and a server receipt; reject clock skew for payroll."""
    received=datetime.fromisoformat(received)
    raw=form.get('device_at','').strip()[:80]
    requested_zone=zone or form.get('device_zone','Australia/Brisbane').strip()[:80]
    try:
        if not requested_zone.startswith('Australia/'): raise ValueError()
        local_zone=ZoneInfo(requested_zone)
    except (ValueError,ZoneInfoNotFoundError):
        local_zone=ZoneInfo('Australia/Brisbane')
    stamp=received;source='Server timestamp'
    if raw:
        try:
            phone=datetime.fromisoformat(raw.replace('Z','+00:00'))
            if phone.tzinfo is None: raise ValueError()
            if abs((phone-received).total_seconds())<=300:
                stamp=phone;source='Phone timestamp'
            else: source='Phone clock differed by over 5 minutes; server timestamp used'
        except ValueError:
            source='Phone timestamp invalid; server timestamp used'
    return stamp.astimezone(local_zone).replace(microsecond=0),raw,local_zone.key,source


def workday_event(conn,wid,uid,action,stamp,notes=''):
    conn.execute('INSERT INTO bf_workday_events(workday_id,actor_id,action,notes,created_at) VALUES(?,?,?,?,?)',
                 (wid,uid,action,notes,stamp))


def active_entry(conn,uid,day):
    return conn.execute('''SELECT d.id FROM bf_workday_entries e JOIN bf_workdays d ON d.id=e.workday_id
        WHERE d.user_id=? AND e.work_date=? AND d.finished_at IS NULL''',(uid,day)).fetchone()


def sheet_for_day(conn,user,day):
    old=conn.execute('SELECT * FROM entries WHERE user_id=? AND work_date=?',(user['id'],day.isoformat())).fetchone()
    ending=profile_end(day,user['work_week'])
    sheet=conn.execute('SELECT * FROM sheets WHERE id=?',(old['sheet_id'],)).fetchone() if old else conn.execute(
        'SELECT * FROM sheets WHERE user_id=? AND week_end=?',(user['id'],ending.isoformat())).fetchone()
    if sheet:
        start,end,_=saved_period(sheet)
        if not start<=day<=end: raise ValueError('This work date falls outside its saved timesheet. Ask the office to review the work-week profile.')
        if sheet['status']!='draft' or sheet['archived_at'] or conn.execute('SELECT 1 FROM payments WHERE sheet_id=?',(sheet['id'],)).fetchone():
            raise ValueError('This timesheet is locked or paid. Ask the office to review it before recording this driver day.')
        return sheet['id'],old
    start=profile_start(ending,user['work_week'])
    if not start<=day<=ending:
        raise ValueError('Your work-week profile excludes this date. Ask the office to update your profile before starting.')
    sid=conn.execute('INSERT INTO sheets(user_id,week_end,week_start,work_week) VALUES(?,?,?,?)',
                     (user['id'],ending.isoformat(),start.isoformat(),user['work_week'])).lastrowid
    return sid,old


def start_entry(conn,user,day,stamp):
    sid,old=sheet_for_day(conn,user,date.fromisoformat(day['work_date']))
    if old and (old['start_time'] or old['finish_time'] or old['units'] or old['lunch_units'] or old['committed_at']):
        raise ValueError('This date already has timesheet hours or a start time. Ask the office to reconcile it before starting; existing hours have been kept.')
    activity=(old['activity'] if old else '')
    conn.execute('''INSERT INTO entries(user_id,work_date,sheet_id,units,activity,start_time,lunch_units)
        VALUES(?,?,?,0,?,?,0) ON CONFLICT(user_id,work_date) DO UPDATE SET start_time=excluded.start_time''',
        (user['id'],day['work_date'],sid,activity,stamp.strftime('%H:%M')))
    stamp_entry(conn, user['id'], day['work_date'], old, user['id'])
    conn.execute('INSERT INTO bf_workday_entries(workday_id,work_date,sheet_id,start_time) VALUES(?,?,?,?)',
                 (day['id'],day['work_date'],sid,stamp.strftime('%H:%M')))


def finish_entries(conn,user,day,finish,summary,actor_id=None):
    zone=ZoneInfo(day['timezone'])
    start=datetime.fromisoformat(day['started_at']).astimezone(zone).replace(second=0,microsecond=0)
    end=finish.astimezone(zone).replace(second=0,microsecond=0)
    if finish.astimezone(timezone.utc)<datetime.fromisoformat(day['started_at']).astimezone(timezone.utc):
        raise ValueError('Finish time is before the recorded start. Check your phone clock and try again.')
    if (finish.astimezone(timezone.utc)-start.astimezone(timezone.utc)).total_seconds()>48*3600:
        raise ValueError('This driver day has been open for over 48 hours. Ask the office to reconcile the clock record.')
    cursor=start
    while cursor.date()<=end.date():
        midnight=datetime.combine(cursor.date()+timedelta(days=1),time.min,zone)
        boundary=min(midnight,end)
        # An exact midnight finish belongs to the previous day's segment only.
        if cursor==end and cursor!=start: break
        key=cursor.date().isoformat();sid,old=sheet_for_day(conn,user,cursor.date())
        linked=conn.execute('SELECT * FROM bf_workday_entries WHERE workday_id=? AND work_date=?',(day['id'],key)).fetchone()
        if linked:
            if not old or old['sheet_id']!=linked['sheet_id'] or old['start_time']!=linked['start_time'] or old['finish_time'] or old['units'] or old['lunch_units'] or old['committed_at']:
                raise ValueError('The timesheet changed while this day was open. Ask the office to reconcile it; existing hours have been kept.')
        elif old and (old['start_time'] or old['finish_time'] or old['units'] or old['lunch_units'] or old['committed_at']):
            raise ValueError('The overnight shift overlaps existing hours. Ask the office to reconcile the timesheet.')
        minutes=Decimal(str((boundary.astimezone(timezone.utc)-cursor.astimezone(timezone.utc)).total_seconds()))/60
        gross=int((minutes*100/60).quantize(Decimal('1'),rounding=ROUND_HALF_UP))
        if gross>2400: raise ValueError('This date exceeds the timesheet limit. Ask the office to review the clock record.')
        units,lunch=deduct_lunch(gross)
        activity='\n'.join(v for v in ((old['activity'] if old else ''),summary) if v)
        if len(activity)>2000: raise ValueError('There are too many activity notes to finalise this day. Shorten the finish notes.')
        finish_time='24:00' if boundary==midnight else boundary.strftime('%H:%M')
        conn.execute('''INSERT INTO entries(user_id,work_date,sheet_id,units,activity,start_time,finish_time,committed_at,lunch_units)
            VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(user_id,work_date) DO UPDATE SET units=excluded.units,activity=excluded.activity,
            start_time=excluded.start_time,finish_time=excluded.finish_time,committed_at=excluded.committed_at,lunch_units=excluded.lunch_units''',
            (user['id'],key,sid,units,activity,cursor.strftime('%H:%M'),finish_time,finish.isoformat(),lunch))
        stamp_entry(conn, user['id'], key, old, actor_id if actor_id is not None else user['id'])
        if not linked: conn.execute('INSERT INTO bf_workday_entries(workday_id,work_date,sheet_id,start_time) VALUES(?,?,?,?)',
                                    (day['id'],key,sid,cursor.strftime('%H:%M')))
        if boundary==end: break
        cursor=midnight


def finish_blockers(conn,day):
    if not day['truck_id']: return []
    reasons=[]
    if conn.execute('SELECT 1 FROM bf_bins WHERE truck_id=?',(day['truck_id'],)).fetchone():
        reasons.append('Scan every bin off the truck before finishing your day.')
    jobs=conn.execute("SELECT * FROM bf_jobs WHERE truck_id=? AND scheduled_date=? AND approval_status='approved' AND status!='cancelled'",
                      (day['truck_id'],day['work_date'])).fetchall()
    if any(j['status'] in ('scheduled','collected') for j in jobs):
        reasons.append('Complete every client stop and warehouse return first. Ask the office to resolve any cancelled work.')
    run=conn.execute('SELECT returned_at FROM bf_runs WHERE truck_id=? AND run_date=?',(day['truck_id'],day['work_date'])).fetchone()
    if jobs and (not run or not run['returned_at']): reasons.append('Confirm that you are back at the warehouse.')
    return reasons


def register_workdays(app,db,access,require,render,success,failure,now):
    app.jinja_env.filters['bfclock']=lambda value: datetime.fromisoformat(value).strftime('%d.%m.%Y · %H:%M:%S') if value else '—'
    def owned(wid):
        row=db().execute('SELECT * FROM bf_workdays WHERE id=? AND user_id=?',(wid,g.user['id'])).fetchone()
        if not row: abort(404)
        if row['state']: require_state(g.user,row['state'])
        return row

    @app.post('/binflow/driver/my-day/<int:day_id>/reconcile')
    @require('admin')
    def bf_reconcile_workday(day_id):
        day=db().execute('SELECT * FROM bf_workdays WHERE id=?',(day_id,)).fetchone()
        if not day: abort(404)
        link=db().execute('SELECT sheet_id FROM bf_workday_entries WHERE workday_id=? ORDER BY work_date LIMIT 1',(day_id,)).fetchone()
        if not link or not link['sheet_id']: abort(404)
        try:
            db().execute('BEGIN IMMEDIATE')
            day=db().execute('SELECT * FROM bf_workdays WHERE id=?',(day_id,)).fetchone()
            if day['finished_at']: return success('Driver day already finalised.','view_sheet',sheet_id=link['sheet_id'])
            blockers=finish_blockers(db(),day)
            if blockers: raise ValueError(' '.join(blockers))
            reason=request.form.get('reason','').strip()
            if not reason or len(reason)>1000: raise ValueError('Record the reason for this clock correction (up to 1,000 characters).')
            try:
                finish=datetime.fromisoformat(request.form.get('finished_at',''))
                if finish.tzinfo is not None: raise ValueError()
                finish=finish.replace(tzinfo=ZoneInfo(day['timezone']))
            except ValueError: raise ValueError('Choose the actual finish date and time in the recorded driver timezone.') from None
            if finish>datetime.fromisoformat(now()): raise ValueError('A corrected finish time cannot be in the future.')
            user=db().execute('SELECT * FROM users WHERE id=?',(day['user_id'],)).fetchone()
            summary='BINFLOW · '+day['timezone']+' · office finish correction: '+reason
            if user['role']=='employee': finish_entries(db(),user,day,finish,summary,actor_id=g.user['id'])
            db().execute('UPDATE bf_workdays SET finished_at=?,finish_received_at=?,notes=? WHERE id=?',
                         (finish.isoformat(),now(),summary,day_id))
            workday_event(db(),day_id,g.user['id'],'Finish time reconciled by office',now(),reason)
            return success('Driver day finalised with the corrected finish time.','view_sheet',sheet_id=link['sheet_id'])
        except ValueError as error:
            return failure(error,'edit_day',sheet_id=link['sheet_id'],work_date=day['work_date'])

    @app.get('/binflow/driver/my-day')
    @access
    def bf_workday():
        requested=request.args.get('day',type=int)
        day=owned(requested) if requested else active_workday(db(),g.user['id'])
        if not day:
            latest=db().execute('SELECT * FROM bf_workdays WHERE user_id=? ORDER BY id DESC LIMIT 1',(g.user['id'],)).fetchone()
            if latest and latest['work_date']==datetime.fromisoformat(now()).astimezone(ZoneInfo(latest['timezone'])).date().isoformat(): day=latest
        if day and day['state']: require_state(g.user,day['state'])
        if day and day['truck_id'] and not day['finished_at'] and request.args.get('summary')!='1':
            return redirect(url_for('bf_run',truck_id=day['truck_id'],date=day['work_date']))
        state=selected_state(g.user,request.args.get('state',day['state'] if day else ''))
        if state not in ('',)+STATES: abort(400)
        trucks=[]
        if day and not day['finished_at'] and state:
            from binflow_runs import run_jobs
            for truck in db().execute('SELECT * FROM bf_trucks WHERE state=? ORDER BY name',(state,)):
                other=db().execute('SELECT 1 FROM bf_workdays WHERE truck_id=? AND finished_at IS NULL AND user_id!=?',(truck['id'],g.user['id'])).fetchone()
                run=db().execute('SELECT driver_id FROM bf_runs WHERE truck_id=? AND run_date=?',(truck['id'],day['work_date'])).fetchone()
                assigned={j['driver_id'] for j in run_jobs(db(),truck['id'],day['work_date']) if j['driver_id']}
                if other or (run and run['driver_id'] and run['driver_id']!=g.user['id']) or (assigned and assigned!={g.user['id']}): continue
                trucks.append(dict(truck))
        entries=db().execute('''SELECT e.*,s.week_end FROM bf_workday_entries x JOIN entries e ON e.sheet_id=x.sheet_id AND e.work_date=x.work_date
            JOIN sheets s ON s.id=e.sheet_id WHERE x.workday_id=? ORDER BY e.work_date''',(day['id'],)).fetchall() if day else []
        can_change_vehicle=bool(day and not day['finished_at'])
        if can_change_vehicle and day['truck_id']:
            from binflow_acceptance import ensure_run_unstarted
            try: ensure_run_unstarted(db(),day['truck_id'],day['work_date'])
            except ValueError: can_change_vehicle=False
            if db().execute('SELECT 1 FROM bf_runs WHERE truck_id=? AND run_date=? AND accepted_at IS NOT NULL',
                            (day['truck_id'],day['work_date'])).fetchone(): can_change_vehicle=False
        return render('workday',section='driver',driver_mode=True,workday=day,selected_state=state,trucks=trucks,
                      blockers=finish_blockers(db(),day) if day and not day['finished_at'] else [],clock_entries=entries,
                      can_change_vehicle=can_change_vehicle)

    @app.post('/binflow/driver/start-day')
    @access
    def bf_start_workday():
        try:
            db().execute('BEGIN IMMEDIATE')
            if active_workday(db(),g.user['id']): return success('Your day is already started.','bf_workday')
            received=now();stamp,phone,zone,source=clock_stamp(request.form,received)
            key=stamp.date().isoformat()
            if db().execute('SELECT 1 FROM bf_workdays WHERE user_id=? AND work_date=?',(g.user['id'],key)).fetchone():
                return success('This driver day has already been finalised.','bf_workday')
            wid=db().execute('''INSERT INTO bf_workdays(user_id,work_date,timezone,started_at,start_received_at,phone_started_at,state)
                VALUES(?,?,?,?,?,?,?)''',(g.user['id'],key,zone,stamp.isoformat(),received,phone,state_scope(g.user) or '')).lastrowid
            day=owned(wid)
            if g.user['role']=='employee': start_entry(db(),g.user,day,stamp)
            workday_event(db(),wid,g.user['id'],'Day started',received,source)
            return success('Day started. Choose your state and vehicle.','bf_workday')
        except (ValueError,sqlite3.IntegrityError) as error:
            return failure(error if isinstance(error,ValueError) else ValueError('Your driver day changed. Refresh and continue.'),'bf_workday')

    @app.post('/binflow/driver/my-day/<int:day_id>/vehicle')
    @access
    def bf_workday_vehicle(day_id):
        try:
            from binflow_runs import check_owner
            from binflow_acceptance import ensure_run_unstarted
            db().execute('BEGIN IMMEDIATE');day=owned(day_id)
            if day['finished_at']: raise ValueError('This driver day is already finalised.')
            tid=request.form.get('truck_id',type=int);state=selected_state(g.user,request.form.get('state',''))
            truck=db().execute('SELECT * FROM bf_trucks WHERE id=? AND state=?',(tid,state)).fetchone()
            if not truck or state not in STATES: raise ValueError('Choose a vehicle allocated to your selected state.')
            check_owner(db(),tid,day['work_date'],g.user)
            run=db().execute('SELECT driver_id FROM bf_runs WHERE truck_id=? AND run_date=?',(tid,day['work_date'])).fetchone()
            if run and run['driver_id'] and run['driver_id']!=g.user['id']: raise ValueError('This vehicle is assigned to another driver.')
            if db().execute("SELECT 1 FROM bf_jobs WHERE truck_id=? AND scheduled_date=? AND status!='cancelled' AND driver_id IS NOT NULL AND driver_id!=?",
                            (tid,day['work_date'],g.user['id'])).fetchone():
                raise ValueError('This vehicle has work assigned to another driver. Ask the office to check its schedule.')
            if day['truck_id'] and day['truck_id']!=tid:
                ensure_run_unstarted(db(),day['truck_id'],day['work_date'])
                if db().execute('SELECT 1 FROM bf_runs WHERE truck_id=? AND run_date=? AND accepted_at IS NOT NULL',(day['truck_id'],day['work_date'])).fetchone():
                    raise ValueError('Ask the office to reassign your accepted schedule before changing vehicles.')
                db().execute('UPDATE bf_runs SET driver_id=NULL WHERE truck_id=? AND run_date=? AND driver_id=?',
                             (day['truck_id'],day['work_date'],g.user['id']))
            db().execute('UPDATE bf_workdays SET truck_id=?,state=? WHERE id=?',(tid,state,day_id))
            db().execute('INSERT OR IGNORE INTO bf_runs(truck_id,run_date,driver_id) VALUES(?,?,?)',(tid,day['work_date'],g.user['id']))
            db().execute('UPDATE bf_runs SET driver_id=? WHERE truck_id=? AND run_date=?',(g.user['id'],tid,day['work_date']))
            if tid!=day['truck_id']: workday_event(db(),day_id,g.user['id'],'Vehicle selected',now(),truck['name']+' · '+state)
            return success('Vehicle selected. Complete your prestart.','bf_run',truck_id=tid,date=day['work_date'])
        except (ValueError,sqlite3.IntegrityError) as error:
            return failure(error if isinstance(error,ValueError) else ValueError('This vehicle is in use by another driver. Choose another vehicle.'),'bf_workday',summary='1')

    @app.post('/binflow/driver/my-day/<int:day_id>/finish')
    @access
    def bf_finish_workday(day_id):
        try:
            db().execute('BEGIN IMMEDIATE');day=owned(day_id)
            if day['finished_at']: return success('Your day is already finalised.','bf_workday',day=day_id,summary='1')
            blockers=finish_blockers(db(),day)
            if blockers: raise ValueError(' '.join(blockers))
            notes=request.form.get('notes','').strip()
            if len(notes)>1000: raise ValueError('Keep work notes under 1,001 characters.')
            received=now();finish,phone,zone,source=clock_stamp(request.form,received,day['timezone'])
            if finish.astimezone(timezone.utc)<datetime.fromisoformat(day['started_at']).astimezone(timezone.utc):
                raise ValueError('Finish time is before the recorded start. Check your phone clock.')
            count=db().execute("SELECT COUNT(*) FROM bf_jobs WHERE truck_id=? AND scheduled_date=? AND status!='cancelled' AND collected_at IS NOT NULL",
                               (day['truck_id'],day['work_date'])).fetchone()[0] if day['truck_id'] else 0
            truck=db().execute('SELECT name FROM bf_trucks WHERE id=?',(day['truck_id'],)).fetchone() if day['truck_id'] else None
            summary=f'BINFLOW · {truck["name"] if truck else "No vehicle selected"} · {count} client stops · {day["timezone"]}'
            if notes: summary+='\n'+notes
            if g.user['role']=='employee': finish_entries(db(),g.user,day,finish,summary)
            db().execute('UPDATE bf_workdays SET finished_at=?,finish_received_at=?,phone_finished_at=?,notes=? WHERE id=?',
                         (finish.isoformat(),received,phone,notes,day_id))
            workday_event(db(),day_id,g.user['id'],'Day finalised',received,source)
            return success('Day finalised. Your daily timesheet is committed.' if g.user['role']=='employee' else 'Driver day finalised. Admin accounts do not create wage entries.','bf_workday',day=day_id,summary='1')
        except ValueError as error:
            return failure(error,'bf_workday',summary='1')
