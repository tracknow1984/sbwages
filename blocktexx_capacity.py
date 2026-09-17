"""Space-based load planning, with optional verified payload inputs."""
import math
import re

KINDS = ('cage', 'bin660', 'bin240', 'bin120', 'pallecon')
LABELS = {'cage': 'cages', 'bin660': '660L bins', 'bin240': '240L bins', 'bin120': '120L bins', 'pallecon': 'pallecons'}
DEFAULT_SPACES = dict(cage=1, bin660=1, bin240=.5, bin120=.5, pallecon=1)


def infer_containers(equipment):
    counts = dict.fromkeys(KINDS, 0)
    for line in equipment.splitlines():
        match = re.match(r'\s*(\d+(?:\.\d+)?)\s*[×x]\s*(.*)', line)
        if not match:
            continue
        qty, description = float(match[1]), match[2].lower()
        kind = next((k for k, token in [('bin660','660l'), ('bin240','240l'), ('bin120','120l'), ('pallecon','pallecon'), ('cage','cage')] if token in description), None)
        if kind:
            counts[kind] += qty
    return counts


def plan_run(model, state, run):
    settings = model['states'][state]['truck']
    slots, payload = settings['pallet_positions'], settings['payload_kg']
    sites = {s['id']: s for s in model['sites']}
    loads = []
    missing = []
    total = dict.fromkeys(KINDS, 0)
    load = None
    for sid in run['site_ids']:
        site = sites[sid]
        counts = site['containers']
        if not any(counts.values()):
            missing.append(site['name'] + ': physical container quantity missing')
        for kind in KINDS:
            qty = counts[kind]
            total[kind] += qty
            space = settings['spaces'][kind]
            weight = settings['weights_kg'][kind]
            if qty and (space > slots or (payload is not None and weight is not None and weight > payload)):
                missing.append(site['name'] + ': one ' + LABELS[kind] + ' exceeds configured truck capacity')
                continue
            for _ in range(int(qty)):
                if load is None or load['spaces'] + space > slots + .000001 or (payload is not None and weight is not None and load['known_kg'] + weight > payload):
                    load = {'spaces': 0, 'known_kg': 0, 'weight_complete': True, 'stops': []}
                    loads.append(load)
                load['spaces'] += space
                load['known_kg'] += weight or 0
                load['weight_complete'] = load['weight_complete'] and weight is not None
                if not load['stops'] or load['stops'][-1]['site_id'] != sid:
                    load['stops'].append({'site_id':sid, 'name':site['name'], 'address':site['address'], 'containers':dict.fromkeys(KINDS,0)})
                load['stops'][-1]['containers'][kind] += 1
    for load in loads:
        load['spare_spaces'] = slots - load['spaces']
        empty = {k: sum(stop['containers'][k] for stop in load['stops']) for k in KINDS}
        full = dict.fromkeys(KINDS, 0)
        load['outbound_empty'] = empty.copy()
        load['return_full'] = empty.copy()
        load['container_moves'] = 2 * sum(empty.values())
        for stop in load['stops']:
            stop['deliver_empty'] = stop['containers'].copy()
            stop['collect_full'] = stop['containers'].copy()
            for kind in KINDS:
                empty[kind] -= stop['containers'][kind]
                full[kind] += stop['containers'][kind]
            stop['onboard_empty_after'] = empty.copy()
            stop['onboard_full_after'] = full.copy()
            stop['onboard_spaces_after'] = sum((empty[k]+full[k])*settings['spaces'][k] for k in KINDS)
    original_loads = run['included_loads']
    return {'loads':loads, 'containers':total, 'container_count':sum(total.values()),
            'outbound_empty':total.copy(), 'return_full':total.copy(), 'customer_container_moves':2*sum(total.values()),
            'bin_count':sum(total[k] for k in ('bin120','bin240','bin660')),
            'spaces':sum(load['spaces'] for load in loads), 'load_count':len(loads),
            'extra_loads':max(0,len(loads)-original_loads), 'issues':missing,
            'payload_checked':bool(loads) and payload is not None and all(l['weight_complete'] for l in loads),
            'notes':'Unload matching empties before loading full containers at each stop. Same footprint throughout the load; no nesting or stacking credit. Loaded weights must include the container. Stock availability, handling time and tailgate limits require confirmation. Full returns cannot supply the next load of empties until emptied.'}


def capacity_plans(model):
    return {state:{run['id']:plan_run(model,state,run) for run in data['runs']}
            for state,data in model['states'].items() if state in ('NSW','QLD')}


def load_sequence(plan):
    legs = ['Depot']
    for load in plan['loads']:
        for stop in load['stops']:
            contents = ', '.join(f'{qty} {LABELS[k]}' for k,qty in stop['containers'].items() if qty)
            legs.append(stop['name'].replace('Blocktexx - ', '') + ' [' + contents + ']')
        legs.append('depot')
    return ' → '.join(legs)
