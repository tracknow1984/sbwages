"""State collection cost scenarios. Amounts are AUD excluding GST."""
import math

FIELDS = {
    'staff_qty': 500, 'staff_hourly': 10000, 'paid_hours_week': 168,
    'workers_comp_pct': 100, 'super_pct': 100,
    'truck_insurance_month': 10000000, 'building_insurance_month': 10000000,
    'building_lease_month': 10000000, 'truck_lease_month': 10000000,
    'fuel_month': 10000000, 'owned_other_month': 10000000,
    'contractor_hourly': 10000, 'contractor_daily': 100000,
    'minimum_hours': 24, 'free_wait_minutes': 540,
    'demurrage_hourly': 10000, 'contractor_other_month': 10000000,
}


def validate_cost_profile(raw):
    if not isinstance(raw, dict):
        raise ValueError('Invalid state cost profile.')
    if type(raw.get('enabled', False)) is not bool:
        raise ValueError('Cost profile enabled must be true or false.')
    result = {'enabled': raw.get('enabled', False), 'contractor_basis': raw.get('contractor_basis', 'hourly')}
    if result['contractor_basis'] not in ('hourly', 'daily'):
        raise ValueError('Choose hourly or daily contractor pricing.')
    for key, limit in FIELDS.items():
        value = raw.get(key)
        if value is None or value == '':
            result[key] = None
            continue
        if isinstance(value, bool):
            raise ValueError('Invalid cost profile: ' + key)
        value = float(value)
        if not math.isfinite(value) or not 0 <= value <= limit:
            raise ValueError('Invalid cost profile: ' + key)
        if key == 'staff_qty' and value != int(value):
            raise ValueError('Full-time staff quantity must be a whole number.')
        result[key] = value
    return result


def cost_comparison(data, slots_for_run):
    p = data.get('cost_profile') or {}
    factor = 13 / 12
    buckets = {}
    schedule_complete = True
    for run in data['runs']:
        slots = slots_for_run(run)
        count = run.get('runs_4w')
        if slots:
            if count is not None and len(slots) != count:
                schedule_complete = False
            for slot in slots:
                key = (slot['week'], slot['day'])
                buckets.setdefault(key, {'runs': [], 'count': 1})['runs'].append(run)
        elif count:
            schedule_complete = False
            buckets[('run', run['id'])] = {'runs': [run], 'count': count}
        elif count is None:
            schedule_complete = False
    days = hours = wait = charged_wait = 0
    time_complete = True
    base_hours = 0
    for bucket in buckets.values():
        count = bucket['count']
        days += count
        runs = bucket['runs']
        keys = ('drive_min', 'service_min', 'depot_min', 'prep_min', 'wait_min', 'break_min')
        if any(r.get(k) is None for r in runs for k in keys):
            time_complete = False
        work = sum(r.get(k) or 0 for r in runs for k in keys[:4]) / 60
        waiting = sum(r.get('wait_min') or 0 for r in runs) / 60
        elapsed = work + waiting + sum(r.get('break_min') or 0 for r in runs) / 60
        if elapsed > 9:
            schedule_complete = False
        free = (p.get('free_wait_minutes') or 0) / 60
        hours += (work + waiting) * count
        wait += waiting * count
        charged_wait += max(0, waiting - free) * count
        base_hours += max(work + min(waiting, free), p.get('minimum_hours') or 0) * count
    def total(values):
        return None if any(v is None for v in values) else sum(values)
    def product(*values):
        if 0 in values:
            return 0
        return None if any(v is None for v in values) else math.prod(values)
    wages = product(p.get('staff_qty'), p.get('staff_hourly'), p.get('paid_hours_week'), 52 / 12)
    wc = product(wages, p.get('workers_comp_pct'), .01)
    super_cost = product(wages, p.get('super_pct'), .01)
    shared = total([p.get('building_insurance_month'), p.get('building_lease_month')])
    owned = total([wages, wc, super_cost, shared, p.get('truck_insurance_month'),
                   p.get('truck_lease_month'), p.get('fuel_month'), p.get('owned_other_month')])
    known_times = time_complete and p.get('free_wait_minutes') is not None
    demurrage = product(charged_wait * factor, p.get('demurrage_hourly')) if known_times else None
    hourly_base = product(base_hours * factor, p.get('contractor_hourly')) if known_times and p.get('minimum_hours') is not None else None
    daily_base = product(days * factor, p.get('contractor_daily'))
    hourly = total([hourly_base, demurrage, shared, p.get('contractor_other_month')])
    daily = total([daily_base, demurrage, shared, p.get('contractor_other_month')])
    return dict(owned=owned, hourly=hourly, daily=daily, wages=wages, workers_comp=wc,
                super_cost=super_cost, shared=shared, hourly_base=hourly_base, daily_base=daily_base,
                demurrage=demurrage, days_month=days*factor, hours_month=hours*factor,
                billed_hours_month=base_hours*factor, demurrage_hours_month=charged_wait*factor,
                schedule_complete=schedule_complete and time_complete,
                selected=owned if data['cost_mode']=='owned' else
                (daily if p.get('contractor_basis')=='daily' else hourly) if data['cost_mode']=='contractor' else None)
