"""Private, admin-only collection modelling. No customer data ships in source."""
import csv
import io
import json
import math
from datetime import datetime, timezone
from flask import abort, g, render_template, request, Response

STATES = ('QLD', 'NSW', 'VIC', 'SA')
STAGES = ('Collections', 'Deliver to decom', 'Return from decom', 'Consolidation', 'Deliver to Threadtexx')


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
    for key in ('name', 'source', 'notes'):
        model[key] = text(value.get(key, ''), key)
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
            r = {k: text(run.get(k, ''), k) for k in ('id', 'name', 'sequence', 'notes', 'evidence')}
            if not r['id'] or r['id'] in run_ids:
                raise ValueError('Run IDs must be unique within the state.')
            run_ids.add(r['id'])
            if run.get('status') not in ('estimated', 'verified', 'unmeasured'):
                raise ValueError('Invalid measurement status.')
            r['status'] = run['status']
            for key, limit in [('runs_4w', 124), ('km', 20000), ('drive_min', 10080),
                               ('service_min', 10080), ('depot_min', 10080), ('prep_min', 1440),
                               ('wait_min', 10080), ('break_min', 1440)]:
                r[key] = number(run.get(key), key, limit, True)
            if r['status'] == 'verified' and (not r['evidence'] or any(r[k] is None for k in ('km', 'drive_min'))):
                raise ValueError('Verified runs need distance, driving time and a measurement source/date.')
            ids = run.get('site_ids', [])
            if not isinstance(ids, list) or len(ids) > 100 or any(not isinstance(i, str) or site_states.get(i) != state for i in ids):
                raise ValueError('Run locations must belong to that state’s customer register.')
            r['site_ids'] = list(dict.fromkeys(ids))
            out['runs'].append(r)
    return model


def summarize(model):
    result = {}
    for state, data in model['states'].items():
        km = work = billed = elapsed = 0
        gaps = []
        active = []
        for run in data['runs']:
            count = run['runs_4w']
            if count is None:
                gaps.append(run['name'] + ': frequency missing')
                continue
            if count == 0:
                continue
            active.append(run)
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
        cost = None
        if active:
            if data['cost_mode'] == 'contractor' and data['hourly_rate'] is not None:
                cost = billed * factor * data['hourly_rate']
            elif data['cost_mode'] == 'owned' and data['fixed_monthly'] is not None:
                cost = data['fixed_monthly']
        kg = data['monthly_kg']
        result[state] = {'km_4w': km, 'work_4w': work, 'billed_4w': billed, 'elapsed_4w': elapsed,
                         'monthly_km': km * factor, 'monthly_work': work * factor,
                         'monthly_cost': cost, 'collection_per_kg': cost / kg if cost is not None and kg and not gaps else None,
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

    def current():
        row = db().execute('SELECT * FROM blocktexx_model WHERE id=1').fetchone()
        return (json.loads(row['data']), row['revision'], row['updated_at']) if row else (empty_model(), 0, None)

    @app.get('/admin/blocktexx')
    @require('admin')
    def admin_blocktexx():
        model, revision, saved = current()
        return render_template('blocktexx.html', tab='blocktexx', model=model, revision=revision,
                               saved=saved, stages=STAGES, summary=summarize(model))

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
        _, actual, _ = current()
        if revision != actual:
            db().rollback()
            return {'ok': False, 'error': 'Another administrator saved changes. Export your draft, then reload and compare.'}, 409
        saved = datetime.now(timezone.utc).isoformat()
        payload = json.dumps(model, allow_nan=False)
        db().execute('INSERT OR REPLACE INTO blocktexx_model VALUES(1,?,?,?,?)',
                     (actual + 1, payload, g.user['id'], saved))
        db().execute('INSERT INTO blocktexx_model_history VALUES(?,?,?,?)',
                     (actual + 1, payload, g.user['id'], saved))
        db().commit()
        return {'ok': True, 'revision': actual + 1, 'saved': saved, 'summary': summarize(model)}

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
