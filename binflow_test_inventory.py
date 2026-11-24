"""Read-only inventory for the retirement of the explicitly provisioned test run."""
import json
import os
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
    logger.warning('BinFlow test inventory: %s', json.dumps(dict(run=dict(run) if run else None,
                   candidates=candidates, references=links,
                   road_routing_configured=any(bool(os.environ.get(k)) for k in
                       ('GOOGLE_MAPS_API_KEY','GOOGLE_ROUTES_API_KEY','BINFLOW_GOOGLE_ROUTES_API_KEY')))))
