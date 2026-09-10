"""Versioned checklist text stored with each submitted prestart."""
ASSETS = {
    'T595 BOBCAT': 'machine',
    'SUMITOMO EXCAVATOR': 'machine',
    'DEMAG ROLLER': 'machine',
    'MITSUBISHI RIGID': 'vehicle',
    'ACCO TIPPER': 'vehicle',
    'CAT D6 DOZER': 'machine',
    'HYSTER FORKLIFT': 'machine',
    'FORKFORCE 2.5T': 'machine',
}
MACHINE_CHECKS = [
    ('tracks', 'Tracks / undercarriage', False),
    ('bucket', 'Bucket / attachment condition', False),
    ('hitch', 'Quick hitch / attachment locking pins secure', False),
    ('oil', 'Engine and hydraulic oil levels', False),
    ('water', 'Coolant / water level (check only when safe and cool)', False),
    ('greased', 'Machine greased as required', False),
    ('lights', 'Lights and indicators', False),
    ('leaks', 'No fluid leaks or damaged hoses', False),
    ('controls', 'Steering, controls and brakes operate correctly', False),
    ('safety', 'Seat belt, guards and safety interlocks', False),
    ('warning', 'Horn and reversing alarm', False),
    ('access', 'Steps, handrails and operator area clear and secure', False),
]
VEHICLE_CHECKS = [
    ('tyres', 'Tyres, wheels and wheel nuts', False),
    ('oil', 'Engine oil level', False),
    ('water', 'Coolant / water level (check only when safe and cool)', False),
    ('leaks', 'No fuel, oil, coolant or air leaks', False),
    ('lights', 'Headlights, brake lights, indicators and reflectors', False),
    ('visibility', 'Windscreen, mirrors, wipers and washers', False),
    ('brakes', 'Service / parking brakes and air pressure where fitted', False),
    ('steering', 'Steering and dashboard warning indicators', False),
    ('horn', 'Horn and reversing alarm where fitted', False),
    ('seatbelt', 'Seat belt, seat and cab doors', False),
    ('body', 'Body, tray, tailgate and latches secure', False),
    ('load', 'Load restraint and load secure (if carrying a load)', True),
    ('coupling', 'Trailer coupling, safety connections and lines (if towing)', True),
    ('equipment', 'Required emergency equipment present and serviceable', False),
]
DECLARATION = ('I have personally completed this prestart and recorded the results accurately. '
               'My fit-for-duty answer reflects whether I am rested, alert and able to work safely, '
               'without impairment from alcohol, drugs, medication, illness or injury. '
               'I am trained and authorised for this equipment and hold any required current licence. '
               'I will report defects or fitness concerns to my supervisor and will not operate '
               'unsafe equipment or work while unfit. By drawing my signature and signing below, '
               'I confirm these statements and my recorded answers.')


def checklist(asset):
    items = list(MACHINE_CHECKS if ASSETS[asset] == 'machine' else VEHICLE_CHECKS)
    if asset == 'DEMAG ROLLER':
        items[0] = ('tracks', 'Drum, tyres and undercarriage', False)
        items[1] = ('bucket', 'Bucket / attachment condition (if fitted)', True)
        items[2] = ('hitch', 'Hitch / attachment locking pins (if fitted)', True)
    if asset == 'CAT D6 DOZER':
        items[1] = ('bucket', 'Blade, cutting edges and mounting points', False)
        items[2] = ('hitch', 'Ripper / attachment pins and locks (if fitted)', True)
    if asset in ('HYSTER FORKLIFT', 'FORKFORCE 2.5T'):
        items[0] = ('tracks', 'Tyres, wheels and wheel nuts', False)
        items[1] = ('bucket', 'Forks, carriage and fork retaining locks', False)
        items[2] = ('hitch', 'Mast, chains, rollers and hydraulic lift / tilt', False)
        items.append(('capacity', 'Capacity plate legible and load backrest secure', False))
        items.append(('power', 'Fuel / LPG or battery connections secure, no leaks or damage', False))
    if asset == 'ACCO TIPPER':
        items.append(('tipper', 'Tipper hydraulics, body lowered and tailgate secured', False))
    return items
