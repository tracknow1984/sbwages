"""State collection profiles and a dated planning calendar; never fabricate stock/jobs."""
import calendar
import json
import re
from collections import Counter, defaultdict
from datetime import date, timedelta

from flask import abort, g, request

STATES = ('QLD','NSW','VIC','SA','WA','TAS','ACT','NT')
DAYS = ('Monday','Tuesday','Wednesday','Thursday','Friday','Saturday','Sunday')
CADENCES = {'weekly':'Weekly','fortnightly':'Fortnightly','four_weekly':'Every 4 weeks',
            'eight_weekly':'Every 8 weeks','monthly':'Calendar monthly','adhoc':'Ad hoc',
            'unconfirmed':'Unconfirmed','paused':'Paused'}
INTERVALS = {'weekly':1,'fortnightly':2,'four_weekly':4,'eight_weekly':8}


def planning_resources(conn):
    """Read saved operating constraints without reusing the old customer schedule."""
    saved = conn.execute('SELECT data FROM blocktexx_model WHERE id=1').fetchone()
    model = json.loads(saved['data']) if saved else {}
    result = {}
    for state, values in model.get('states', {}).items():
        result[state] = {k: values.get(k) for k in
                         ('depot', 'depot_status', 'available_weekly_hours', 'truck')}
        result[state]['warehouses'] = [dict(r) for r in conn.execute(
            "SELECT id,name,address FROM bf_locations WHERE kind='warehouse' AND state=?", (state,))]
    return result


def migrate_collections(conn):
    conn.executescript('''
    CREATE TABLE IF NOT EXISTS bf_collection_profiles (
      location_id INTEGER PRIMARY KEY REFERENCES bf_locations(id), version INTEGER NOT NULL,
      cadence TEXT NOT NULL, service_days TEXT NOT NULL DEFAULT '[]', start_date TEXT NOT NULL DEFAULT '',
      end_date TEXT NOT NULL DEFAULT '', warehouse_id INTEGER REFERENCES bf_locations(id),
      confirmed INTEGER NOT NULL DEFAULT 0 CHECK(confirmed IN (0,1)),
      notes TEXT NOT NULL DEFAULT '', review_note TEXT NOT NULL DEFAULT '',
      source_frequency TEXT NOT NULL DEFAULT '', source_revision INTEGER,
      source_snapshot TEXT NOT NULL DEFAULT '[]', site_kind TEXT NOT NULL DEFAULT 'collection',
      updated_at TEXT NOT NULL, updated_by TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS bf_collection_profile_history (
      id INTEGER PRIMARY KEY, location_id INTEGER NOT NULL REFERENCES bf_locations(id),
      version INTEGER NOT NULL, snapshot TEXT NOT NULL, actor TEXT NOT NULL, created_at TEXT NOT NULL,
      UNIQUE(location_id,version));
    ''')


def parse_frequency(source):
    value = source.get('frequency','').strip().lower()
    days = sorted({d for d in source.get('service_days',[]) if type(d) is int and 0<=d<7})
    if not days:
        days = [i for i,name in enumerate(DAYS) if re.search(r'\b'+name[:3]+r'(?:'+name[3:]+r')?\b',value,re.I)]
    if 'paused' in value or source.get('visits_4w') == 0:
        return 'paused',days,0
    if 'unconfirmed' in value or 'confirm' in value or not value:
        return 'unconfirmed',days,None
    if 'adhoc' in value.replace(' ','').replace('-','') or 'on request' in value:
        return 'adhoc',[],None
    if value in ('monthly','calendar monthly'):
        return 'monthly',[],None
    if re.search(r'\blast\b.*\bmonth', value) and days:
        return 'monthly',days,None
    # The saved model's visit demand may have been edited since the original spreadsheet.
    visits=source.get('visits_4w')
    if visits is None:
        if 'fortnight' in value or re.search(r'(?:every\s*)?2\s*weeks?',value): visits=2
        elif re.search(r'(?:every\s*)?4\s*weeks?',value): visits=1
        elif re.search(r'(?:every\s*)?8\s*weeks?',value): visits=.5
        elif 'weekly' in value:
            visits=12 if 'three' in value else 8 if 'twice' in value else 4*max(1,len(days))
        elif value=='weekdays': visits=20
    mapping={.5:'eight_weekly',1:'four_weekly',2:'fortnightly',4:'weekly',8:'weekly',12:'weekly',20:'weekly',28:'weekly'}
    return mapping.get(visits,'unconfirmed'),days,visits


def source_plan(source, model, today, kind):
    """Infer only supported recurrence patterns; retain uncertain days for review."""
    from blocktexx import calendar_slots
    cadence,days,visits=parse_frequency(source)
    anchor=today-timedelta(days=today.weekday())
    notes=[]
    slots=set()
    for run in model.get('states',{}).get(source.get('state'),{}).get('runs',[]):
        if run.get('activity_type','collection')!='collection' or source.get('id') not in run.get('site_ids',[]) or run.get('runs_4w')==0:
            continue
        slots.update((s['week'],s['day']) for s in calendar_slots(run))
    if cadence in INTERVALS:
        interval=INTERVALS[cadence]
        expected=visits*interval/4 if visits is not None else 1
        if days and len(days)!=expected:
            notes.append('Saved weekday count differs from the collection frequency; confirm the days.')
            days=[]
        inferred=sorted({d for w,d in slots})
        phase=0
        if slots and cadence=='eight_weekly' and len(inferred)==expected and (not days or days==inferred):
            days=inferred;phase=min(w for w,d in slots)-1
            notes.append('Weekday copied from the route plan. Confirm the eight-week cycle phase; the source calendar spans only four weeks.')
        if slots and cadence!='eight_weekly':
            candidates=[]
            for offset in range(interval):
                pattern={(w,d) for w in range(1,5) if (w-1)%interval==offset for d in inferred}
                if pattern==slots and len(inferred)==expected:
                    candidates.append(offset)
            if len(candidates)==1 and (not days or days==inferred):
                days=inferred;phase=candidates[0]
                notes.append('Weekdays and cycle week copied from the saved route plan.')
            else:
                notes.append('Saved route slots do not match this site frequency; confirm cycle and weekdays.')
        anchor+=timedelta(weeks=phase)
        if not days: notes.append('Choose collection weekdays before this site can appear on dated visits.')
        notes.append('Confirm the first cycle date; the source planner has numbered weeks, not actual dates.')
    elif cadence=='monthly':
        notes.append('Choose the first collection date to set the monthly day.')
    elif cadence=='unconfirmed':
        notes.append('Confirm the collection frequency from the client or source notes.')
    if kind=='decomm':
        notes.append('Decommissioning partner: confirm whether this profile covers deliveries or return collections.')
    return dict(cadence=cadence,service_days=json.dumps(days),
                start_date=anchor.isoformat() if cadence in INTERVALS else '',review_note=' '.join(notes))


def record_profile(conn, profile, person, stamp):
    conn.execute('INSERT INTO bf_collection_profile_history(location_id,version,snapshot,actor,created_at) VALUES(?,?,?,?,?)',
                 (profile['location_id'],profile['version'],json.dumps(dict(profile)),person,stamp))


def populate_collection_profiles(conn, stamp):
    """Seed existing imported identities from the current saved model once per site."""
    if conn.execute("SELECT 1 FROM sqlite_master WHERE name='bf_calendar_rebuilds'").fetchone() and conn.execute('SELECT 1 FROM bf_calendar_rebuilds').fetchone():
        return {}
    saved=conn.execute('SELECT * FROM blocktexx_model WHERE id=1').fetchone()
    model=json.loads(saved['data']) if saved else {}
    current={kind+':'+s['id']:s for key,kind in (('sites','collection'),('partners','decomm')) for s in model.get(key,[])}
    grouped=defaultdict(list)
    for row in conn.execute('SELECT s.*,r.source_revision FROM bf_location_sources s JOIN bf_import_runs r ON r.id=s.import_id'):
        grouped[row['location_id']].append(row)
    summary=Counter()
    conn.execute('BEGIN IMMEDIATE')
    try:
        for lid,rows in grouped.items():
            if conn.execute('SELECT 1 FROM bf_collection_profiles WHERE location_id=?',(lid,)).fetchone(): continue
            location=conn.execute('SELECT * FROM bf_locations WHERE id=?',(lid,)).fetchone()
            sources=[current.get(r['source_id'],json.loads(r['payload'])['source']) for r in rows]
            kind=rows[0]['kind']
            profile=source_plan(sources[0],model,date.fromisoformat(stamp[:10]),kind)
            if len(sources)!=1 or any(s['state']!=location['state'] for s in sources):
                profile.update(cadence='unconfirmed',service_days='[]',start_date='',review_note='Multiple or mismatched source records: review this location before scheduling.')
            if any(r['source_id'] not in current for r in rows):
                profile['review_note']+=' Using the imported source snapshot; no matching record in the current saved model.'
            warehouses=conn.execute("SELECT id FROM bf_locations WHERE kind='warehouse' AND state=?",(location['state'],)).fetchall()
            profile.update(location_id=lid,version=1,end_date='',warehouse_id=warehouses[0]['id'] if len(warehouses)==1 else None,
                confirmed=0,notes=sources[0].get('notes','')[:1000],source_frequency=' / '.join(s.get('frequency','') for s in sources),
                source_revision=saved['revision'] if saved and all(r['source_id'] in current for r in rows) else rows[0]['source_revision'],
                source_snapshot=json.dumps(sources),site_kind=kind,updated_at=stamp,updated_by='Blocktexx profile import')
            fields=','.join(profile);marks=','.join('?' for _ in profile)
            conn.execute(f'INSERT INTO bf_collection_profiles({fields}) VALUES({marks})',tuple(profile.values()))
            record_profile(conn,profile,profile['updated_by'],stamp)
            summary[location['state']]+=1
        conn.commit()
    except Exception:
        conn.rollback();raise
    return dict(summary)


def profile_rows(conn,state=''):
    rows=conn.execute('''SELECT l.id,l.name,l.state,l.address,c.name AS client_name,p.*,
        w.name AS warehouse_name FROM bf_locations l JOIN bf_clients c ON c.id=l.client_id
        LEFT JOIN bf_collection_profiles p ON p.location_id=l.id
        LEFT JOIN bf_locations w ON w.id=p.warehouse_id WHERE l.kind='client' '''+
        ('AND l.state=? ' if state else '')+'ORDER BY l.state,c.name,l.name',(state,) if state else ()).fetchall()
    quantities=defaultdict(list)
    for r in conn.execute('''SELECT r.location_id,r.quantity,s.name FROM bf_site_requirements r
        JOIN bf_sizes s ON s.id=r.size_id WHERE r.quantity>0 ORDER BY s.litres'''):
        quantities[r['location_id']].append(dict(r))
    result=[]
    for row in rows:
        p=dict(row);p['cadence']=p['cadence'] or 'unconfirmed'
        p['days']=json.loads(p['service_days'] or '[]');p['day_names']=', '.join(DAYS[d][:3] for d in p['days'])
        p['frequency_label']=CADENCES[p['cadence']];p['quantities']=quantities[p['id']]
        p['dated']=bool(p['start_date'] and (p['cadence']=='monthly' or (p['cadence'] in INTERVALS and p['days'])))
        result.append(p)
    return result


def occurs(profile, day):
    if not profile.get('start_date'): return False
    start=date.fromisoformat(profile['start_date'])
    if day<start or (profile.get('end_date') and day>date.fromisoformat(profile['end_date'])): return False
    if profile['cadence']=='monthly':
        if profile.get('monthly_rule') == 'last_weekday':
            days=profile.get('days',json.loads(profile.get('service_days') or '[]'))
            return day.weekday() in days and (day+timedelta(days=7)).month!=day.month
        return day.day==min(start.day,calendar.monthrange(day.year,day.month)[1])
    interval=INTERVALS.get(profile['cadence'])
    if not interval: return False
    monday=start-timedelta(days=start.weekday())
    days=profile.get('days',json.loads(profile.get('service_days') or '[]'))
    return day.weekday() in days and ((day-monday).days//7)%interval==0


def dated(value):
    parsed=date.fromisoformat(value)
    if not 2000<=parsed.year<=2100 or parsed.isoformat()!=value: raise ValueError('Choose a date from 2000 to 2100.')
    return parsed


def calendar_data(conn,start,end,state=''):
    profiles=profile_rows(conn,state);by_id={p['id']:p for p in profiles}
    jobs=conn.execute('''SELECT j.*,
        EXISTS(SELECT 1 FROM bf_job_bins i WHERE i.job_id=j.id AND i.direction='collect') AS collects,
        EXISTS(SELECT 1 FROM bf_job_bins i WHERE i.job_id=j.id AND (i.loaded_at IS NOT NULL OR i.picked_up_at IS NOT NULL)) AS moved
        FROM bf_jobs j WHERE scheduled_date BETWEEN ? AND ? AND status!='cancelled' '''+
        ('AND state=? ' if state else '')+'ORDER BY scheduled_date,state,client_name,id',
        (start.isoformat(),end.isoformat(),state) if state else (start.isoformat(),end.isoformat())).fetchall()
    booked={(j['site_id'],j['scheduled_date']) for j in jobs if j['collects']}
    events=defaultdict(list);counts=Counter()
    for offset in range((end-start).days+1):
        day=start+timedelta(days=offset);key=day.isoformat()
        for p in profiles:
            if occurs(p,day) and (p['id'],key) not in booked:
                events[key].append(dict(profile=p,job=None,phase='Needs allocation',colour='proposed'))
                counts['proposed']+=1
    for j in jobs:
        p=by_id.get(j['site_id'])
        if not p: continue
        if j['approval_status']=='pending': colour,phase='pending','Awaiting approval';counts['pending']+=1
        elif j['received_at']: colour,phase='orange','Back at warehouse';counts['approved']+=1
        elif j['collected_at']: colour,phase='green','Client completed';counts['approved']+=1
        elif j['moved']: colour,phase='blue','In progress';counts['approved']+=1
        else: colour,phase='scheduled','Approved · scheduled';counts['approved']+=1
        events[j['scheduled_date']].append(dict(profile=p,job=j,phase=phase,colour=colour))
    return dict(profiles=profiles,events=events,counts=counts,
                undated=[p for p in profiles if not p['dated'] and p['cadence']!='paused'])


def register_collections(app,db,access,require,render,success,now,actor):
    @app.get('/binflow/collection-calendar')
    @access
    def bf_collection_calendar():
        from binflow_route_calendar import enabled, view_data
        rebuilt=enabled(db())
        state=request.args.get('state','');view=request.args.get('view','week')
        if state not in ('',)+STATES or view not in ('month','week'): abort(400)
        try: chosen=dated(request.args.get('date') or now()[:10])
        except ValueError: abort(400,'Choose a valid calendar date.')
        if view=='month':
            first=chosen.replace(day=1);last=chosen.replace(day=calendar.monthrange(chosen.year,chosen.month)[1])
            start=first-timedelta(days=first.weekday());end=last+timedelta(days=6-last.weekday())
            previous=first-timedelta(days=1);following=last+timedelta(days=1);title=chosen.strftime('%B %Y')
        else:
            start=chosen-timedelta(days=chosen.weekday());end=start+timedelta(days=6)
            previous=start-timedelta(days=7);following=start+timedelta(days=7);title=start.strftime('%d %b')+' – '+end.strftime('%d %b %Y')
        weeks=[[start+timedelta(days=n+d) for d in range(7)] for n in range(0,(end-start).days+1,7)]
        if rebuilt:
            return render('route_calendar',section='collection_calendar',selected=state,view=view,chosen=chosen,
                title=title,previous=previous.isoformat(),following=following.isoformat(),today=now()[:10],
                **view_data(db(),start,end,state))
        data=calendar_data(db(),start,end,state)
        state_counts={r['state']:r['n'] for r in db().execute("SELECT state,COUNT(*) n FROM bf_locations WHERE kind='client' GROUP BY state")}
        return render('collection_calendar',section='collection_calendar',selected=state,view=view,chosen=chosen,
            weeks=weeks,title=title,previous=previous.isoformat(),following=following.isoformat(),days=DAYS,
            today=now()[:10],state_counts=state_counts,**data)

    @app.route('/binflow/locations/<int:location_id>/collection-profile',methods=['GET','POST'])
    @require('admin')
    def bf_collection_profile(location_id):
        location=db().execute("SELECT l.*,c.name AS client_name FROM bf_locations l JOIN bf_clients c ON c.id=l.client_id WHERE l.id=? AND l.kind='client'",(location_id,)).fetchone()
        if not location: abort(404)
        if request.method=='POST': db().execute('BEGIN IMMEDIATE')
        saved=db().execute('SELECT * FROM bf_collection_profiles WHERE location_id=?',(location_id,)).fetchone()
        values=dict(saved) if saved else dict(location_id=location_id,version=0,cadence='unconfirmed',service_days='[]',start_date='',end_date='',warehouse_id=None,
            confirmed=0,notes='',review_note='',source_frequency='',source_revision=None,source_snapshot='[]',site_kind='collection')
        selected_days=json.loads(values['service_days']);error=None
        if request.method=='POST':
            try:
                if request.form.get('version')!=str(values['version']): raise ValueError('This profile changed. Reload and review the latest version before saving.')
                cadence=request.form.get('cadence','');start=request.form.get('start_date','');end=request.form.get('end_date','')
                days=sorted(set(int(d) for d in request.form.getlist('days')))
                if cadence not in CADENCES or any(d not in range(7) for d in days): raise ValueError('Choose a valid frequency and collection weekdays.')
                if start: dated(start)
                if end: dated(end)
                if end and (not start or end<start): raise ValueError('End date must be on or after the first cycle date.')
                confirmed=request.form.get('confirmed')=='yes'
                if cadence in INTERVALS and confirmed and (not days or not start): raise ValueError('Choose the first cycle date and weekdays before confirming a recurring profile.')
                if cadence=='monthly' and confirmed and not start: raise ValueError('Choose the first monthly collection date.')
                if cadence=='unconfirmed' and confirmed: raise ValueError('Choose a confirmed frequency first.')
                warehouse=int(request.form.get('warehouse_id') or '0') or None
                if warehouse and not db().execute("SELECT 1 FROM bf_locations WHERE id=? AND kind='warehouse' AND state=?",(warehouse,location['state'])).fetchone():
                    raise ValueError('Choose a warehouse in the client’s state.')
                notes=request.form.get('notes','').strip()
                if len(notes)>1000: raise ValueError('Keep notes to 1,000 characters.')
                values.update(cadence=cadence,service_days=json.dumps(days if cadence in INTERVALS else []),start_date=start,end_date=end,
                    warehouse_id=warehouse,confirmed=int(confirmed),notes=notes,review_note='' if confirmed else 'Profile dates and frequency need confirmation.',
                    version=values['version']+1,updated_at=now(),updated_by=actor())
                keys=','.join(values);marks=','.join('?' for _ in values)
                conn=db();conn.execute(f'INSERT INTO bf_collection_profiles({keys}) VALUES({marks}) ON CONFLICT(location_id) DO UPDATE SET '+
                    ','.join(k+'=excluded.'+k for k in values if k!='location_id'),tuple(values.values()))
                record_profile(conn,values,actor(),values['updated_at'])
                return success('Collection profile saved. The planning calendar now uses this frequency.','bf_collection_calendar',state=location['state'],date=start or now()[:10])
            except (ValueError,TypeError) as exc:
                db().rollback();error=str(exc) or 'Check the profile fields.'
                values.update({key:request.form.get(key,'') for key in ('cadence','start_date','end_date','notes')})
                selected_days=[d for d in range(7) if str(d) in request.form.getlist('days')]
        warehouses=db().execute("SELECT * FROM bf_locations WHERE kind='warehouse' AND state=? ORDER BY name",(location['state'],)).fetchall()
        history=db().execute('SELECT * FROM bf_collection_profile_history WHERE location_id=? ORDER BY version DESC LIMIT 10',(location_id,)).fetchall()
        return render('collection_profile',section='collection_calendar',location=location,values=values,selected_days=selected_days,
            cadences=CADENCES,days=DAYS,warehouses=warehouses,history=history,error=error),400 if error else 200
