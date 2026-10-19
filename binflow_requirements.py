"""Client-site bin targets and loading plans built from actual reservations."""
import json
import re
import math
from collections import Counter, defaultdict
from datetime import date

from flask import abort, g, request


SCHEMA = '''
CREATE TABLE IF NOT EXISTS bf_site_profiles (
 location_id INTEGER PRIMARY KEY REFERENCES bf_locations(id), version INTEGER NOT NULL,
 notes TEXT NOT NULL DEFAULT '', updated_at TEXT NOT NULL, updated_by TEXT NOT NULL,
 review_note TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS bf_site_requirements (
 location_id INTEGER NOT NULL REFERENCES bf_locations(id), size_id INTEGER NOT NULL REFERENCES bf_sizes(id),
 quantity INTEGER NOT NULL CHECK(quantity>=0 AND quantity<=10000), PRIMARY KEY(location_id,size_id)
);
CREATE TABLE IF NOT EXISTS bf_profile_history (
 id INTEGER PRIMARY KEY, location_id INTEGER NOT NULL REFERENCES bf_locations(id), version INTEGER NOT NULL,
 snapshot TEXT NOT NULL, created_at TEXT NOT NULL, actor TEXT NOT NULL, UNIQUE(location_id,version)
);
CREATE TABLE IF NOT EXISTS bf_requirements_imports (
 id TEXT PRIMARY KEY, summary TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS bf_state_bin_pools (
 state TEXT NOT NULL,size_id INTEGER NOT NULL REFERENCES bf_sizes(id),
 rotation INTEGER NOT NULL CHECK(rotation>=0 AND rotation<=10000),
 updated_at TEXT NOT NULL,updated_by TEXT NOT NULL,PRIMARY KEY(state,size_id)
);
'''

PLAN_IMPORT = 'national-240-1100-2026-10-19-v1'


def converted_requirements(source):
    """A proposed site holding, not a sum of repeated service visits.

    Multiple service lines of one type are explicitly unverified in the source.
    Use the largest listed quantity as a provisional holding and retain a review
    flag. Never turn pallet/bulk descriptions into invented physical bins.
    """
    lines = defaultdict(list)
    for line in source.get('equipment','').splitlines():
        match = re.match(r'\s*(\d+(?:\.\d+)?)\s*[×x]\s*(.*)',line,re.I)
        if not match:
            continue
        qty, description = float(match[1]), match[2].lower().replace(' ','')
        kind = next((key for key,token in (('bin1100','1100l'),('bin660','660l'),('bin240','240l'),('bin120','120l'),('pallecon','pallecon'),('cage','cage')) if token in description),None)
        if kind:
            lines[kind].append(qty)
    review = []
    if lines:
        counts = {key:math.ceil(max(values)) for key,values in lines.items()}
        if any(len(values)>1 for values in lines.values()):
            review.append('Repeated service lines: largest listed quantity per equipment type used as a provisional site holding; confirm the unique bins needed.')
    else:
        counts = {}
        for key,value in source.get('containers',{}).items():
            if type(value) in (int,float) and math.isfinite(value) and value>=0:
                counts[key] = math.ceil(value)
    if any(value>10000 for value in counts.values()):
        raise ValueError('Source bin quantity exceeds the supported planning range.')
    large = counts.get('bin1100',0)+counts.get('cage',0)+math.ceil(counts.get('bin660',0)*660/1100)
    small = counts.get('bin240',0)+math.ceil(counts.get('bin120',0)*120/240)
    if counts.get('pallecon'):
        review.append(f'{counts["pallecon"]} pallecons listed; confirm whether these remain or need a bin replacement.')
    if not large and not small:
        review.append('No confirmed 240L / 1100L bin quantity in the source. Confirm this site before allocating stock.')
    if 'reconcile' in source.get('notes','').lower():
        review.append('The source flags a downstream quantity discrepancy; confirm the required site holding.')
    return {240:small,1100:large}, ' '.join(review), counts


def populate_requirements(conn, timestamp):
    conn.execute('BEGIN IMMEDIATE')
    try:
        done = conn.execute('SELECT summary FROM bf_requirements_imports WHERE id=?',(PLAN_IMPORT,)).fetchone()
        if done:
            conn.rollback()
            return json.loads(done['summary']), False
        sources = conn.execute('''SELECT s.*,l.state FROM bf_location_sources s
            JOIN bf_locations l ON l.id=s.location_id WHERE l.kind='client' ORDER BY s.location_id,s.source_id''').fetchall()
        if not sources:
            conn.rollback()
            return None,False
        sizes = {r['litres']:r['id'] for r in conn.execute("SELECT id,litres FROM bf_sizes WHERE name IN ('240 L','1100 L')")}
        if set(sizes)!={240,1100}:
            raise ValueError('Standard 240 L and 1100 L bin sizes are required.')
        summary = dict(profiles_added=0,profiles_preserved=0,review_sites=0,states={})
        grouped = defaultdict(list)
        for row in sources:
            grouped[row['location_id']].append(row)
        for lid,rows in grouped.items():
            if conn.execute('SELECT 1 FROM bf_site_profiles WHERE location_id=?',(lid,)).fetchone():
                summary['profiles_preserved'] += 1
                continue
            totals, reviews, originals = Counter(), [], []
            for row in rows:
                source = json.loads(row['payload'])['source']
                quantities, review, counts = converted_requirements(source)
                # Multiple source records matching one location are not proven additional stock.
                for litres,qty in quantities.items():
                    totals[litres] = max(totals[litres],qty)
                if review:
                    reviews.append(review)
                originals.append(dict(source_id=row['source_id'],counts=counts))
            if len(rows)>1:
                reviews.append('Multiple source records map to this site; confirm the combined holding.')
            review_note = ' '.join(dict.fromkeys(reviews))
            notes = 'Initial plan: cages → 1100L one-for-one; 660L → 1100L by total capacity, rounded up per site; 240L retained. Physical ownership and site delivery are recorded separately.'
            who = 'Initial national allocation plan'
            conn.execute('INSERT INTO bf_site_profiles(location_id,version,notes,updated_at,updated_by,review_note) VALUES(?,1,?,?,?,?)',(lid,notes,timestamp,who,review_note))
            conn.executemany('INSERT INTO bf_site_requirements(location_id,size_id,quantity) VALUES(?,?,?)',[(lid,sizes[litres],totals[litres]) for litres in (240,1100)])
            snapshot = dict(quantities=[dict(size_id=sizes[litres],name=f'{litres} L',quantity=totals[litres]) for litres in (240,1100)],notes=notes,review_note=review_note,source=originals)
            conn.execute('INSERT INTO bf_profile_history(location_id,version,snapshot,created_at,actor) VALUES(?,1,?,?,?)',(lid,json.dumps(snapshot),timestamp,who))
            summary['profiles_added'] += 1
            summary['review_sites'] += bool(review_note)
            state = summary['states'].setdefault(rows[0]['state'],{'240 L':0,'1100 L':0})
            state['240 L'] += totals[240]
            state['1100 L'] += totals[1100]
        conn.execute('INSERT INTO bf_requirements_imports VALUES(?,?,?)',(PLAN_IMPORT,json.dumps(summary),timestamp))
        conn.commit()
        return summary,True
    except Exception:
        conn.rollback()
        raise


def migrate_requirements(conn):
    conn.executescript(SCHEMA)
    if 'bin_plan' not in {r['name'] for r in conn.execute('PRAGMA table_info(bf_jobs)')}:
        conn.execute("ALTER TABLE bf_jobs ADD COLUMN bin_plan TEXT NOT NULL DEFAULT '[]'")


def site_plan(conn, site_id, warehouse_id, collections=None, run_date=None):
    """Projected site stock accounts for other open jobs, including partially scanned jobs.

    Profile targets are quantities to keep on site. A normal visit swaps up to that
    quantity; changing the actual collection quantity changes the recommended load.
    """
    profile = conn.execute('SELECT * FROM bf_site_profiles WHERE location_id=?', (site_id,)).fetchone()
    if not profile:
        return None, []
    rows = conn.execute('''SELECT r.*,s.name,s.litres FROM bf_site_requirements r
        JOIN bf_sizes s ON s.id=r.size_id WHERE r.location_id=? ORDER BY s.litres,s.id''', (site_id,)).fetchall()
    onsite = Counter({r['size_id']:r['n'] for r in conn.execute('SELECT size_id,COUNT(*) AS n FROM bf_bins WHERE location_id=? GROUP BY size_id', (site_id,))})
    outgoing, incoming = Counter(), Counter()
    for r in conn.execute('''SELECT b.size_id,i.direction,b.location_id FROM bf_job_bins i
        JOIN bf_jobs j ON j.id=i.job_id JOIN bf_bins b ON b.id=i.bin_id
        WHERE j.site_id=? AND j.status='scheduled' AND i.active=1
        AND (? IS NULL OR j.scheduled_date<=?)''', (site_id,run_date,run_date)):
        if r['direction']=='deliver' and r['location_id'] != site_id:
            incoming[r['size_id']] += 1
        elif r['direction']=='collect' and r['location_id'] == site_id:
            outgoing[r['size_id']] += 1
    available = Counter({r['size_id']:r['n'] for r in conn.execute('''SELECT b.size_id,COUNT(*) AS n FROM bf_bins b
        WHERE b.location_id=? AND b.status='empty' AND NOT EXISTS(SELECT 1 FROM bf_job_bins i WHERE i.bin_id=b.id AND i.active=1)
        GROUP BY b.size_id''', (warehouse_id,))})
    collectable = Counter({r['size_id']:r['n'] for r in conn.execute('''SELECT b.size_id,COUNT(*) AS n FROM bf_bins b
        WHERE b.location_id=? AND b.status IN ('full','at_client') AND NOT EXISTS(SELECT 1 FROM bf_job_bins i WHERE i.bin_id=b.id AND i.active=1)
        GROUP BY b.size_id''', (site_id,))})
    result, collection_slots, delivery_slots = [], 200, 200
    for r in rows:
        sid, target = r['size_id'], r['quantity']
        collect = collections.get(sid,0) if collections is not None else min(target,collectable[sid],collection_slots)
        collection_slots -= collect
        projected = onsite[sid] + incoming[sid] - outgoing[sid]
        needed = max(0,target-projected+collect)
        selected = min(needed,available[sid],delivery_slots)
        delivery_slots -= selected
        result.append(dict(size_id=sid,name=r['name'],target=target,onsite=onsite[sid],
            incoming=incoming[sid],outgoing=outgoing[sid],projected=projected,collect=collect,
            recommended=needed,available=available[sid],selected=selected,shortfall=max(0,needed-selected)))
    return profile, result


def load_plan(conn, truck_id, run_date):
    """Dated load sheet uses reserved physical bins, never unconfirmed profile estimates."""
    jobs = conn.execute("SELECT * FROM bf_jobs WHERE truck_id=? AND scheduled_date=? AND approval_status='approved' AND status!='cancelled' ORDER BY run_order,id", (truck_id,run_date)).fetchall()
    rows = conn.execute('''SELECT j.id AS job_id,j.warehouse_id,j.warehouse_name,i.bin_code,i.loaded_at,i.delivered_at,
        i.size_name,b.size_id,s.litres,b.truck_id,b.truck_load,b.location_id
        FROM bf_jobs j JOIN bf_job_bins i ON i.job_id=j.id JOIN bf_bins b ON b.id=i.bin_id
        JOIN bf_sizes s ON s.id=b.size_id
        WHERE j.truck_id=? AND j.scheduled_date=? AND j.approval_status='approved' AND j.status!='cancelled' AND i.direction='deliver'
        ORDER BY s.litres,b.size_id,i.bin_code''', (truck_id,run_date)).fetchall()
    totals, warehouses, per_job = {}, {}, defaultdict(dict)
    for r in rows:
        sid, wid, jid = r['size_id'],r['warehouse_id'],r['job_id']
        delivered = bool(r['delivered_at'])
        loaded = not delivered and r['truck_id']==truck_id and r['truck_load']=='empty'
        # All assigned jobs use scan mode. The timestamp also makes past load sheets stable.
        waiting = not delivered and not loaded
        for group, key in ((totals,sid),(warehouses,(wid,sid)),(per_job[jid],sid)):
            entry = group.setdefault(key,dict(name=r['size_name'],warehouse=r['warehouse_name'],planned=0,loaded=0,delivered=0,waiting=0,codes=[]))
            entry['planned'] += 1
            entry['loaded'] += int(loaded)
            entry['delivered'] += int(delivered)
            entry['waiting'] += int(waiting)
            if waiting:
                entry['codes'].append(r['bin_code'])
    stops = [dict(job=j,rows=list(per_job[j['id']].values()),shortfalls=[r for r in json.loads(j['bin_plan']) if r.get('shortfall',0)>0]) for j in jobs]
    return dict(stops=stops,totals=list(totals.values()),warehouses=list(warehouses.values()),
                total_to_load=sum(r['waiting'] for r in totals.values()))


def register_requirements(app, db, require, access, location, render, success, actor, now, source_profiles):
    @app.route('/binflow/locations/<int:location_id>/requirements', methods=['GET','POST'])
    @require('admin')
    def bf_requirements(location_id):
        if request.method=='POST':
            db().execute('BEGIN IMMEDIATE')
        try:
            record = location(location_id)
        except ValueError:
            abort(404)
        if record['kind'] != 'client':
            abort(404)
        sizes = db().execute('SELECT * FROM bf_sizes ORDER BY litres,id').fetchall()
        profile = db().execute('SELECT * FROM bf_site_profiles WHERE location_id=?', (location_id,)).fetchone()
        version = profile['version'] if profile else 0
        quantities = {r['size_id']:r['quantity'] for r in db().execute('SELECT * FROM bf_site_requirements WHERE location_id=?', (location_id,))}
        sources = source_profiles(db(),location_id)
        suggested = False
        if not profile and len(sources)==1:
            for size in sizes:
                value = sources[0]['source'].get('containers',{}).get('bin'+str(size['litres']))
                if type(value) is int and 0<=value<=10000 and sum(s['litres']==size['litres'] for s in sizes)==1:
                    quantities[size['id']] = value
                    suggested = suggested or value>0
        values = {s['id']:str(quantities.get(s['id'],0)) for s in sizes}
        notes, error = profile['notes'] if profile else '', None
        if request.method=='POST':
            values = {s['id']:request.form.get('quantity_'+str(s['id']), '').strip() for s in sizes}
            notes = request.form.get('notes','').strip()
            try:
                if request.form.get('version') != str(version):
                    raise ValueError('This profile was changed by another user. Review the latest quantities before saving again.')
                if len(notes)>1000:
                    raise ValueError('Keep notes to 1,000 characters.')
                parsed = {}
                for size in sizes:
                    raw = values[size['id']]
                    if not raw.isascii() or not raw.isdigit() or not 0<=int(raw)<=10000:
                        raise ValueError('Enter a whole quantity from 0 to 10,000 for each bin size.')
                    parsed[size['id']] = int(raw)
                if profile and not profile['review_note'] and parsed==quantities and notes==profile['notes']:
                    return success('Bin requirements are already saved.','bf_requirements',location_id=location_id)
                stamp, person = now(), actor()
                db().execute('''INSERT INTO bf_site_profiles(location_id,version,notes,updated_at,updated_by) VALUES(?,?,?,?,?)
                    ON CONFLICT(location_id) DO UPDATE SET version=excluded.version,notes=excluded.notes,updated_at=excluded.updated_at,updated_by=excluded.updated_by,review_note='' ''',
                    (location_id,version+1,notes,stamp,person))
                db().execute('DELETE FROM bf_site_requirements WHERE location_id=?',(location_id,))
                db().executemany('INSERT INTO bf_site_requirements(location_id,size_id,quantity) VALUES(?,?,?)',[(location_id,sid,qty) for sid,qty in parsed.items()])
                snapshot = dict(notes=notes,quantities=[dict(size_id=s['id'],name=s['name'],quantity=parsed[s['id']]) for s in sizes])
                db().execute('INSERT INTO bf_profile_history(location_id,version,snapshot,created_at,actor) VALUES(?,?,?,?,?)',
                    (location_id,version+1,json.dumps(snapshot),stamp,person))
                return success('Client bin requirements saved. New pickup plans will use these quantities.','bf_requirements',location_id=location_id)
            except ValueError as exc:
                db().rollback()
                error = str(exc)
        history = db().execute('SELECT version,created_at,actor FROM bf_profile_history WHERE location_id=? ORDER BY version DESC LIMIT 10',(location_id,)).fetchall()
        return render('requirements',section='setup',record=record,sizes=sizes,values=values,notes=notes,
            version=version,profile=profile,suggested=suggested,source_profiles=sources,history=history,error=error), 400 if error else 200

    @app.get('/binflow/driver/<int:truck_id>/load-list')
    @access
    def bf_load_list(truck_id):
        truck = db().execute('SELECT * FROM bf_trucks WHERE id=?',(truck_id,)).fetchone()
        if not truck:
            abort(404)
        try:
            run_date = date.fromisoformat(request.args.get('date') or now()[:10]).isoformat()
        except ValueError:
            abort(400,'Choose a valid run date.')
        return render('load_list',section='driver',truck=truck,run_date=run_date,**load_plan(db(),truck_id,run_date))

    @app.route('/binflow/allocations',methods=['GET','POST'])
    @access
    def bf_allocations():
        selected = request.args.get('state','')
        states = ('QLD','NSW','VIC','SA','WA','TAS','ACT','NT')
        if selected and selected not in states:
            abort(400)
        if request.method=='POST':
            if g.user['role']!='admin':
                abort(403)
            try:
                updates=[]
                for key in request.form:
                    if not key.startswith('rotation_'):
                        continue
                    _,state,sid=key.split('_')
                    value=request.form[key]
                    if state not in states or not sid.isdigit() or not db().execute('SELECT 1 FROM bf_sizes WHERE id=?',(sid,)).fetchone() or not value.isascii() or not value.isdigit() or int(value)>10000:
                        raise ValueError()
                    updates.append((state,int(sid),int(value),now(),actor()))
                if not updates:
                    raise ValueError()
                db().execute('BEGIN IMMEDIATE')
                db().executemany('''INSERT INTO bf_state_bin_pools VALUES(?,?,?,?,?)
                    ON CONFLICT(state,size_id) DO UPDATE SET rotation=excluded.rotation,updated_at=excluded.updated_at,updated_by=excluded.updated_by''',updates)
                return success('State rotation allowances saved.','bf_allocations',state=selected)
            except (ValueError,TypeError):
                db().rollback()
                abort(400,'Enter a whole rotation quantity from 0 to 10,000 for each state and size.')
        rows = db().execute('''SELECT l.*,c.name AS client_name,p.version,p.review_note,
            s.id AS size_id,s.name AS size_name,r.quantity
            FROM bf_locations l JOIN bf_clients c ON c.id=l.client_id
            LEFT JOIN bf_site_profiles p ON p.location_id=l.id
            LEFT JOIN bf_site_requirements r ON r.location_id=l.id
            LEFT JOIN bf_sizes s ON s.id=r.size_id
            WHERE l.kind='client' ORDER BY l.state,c.name,l.name,s.litres''').fetchall()
        sites, needs, stock, review_counts = {},Counter(),Counter(),Counter()
        for r in rows:
            item = sites.setdefault(r['id'],dict(id=r['id'],name=r['name'],client=r['client_name'],state=r['state'],profile=r['version'],review=r['review_note'],quantities=[]))
            if r['quantity']:
                item['quantities'].append(dict(name=r['size_name'],quantity=r['quantity']))
                needs[(r['state'],r['size_id'])] += r['quantity']
        for item in sites.values():
            if item['review'] or not item['profile']:
                review_counts[item['state']] += 1
        for r in db().execute('SELECT state,size_id,COUNT(*) AS n FROM bf_bins GROUP BY state,size_id'):
            stock[(r['state'],r['size_id'])] = r['n']
        sizes = db().execute('SELECT * FROM bf_sizes WHERE litres IN (240,1100) OR id IN (SELECT size_id FROM bf_site_requirements WHERE quantity>0) ORDER BY litres,id').fetchall()
        pools = []
        rotations={(r['state'],r['size_id']):r['rotation'] for r in db().execute('SELECT * FROM bf_state_bin_pools')}
        for state in states:
            if selected and selected!=state:
                continue
            if not selected and not any(s['state']==state for s in sites.values()) and not any(k[0]==state and v for k,v in stock.items()):
                continue
            for size in sizes:
                target,registered = needs[(state,size['id'])],stock[(state,size['id'])]
                rotation=rotations.get((state,size['id']),target)
                pools.append(dict(state=state,size_id=size['id'],size=size['name'],target=target,rotation=rotation,total=target+rotation,registered=registered,gap=max(0,target+rotation-registered),reviews=review_counts[state]))
        return render('allocations',section='allocations',selected=selected,pools=pools,
            sites=[s for s in sites.values() if not selected or s['state']==selected],
            review_count=sum(v for k,v in review_counts.items() if not selected or k==selected))
