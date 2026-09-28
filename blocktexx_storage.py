"""Private storage scenario inputs; commercial figures are stored in model JSON."""
import math
from datetime import date


def validate_storage(raw):
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise ValueError('Invalid storage model.')
    result = {}
    limits = {'total_containers':10000, 'occupied_containers':10000,
              'monthly_rate':100000, 'free_containers':10000,
              'current_pallets':10000, 'new_pallets':10000,
              'repack_cost':100000, 'containers_per_day':100,
              'minimum_containers':10000, 'lease_months':120}
    integers = {'total_containers','occupied_containers','free_containers',
                'current_pallets','new_pallets','minimum_containers','lease_months'}
    positive = {'total_containers','occupied_containers','current_pallets','new_pallets',
                'containers_per_day','lease_months'}
    for key, maximum in limits.items():
        value=raw.get(key)
        if value is None or value == '':
            result[key]=None
            continue
        if isinstance(value,bool):
            raise ValueError('Invalid storage value: '+key)
        try:
            value=float(value)
        except (ValueError,TypeError):
            raise ValueError('Invalid storage value: '+key)
        if not math.isfinite(value) or value < 0 or value > maximum or (key in positive and value == 0):
            raise ValueError('Storage value outside allowed range: '+key)
        if key in integers and value != int(value):
            raise ValueError('Storage container and pallet quantities must be whole numbers.')
        result[key]=int(value) if key in integers else value
    days=raw.get('days_per_week',5)
    if type(days) is not int or days not in (5,6,7):
        raise ValueError('Choose 5, 6 or 7 working days per week.')
    result['days_per_week']=days
    start=raw.get('start_date','')
    if not isinstance(start,str):
        raise ValueError('Enter a valid repack start date.')
    if start:
        try:
            if date.fromisoformat(start).isoformat()!=start:raise ValueError()
        except ValueError:
            raise ValueError('Enter a valid repack start date.')
    result['start_date']=start
    result['recovery']=raw.get('recovery','absorbed')
    if result['recovery'] not in ('absorbed','upfront','monthly'):
        raise ValueError('Choose a valid repacking cost recovery option.')
    total=result['total_containers']
    if total is not None:
        for key in ('occupied_containers','minimum_containers','free_containers'):
            if result[key] is not None and result[key]>total:
                raise ValueError('Storage quantities cannot exceed total available containers.')
    if result['occupied_containers'] and result['containers_per_day'] and math.ceil(result['occupied_containers']/result['containers_per_day'])>3660:
        raise ValueError('Repacking timeline exceeds 3,660 working days; increase daily throughput.')
    return result
