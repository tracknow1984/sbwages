"""Independent interstate freight cost centre; no stock added to intake kilograms."""
import copy
import json
import os

LANES = {
    'sydney_brisbane_bdouble': ('Wetherill Park NSW → Brisbane · B-double', 'NSW'),
    'sydney_adelaide_bdouble': ('Wetherill Park NSW → Wingfield SA · B-double', 'NSW'),
    'melbourne_sydney_bdouble': ('Laverton VIC → Wetherill Park NSW · B-double', 'VIC'),
    'melbourne_adelaide_bdouble': ('Laverton VIC → Wingfield SA · B-double', 'VIC'),
    'melbourne_brisbane_bdouble': ('Laverton VIC → Brisbane · B-double', 'VIC'),
    'sydney_melbourne_semi': ('Wetherill Park NSW → Laverton VIC · Single', 'NSW'),
    'melbourne_brisbane_semi': ('Laverton VIC → Brisbane · Single', 'VIC'),
    'melbourne_adelaide_semi': ('Laverton VIC → Wingfield SA · Single', 'VIC'),
    'sydney_brisbane_semi': ('Sydney → Brisbane · Semi (additional lane)', 'NSW'),
}
QUOTED_LANES = tuple(key for key in LANES if key != 'sydney_brisbane_semi')
RATE_FIELDS = ('base_trip', 'fuel_pct', 'tolls_trip', 'other_trip', 'payload_kg')


def validate_interstate(raw, number, text):
    if not isinstance(raw, dict) or not isinstance(raw.get('lanes', {}), dict):
        raise ValueError('Invalid interstate cost centre.')
    raw = copy.deepcopy(raw)
    version = number(raw.get('rates_version', 0), 'Interstate rates version', 1)
    # Commercial defaults are private deployment configuration, not public source.
    configured = os.environ.get('BLOCKTEXX_INTERSTATE_DEFAULTS_JSON')
    if configured and version == 0:
        defaults = json.loads(configured)
        for key in QUOTED_LANES:
            lane = raw.setdefault('lanes', {}).setdefault(key, {})
            lane.update(base_trip=defaults['base_rates'][key], fuel_pct=defaults['fuel_pct'])
            for field in ('tolls_trip', 'other_trip'):
                if lane.get(field) is None:
                    lane[field] = 0
        raw['mainfreight_base_total'] = defaults['mainfreight_base_total']
        version = 1
    comparison = number(raw.get('mainfreight_base_total'), 'Mainfreight total base rates', 10000000, True)
    lanes = {}
    for key in LANES:
        source = raw.get('lanes', {}).get(key, {})
        if not isinstance(source, dict):
            raise ValueError('Invalid interstate lane.')
        lanes[key] = {f: number(source.get(f), f, 1000 if f == 'fuel_pct' else 1000000, True) for f in RATE_FIELDS}
    rows = raw.get('bookings', [])
    if not isinstance(rows, list) or len(rows) > 300:
        raise ValueError('Use at most 300 interstate bookings.')
    bookings = []
    ids = set()
    for row in rows:
        if not isinstance(row, dict) or row.get('lane_id') not in LANES:
            raise ValueError('Choose a valid interstate lane.')
        item = {k: text(row.get(k, ''), k) for k in ('id', 'origin', 'destination', 'notes')}
        if not item['id'] or item['id'] in ids or not item['origin'] or not item['destination']:
            raise ValueError('Interstate bookings need unique IDs, origin and destination.')
        ids.add(item['id'])
        item['lane_id'] = row['lane_id']
        for key, maximum, minimum in [('week', 4, 1), ('day', 6, 0), ('trips', 50, 1), ('transit_days', 14, 0)]:
            n = number(row.get(key), key, maximum)
            if n < minimum or n != int(n):
                raise ValueError('Invalid interstate ' + key)
            item[key] = int(n)
        item['kg_trip'] = number(row.get('kg_trip'), 'Interstate kg per truck trip', 1000000, True)
        capacity = lanes[item['lane_id']]['payload_kg']
        if capacity is not None and item['kg_trip'] is not None and item['kg_trip'] > capacity:
            raise ValueError('Interstate load exceeds the configured payload for ' + LANES[item['lane_id']][0])
        bookings.append(item)
    return {'lanes': lanes, 'bookings': bookings, 'rates_version': version, 'mainfreight_base_total': comparison}


def interstate_summary(data):
    result = {}
    for key, (name, state) in LANES.items():
        p = data['lanes'][key]
        rate = None if any(p[k] is None for k in RATE_FIELDS[:4]) else p['base_trip'] * (1 + p['fuel_pct'] / 100) + p['tolls_trip'] + p['other_trip']
        rows = [r for r in data['bookings'] if r['lane_id'] == key]
        trips = sum(r['trips'] for r in rows)
        kg = None if any(r['kg_trip'] is None for r in rows) else sum(r['kg_trip'] * r['trips'] for r in rows)
        cost = 0 if not trips else None if rate is None else trips * rate
        result[key] = dict(name=name, state=state, trips=trips, rate=rate, kg=kg, cost_4w=cost,
                           monthly_cost=None if cost is None else cost*13/12,
                           cost_per_kg=cost/kg if cost is not None and kg else None)
    costs = [r['cost_4w'] for r in result.values()]
    total = None if None in costs else sum(costs)
    return dict(lanes=result, cost_4w=total, monthly_cost=None if total is None else total*13/12)
