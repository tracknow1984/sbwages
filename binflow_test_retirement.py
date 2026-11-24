"""Retire known acceptance fixtures, with an atomic verified recovery copy.

Names alone never select stock for deletion. The provisioning record anchors the
scope; unfamiliar/shared dependencies stop the transaction for review.
"""
import base64
import json
import zlib

VERSION = 'retire-live-test-2026-11-24-v1'
CHILDREN = set('''bf_locations bf_job_bins bf_events bf_access bf_driver_resume
bf_runs bf_run_inspections bf_run_acceptance_history bf_workdays bf_workday_events
bf_workday_entries bf_step_timings bf_run_depot_arrivals prestart_photos
bf_geofences bf_geofence_observations bf_truck_gps bf_bale_settings bf_bale_managers
bf_bale_sources bf_bale_events bf_bale_transfer_items bf_bale_storage bf_bale_pickups
bf_open_bale_sources bf_unused_returns bf_stop_exceptions bf_pod_history bf_pod_emails
bf_allocation_history bf_job_pickup_rates bf_pod_bill_to bf_client_account_scope
bf_client_site_scope bf_client_permissions bf_client_named_defaults bf_client_access_history
bf_supplier_profile_history bf_supplier_freight_rates bf_supplier_freight_terms
bf_site_profiles bf_site_requirements bf_profile_history bf_collection_profiles
bf_collection_profile_history bf_client_site_profiles bf_client_profile_history
bf_calendar_cycles bf_auto_claims bf_live_cycle employee_badges'''.split())


def quote(name):
    return '"' + name.replace('"', '""') + '"'


def snapshot(conn, table, where='1', args=()):
    return {r['_row']: dict(r) for r in conn.execute(
        f'SELECT rowid AS _row,* FROM {quote(table)} WHERE {where}', args)}


def archive(conn, key, stamp, records):
    payload = json.dumps(records, sort_keys=True, separators=(',', ':'),
                         default=lambda v: {'base64': base64.b64encode(v).decode()}).encode()
    conn.execute('INSERT INTO bf_test_retirement VALUES(?,?,?)', (key, stamp, zlib.compress(payload)))
    stored = conn.execute('SELECT rows_zlib FROM bf_test_retirement WHERE id=?', (key,)).fetchone()[0]
    if zlib.decompress(stored) != payload:
        raise ValueError('Test recovery copy failed verification.')


def retire(conn, stamp):
    conn.execute('''CREATE TABLE IF NOT EXISTS bf_test_retirement (
        id TEXT PRIMARY KEY,saved_at TEXT NOT NULL,rows_zlib BLOB NOT NULL)''')
    conn.commit()
    if conn.execute('SELECT 1 FROM bf_test_retirement WHERE id=?', (VERSION,)).fetchone():
        return {'status': 'already retired'}
    with conn:
        conn.execute('BEGIN IMMEDIATE')
        run = conn.execute('SELECT * FROM bf_live_cycle').fetchone()
        selected = {}
        def take(table, where, args=()):
            selected.setdefault(table, {}).update(snapshot(conn, table, where, args))
        if run:
            identities = [('bf_locations', 'origin_id', 'name', 'LIVE TEST · Wetherill Park'),
                          ('bf_locations', 'destination_id', 'name', 'LIVE TEST · Brisbane'),
                          ('bf_clients', 'client_id', 'name', 'LIVE TEST · Textile collections'),
                          ('bf_trucks', 'truck_id', 'name', 'LIVE TEST · NSW rigid'),
                          ('bf_client_accounts', 'payer_id', 'name', 'LIVE TEST · Draft only'),
                          ('bf_supplier_site_profiles', 'carrier_id', 'customer_site', 'LIVE TEST · Internal carrier')]
            for table, field, label, expected in identities:
                row = conn.execute(f'SELECT {label} FROM {table} WHERE id=?', (run[field],)).fetchone()
                if not row or row[0] != expected:
                    raise ValueError(f'Test identity changed: {table}/{run[field]}.')
                take(table, 'id=?', (run[field],))
            take('bf_locations', 'client_id=?', (run['client_id'],))
            locations = selected['bf_locations']
            if {r['name'] for r in locations.values()} != {'LIVE TEST · '+s for s in
                    ('Wetherill Park','Brisbane','Smithfield','Fairfield')}:
                raise ValueError('Additional locations share the test client.')
            take('bf_jobs', 'pod_client_id=?', (run['client_id'],))
            jobs = selected['bf_jobs']
            if not jobs or any(j['site_id'] not in locations or j['warehouse_id'] != run['origin_id']
                               or j['driver_id'] != run['driver_id'] for j in jobs.values()):
                raise ValueError('Test jobs are linked to other operations.')
            take('bf_bins', 'id IN (SELECT bin_id FROM bf_job_bins WHERE job_id IN (SELECT id FROM bf_jobs WHERE pod_client_id=?))', (run['client_id'],))
            bins = selected['bf_bins']
            expected = {f'LT-{n}-{s}' for n in (240,660,1100) for s in ('EMPTY','FULL')}
            if len(bins) != 6 or {r['code'] for r in bins.values()} != expected:
                raise ValueError('The six test bins no longer match the provisioned run.')
            take('bf_bales', 'origin_id=?', (run['origin_id'],))
            take('bf_bale_transfers', 'origin_id=?', (run['origin_id'],))
            take('prestarts', 'user_id=?', (run['driver_id'],))
            take('bf_workdays', 'user_id=?', (run['driver_id'],))
            take('staff_notices', 'user_id=?', (run['driver_id'],))
            take('bf_driver_resume', 'user_id=?', (run['driver_id'],))
            take('employee_badges', 'user_id=?', (run['driver_id'],))
            take('bf_auto_drafts', 'account_id=?', (run['payer_id'],))
            for draft in selected['bf_auto_drafts'].values():
                if draft['state'] != 'open' or not draft['group_key'].startswith('pods:live-test:'):
                    raise ValueError('Test billing has an exported or unrelated draft.')
                if any(line.get('source') not in {'pod:'+str(jid) for jid in jobs} for line in json.loads(draft['lines'])):
                    raise ValueError('Test draft contains a real source record.')
            if any(r['sheet_id'] is not None for r in conn.execute(
                    'SELECT sheet_id FROM bf_workday_entries WHERE workday_id IN (SELECT id FROM bf_workdays WHERE user_id=?)', (run['driver_id'],))):
                raise ValueError('Test workday is linked to payroll.')

            tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
            fks = [(table, dict(f)) for table in tables for f in conn.execute(f'PRAGMA foreign_key_list({quote(table)})')]
            # Follow only reviewed child table types. Never silently cascade invoices,
            # bookings, real stock, shared schedules, payroll or an unfamiliar table.
            changed = True
            while changed:
                changed = False
                for table, fk in fks:
                    parents = selected.get(fk['table'], {})
                    if not parents:
                        continue
                    values = [r[fk['to']] for r in parents.values()]
                    rows = snapshot(conn, table, f'{quote(fk["from"])} IN ({",".join("?" for _ in values)})', values)
                    new = rows.keys() - selected.get(table, {}).keys()
                    if not new:
                        continue
                    if table not in CHILDREN:
                        raise ValueError(f'Shared or unreviewed dependency: {table}/{fk["from"]}, {len(new)} records.')
                    selected.setdefault(table, {}).update(rows)
                    changed = True
            # A bridge may not pull a real parent into the test scope indirectly.
            for table, columns in {
                'bf_job_bins': {'bin_id':'bf_bins','job_id':'bf_jobs'},
                'bf_events': {'bin_id':'bf_bins','job_id':'bf_jobs'},
                'bf_bale_sources': {'bin_id':'bf_bins','item_id':'bf_job_bins','bale_id':'bf_bales'},
                'bf_bale_transfer_items': {'bale_id':'bf_bales','transfer_id':'bf_bale_transfers'},
                'bf_bale_storage': {'bale_id':'bf_bales','transfer_id':'bf_bale_transfers'},
                'bf_bale_events': {'bale_id':'bf_bales','transfer_id':'bf_bale_transfers'},
                'bf_open_bale_sources': {'bin_id':'bf_bins','item_id':'bf_job_bins'},
                'bf_collection_profiles': {'location_id':'bf_locations'},
                'bf_stop_exceptions': {'job_id':'bf_jobs','original_id':'bf_jobs','replacement_id':'bf_jobs'},
            }.items():
                for row in selected.get(table, {}).values():
                    if any(row[col] is not None and row[col] not in selected.get(parent, {}) for col,parent in columns.items()):
                        raise ValueError(f'Mixed real/test records: {table}/{row["_row"]}.')
            for table, owner in (('bf_runs','driver_id'),('bf_run_inspections','driver_id'),
                                 ('bf_workdays','user_id'),('bf_run_depot_arrivals','driver_id')):
                for row in selected.get(table, {}).values():
                    if row[owner] not in (None,run['driver_id']) or row['truck_id'] not in (None,run['truck_id']):
                        raise ValueError(f'Test vehicle has shared activity: {table}/{row["_row"]}.')
            # Geofence events intentionally have no foreign keys in the original schema.
            take('bf_geofence_events', 'truck_id=? OR location_id IN (?,?)',
                 (run['truck_id'],run['origin_id'],run['destination_id']))
        users = snapshot(conn, 'users', "(username='testdriver' AND first_name='Test' AND last_name='Driver' AND role='employee') OR (id=? AND first_name='Live Test' AND last_name='Driver' AND username LIKE 'bf-live-%')", (run['driver_id'] if run else -1,))
        if run and run['driver_id'] not in users:
            raise ValueError('Test driver identity changed.')
        before_fk = set(tuple(r) for r in conn.execute('PRAGMA foreign_key_check'))
        plans=[]
        for table in ('bf_schedule_weeks','bf_schedule_templates'):
            for row in conn.execute(f'SELECT * FROM {table}'):
                plan=json.loads(row['plan_json'])
                changed=False
                for day in plan.get('days',[]):
                    old=day.get('items',[])
                    day['items']=[i for i in old if not (i.get('job_id') in selected.get('bf_jobs',{})
                        or i.get('site_id') in selected.get('bf_locations',{})
                        or i.get('profile_id') in selected.get('bf_client_site_profiles',{}))]
                    changed=changed or len(old)!=len(day['items'])
                if changed:plans.append((table,dict(row),json.dumps(plan)))
        archive(conn, VERSION, stamp, {'removed':selected, 'archived_users':users,
                                      'previous_plans':[(t,r) for t,r,_ in plans]})
        conn.execute('PRAGMA defer_foreign_keys=ON')
        for table, rows in selected.items():
            conn.executemany(f'DELETE FROM {quote(table)} WHERE rowid=?', [(rid,) for rid in rows])
        for uid in users:
            conn.execute("UPDATE users SET status='terminated',auth_version=auth_version+1 WHERE id=?", (uid,))
        for table,row,plan in plans:
            if table=='bf_schedule_weeks':
                conn.execute('UPDATE bf_schedule_weeks SET plan_json=?,revision=revision+1,updated_at=? WHERE id=?',(plan,stamp,row['id']))
            else:
                version=conn.execute('SELECT MAX(version)+1 FROM bf_schedule_templates WHERE state=? AND name=?',
                                     (row['state'],row['name'])).fetchone()[0]
                conn.execute('UPDATE bf_schedule_templates SET plan_json=?,version=? WHERE id=?',(plan,version,row['id']))
        if set(tuple(r) for r in conn.execute('PRAGMA foreign_key_check')) - before_fk:
            raise ValueError('Cleanup would leave a broken record reference.')
        return {'status':'retired', 'removed':{t:len(v) for t,v in selected.items() if v}, 'archived_test_staff':len(users)}


def clear_unconfirmed_sa(conn, stamp):
    key = VERSION + '-sa-address'
    if conn.execute('SELECT 1 FROM bf_test_retirement WHERE id=?',(key,)).fetchone():
        return
    with conn:
        conn.execute('BEGIN IMMEDIATE')
        rows = snapshot(conn,'bf_locations',"kind='warehouse' AND state='SA' AND name='SB EMPIRE - SA' AND address='PORT HEADLAND'")
        fences = {}
        for row in rows.values():
            fences.update(snapshot(conn,'bf_geofences','location_id=?',(row['id'],)))
        archive(conn,key,stamp,{'locations':rows,'geofences':fences})
        for row in rows.values():
            conn.execute("UPDATE bf_locations SET address='' WHERE id=?",(row['id'],))
            conn.execute('DELETE FROM bf_geofences WHERE location_id=?',(row['id'],))
