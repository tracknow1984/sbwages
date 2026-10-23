"""Current-profile route proposals, constrained by depot returns and working hours.

No jobs, physical bins or dispatch approvals are created by this planner.
"""
import base64
import calendar
import gzip
import json
import math
import re
from functools import lru_cache
from pathlib import Path
from collections import Counter, defaultdict
from datetime import date, timedelta

from binflow_client_profiles import register_rows
from binflow_collections import DAYS, INTERVALS, occurs, parse_frequency, planning_resources, record_profile

RESET_ID = 'current-client-calendar-2026-10-26-v1'
START = date(2026, 10, 26)
PRIMARY = ('QLD', 'NSW', 'VIC', 'SA')
DAY_LIMIT = 540
# Explicit planning allowances, not measured site times or legal fatigue advice.
PREP, BREAK, WAIT = 15, 30, 30


def migrate(conn):
    conn.executescript('''CREATE TABLE IF NOT EXISTS bf_calendar_rebuilds(
        id TEXT PRIMARY KEY, start_date TEXT NOT NULL, previous_plan TEXT NOT NULL,
        summary TEXT NOT NULL, created_at TEXT NOT NULL);''')
    columns = {r['name'] for r in conn.execute('PRAGMA table_info(bf_collection_profiles)')}
    for key, declaration in (('active', 'INTEGER NOT NULL DEFAULT 1'),
                             ('monthly_rule', "TEXT NOT NULL DEFAULT ''"),
                             ('flexible', 'INTEGER NOT NULL DEFAULT 0'),
                             ('cycle_phase', 'INTEGER NOT NULL DEFAULT 0')):
        if key not in columns:
            conn.execute(f'ALTER TABLE bf_collection_profiles ADD COLUMN {key} {declaration}')
    conn.commit()


def enabled(conn):
    return bool(conn.execute('SELECT 1 FROM bf_calendar_rebuilds WHERE id=?', (RESET_ID,)).fetchone())


def clients(conn):
    requirements = defaultdict(list)
    for r in conn.execute('''SELECT r.*,s.litres,s.name FROM bf_site_requirements r
                            JOIN bf_sizes s ON s.id=r.size_id WHERE r.quantity>0'''):
        requirements[r['location_id']].append(dict(r))
    equipment = {r['profile_id']: json.loads(r['equipment']) for r in conn.execute(
        'SELECT profile_id,equipment FROM bf_allocation_workbook_rows WHERE active=1')}
    saved = {r['location_id']: dict(r) for r in conn.execute('SELECT location_id,cycle_phase,source_frequency FROM bf_collection_profiles')}
    profiles = []
    for r in register_rows(conn):
        if r['archived']:
            continue
        p = dict(r)
        p.update(profile_id=p['id'], id=p['location_id'], name=p['customer_site'], client_name=p['group_name'],
                 quantities=requirements[p['location_id']], equipment=equipment.get(p['id'], {}))
        p['cadence'], p['days'], _ = parse_frequency({'frequency': p['schedule']})
        p['monthly_rule'] = 'last_weekday' if re.search(r'\blast\b.*\bmonth', p['schedule'], re.I) and p['days'] else ''
        p['flexible'] = not p['days']
        old = saved.get(p['id'], {})
        p['cycle_phase'] = old.get('cycle_phase', 0) if old.get('source_frequency') == p['schedule'] else 0
        p['start_date'] = (START+timedelta(weeks=p['cycle_phase'])).isoformat()
        p['end_date'] = ''
        p['geo_key'] = p['state'] + '|' + p['suburb'].strip().casefold()
        p['large'] = sum(q['quantity'] for q in p['quantities'] if q['litres'] == 1100)
        p['small'] = sum(q['quantity'] for q in p['quantities'] if q['litres'] == 240)
        profiles.append(p)
    return profiles


@lru_cache(maxsize=1)
def geography():
    # All Australian public place records are bundled. Client data never leaves the app.
    value = (Path(__file__).parent/'data'/'au_places.json.gz.b64').read_text()
    return {'points': json.loads(gzip.decompress(base64.b64decode(value)))}


def resources(conn, geo):
    result = planning_resources(conn)
    for state in PRIMARY:
        r = result.setdefault(state, {})
        depot = r.get('depot') or ''
        r['depot'] = depot or 'Starting depot not supplied'
        r['issues'] = []
        points = [k for k in geo['points'] if k.startswith(state+'|') and k.split('|', 1)[1] in depot.casefold()]
        r['depot_key'] = max(points, key=len) if points else None
        if not r['depot_key']:
            r['issues'].append('Depot location estimate unavailable; travel time cannot be checked.')
        if r.get('depot_status') != 'confirmed' or re.search(r'confirm|provisional|suburb only', depot, re.I):
            r['issues'].append('Exact starting depot address requires confirmation.')
        # Warehouse destinations are inventory records, not permission to change the route base.
        warehouses = r.get('warehouses', [])
        if warehouses and not any(r['depot_key'] and r['depot_key'].split('|')[1] in w['address'].casefold() for w in warehouses):
            r['issues'].append('Stock warehouse address differs from the starting depot; confirm loading and return arrangements.')
        truck = r.get('truck') or {}
        r['capacity'] = truck.get('pallet_positions') or 14
        r['small_space'] = (truck.get('spaces') or {}).get('bin240', .5)
        r['weekly_hours'] = r.get('available_weekly_hours')
        r['issues'].append('1100L footprint assumed at one truck position; loaded weights, payload and tailgate limits need checking.')
        if not r['weekly_hours']:
            r['issues'].append('No staff hours available in saved settings; assign staff or a contractor before dispatch.')
    return result


def leg(geo, state, a, b):
    points = geo.get('points', {})
    if a not in points or b not in points:
        return None
    p, q = points[a], points[b]
    lat1, lat2 = math.radians(p['lat']), math.radians(q['lat'])
    dlat, dlon = lat2-lat1, math.radians(q['lon']-p['lon'])
    great_circle = 6371*2*math.asin(min(1, math.sqrt(math.sin(dlat/2)**2+math.cos(lat1)*math.cos(lat2)*math.sin(dlon/2)**2)))
    # A transparent planning estimate, NOT a measured road route: 1.4 distance factor,
    # 35 km/h metro / 65 km/h regional, 25% time buffer, five minutes local access.
    km = great_circle*1.4
    minutes = km/(65 if great_circle>80 else 35)*60*1.25+5
    return max(10, math.ceil(minutes)), round(km, 1)


def split_loads(p, r):
    """Whole containers only; no stacking/nesting credit or fractional large bins."""
    capacity, small_space = r['capacity'], r['small_space']
    if capacity < 1 or small_space <= 0 or small_space > capacity:
        return []
    units = [('large', 1)] * p['large'] + [('small', small_space)] * p['small']
    loads = []
    for kind, space in units:
        if not loads or loads[-1]['spaces']+space > capacity+1e-8:
            loads.append(dict(profile=p, spaces=0, large=0, small=0))
        loads[-1]['spaces'] += space
        loads[-1][kind] += 1
    return loads


def trip_stats(stops, state, r, geo):
    last = r['depot_key']
    drive = km = 0
    for s in stops:
        item = leg(geo, state, last, s['profile']['geo_key'])
        if item is None:
            return None
        drive += item[0]; km += item[1]; last = s['profile']['geo_key']
    home = leg(geo, state, last, r['depot_key'])
    if home is None:
        return None
    drive += home[0]; km += home[1]
    bins = sum(s['large']+s['small'] for s in stops)
    service = sum(10+3*(s['large']+s['small']) for s in stops)
    handling = 20+4*bins  # load empties and unload full bins at depot
    return dict(stops=stops, drive=drive, service=service, handling=handling,
                duration=drive+service+handling, km=round(km, 1),
                spaces=sum(s['spaces'] for s in stops))


def pack(stops, state, r, geo):
    """Cheapest insertion into capacity-limited depot loops, then local order improvement."""
    trips = []
    # Largest / furthest stops first prevents a small stop consuming a full-load route.
    ordered = sorted(stops, key=lambda s: (-s['spaces'], s['profile']['geo_key'], s['profile']['id'] or 0))
    for stop in ordered:
        best = None
        for ti, trip in enumerate(trips):
            if trip['spaces']+stop['spaces'] > r['capacity']+1e-8:
                continue
            for at in range(len(trip['stops'])+1):
                candidate = trip_stats(trip['stops'][:at]+[stop]+trip['stops'][at:], state, r, geo)
                if candidate and candidate['duration']+PREP+BREAK+WAIT <= DAY_LIMIT:
                    score = candidate['duration']-trip['duration']
                    if best is None or score < best[0]:
                        best = (score, ti, candidate)
        single = trip_stats([stop], state, r, geo)
        if single is None:
            return None
        if best is not None and best[0] < single['duration']:
            trips[best[1]] = best[2]
        else:
            trips.append(single)
    # Two-opt the stop order within each load; retain start/return to the same depot.
    for ti, trip in enumerate(trips):
        improved = True
        while improved:
            improved = False
            for a in range(len(trip['stops'])):
                for b in range(a+2, len(trip['stops'])+1):
                    order = trip['stops'][:a]+list(reversed(trip['stops'][a:b]))+trip['stops'][b:]
                    trial = trip_stats(order, state, r, geo)
                    if trial and trial['duration'] < trip['duration']:
                        trip = trial; improved = True
            trips[ti] = trip
    return trips


def due(p, monday):
    """Return allowed weekdays for one service week, or no demand. Fixed days are exact."""
    cadence = p['cadence']
    if monday < START or cadence in ('adhoc', 'unconfirmed', 'paused'):
        return []
    interval = INTERVALS.get(cadence)
    if interval:
        week = (monday-START).days//7
        if week < p.get('cycle_phase', 0) or (week-p.get('cycle_phase', 0)) % interval:
            return []
        return p['days'] or [None]
    if cadence == 'monthly':
        if p['monthly_rule'] == 'last_weekday':
            return [d for d in p['days'] if occurs(p, monday+timedelta(days=d))]
        # Flexible monthly demand falls in the service week containing the 26th.
        for offset in (0, 6):
            sample = monday+timedelta(days=offset)
            target = sample.replace(day=min(START.day, calendar.monthrange(sample.year, sample.month)[1]))
            if monday <= target <= monday+timedelta(days=6):
                return [None]
    return []


def elapsed(trips):
    return sum(t['duration'] for t in trips)+(PREP+BREAK+WAIT if trips else 0)


def clock(minutes):
    return f'{int(minutes)//60:02d}:{int(minutes)%60:02d}'


def week_plan(conn, monday, state='', geo=None, people=None):
    geo = geography() if geo is None else geo
    people = clients(conn) if people is None else people
    settings = resources(conn, geo)
    selected = [state] if state else list(PRIMARY)+['WA', 'ACT', 'TAS', 'NT']
    output = []
    for code in selected:
        ps = [p for p in people if p['state'] == code]
        if not ps and code not in PRIMARY:
            continue
        r = settings.get(code, dict(depot='Local partner depot to confirm', depot_key=None,
                                   capacity=14, small_space=.5, weekly_hours=0,
                                   issues=['Local partner, depot and working hours must be confirmed.']))
        days = [dict(date=(monday+timedelta(days=i)).isoformat(), name=DAYS[i], stops=[], trips=[],
                     exceptions=[], baling=False, minutes=0, jobs=[]) for i in range(7)]
        flexible, issues = [], list(r['issues'])
        fixed_demand = Counter()
        for p in ps:
            for d in due(p, monday):
                if d is not None:
                    fixed_demand[d] += p['large']+p['small']*.5
        # Protect a whole weekday; prefer a midweek baling day when fixed visits permit it.
        baling = min((2, 3, 1, 4, 0), key=lambda d: fixed_demand[d]) if code in PRIMARY else None
        if baling is not None:
            days[baling]['baling'] = True
            days[baling]['minutes'] = 480
            if fixed_demand[baling]:
                issues.append('Fixed client days conflict with the protected baling day; additional crew needed.')
        for p in ps:
            for d in due(p, monday):
                reason = ''
                if not p['id'] or not p['address']:
                    reason = 'Site address or operational location is missing.'
                elif not p['large'] and not p['small']:
                    reason = 'Collection quantity / container type needs confirmation.'
                loads = split_loads(p, r) if not reason else []
                proposal = pack(loads, code, r, geo) if loads else None
                if not reason and proposal is None:
                    reason = 'Suburb or starting depot cannot be matched locally; travel time needs checking.'
                if proposal and elapsed(proposal) > DAY_LIMIT:
                    reason = 'Full site service cannot fit the 06:30–15:30 depot-return day; extra truck or regional partner required.'
                if reason:
                    days[d if d is not None else 0]['exceptions'].append(dict(profile=p, reason=reason, fixed=d is not None))
                elif d is not None:
                    days[d]['stops'].extend(loads)
                else:
                    flexible.append((p, loads))
        # Fixed commitments first, then assign flexible visits by added travel/time and daily slack.
        for day in days:
            day['trips'] = pack(day['stops'], code, r, geo) or []
        flexible.sort(key=lambda pair: (-elapsed(pack(pair[1], code, r, geo) or []), pair[0]['name']))
        for p, loads in flexible:
            best = None
            for d in range(5):
                if days[d]['baling']:
                    continue
                trial = pack(days[d]['stops']+loads, code, r, geo)
                duration = elapsed(trial or [])
                if trial is None or duration > DAY_LIMIT:
                    continue
                added = duration-elapsed(days[d]['trips'])
                score = (added, duration, d)
                if best is None or score < best[0]:
                    best = (score, d, trial)
            if best is None:
                days[0]['exceptions'].append(dict(profile=p, reason='No weekday has enough time with the baling day protected; additional capacity needed.', fixed=False))
            else:
                _, d, trips = best
                days[d]['stops'].extend(loads); days[d]['trips'] = trips
        for day in days:
            day['minutes'] = (480 if day['baling'] else 0)+elapsed(day['trips'])
            day['over_limit'] = day['minutes'] > DAY_LIMIT
            time = 390+PREP+WAIT+BREAK
            for trip in day['trips']:
                trip['start'] = clock(time)
                time += trip['duration']
                trip['end'] = clock(time)
            day['finish'] = clock(390+day['minutes']) if day['minutes'] else ''
            day['profiles'] = list({s['profile']['id']: s['profile'] for s in day['stops']}.values())
            if day['over_limit']:
                issues.append(day['name']+': fixed commitments exceed the day limit; additional truck/crew required.')
        total = sum(d['minutes'] for d in days)
        budget = r.get('weekly_hours')
        if budget is None or total > budget*60:
            issues.append(f'Weekly plan requires {total/60:.1f} hours including baling; saved availability is {budget or 0:g} hours. Additional staffing needed.')
        output.append(dict(state=code, resource=r, days=days, minutes=total, issues=list(dict.fromkeys(issues)),
                           staff_shortfall=budget is None or total > budget*60,
                           sites=len(ps), baling_day=DAYS[baling] if baling is not None else '',
                           adhoc=[p for p in ps if p['cadence']=='adhoc'],
                           exceptions=sum(len(d['exceptions']) for d in days)))
    # Actual booked work remains visible and does not silently disappear in a reset.
    for row in conn.execute("SELECT id,code,state,scheduled_date,status FROM bf_jobs WHERE scheduled_date BETWEEN ? AND ? AND status!='cancelled'",
                            (monday.isoformat(), (monday+timedelta(days=6)).isoformat())):
        for group in output:
            if group['state'] == row['state']:
                day = next(d for d in group['days'] if d['date']==row['scheduled_date'])
                day['jobs'].append(dict(row))
                if 'Existing jobs need reconciliation with this proposal before dispatch.' not in group['issues']:
                    group['issues'].append('Existing jobs need reconciliation with this proposal before dispatch.')
    return output


def balance_cycles(conn, people, geo):
    """Stagger flexible fortnightly suburb groups; retain fixed weekday commitments."""
    grouped = defaultdict(list)
    for p in people:
        p['cycle_phase'] = 0
        if p['cadence']=='fortnightly':
            grouped[p['state'], p['geo_key']].append(p)
    def score(state):
        result = [week_plan(conn, START+timedelta(weeks=w), state, geo, people)[0] for w in (0,1)]
        return (sum(r['exceptions'] for r in result),
                sum(max(0,r['minutes']-(r['resource'].get('weekly_hours') or 0)*60) for r in result),
                sum(r['minutes'] for r in result), max(r['minutes'] for r in result))
    for (state, _), group in sorted(grouped.items(), key=lambda item: -sum(p['large']+p['small']*.5 for p in item[1])):
        if state not in PRIMARY:
            continue
        before = score(state)
        for p in group: p['cycle_phase'] = 1
        if score(state) >= before:
            for p in group: p['cycle_phase'] = 0
    for p in people:
        p['start_date'] = (START+timedelta(weeks=p['cycle_phase'])).isoformat()


def rebuild(conn, stamp):
    done = conn.execute('SELECT summary FROM bf_calendar_rebuilds WHERE id=?', (RESET_ID,)).fetchone()
    if done:
        return json.loads(done['summary']), False
    if not conn.execute('SELECT 1 FROM bf_allocation_workbook_rows WHERE active=1').fetchone():
        return None, False
    people = clients(conn)
    geo = geography()
    balance_cycles(conn, people, geo)
    initial = week_plan(conn, START, geo=geo, people=people)
    assigned = {}
    for state in initial:
        for d, day in enumerate(state['days']):
            for p in day['profiles']:
                assigned.setdefault(p['id'], []).append(d)
    conn.execute('BEGIN IMMEDIATE')
    try:
        previous = [dict(r) for r in conn.execute('SELECT * FROM bf_collection_profiles')]
        for old in previous:
            old.update(version=old['version']+1, active=0, cadence='paused', end_date='2026-10-25',
                       updated_at=stamp, updated_by='Current client calendar rebuild')
            conn.execute("UPDATE bf_collection_profiles SET version=?,active=0,cadence='paused',end_date='2026-10-25',updated_at=?,updated_by=? WHERE location_id=?",
                         (old['version'],stamp,old['updated_by'],old['location_id']))
            record_profile(conn, old, old['updated_by'], stamp)
        for p in people:
            if not p['id']:
                continue
            old = conn.execute('SELECT version FROM bf_collection_profiles WHERE location_id=?', (p['id'],)).fetchone()
            warehouses = conn.execute("SELECT id FROM bf_locations WHERE kind='warehouse' AND state=?", (p['state'],)).fetchall()
            values = dict(location_id=p['id'], version=(old['version'] if old else 0)+1,
                cadence=p['cadence'], service_days=json.dumps(p['days'] or assigned.get(p['id'], [])),
                start_date=p['start_date'] if p['cadence'] in (*INTERVALS,'monthly') else '', end_date='',
                warehouse_id=warehouses[0]['id'] if len(warehouses)==1 else None, confirmed=0,
                notes='Rebuilt from the current client profile. Flexible weekdays are assigned by the weekly route planner.',
                review_note='Proposed service from 26 October; confirm receiving windows, access, vehicle and staff before dispatch.',
                source_frequency=p['schedule'], source_revision=p['version'],
                source_snapshot=json.dumps(dict(profile_id=p['profile_id'], schedule=p['schedule'])),
                site_kind='collection', updated_at=stamp, updated_by='Current client calendar rebuild',
                active=1, monthly_rule=p['monthly_rule'], flexible=int(p['flexible']),cycle_phase=p['cycle_phase'])
            conn.execute('INSERT INTO bf_collection_profiles('+','.join(values)+') VALUES('+','.join('?' for _ in values)+') ON CONFLICT(location_id) DO UPDATE SET '+
                         ','.join(k+'=excluded.'+k for k in values if k!='location_id'), tuple(values.values()))
            record_profile(conn, values, values['updated_by'], stamp)
        summary = dict(start=START.isoformat(), current=len(people), linked=sum(bool(p['id']) for p in people),
                       missing_location=sum(not p['id'] for p in people), retired=len(previous),
                       cadences=dict(Counter(p['cadence'] for p in people)),
                       first_week=[dict(state=r['state'], hours=round(r['minutes']/60,1), exceptions=r['exceptions'], baling=r['baling_day']) for r in initial])
        conn.execute('INSERT INTO bf_calendar_rebuilds VALUES(?,?,?,?,?)',
                     (RESET_ID, START.isoformat(), json.dumps(previous), json.dumps(summary), stamp))
        conn.commit()
        return summary, True
    except Exception:
        conn.rollback()
        raise


def view_data(conn, start, end, state):
    people = clients(conn); geo = geography()
    weeks = []
    monday = start-timedelta(days=start.weekday())
    while monday <= end:
        if monday >= START:
            weeks.append(dict(start=monday, states=week_plan(conn, monday, state, geo, people)))
        monday += timedelta(weeks=1)
    # Saved state weeks are authoritative. Never regenerate over an admin's edits.
    from binflow_schedule import saved_week
    for week in weeks:
        for region in ((state,) if state else ('QLD','NSW','VIC','SA','WA','TAS','ACT','NT')):
            saved = saved_week(conn, region, week['start'].isoformat())
            if not saved:
                continue
            plan = json.loads(saved['plan_json'])
            days = [dict(date=d['date'], name=date.fromisoformat(d['date']).strftime('%A'),
                profiles=[], trips=[], jobs=[], exceptions=[], baling=any(i['kind']=='baling' for i in d['items']),
                minutes=d['estimate_minutes'] if d['estimate_valid'] else 0, over_limit=False,
                saved_items=d['items'], saved_notes=d['notes'], saved_status=saved['status']) for d in plan['days']]
            group = dict(state=region, resource=plan['resource'], days=days,
                minutes=sum(d['minutes'] for d in days), staff_shortfall=False, baling_day='',
                issues=['Saved calendar plan. Expand each day to check edited routes and working hours.'],
                saved_status=saved['status'], driver_name=saved['driver_name'], saved_start=week['start'].isoformat())
            week['states'] = [g for g in week['states'] if g['state']!=region] + [group]
    filtered = [p for p in people if not state or p['state']==state]
    events = defaultdict(list)
    backlog = []
    for week in weeks:
        awaiting = []
        for group in week['states']:
            for day in group['days']:
                fixed = [item for item in day['exceptions'] if item['fixed']]
                awaiting.extend(dict(item, state=group['state']) for item in day['exceptions'] if not item['fixed'])
                if day.get('saved_items') or day['trips'] or day['baling'] or day['jobs'] or fixed:
                    events[day['date']].append(dict(group=group, day=dict(day, exceptions=fixed),
                        dialog_id='route-'+group['state']+'-'+day['date']))
        if awaiting:
            backlog.append(dict(start=week['start'], items=awaiting))
    grid = [[start+timedelta(days=n+d) for d in range(7)] for n in range(0,(end-start).days+1,7)]
    return dict(route_weeks=weeks, route_clients=filtered, route_start=START,
                calendar_weeks=grid, calendar_events=events, calendar_backlog=backlog,
                calendar_unassigned=sum(len(w['items']) for w in backlog),
                route_pending=[p for p in people if not p['id'] or not p['state']],
                route_undated=[p for p in filtered if p['cadence'] in ('adhoc','unconfirmed','paused')],
                route_counts=dict(Counter(p['state'] for p in people)))
