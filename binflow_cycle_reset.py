"""Restart the two dedicated LIVE TEST dockets without changing their printed bin labels."""
import base64
import json
import zlib

from flask import abort, flash, g, redirect, render_template, request, session, url_for


def reset(conn, run, stamp):
    # Anchor the scope to the provisioning record, then check the fixtures before touching stock.
    params=dict(client=run['client_id'],truck=run['truck_id'],uid=run['driver_id'],origin=run['origin_id'],dest=run['destination_id'],payer=run['payer_id'],day=stamp[:10])
    original="SELECT id FROM bf_jobs WHERE pod_client_id=:client AND code IN ('LIVE-TEST-POD-01','LIVE-TEST-POD-02')"
    jobs=conn.execute(f"SELECT id,code,site_id FROM bf_jobs WHERE id IN ({original}) ORDER BY code",params).fetchall()
    if [r['code'] for r in jobs]!=['LIVE-TEST-POD-01','LIVE-TEST-POD-02']:
        raise ValueError('The two live test allocations need review before restarting.')
    jid="SELECT id FROM bf_jobs WHERE pod_client_id=:client"
    extras=f"SELECT id FROM bf_jobs WHERE id IN ({jid}) AND id NOT IN ({original})"
    # Only follow descendants recorded by this cancellation workflow. Reject unrelated test-client work.
    known=conn.execute(f"""WITH RECURSIVE links(parent,child) AS (
        SELECT original_id,job_id FROM bf_stop_exceptions WHERE original_id!=job_id UNION ALL
        SELECT job_id,replacement_id FROM bf_stop_exceptions WHERE replacement_id IS NOT NULL),
        family(id) AS ({original} UNION SELECT l.child FROM links l JOIN family f ON f.id=l.parent)
        SELECT id FROM family ORDER BY id""",params).fetchall()
    if {r[0] for r in known}!={r[0] for r in conn.execute(jid,params)}:
        raise ValueError('This test client has other jobs. Review them before restarting.')
    bins=conn.execute(f"""SELECT DISTINCT i.bin_id,b.code,i.direction,j.site_id FROM bf_job_bins i
        JOIN bf_bins b ON b.id=i.bin_id JOIN bf_jobs j ON j.id=i.job_id WHERE i.job_id IN ({jid})""",params).fetchall()
    codes={f'LT-{n}-{suffix}' for n in (1100,240,660) for suffix in ('EMPTY','FULL')}
    if len(bins)!=6 or {b['code'] for b in bins}!=codes:
        raise ValueError('The six labelled test bins need review before restarting.')
    bids=f'SELECT bin_id FROM bf_job_bins WHERE job_id IN ({jid})'
    bale=f'SELECT DISTINCT bale_id FROM bf_bale_sources WHERE bin_id IN ({bids})'
    transfers='SELECT id FROM bf_bale_transfers WHERE origin_id=:origin AND destination_id=:dest AND carrier_supplier_id=:carrier'
    params['carrier']=run['carrier_id']
    work='SELECT id FROM bf_workdays WHERE user_id=:uid'
    for query in (
        f'SELECT 1 FROM bf_job_bins WHERE bin_id IN ({bids}) AND job_id NOT IN ({jid})',
        f'SELECT 1 FROM bf_bale_sources WHERE bale_id IN ({bale}) AND bin_id NOT IN ({bids})',
        f'SELECT 1 FROM bf_bale_transfer_items WHERE (transfer_id IN ({transfers})) != (bale_id IN ({bale}))',
        "SELECT 1 FROM bf_jobs WHERE truck_id=:truck AND (pod_client_id IS NOT :client)",
        "SELECT 1 FROM bf_auto_drafts WHERE group_key LIKE 'pods:live-test:%' AND (state!='open' OR account_id IS NOT :payer)",
        f'SELECT 1 FROM bf_workday_entries WHERE workday_id IN ({work}) AND sheet_id IS NOT NULL',
    ):
        if conn.execute(query,params).fetchone():
            raise ValueError('A test record is linked to other operations. Review it before clearing the run.')
    # Capture tested scope before deleting the source rows used to identify bales or inspections.
    conn.execute(f'CREATE TEMP TABLE reset_bales AS {bale}',params)
    conn.execute('CREATE TEMP TABLE reset_inspections AS SELECT prestart_id FROM bf_run_inspections WHERE truck_id=:truck AND driver_id=:uid',params)
    # Recover original item ownership before removing partial-stop or replacement records.
    moved=conn.execute(f"""SELECT i.id,e.original_id FROM bf_job_bins i JOIN bf_stop_exceptions e ON e.job_id=i.job_id
        WHERE e.original_id IN ({original}) AND e.original_id!=e.job_id""",params).fetchall()
    removed = (
        ('bf_unused_returns',f'item_id IN (SELECT id FROM bf_job_bins WHERE job_id IN ({jid}))'),
        ('bf_stop_exceptions',f'job_id IN ({jid})'),('bf_allocation_history',f'job_id IN ({extras})'),
        ('bf_bale_storage',f'transfer_id IN ({transfers})'),
        ('bf_bale_events',f'bale_id IN reset_bales OR transfer_id IN ({transfers})'),
        ('bf_bale_transfer_items',f'transfer_id IN ({transfers})'),
        ('bf_interstate_manifests',f'transfer_id IN ({transfers})'),
        ('bf_bale_transfers',f'id IN ({transfers})'),
        ('bf_bale_sources','bale_id IN reset_bales'),('bf_bales','id IN reset_bales'),
        ('bf_bale_pickups','warehouse_id IN (:origin,:dest)'),
        ('bf_open_bale_sources',f'bin_id IN ({bids})'),
        ('bf_events',f'job_id IN ({jid}) OR bin_id IN ({bids})'),
        ('bf_pod_emails',f'job_id IN ({jid})'),('bf_pod_history',f'job_id IN ({jid})'),
        ('bf_job_pickup_rates',f'job_id IN ({jid})'),
        ('bf_auto_drafts',"group_key LIKE 'pods:live-test:%' AND account_id=:payer AND state='open'"),
        ('bf_step_timings',f'workday_id IN ({work}) OR truck_id=:truck'),
        ('bf_workday_events',f'workday_id IN ({work})'),('bf_workday_entries',f'workday_id IN ({work})'),
        ('bf_workdays','user_id=:uid'),('bf_run_depot_arrivals','truck_id=:truck'),
        ('bf_run_acceptance_history','truck_id=:truck'),('bf_runs','truck_id=:truck'),
        ('prestart_photos','prestart_id IN reset_inspections'),('bf_run_inspections','prestart_id IN reset_inspections'),
        ('prestarts','id IN reset_inspections AND user_id=:uid'),('bf_driver_resume','user_id=:uid'),
    )
    # Keep the exact pre-restart rows, including signatures and QR identifiers, in
    # the same transaction as the reset. A failed archive or reset rolls back both.
    changed = (('bf_live_cycle','id=1'),('bf_jobs',f'id IN ({jid})'),
               ('bf_job_bins',f'job_id IN ({jid})'),('bf_bins',f'id IN ({bids})'))
    archive = {table: [dict(row) for row in conn.execute(f'SELECT * FROM {table} WHERE {where}',params)]
               for table,where in (*changed,*removed)}
    payload = json.dumps(archive, separators=(',',':'),
                         default=lambda value: {'base64': base64.b64encode(value).decode('ascii')}).encode()
    conn.execute('''CREATE TABLE IF NOT EXISTS bf_live_restart_archive (
        generation INTEGER PRIMARY KEY, saved_at TEXT NOT NULL, rows_zlib BLOB NOT NULL)''')
    generation = run['generation']
    conn.execute('INSERT INTO bf_live_restart_archive VALUES(?,?,?)',(generation,stamp,zlib.compress(payload)))
    stored = conn.execute('SELECT rows_zlib FROM bf_live_restart_archive WHERE generation=?',(generation,)).fetchone()[0]
    if zlib.decompress(stored) != payload:
        raise ValueError('The recovery copy could not be verified. Reopen the restart page and try again.')
    for table,where in removed:
        conn.execute(f'DELETE FROM {table} WHERE {where}',params)
    conn.execute(f"UPDATE bf_job_bins SET active=0 WHERE job_id IN ({jid})",params)
    for item in moved:
        conn.execute('UPDATE bf_job_bins SET job_id=? WHERE id=?',(item['original_id'],item['id']))
    conn.execute(f"DELETE FROM bf_job_bins WHERE job_id IN ({extras})",params)
    conn.execute(f"DELETE FROM bf_jobs WHERE id IN ({extras})",params)
    conn.execute('DROP TABLE reset_bales')
    conn.execute('DROP TABLE reset_inspections')
    for b in bins:
        empty=b['direction']=='deliver'
        conn.execute("""UPDATE bf_bins SET truck_id=NULL,truck_load=NULL,location_id=?,state='NSW',
            status=?,archived=0,service_status='in_service' WHERE id=?""",(run['origin_id'] if empty else b['site_id'],'empty' if empty else 'full',b['bin_id']))
    conn.execute(f"""UPDATE bf_job_bins SET gross_g=NULL,net_g=NULL,weighed_at=NULL,weighed_by=NULL,active=1,
        loaded_at=NULL,delivered_at=NULL,picked_up_at=NULL,returned_at=NULL,material_id=NULL,material_name='',
        tare_g=(SELECT tare_g FROM bf_bins WHERE id=bf_job_bins.bin_id) WHERE job_id IN ({jid})""",params)
    conn.execute(f"""UPDATE bf_jobs SET scheduled_date=:day,status='scheduled',collected_at=NULL,collected_by=NULL,
        received_at=NULL,completed_at=NULL,receiver_name='',confirmation='',confirmation_note='',signature=NULL,photo=NULL,
        driver_arrived_at=NULL,pod_archived_at=NULL,pod_deleted_at=NULL,pod_signed_at=NULL,pod_signed_by=NULL,
        pod_signed_name='',pod_signature=NULL,pod_snapshot='',pod_version=pod_version+1 WHERE id IN ({jid})""",params)


def register(app,db,access,now):
    @app.route('/binflow/live-test/restart',methods=['GET','POST'])
    @access
    def bf_live_restart():
        if not g.get('live_cycle') and g.user['role']!='admin':
            abort(403)
        conn=db()
        if request.method=='POST':
            with conn:
                conn.execute('BEGIN IMMEDIATE')
                run=conn.execute('SELECT * FROM bf_live_cycle').fetchone()
                if not run: abort(404)
                if request.form.get('confirm')!='restart' or request.form.get('generation',type=int)!=run['generation']:
                    abort(409,'Reopen the restart page to check the current run.')
                try:
                    reset(conn,run,now())
                except ValueError as err:
                    abort(409,str(err))
                conn.execute('UPDATE bf_live_cycle SET generation=generation+1 WHERE id=1')
            session.pop('binflow_workspace',None)
            flash('Live test cleared. Both stops and all six bin labels are ready for a new day.','success')
            return redirect(url_for('bf_live_watch' if g.user['role']=='admin' else 'employee_dashboard'),303)
        run=conn.execute('SELECT * FROM bf_live_cycle').fetchone()
        if not run:abort(404)
        return render_template('binflow/live_restart.html',run=run,tab='binflow')
