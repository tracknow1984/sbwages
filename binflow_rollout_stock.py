"""Quantity-based rollout bookings and empty stock in a dated truck manifest."""
from collections import Counter


def migrate(conn):
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS bf_rollout_slots (
            id INTEGER PRIMARY KEY, job_id INTEGER NOT NULL REFERENCES bf_jobs(id) ON DELETE CASCADE,
            size_id INTEGER NOT NULL REFERENCES bf_sizes(id),
            item_id INTEGER UNIQUE REFERENCES bf_job_bins(id));
        CREATE INDEX IF NOT EXISTS bf_rollout_job_size ON bf_rollout_slots(job_id,size_id);
        CREATE TABLE IF NOT EXISTS bf_rollout_loads (
            id INTEGER PRIMARY KEY, bin_id INTEGER NOT NULL REFERENCES bf_bins(id),
            truck_id INTEGER NOT NULL REFERENCES bf_trucks(id), run_date TEXT NOT NULL,
            warehouse_id INTEGER NOT NULL REFERENCES bf_locations(id), loaded_at TEXT NOT NULL,
            delivered_at TEXT, returned_at TEXT);
        CREATE UNIQUE INDEX IF NOT EXISTS bf_rollout_onboard ON bf_rollout_loads(bin_id)
            WHERE delivered_at IS NULL AND returned_at IS NULL;
        CREATE INDEX IF NOT EXISTS bf_rollout_run_loads ON bf_rollout_loads(truck_id,run_date);
    """)


def schedule_slots(conn, jid, counts):
    conn.executemany('INSERT INTO bf_rollout_slots(job_id,size_id) VALUES(?,?)',
                     ((jid, sid) for sid, quantity in counts.items() for _ in range(quantity)))


def planned(conn, jid):
    return conn.execute('SELECT 1 FROM bf_rollout_slots WHERE job_id=? LIMIT 1', (jid,)).fetchone()


def outstanding(conn, jid):
    return conn.execute('SELECT 1 FROM bf_rollout_slots WHERE job_id=? AND item_id IS NULL LIMIT 1', (jid,)).fetchone()


def pool(conn, truck, day):
    return conn.execute("""SELECT l.*,b.code AS bin_code,b.size_id,s.name AS size_name,w.name AS warehouse_name
        FROM bf_rollout_loads l JOIN bf_bins b ON b.id=l.bin_id JOIN bf_sizes s ON s.id=b.size_id
        JOIN bf_locations w ON w.id=l.warehouse_id WHERE l.truck_id=? AND l.run_date=?
        AND l.returned_at IS NULL AND l.delivered_at IS NULL ORDER BY l.id""", (truck,day)).fetchall()


def pending_rows(conn, truck, day, loads):
    """Show planned quantities and pooled load progress without attaching serials to sites."""
    available = Counter((r['warehouse_id'], r['size_id']) for r in loads)
    rows = []
    for r in conn.execute("""SELECT s.id AS slot_id,j.id AS job_id,j.warehouse_id,j.warehouse_name,j.site_id,
            j.site_name,j.client_name,s.size_id,z.name AS size_name
        FROM bf_rollout_slots s JOIN bf_jobs j ON j.id=s.job_id JOIN bf_sizes z ON z.id=s.size_id
        WHERE j.truck_id=? AND j.scheduled_date=? AND j.schedule_live=1 AND j.approval_status='approved'
            AND j.status='scheduled' AND s.item_id IS NULL ORDER BY j.run_order,j.id,z.litres,s.id""", (truck,day)):
        key = (r['warehouse_id'], r['size_id'])
        loaded = available[key] > 0
        available[key] -= int(loaded)
        rows.append(dict(r, id=None, direction='deliver', bin_id=None, bin_code='Any matching empty',
                         loaded_at=True if loaded else None, delivered_at=None, picked_up_at=None,
                         returned_at=None, material_name=None))
    return rows


def scan(conn, job, b, action, user, today, stamp, event):
    from binflow_runs import guard_movement
    from binflow_journey import complete_scanned_client
    from binflow_reports import begin_current, finish_scan

    truck, day, depot = job['truck_id'], job['scheduled_date'], job['warehouse_id']
    load = conn.execute("""SELECT * FROM bf_rollout_loads WHERE bin_id=? AND truck_id=? AND run_date=?
        ORDER BY id DESC LIMIT 1""", (b['id'], truck, day)).fetchone()
    onboard = load and not load['returned_at'] and not load['delivered_at']
    if action=='load' and load and not load['returned_at']:
        return True, 'Already scanned onto this truck.'
    if action=='unload' and load and load['returned_at']:
        return True, 'Already returned to the warehouse.'
    if action=='deliver' and conn.execute("""SELECT 1 FROM bf_rollout_slots s JOIN bf_job_bins i ON s.item_id=i.id
            WHERE s.job_id=? AND i.bin_id=? AND i.delivered_at IS NOT NULL""", (job['id'],b['id'])).fetchone():
        return True, 'Delivery already recorded for this site.'
    guard_movement(conn,job,action,user,today)
    if b['state']!=job['state'] or b['archived'] or (action!='unload' and b['service_status']!='in_service') or b['reserved_job']:
        raise ValueError('Scan an available in-service bin from this state.')
    begin_current(conn,user,truck,day,stamp,basis='partial')
    if action=='load':
        if b['status']!='empty' or b['location_id']!=depot or b['truck_id']:
            raise ValueError('Scan an available empty at the scheduled warehouse.')
        loads = pool(conn,truck,day)
        if not any(r['warehouse_id']==depot and r['size_id']==b['size_id'] and not r['loaded_at']
                   for r in pending_rows(conn,truck,day,loads)):
            raise ValueError('This run has no more empty bins of that size to load from this warehouse.')
        conn.execute('INSERT INTO bf_rollout_loads(bin_id,truck_id,run_date,warehouse_id,loaded_at) VALUES(?,?,?,?,?)',
                     (b['id'],truck,day,depot,stamp))
        conn.execute("UPDATE bf_bins SET location_id=NULL,truck_id=?,truck_load='empty',status='transit' WHERE id=?", (truck,b['id']))
        event(b['id'],'Empty bin scanned onto truck',origin=depot,to_truck=truck)
        message = 'Loaded onto truck. Scan at the site to assign the bin.'
    else:
        if not onboard or b['truck_id']!=truck or b['truck_load']!='empty' or load['warehouse_id']!=depot:
            raise ValueError('Scan a matching empty loaded for this rollout from the scheduled warehouse.')
        if action=='unload':
            receive(conn,load,stamp,event)
            message = 'Empty bin returned to warehouse stock.'
        elif action=='deliver':
            slot = conn.execute("""SELECT id FROM bf_rollout_slots WHERE job_id=? AND size_id=? AND item_id IS NULL
                ORDER BY id LIMIT 1""", (job['id'],b['size_id'])).fetchone()
            if not slot:
                raise ValueError('This site has no outstanding delivery of that bin size.')
            item = conn.execute("""INSERT INTO bf_job_bins(job_id,bin_id,direction,bin_code,size_name,tare_g,loaded_at,delivered_at)
                VALUES(?,?,'deliver',?,?,?,?,?)""", (job['id'],b['id'],b['code'],b['size_name'],b['tare_g'],load['loaded_at'],stamp)).lastrowid
            conn.execute('UPDATE bf_rollout_slots SET item_id=? WHERE id=?',(item,slot['id']))
            conn.execute('UPDATE bf_rollout_loads SET delivered_at=? WHERE id=?',(stamp,load['id']))
            conn.execute("UPDATE bf_bins SET truck_id=NULL,truck_load=NULL,status='at_client',location_id=? WHERE id=?", (job['site_id'],b['id']))
            event(b['id'],'Empty bin scanned to client',job['id'],target=job['site_id'],from_truck=truck)
            complete_scanned_client(conn,job,user,stamp)
            message = 'Delivered and assigned to '+job['site_name']+'.'
        else:
            raise ValueError('Choose a rollout loading or delivery scan.')
    finish_scan(conn,user,job,action,stamp)
    return False, message


def receive(conn, load, stamp, event):
    moved = conn.execute("""UPDATE bf_bins SET truck_id=NULL,truck_load=NULL,status='empty',location_id=?
        WHERE id=? AND truck_id=? AND status='transit' AND truck_load='empty'""",
        (load['warehouse_id'],load['bin_id'],load['truck_id']))
    if moved.rowcount!=1:
        raise ValueError('This empty is no longer on the truck.')
    conn.execute('UPDATE bf_rollout_loads SET returned_at=? WHERE id=?',(stamp,load['id']))
    event(load['bin_id'],'Unused empty returned to warehouse',target=load['warehouse_id'],from_truck=load['truck_id'])


def qr_step(conn, b, action, user, today, selected):
    """Resolve a physical label against the signed-in driver's run, including unassigned empties."""
    from binflow_runs import run_data
    from binflow_workday import active_workday
    day = active_workday(conn,user['id'])
    if not day or not day['truck_id'] or action not in ('load','deliver'):
        return None
    truck, date = day['truck_id'], day['work_date']
    data = run_data(conn,truck,date,user['id'],today)
    load = conn.execute("""SELECT * FROM bf_rollout_loads WHERE truck_id=? AND run_date=? AND bin_id=?
        AND returned_at IS NULL ORDER BY id DESC LIMIT 1""", (truck,date,b['id'])).fetchone()
    current = data['current']
    if action=='load':
        if load:
            job = next((j for j in data['jobs'] if j['warehouse_id']==load['warehouse_id'] and planned(conn,j['id'])
                        and (not selected or j['id']==selected)),None)
        else:
            slot = next((r for r in data['scan_rows'] if data['stage']=='load' and 'slot_id' in r.keys() and
                         r['warehouse_id']==b['location_id'] and r['size_id']==b['size_id'] and (not selected or r['job_id']==selected)),None)
            job = next((j for j in data['jobs'] if slot and slot['job_id']==j['id']),None)
    else:
        job = current if load and not load['delivered_at'] and data['stage']=='deliver' and current and planned(conn,current['id']) else None
    if not job or selected and job['id']!=selected:
        return None
    return dict(job_id=job['id'],truck_id=truck,scheduled_date=date,code=job['code'],active=True,
                client_name=job['warehouse_name'] if action=='load' else job['client_name'],
                site_name='Assign at delivery' if action=='load' else job['site_name'],
                loaded_at=load['loaded_at'] if load else None,delivered_at=None,material_name=None)


def convertible(conn, job):
    if (not job['scan_mode'] or job['status']!='scheduled' or job['approval_status']=='rejected'
            or planned(conn,job['id'])):
        return False
    return bool(conn.execute("""SELECT 1 FROM bf_job_bins WHERE job_id=? GROUP BY job_id
        HAVING MIN(direction='deliver' AND active=1 AND loaded_at IS NULL AND delivered_at IS NULL)=1
        AND NOT EXISTS(SELECT 1 FROM bf_live_cycle WHERE client_id=?)
        AND NOT EXISTS(SELECT 1 FROM bf_runs WHERE truck_id=? AND run_date=? AND departed_at IS NOT NULL)""",
        (job['id'],job['pod_client_id'],job['truck_id'],job['scheduled_date'])).fetchone())
