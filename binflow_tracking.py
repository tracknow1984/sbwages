"""Read-only map payloads. Wialon credentials never leave the server."""
import math
import time

STATES = ('QLD','NSW','VIC','SA','WA','TAS','ACT','NT')


def tracking_snapshot(conn, state='', include_unlinked=False, epoch=None):
    epoch=int(time.time()) if epoch is None else epoch
    config=conn.execute('SELECT last_success,last_error FROM bf_gps_config WHERE id=1').fetchone()
    result=dict(connected=bool(config),state=state,server_time=epoch,refresh_seconds=30,
        stale_seconds=300,last_success=config['last_success'] if config else None,
        error=config['last_error'] if config else '',units=[],geofences=[],unlinked_count=0)
    if not config: return result
    rows=[dict(r) for r in conn.execute('''SELECT t.id AS truck_id,t.name,t.state,m.unit_id,u.name AS unit_name,
        u.latitude,u.longitude,u.position_time,u.speed,u.available FROM bf_trucks t
        LEFT JOIN bf_truck_gps m ON m.truck_id=t.id LEFT JOIN bf_gps_units u ON u.unit_id=m.unit_id '''+
        ('WHERE t.state=? ' if state else '')+'ORDER BY t.state,t.name',(state,) if state else ())]
    if include_unlinked:
        # Admins can see the connected account immediately. Staff only see explicitly linked transport units.
        unlinked=[dict(r) for r in conn.execute('''SELECT NULL AS truck_id,name,NULL AS state,unit_id,name AS unit_name,
            latitude,longitude,position_time,speed,available FROM bf_gps_units u WHERE available=1
            AND NOT EXISTS(SELECT 1 FROM bf_truck_gps m WHERE m.unit_id=u.unit_id) ORDER BY name''')]
        result['unlinked_count']=len(unlinked)
        if not state: rows+=unlinked
    for unit in rows:
        lat,lon,stamp=unit['latitude'],unit['longitude'],unit['position_time']
        valid=bool(unit['available'] and type(lat) in (int,float) and type(lon) in (int,float)
            and math.isfinite(lat) and math.isfinite(lon) and -90<=lat<=90 and -180<=lon<=180
            and type(stamp) is int and 0<stamp<=epoch+60)
        fresh=bool(valid and not result['error'] and epoch-stamp<=result['stale_seconds'])
        # Removed/private units must not retain displayable coordinates or old speed values.
        if not valid:
            unit.update(latitude=None,longitude=None,position_time=None,speed=None)
        speed=unit['speed']
        if type(speed) not in (int,float) or not math.isfinite(speed) or speed<0: unit['speed']=None
        status=('unlinked' if not unit['unit_id'] else 'unavailable') if not valid else (
            'stale' if not fresh else 'current' if unit['speed'] is None else 'moving' if unit['speed']>0 else 'stopped')
        places=conn.execute('''SELECT l.name FROM bf_geofence_observations o JOIN bf_locations l ON l.id=o.location_id
            WHERE o.truck_id=? AND o.inside=1 AND o.position_time=? ORDER BY l.kind,l.name''',
            (unit['truck_id'],stamp)).fetchall() if fresh and unit['truck_id'] else []
        unit.update(key='truck-'+str(unit['truck_id']) if unit['truck_id'] else 'unit-'+str(unit['unit_id']),
            fresh=fresh,status=status,places=[r['name'] for r in places])
        result['units'].append(unit)
    result['geofences']=[dict(r) for r in conn.execute('''SELECT l.id,l.name,l.state,l.kind,c.name AS client_name,
        f.latitude,f.longitude,f.radius_m FROM bf_geofences f JOIN bf_locations l ON l.id=f.location_id
        LEFT JOIN bf_clients c ON c.id=l.client_id '''+('WHERE l.state=? ' if state else '')+
        'ORDER BY l.state,l.kind,l.name',(state,) if state else ())]
    return result
