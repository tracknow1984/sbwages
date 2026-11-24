"""State-scoped distance ordering and transparent, provisional day estimates."""
import re

from binflow_route_calendar import geography, leg, split_loads, trip_stats, clients, PREP, BREAK, WAIT, clock
from binflow_collections import planning_resources


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
    depot_key,_=point(conn,warehouse,geo) if warehouse else (None,'')
    result=[]
    for row in sites:
        item=dict(row);profile=people.get(row['id'])
        key,basis=point(conn,row,geo,profile['suburb'] if profile else '')
        distance=leg(geo,row['state'],depot_key,key) if depot_key and key else None
        item.update(distance_km=distance[1] if distance else None,drive_minutes=distance[0] if distance else None,
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
    trips=[trip_stats(load,state,resource,geo) for load in loads]
    travel=sum(t['drive'] for t in trips);service=sum(t['service'] for t in trips);handling=sum(t['handling'] for t in trips)
    baling=480 if any(i['kind']=='baling' for i in planned['items']) else 0
    allowances=PREP+BREAK+WAIT if trips else 0
    total=travel+service+handling+allowances+baling
    return dict(stops=stops,missing=unknown,loads=len(loads),capacity=capacity,
                small_space=small_space,assumed_capacity=not truck.get('pallet_positions'),travel=travel,service=service,handling=handling,
                allowances=allowances,baling=baling,minutes=total,hours=round(total/60,1),
                finish=clock(390+total) if total and not unknown else None,over_limit=total>540,
                depot=warehouse['name'],address=warehouse['address'])
