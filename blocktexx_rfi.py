"""RFI response inputs. Confidential source figures live in runtime config, never code."""
import json
import os

RFI_STATES = ('ACT', 'NSW', 'QLD', 'SA', 'TAS', 'VIC', 'WA')
ITEM_COUNTS = (10, 4, 7, 6, 7, 9, 7, 8)
ITEM_IDS = {f'{section}.{item}' for section, count in enumerate(ITEM_COUNTS, 1) for item in range(1, count + 1)}


def validate_rfi(raw, number, text):
    if not isinstance(raw, dict):
        raise ValueError('Invalid RFI response settings.')
    result = {'responses': {}, 'forecasts': {}, 'scenarios': {}}
    responses = raw.get('responses', {})
    if not isinstance(responses, dict) or set(responses) - ITEM_IDS:
        raise ValueError('Invalid RFI response item.')
    for key, item in responses.items():
        if not isinstance(item, dict) or item.get('status', 'draft') not in ('draft', 'proposed', 'confirmed', 'clarify'):
            raise ValueError('Invalid RFI response status.')
        result['responses'][key] = {'text': text(item.get('text', ''), 'RFI response', 4000), 'status': item.get('status', 'draft')}
    forecasts = raw.get('forecasts', {})
    if not isinstance(forecasts, dict) or set(forecasts) - set(RFI_STATES):
        raise ValueError('Invalid RFI forecast state.')
    for state, forecast in forecasts.items():
        if not isinstance(forecast, dict):
            raise ValueError('Invalid RFI forecast.')
        row = {key: number(forecast.get(key), 'Monthly forecast kg', 10000000, True) for key in ('low', 'high')}
        if row['low'] is not None and row['high'] is not None and row['low'] > row['high']:
            raise ValueError('Forecast minimum cannot exceed maximum.')
        result['forecasts'][state] = row
    scenarios = raw.get('scenarios', {})
    if not isinstance(scenarios, dict) or set(scenarios) - {'150', '250', '400'}:
        raise ValueError('Invalid RFI volume scenario.')
    for key, scenario in scenarios.items():
        if not isinstance(scenario, dict):
            raise ValueError('Invalid RFI scenario.')
        target = number(scenario.get('tonnes', int(key)), 'Scenario tonnes', 10000)
        if (key != '400' and target != int(key)) or (key == '400' and target < 400):
            raise ValueError('Use 150t, 250t and 400t or more for RFI scenarios.')
        result['scenarios'][key] = {'tonnes': target,
            'rate': number(scenario.get('rate'), 'Proposed scenario rate per kg', 10000, True),
            'notes': text(scenario.get('notes', ''), 'Scenario capacity and pricing notes', 4000)}
    return result


def rfi_reference():
    """Read only configured reference data. Failure cannot break the staff application."""
    from blocktexx import number, text
    raw = os.environ.get('BLOCKTEXX_RFI_REFERENCE_JSON', '')
    if not raw:
        return {}
    try:
        data = json.loads(raw)
        result = {'source': text(data.get('source', ''), 'RFI source', 300),
                  'monthly_kg': number(data.get('monthly_kg'), 'RFI monthly kg', 10000000),
                  'locations': number(data.get('locations'), 'RFI locations', 100000),
                  'equipment': number(data.get('equipment'), 'RFI equipment', 100000),
                  'states': {}}
        for state, row in data.get('states', {}).items():
            if state not in RFI_STATES or not isinstance(row, dict):
                raise ValueError('Invalid RFI state.')
            result['states'][state] = {key: number(row.get(key), 'RFI ' + key, 10000000) for key in ('kg', 'locations', 'equipment')}
        return result
    except (ValueError, TypeError, AttributeError):
        return {'error': 'RFI source figures unavailable. Check the private reference configuration.'}
