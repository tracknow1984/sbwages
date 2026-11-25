"""State-scoped distance ordering and transparent, provisional day estimates."""
import re
import math

from binflow_route_calendar import geography, leg, split_loads, trip_stats, clients, PREP, BREAK, WAIT, clock
from binflow_collections import planning_resources
import binflow_road_routes as roads


def compass_direction(geo, origin, destination):
    """Nearest cardinal sector from the selected depot, using saved geography."""
    p,q=(geo['points'].get(key) for key in (origin,destination))
    if not p or not q:
        return 'Location to confirm'
    if p==q:
        return 'Depot area'
    lat1,lat2=math.radians(p['lat']),math.radians(q['lat'])
    delta=math.radians(q['lon']-p['lon'])
    east=math.sin(delta)*math.cos(lat2)
    north=math.cos(lat1)*math.sin(lat2)-math.sin(lat1)*math.cos(lat2)*math.cos(delta)
    bearing=(math.degrees(math.atan2(east,north))+360)%360
    return ('NORTH','EAST','SOUTH','WEST')[int((bearing+45)%360//90)]


def point(conn, location, geo, suburb=''):
    fence=conn.execute('SELECT latitude,longitude FROM bf_geofences WHERE location_id=?',(location['id'],)).fetchone()
    key='location:'+str(location['id'])
    if fence:
        geo['points'][key]=dict(lat=fence['latitude'],lon=fence['longitude'])
        return key,'Saved location'
    candidate=location['state']+'|'+suburb.strip().casefold()
    if suburb and candidate in geo['points']:return candidate,'Suburb estimate'
    address=location['address'].casefold()
    matches=[k for k in geo['points'] if k.startswith(location['state']+'|')
             and re.search(r'(?<!\w)'+re.escape(k.split('|',1)[1])+r'(?!\w)',address)]
    if matches:return max(matches,key=len),'Suburb estimate'
    return None,'Location to confirm'


def ordered_sites(conn, sites, warehouse):
    geo=dict(points=dict(geography()['points']))
    people={p['id']:p for p in clients(conn) if p['id']}
    sites=[dict(s) for s in sites]
    for s in sites:
        profile=people.get(s['id'])
        if profile:
            # Saved locations may contain only the street. Include the separately
            # saved suburb/postcode without replacing that street with stale data.
            parts=[s['address']]
            for field in ('suburb','postcode'):
                value=profile.get(field,'').strip()
                if value and value.casefold() not in s['address'].casefold():parts.append(value)
            s['address']=', '.join(p for p in parts if p)
    road_values,geo['road_status'],geo['road_enabled']=roads.distances(conn,warehouse,sites)
    depot_key,_=point(conn,warehouse,geo) if warehouse else (None,'')
    compass_origin=depot_key
    if geo['road_enabled'] and warehouse and roads.address(warehouse):
        depot_key='location:'+str(warehouse['id'])
    result=[]
    for row in sites:
        item=dict(row);profile=people.get(row['id'])
        key,basis=point(conn,row,geo,profile['suburb'] if profile else '')
        direction=compass_direction(geo,compass_origin,key)
        distance=leg(geo,row['state'],depot_key,key) if depot_key and key else None
        route=road_values.get(row['id'])
        if geo['road_enabled']:
            distance=(route['drive_minutes'],route['distance_km']) if route else None
            key='location:'+str(row['id']) if route else None
        item.update(distance_km=distance[1] if distance else None,drive_minutes=distance[0] if distance else None,
                    distance_basis='road' if route else 'estimate',
                    direction=direction,
                    directions_url=roads.maps_link(roads.address(warehouse) if warehouse else None,roads.address(row)),
                    geo_key=key,location_basis=basis,large=profile['large'] if profile else 0,small=profile['small'] if profile else 0,
                    other_sizes=[q['name'] for q in profile['quantities'] if q['litres'] not in (240,1100)] if profile else [])
        result.append(item)
    result.sort(key=lambda r:(r['distance_km'] is None,-(r['distance_km'] or 0),r['client_name'].casefold(),r['name'].casefold()))
    return result,geo,depot_key


def day_estimate(conn, state, day, sites, warehouse, selected_site, geo, depot_key):
    from binflow_schedule import get_plan, monday
    if not state or not warehouse or not depot_key:return None
    _,plan=get_plan(conn,state,monday(day))
    planned=next(d for d in plan['days'] if d['date']==day)
    wanted={i.get('site_id') for i in planned['items'] if i.get('site_id')}
    if selected_site:wanted.add(selected_site['id'])
    stops=[s for s in sites if s['id'] in wanted]
    missing=wanted-{s['id'] for s in stops}
    settings=planning_resources(conn).get(state,{})
    truck=settings.get('truck') or {}
    capacity=truck.get('pallet_positions') or 14
    small_space=(truck.get('spaces') or {}).get('bin240',.5)
    resource=dict(depot_key=depot_key,capacity=capacity,small_space=small_space)
    unknown=[s['name'] for s in stops if not s['geo_key'] or s['large']+s['small']==0]
    unknown.extend(s['name']+' ('+', '.join(s['other_sizes'])+' footprint to confirm)' for s in stops if s['other_sizes'])
    if missing:unknown.append(f'{len(missing)} sites outside this depot/state list')
    loads=[]
    for s in stops:
        if not s['geo_key'] or s['large']+s['small']==0:continue
        for load in split_loads(s,resource):
            if not loads or sum(x['spaces'] for x in loads[-1])+load['spaces']>capacity:loads.append([])
            loads[-1].append(load)
    trips=[]
    road_trips=[]
    if geo.get('road_enabled'):
        from concurrent.futures import ThreadPoolExecutor
        route_key=roads.api_key(conn)
        origin=roads.address(warehouse)
        if len(loads)>8:unknown.append('More than eight depot loads; split this day before calculating road times')
        with ThreadPoolExecutor(max_workers=4) as pool:
            road_trips=list(pool.map(lambda load: roads.round_trip(route_key,origin,[roads.address(s['profile']) for s in load]),loads[:8]))
    for index,load in enumerate(loads):
        if geo.get('road_enabled'):
            route=road_trips[index] if index<len(road_trips) else None
            if not route:
                unknown.append('Road route for a depot round trip could not be calculated')
                continue
            bins=sum(s['large']+s['small'] for s in load)
            trips.append(dict(drive=route['drive_minutes'],service=sum(10+3*(s['large']+s['small']) for s in load),handling=20+4*bins))
        else:
            trips.append(trip_stats(load,state,resource,geo))
    travel=sum(t['drive'] for t in trips);service=sum(t['service'] for t in trips);handling=sum(t['handling'] for t in trips)
    baling=480 if any(i['kind']=='baling' for i in planned['items']) else 0
    allowances=PREP+BREAK+WAIT if trips else 0
    total=travel+service+handling+allowances+baling
    return dict(stops=stops,missing=unknown,loads=len(loads),capacity=capacity,road_enabled=geo.get('road_enabled',False),
                small_space=small_space,assumed_capacity=not truck.get('pallet_positions'),travel=travel,service=service,handling=handling,
                allowances=allowances,baling=baling,minutes=total,hours=round(total/60,1),
                finish=clock(390+total) if total and not unknown else None,over_limit=total>540,
                depot=warehouse['name'],address=warehouse['address'])
