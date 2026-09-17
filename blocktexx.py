"""Private, admin-only collection modelling. No customer data ships in source."""
from blocktexx_costs import validate_cost_profile, cost_comparison
from blocktexx_interstate import validate_interstate, interstate_summary
import csv
import io
import json
import math
import os
import base64
import gzip
from datetime import datetime, timezone
from flask import abort, g, render_template, request, Response
from blocktexx_capacity import KINDS, DEFAULT_SPACES, infer_containers, capacity_plans, load_sequence
import re

STATES = ('QLD', 'NSW', 'VIC', 'SA')
STAGES = ('Collections', 'Deliver to decom', 'Return from decom', 'Consolidation', 'Deliver to Threadtexx')


def source_visits(frequency):
    value = frequency.strip().lower()
    return {'weekly': 4, 'fortnightly': 2, 'every 4 weeks': 1, 'every 8 weeks': .5,
            'tue fri - weekly': 8, 'mon wed fri - weekly': 12}.get(value)


def empty_model():
    return {'version': 1, 'name': 'BlockTexx collection analysis', 'source': '',
            'notes': 'Stage 1 only. Shredding and downstream transport are not costed.',
            'states': {s: {'depot': '', 'depot_status': 'unconfirmed', 'monthly_kg': None,
                           'cost_mode': 'unpriced', 'hourly_rate': None, 'minimum_hours': 0,
                           'fixed_monthly': None, 'available_weekly_hours': 40,
                           'notes': '', 'runs': []} for s in STATES},
            'sites': [], 'partners': []}


def number(value, label, maximum, optional=False):
    if value is None or value == '':
        if optional:
            return None
        raise ValueError(f'{label} is required.')
    if isinstance(value, bool):
        raise ValueError(f'{label} must be a number.')
    try:
        n = float(value)
    except (ValueError, TypeError):
        raise ValueError(f'{label} must be a number.') from None
    if not math.isfinite(n) or not 0 <= n <= maximum:
        raise ValueError(f'{label} must be between 0 and {maximum}.')
    return n


def text(value, label, maximum=2000):
    if not isinstance(value, str) or len(value) > maximum:
        raise ValueError(f'{label} must be text up to {maximum} characters.')
    return value.strip()


def validate_model(value):
    if not isinstance(value, dict) or value.get('version') != 1 or set(value.get('states', {})) != set(STATES):
        raise ValueError('Use a version 1 model containing QLD, NSW, VIC and SA.')
    model = empty_model()
    model['capacity_version'] = int(number(value.get('capacity_version', 0), 'Capacity model version', 1))
    for key in ('name', 'source', 'notes'):
        model[key] = text(value.get(key, ''), key)
    model['interstate'] = validate_interstate(value.get('interstate', {}), number, text)
    seen = set()
    for key in ('sites', 'partners'):
        rows = value.get(key, [])
        if not isinstance(rows, list) or len(rows) > 500:
            raise ValueError('Too many source locations.')
        for row in rows:
            if not isinstance(row, dict) or row.get('state') not in STATES:
                raise ValueError('Each location needs a valid state.')
            item = {k: text(row.get(k, ''), k) for k in ('id', 'name', 'address', 'frequency', 'equipment', 'source_rows', 'notes')}
            item['state'] = row['state']
            if key == 'sites':
                item['source_frequency'] = text(row.get('source_frequency', item['frequency']), 'Source frequency')
                item['visits_4w'] = number(row.get('visits_4w', source_visits(item['frequency'])), 'Visits per four weeks', 124, True)
                source_days = [i for i, day in enumerate(('mon','tue','wed','thu','fri','sat','sun')) if re.search(r'\b'+day+r'(?:day|sday|nesday|rsday|urday)?\b', item['source_frequency'], re.I)]
                item['day_rule'] = row.get('day_rule', 'fixed' if source_days else 'unknown')
                if item['day_rule'] not in ('unknown','flexible','fixed'):
                    raise ValueError('Invalid customer day rule.')
                days = row.get('service_days', source_days)
                if not isinstance(days, list) or any(isinstance(d,bool) or not isinstance(d,int) or d not in range(7) for d in days):
                    raise ValueError('Customer service days must be weekdays 0–6.')
                item['service_days'] = sorted(set(days))
                if item['day_rule']=='fixed' and not days:
                    raise ValueError('Select at least one fixed customer day.')
                address_parts=item['address'].split(',')
                item['service_area'] = text(row.get('service_area', address_parts[-2].strip() if len(address_parts)>1 else ''), 'Collection area', 100)
                containers = row.get('containers', infer_containers(item['equipment']))
                item['containers'] = {}
                for kind in KINDS:
                    qty = number(containers.get(kind, 0), 'Container quantity', 500)
                    if qty != int(qty):
                        raise ValueError('Container quantities must be whole numbers.')
                    item['containers'][kind] = int(qty)
            if not item['id'] or item['id'] in seen:
                raise ValueError('Location IDs must be unique and nonempty.')
            seen.add(item['id'])
            model[key].append(item)
    site_states = {s['id']: s['state'] for s in model['sites']}
    for state in STATES:
        data = value['states'][state]
        if not isinstance(data, dict):
            raise ValueError('Invalid state settings.')
        out = model['states'][state]
        if 'cost_profile' in data:
            out['cost_profile'] = validate_cost_profile(data['cost_profile'])
        pricing = data.get('resource_pricing', {})
        if not isinstance(pricing, dict):
            raise ValueError('Resource pricing must contain model profiles.')
        out['resource_pricing'] = {}
        for kind in KINDS:
            profile = pricing.get(kind, {})
            if not isinstance(profile, dict):
                raise ValueError('Invalid resource price profile.')
            rental_qty = number(profile.get('rental_qty'), 'Rental quantity', 1000000, True)
            if rental_qty is not None and rental_qty != int(rental_qty):
                raise ValueError('Rental quantities must be whole numbers.')
            out['resource_pricing'][kind] = {
                'purchase_each': number(profile.get('purchase_each'), 'Purchase cost per container', 1000000, True),
                'weekly_rent_each': number(profile.get('weekly_rent_each'), 'Weekly rental per container', 1000000, True),
                'rental_qty': rental_qty}
        truck = data.get('truck', {})
        positions = number(truck.get('pallet_positions', 14), 'Pallet positions', 40)
        if positions < 1:
            raise ValueError('Truck must have at least one pallet position.')
        out['truck'] = {'pallet_positions': positions, 'payload_kg': number(truck.get('payload_kg'), 'Usable payload kg', 100000, True), 'spaces':{}, 'weights_kg':{}}
        for kind in KINDS:
            space = number(truck.get('spaces', {}).get(kind, DEFAULT_SPACES[kind]), 'Positions per container', 40)
            if space <= 0:
                raise ValueError('Positions per container must be greater than zero.')
            out['truck']['spaces'][kind] = space
            out['truck']['weights_kg'][kind] = number(truck.get('weights_kg', {}).get(kind), 'Loaded container weight', 100000, True)
        for key in ('depot', 'notes'):
            out[key] = text(data.get(key, ''), key)
        if data.get('depot_status') not in ('unconfirmed', 'assumed', 'confirmed'):
            raise ValueError('Invalid depot status.')
        out['depot_status'] = data['depot_status']
        if data.get('cost_mode') not in ('unpriced', 'contractor', 'owned'):
            raise ValueError('Invalid cost mode.')
        out['cost_mode'] = data['cost_mode']
        for key, limit, optional in [('monthly_kg', 10000000, True), ('hourly_rate', 10000, True),
                                     ('minimum_hours', 24, False), ('fixed_monthly', 10000000, True),
                                     ('available_weekly_hours', 168, False)]:
            out[key] = number(data.get(key), key, limit, optional)
        runs = data.get('runs', [])
        if not isinstance(runs, list) or len(runs) > 200:
            raise ValueError('Use at most 200 run rows per state.')
        run_ids = set()
        for run in runs:
            if not isinstance(run, dict):
                raise ValueError('Invalid run.')
            r = {k: text(run.get(k, ''), k) for k in ('id', 'name', 'sequence', 'original_sequence', 'notes', 'evidence')}
            if not r['id'] or r['id'] in run_ids:
                raise ValueError('Run IDs must be unique within the state.')
            run_ids.add(r['id'])
            if run.get('status') not in ('estimated', 'verified', 'unmeasured'):
                raise ValueError('Invalid measurement status.')
            r['status'] = run['status']
            r['activity_type'] = run.get('activity_type', 'collection')
            if r['activity_type'] not in ('collection', 'deliver_decomm', 'collect_decomm', 'deliver_threadtexx', 'deliver_blocktexx', 'return_storage'):
                raise ValueError('Invalid local activity type.')
            for field in ('partner_id', 'origin', 'destination', 'cargo'):
                r[field] = text(run.get(field, ''), field)
            r['movement_kg'] = number(run.get('movement_kg'), 'Movement kg', 1000000, True)
            if r['activity_type'] != 'collection':
                if not r['origin'] or not r['destination']:
                    raise ValueError('Local movements need an origin and destination.')
                if r['activity_type'] in ('deliver_decomm', 'collect_decomm') and not r['partner_id']:
                    raise ValueError('Select a decomm partner for this movement.')
                if r['partner_id'] and not any(p['id'] == r['partner_id'] and p['state'] == state for p in model['partners']):
                    raise ValueError('Choose a decomm partner in this state.')

            planner_frequency = run.get('planner_frequency')
            if planner_frequency is not None:
                if planner_frequency not in ('Weekly', 'Fortnightly', 'Monthly', 'Ad hoc'):
                    raise ValueError('Invalid planner frequency.')
                r['planner_frequency'] = planner_frequency
            slots = run.get('planner_slots')
            if slots is not None:
                if not isinstance(slots, list) or len(slots) > 124:
                    raise ValueError('Use at most 124 planner allocations per run.')
                normalized = []
                for slot in slots:
                    if not isinstance(slot, dict):
                        raise ValueError('Invalid planner allocation.')
                    week = number(slot.get('week'), 'Planner week', 4)
                    day = number(slot.get('day'), 'Planner day', 6)
                    if week < 1 or week != int(week) or day != int(day):
                        raise ValueError('Planner weeks must be 1–4 and days 0–6.')
                    item = {'week': int(week), 'day': int(day)}
                    if slot.get('overtime_limit_min') is not None:
                        limit = number(slot['overtime_limit_min'], 'Approved overtime day minutes', 10080)
                        if limit <= 540:
                            raise ValueError('Overtime approval must exceed 540 minutes.')
                        item['overtime_limit_min'] = limit
                    normalized.append(item)
                r['planner_slots'] = normalized

            r['included_loads'] = number(run.get('included_loads', max(1,len(re.findall(r'\bdepot\b',r['sequence'], re.I))-1)), 'Loads already included in time and km', 500)
            if r['included_loads'] < 1 or r['included_loads'] != int(r['included_loads']):
                raise ValueError('Included loads must be a whole number of at least one.')
            for key, limit in [('runs_4w', 124), ('km', 20000), ('drive_min', 10080),
                               ('service_min', 10080), ('depot_min', 10080), ('prep_min', 1440),
                               ('wait_min', 10080), ('break_min', 1440)]:
                r[key] = number(run.get(key), key, limit, True)
            if r['status'] == 'verified' and (not r['evidence'] or any(r[k] is None for k in ('km', 'drive_min'))):
                raise ValueError('Verified runs need distance, driving time and a measurement source/date.')
            ids = run.get('site_ids', [])
            if not isinstance(ids, list) or len(ids) > 100 or any(not isinstance(i, str) or site_states.get(i) != state for i in ids):
                raise ValueError('Run locations must belong to that state’s customer register.')
            if r['activity_type'] != 'collection' and ids:
                raise ValueError('Downstream movements cannot count as customer collections.')
            r['site_ids'] = list(dict.fromkeys(ids))
            out['runs'].append(r)
    return model


DAY_TIME_KEYS = ('drive_min', 'service_min', 'depot_min', 'prep_min', 'wait_min', 'break_min')
DAY_NAMES = ('Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday')


def calendar_slots(run):
    if isinstance(run.get('planner_slots'), list):
        return run['planner_slots']
    day = next((i for i, name in enumerate(DAY_NAMES) if re.search(r'\b' + name + r'\b', run['name'], re.I)), None)
    if day is None or not run.get('runs_4w'):
        return []
    week = re.search(r'\bWeek\s+([1-4])\b', run['name'], re.I)
    if week and run['runs_4w'] == 1:
        return [dict(week=int(week[1]), day=day)]
    if not week and run['runs_4w'] == 4:
        return [dict(week=w, day=day) for w in range(1, 5)]
    return []


def check_calendar_limits(model, previous):
    def days(data):
        result = {}
        for run in data['runs']:
            values = tuple(run.get(k) for k in DAY_TIME_KEYS)
            duration = None if None in values else sum(values)
            for slot in calendar_slots(run):
                result.setdefault((slot['week'], slot['day']), []).append((run['id'], duration))
        return result

    for state in STATES:
        old_days = days(previous['states'][state])
        for (week, day), entries in days(model['states'][state]).items():
            unknown = any(minutes is None for _, minutes in entries)
            total = sum(minutes or 0 for _, minutes in entries)
            approved = max([540] + [slot.get('overtime_limit_min', 540)
                for run in model['states'][state]['runs'] for slot in calendar_slots(run)
                if slot['week'] == week and slot['day'] == day])
            if not unknown and total <= approved:
                continue
            # Retain or reduce existing problem days without preventing unrelated saves.
            remaining = list(old_days.get((week, day), []))
            unchanged_or_reduced = True
            for run_id, minutes in entries:
                match = next((i for i, (old_id, old_minutes) in enumerate(remaining)
                              if old_id == run_id and (old_minutes is None or
                                 (minutes is not None and minutes <= old_minutes))), None)
                if match is None:
                    unchanged_or_reduced = False
                    break
                remaining.pop(match)
            if unchanged_or_reduced:
                continue
            reason = 'run times are incomplete' if unknown else f'{total:g} minutes exceeds the 540-minute limit'
            raise ValueError(f'{state} Week {week} {DAY_NAMES[day]}: {reason}. '
                             'Working day is 6:30 am–3:30 pm, including handling and breaks. '
                             'Reduce or move the allocation before saving.')


def summarize(model):
    plans = capacity_plans(model)
    result = {}
    for state, data in model['states'].items():
        km = work = billed = elapsed = 0
        gaps = []
        active = []
        for run in data['runs']:
            count = run['runs_4w']
            if count is None and run.get('activity_type', 'collection') != 'collection' and calendar_slots(run):
                count = len(calendar_slots(run))
            if count is None:
                gaps.append(run['name'] + ': frequency missing')
                continue
            if count == 0:
                continue
            active.append(run)
            plan = plans.get(state, {}).get(run['id'])
            if plan and (plan['issues'] or plan['extra_loads']):
                gaps.append(run['name'] + ': capacity plan needs quantities or extra-trip distance/time')
            fields = ('drive_min', 'service_min', 'depot_min', 'prep_min', 'wait_min')
            if run['km'] is not None:
                km += run['km'] * count
            if run['km'] is None or any(run[k] is None for k in fields) or run['break_min'] is None:
                gaps.append(run['name'] + ': measurements missing')
            if all(run[k] is not None for k in fields):
                h = sum(run[k] for k in fields) / 60
                work += h * count
                # Each run row is one contractor attendance/day; minimum applies once.
                billed += max(h, data['minimum_hours']) * count
                elapsed += (h + (run['break_min'] or 0) / 60) * count
        factor = 13 / 12
        assigned = {i for r in data['runs'] if r['runs_4w'] != 0 for i in r['site_ids']}
        gaps.extend(s['name'] + ': no run assigned' for s in model['sites'] if s['state'] == state and s['id'] not in assigned)
        for site in (s for s in model['sites'] if s['state'] == state):
            required = site.get('visits_4w', source_visits(site['frequency']))
            planned = sum(r['runs_4w'] or 0 for r in data['runs'] if site['id'] in r['site_ids'])
            if required is not None and abs(required - planned) > .001:
                gaps.append(site['name'] + ': customer frequency differs from route plan')
        cost = None
        if active:
            if data['cost_mode'] == 'contractor' and data['hourly_rate'] is not None:
                cost = billed * factor * data['hourly_rate']
            elif data['cost_mode'] == 'owned' and data['fixed_monthly'] is not None:
                cost = data['fixed_monthly']
        comparison = cost_comparison(data, calendar_slots) if data.get('cost_profile', {}).get('enabled') else None
        if comparison:
            cost = comparison['selected']
            if not comparison['schedule_complete']:
                gaps.append('Cost comparison: calendar allocation or day times incomplete')
        kg = data['monthly_kg']
        result[state] = {'km_4w': km, 'work_4w': work, 'billed_4w': billed, 'elapsed_4w': elapsed,
                         'monthly_km': km * factor, 'monthly_work': work * factor,
                         'cost_comparison': comparison, 'monthly_cost': cost, 'collection_per_kg': cost / kg if cost is not None and kg and not gaps else None,
                         'spare_4w': data['available_weekly_hours'] * 4 - work,
                         'gaps': gaps, 'estimated': sum(r['status'] != 'verified' for r in active),
                         'active_runs': len(active)}
    return result


def register_blocktexx(app, db, require):
    with app.app_context():
        db().executescript('''
            CREATE TABLE IF NOT EXISTS blocktexx_model (
              id INTEGER PRIMARY KEY CHECK(id=1), revision INTEGER NOT NULL,
              data TEXT NOT NULL, updated_by INTEGER NOT NULL REFERENCES users(id), updated_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS blocktexx_model_history (
              revision INTEGER PRIMARY KEY, data TEXT NOT NULL,
              updated_by INTEGER NOT NULL REFERENCES users(id), updated_at TEXT NOT NULL);
        ''')
        # Private deployment data is supplied through Render configuration, never public source.
        seed = os.environ.get('BLOCKTEXX_INITIAL_MODEL_GZIP_B64')
        if seed:
            model = validate_model(json.loads(gzip.decompress(base64.b64decode(seed, validate=True))))
            db().execute('BEGIN IMMEDIATE')
            existing = db().execute('SELECT * FROM blocktexx_model WHERE id=1').fetchone()
            if existing and json.loads(existing['data']).get('sites'):
                db().rollback()
                app.logger.warning('BlockTexx bootstrap: existing populated model retained; no edits overwritten.')
            else:
                admin = db().execute("SELECT id FROM users WHERE role='admin' ORDER BY id LIMIT 1").fetchone()
                if not admin:
                    db().rollback()
                    raise RuntimeError('BlockTexx initialization requires an existing administrator.')
                revision = existing['revision'] + 1 if existing else 1
                saved = datetime.now(timezone.utc).isoformat()
                payload = json.dumps(model, allow_nan=False)
                db().execute('INSERT OR REPLACE INTO blocktexx_model VALUES(1,?,?,?,?)', (revision, payload, admin['id'], saved))
                db().execute('INSERT INTO blocktexx_model_history VALUES(?,?,?,?)', (revision, payload, admin['id'], saved))
                db().commit()
                app.logger.warning('BlockTexx bootstrap: imported %s collection sites across QLD, NSW, VIC and SA.', len(model['sites']))

        # One-time, audited capacity regrouping of the already saved collection model.
        db().execute('BEGIN IMMEDIATE')
        saved_model = db().execute('SELECT * FROM blocktexx_model WHERE id=1').fetchone()
        if saved_model and json.loads(saved_model['data']).get('capacity_version', 0) == 0:
            upgraded = validate_model(json.loads(saved_model['data']))
            plans = capacity_plans(upgraded)
            changed = 0
            for state in ('NSW', 'QLD'):
                for run in upgraded['states'][state]['runs']:
                    plan = plans[state][run['id']]
                    if plan['issues'] or not plan['loads']:
                        continue
                    run['original_sequence'] = run['sequence']
                    run['sequence'] = load_sequence(plan)
                    run['included_loads'] = plan['load_count']
                    run['status'] = 'estimated'
                    run['evidence'] = '14-position load regrouping. Existing distance/time allowances retained where trip count fits; remeasure changed stop order. No road navigation validation.'
                    if plan['extra_loads']:
                        run['km'] = run['drive_min'] = run['depot_min'] = None
                    changed += 1
            upgraded['capacity_version'] = 1
            payload = json.dumps(validate_model(upgraded), allow_nan=False)
            revision = saved_model['revision'] + 1
            now = datetime.now(timezone.utc).isoformat()
            db().execute('UPDATE blocktexx_model SET revision=?,data=?,updated_at=? WHERE id=1', (revision,payload,now))
            db().execute('INSERT INTO blocktexx_model_history VALUES(?,?,?,?)', (revision,payload,saved_model['updated_by'],now))
            db().commit()
            app.logger.warning('BlockTexx capacity: updated %s NSW/QLD run sequences for 14-position trucks; previous model retained in revision history.', changed)
        else:
            db().rollback()

    # Apply an explicitly configured, one-time depot correction to the saved model.
    # Operational addresses remain in private deployment configuration, not source.
    depot_update = os.environ.get('BLOCKTEXX_DEPOT_UPDATE_JSON')
    if depot_update:
        update = json.loads(depot_update)
        update_id = text(update.get('id', ''), 'Depot update ID', 100)
        state = update.get('state')
        depot = text(update.get('depot', ''), 'Depot', 2000)
        if not update_id or state not in STATES or not depot:
            raise ValueError('Depot update needs an ID, valid state and depot.')
        with app.app_context():
            db().execute('CREATE TABLE IF NOT EXISTS blocktexx_applied_updates (id TEXT PRIMARY KEY, applied_at TEXT NOT NULL)')
            db().commit()
            db().execute('BEGIN IMMEDIATE')
            done = db().execute('SELECT id FROM blocktexx_applied_updates WHERE id=?', (update_id,)).fetchone()
            saved_model = db().execute('SELECT * FROM blocktexx_model WHERE id=1').fetchone()
            if done or not saved_model:
                db().rollback()
            else:
                updated = json.loads(saved_model['data'])
                updated['states'][state]['depot'] = depot
                updated['states'][state]['depot_status'] = 'confirmed'
                for run in updated['states'][state]['runs']:
                    if run.get('status') == 'verified':
                        run['status'] = 'estimated'
                    note = 'Exact depot address confirmed; recheck distance and driving allowances.'
                    run['evidence'] = (run.get('evidence', '')[:1900] + ' ' + note).strip()
                payload = json.dumps(validate_model(updated), allow_nan=False)
                revision = saved_model['revision'] + 1
                now = datetime.now(timezone.utc).isoformat()
                db().execute('UPDATE blocktexx_model SET revision=?,data=?,updated_at=? WHERE id=1', (revision,payload,now))
                db().execute('INSERT INTO blocktexx_model_history VALUES(?,?,?,?)', (revision,payload,saved_model['updated_by'],now))
                db().execute('INSERT INTO blocktexx_applied_updates VALUES(?,?)', (update_id,now))
                db().commit()
                app.logger.warning('BlockTexx depot update: %s applied to %s; saved revision %s with history retained.', update_id,state,revision)

    def current():
        row = db().execute('SELECT * FROM blocktexx_model WHERE id=1').fetchone()
        return (validate_model(json.loads(row['data'])), row['revision'], row['updated_at']) if row else (empty_model(), 0, None)

    @app.get('/admin/blocktexx')
    @require('admin')
    def admin_blocktexx():
        model, revision, saved = current()
        return render_template('blocktexx.html', tab='blocktexx', model=model, revision=revision,
                               saved=saved, stages=STAGES, summary=summarize(model))

    @app.post('/admin/blocktexx/capacity')
    @require('admin')
    def preview_blocktexx_capacity():
        try:
            raw = request.form.get('model', '')
            if len(raw.encode()) > 900000:
                raise ValueError('Model is too large.')
            model = validate_model(json.loads(raw))
            return {'ok': True, 'plans': capacity_plans(model)}
        except (ValueError, TypeError, KeyError, AttributeError, OverflowError) as exc:
            return {'ok': False, 'error': str(exc) if isinstance(exc, ValueError) else 'Invalid capacity model.'}, 400

    @app.post('/admin/blocktexx')
    @require('admin')
    def save_blocktexx():
        try:
            raw = request.form.get('model', '')
            if len(raw.encode()) > 900000:
                raise ValueError('Model is too large.')
            model = validate_model(json.loads(raw))
            revision = int(request.form.get('revision', '-1'))
        except (ValueError, TypeError, KeyError, AttributeError, OverflowError) as exc:
            return {'ok': False, 'error': str(exc) if isinstance(exc, ValueError) else 'Invalid model structure.'}, 400
        db().execute('BEGIN IMMEDIATE')
        previous, actual, _ = current()
        if revision != actual:
            db().rollback()
            return {'ok': False, 'error': 'Another administrator saved changes. Export your draft, then reload and compare.'}, 409
        try:
            check_calendar_limits(model, previous)
        except ValueError as exc:
            db().rollback()
            return {'ok': False, 'error': str(exc)}, 400
        saved = datetime.now(timezone.utc).isoformat()
        payload = json.dumps(model, allow_nan=False)
        db().execute('INSERT OR REPLACE INTO blocktexx_model VALUES(1,?,?,?,?)',
                     (actual + 1, payload, g.user['id'], saved))
        db().execute('INSERT INTO blocktexx_model_history VALUES(?,?,?,?)',
                     (actual + 1, payload, g.user['id'], saved))
        db().commit()
        return {'ok': True, 'revision': actual + 1, 'saved': saved, 'summary': summarize(model), 'interstate_summary': interstate_summary(model['interstate'])}

    @app.post('/admin/blocktexx/validate')
    @require('admin')
    def validate_blocktexx():
        try:
            raw = request.form.get('model', '')
            if len(raw.encode()) > 900000:
                raise ValueError('Model is too large.')
            return {'ok': True, 'model': validate_model(json.loads(raw))}
        except (ValueError, TypeError, KeyError, AttributeError, OverflowError) as exc:
            return {'ok': False, 'error': str(exc) if isinstance(exc, ValueError) else 'Invalid model structure.'}, 400

    @app.get('/admin/blocktexx/export')
    @require('admin')
    def export_blocktexx():
        model, _, _ = current()
        if request.args.get('format') != 'csv':
            return Response(json.dumps(model, indent=2), mimetype='application/json',
                            headers={'Content-Disposition': 'attachment; filename=BlockTexx-model.json'})
        stream = io.StringIO()
        writer = csv.writer(stream)
        writer.writerow(['State', 'Depot', 'Run', 'Sequence', 'Runs per 4 weeks', 'Km per run',
                         'Drive minutes', 'Customer minutes', 'Depot minutes', 'Prep minutes',
                         'Wait minutes', 'Break minutes', 'Status', 'Evidence', 'Notes'])
        def safe(value):
            if isinstance(value, str) and value.lstrip().startswith(('=', '+', '-', '@')):
                return "'" + value
            return value
        for state, data in model['states'].items():
            for r in data['runs']:
                writer.writerow([safe(v) for v in [state, data['depot'], r['name'], r['sequence'],
                                  r['runs_4w'], r['km'], r['drive_min'], r['service_min'], r['depot_min'],
                                  r['prep_min'], r['wait_min'], r['break_min'], r['status'], r['evidence'], r['notes']]])
        return Response(stream.getvalue(), mimetype='text/csv',
                        headers={'Content-Disposition': 'attachment; filename=BlockTexx-collection-runs.csv'})
