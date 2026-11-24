"""Read-only inventory for the retirement of the explicitly provisioned test run."""
import json
import re


def inventory(conn, logger):
    run = conn.execute('SELECT * FROM bf_live_cycle').fetchone()
    roots = {}
    if run:
        roots = {'users': [run['driver_id']], 'bf_trucks': [run['truck_id']],
                 'bf_locations': [run['origin_id'], run['destination_id']],
                 'bf_clients': [run['client_id']], 'bf_client_accounts': [run['payer_id']],
                 'bf_supplier_site_profiles': [run['carrier_id']]}
    tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")]
    links = []
    for table in tables:
        for fk in conn.execute(f'PRAGMA foreign_key_list("{table}")'):
            if fk['table'] not in roots or fk['to'] != 'id':
                continue
            ids = roots[fk['table']]
            count = conn.execute(f'SELECT COUNT(*) FROM "{table}" WHERE "{fk["from"]}" IN ({",".join("?" for _ in ids)})', ids).fetchone()[0]
            if count:
                links.append([table, fk['from'], count])
    candidates = {}
    for table, fields in (('bf_locations', 'id,kind,name,state,address'),
                          ('bf_clients', 'id,name'), ('bf_trucks', 'id,name'),
                          ('bf_client_accounts', 'id,name'),
                          ('bf_supplier_site_profiles', 'id,customer_site'),
                          ('users', 'id,first_name,last_name,username,status')):
        candidates[table] = [dict(r) for r in conn.execute(f'SELECT {fields} FROM {table}')
                             if any(re.search(r'\b(test|sample|demo)\b', str(v), re.I) for v in tuple(r)[1:])]
    from binflow_road_routes import api_key
    configured=bool(api_key(conn))
    logger.warning('BinFlow test inventory: %s', json.dumps(dict(run=dict(run) if run else None,
                   candidates=candidates, references=links,road_routing_configured=configured)))
    preserved=[dict(r) for r in conn.execute("""SELECT d.id,d.booking_id,b.pickup_date,d.event,d.status,d.decision,
        (d.id=(SELECT MAX(x.id) FROM bf_load_dispatches x WHERE x.booking_id=d.booking_id)) AS latest,
        (b.cancelled_at IS NOT NULL) AS cancelled FROM bf_load_dispatches d JOIN staff_notices n ON n.id=d.notice_id
        JOIN users u ON u.id=n.user_id JOIN bf_interstate_bookings b ON b.id=d.booking_id
        WHERE u.status='terminated' AND u.first_name IN ('Test','Live Test') AND u.last_name='Driver'
        ORDER BY d.booking_id,d.id""")]
    logger.warning('BinFlow preserved booking dispatch history: %s',json.dumps(preserved))
    if not configured:
        from binflow_allocation_planning import ordered_sites
        coverage=[]
        for depot in conn.execute("SELECT * FROM bf_locations WHERE kind='warehouse' AND name LIKE 'SB EMPIRE%' ORDER BY state"):
            sites=conn.execute("SELECT l.*,c.name client_name FROM bf_locations l JOIN bf_clients c ON c.id=l.client_id WHERE l.state=?",(depot['state'],)).fetchall()
            ordered,_,_=ordered_sites(conn,sites,depot)
            coverage.append(dict(state=depot['state'],depot=depot['name'],address=depot['address'],
                                 approximate_distances=sum(s['distance_km'] is not None for s in ordered),
                                 total=len(ordered)))
        logger.warning('BinFlow depot distance coverage: %s',json.dumps(coverage))
