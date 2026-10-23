"""Review bin allocations before they can enter a driver schedule."""
from flask import abort, g, redirect, request, url_for


def migrate_approvals(conn):
    columns = {r['name'] for r in conn.execute('PRAGMA table_info(bf_jobs)')}
    additions = {
        'approval_status': "TEXT NOT NULL DEFAULT 'approved' CHECK(approval_status IN ('pending','approved','rejected'))",
        'submitted_at': 'TEXT',
        'reviewed_at': 'TEXT',
        'reviewed_by': "TEXT NOT NULL DEFAULT ''",
        'review_note': "TEXT NOT NULL DEFAULT ''",
    }
    for name, definition in additions.items():
        if name not in columns:
            conn.execute(f'ALTER TABLE bf_jobs ADD COLUMN {name} {definition}')
    conn.execute('''CREATE TABLE IF NOT EXISTS bf_allocation_history (
        id INTEGER PRIMARY KEY, job_id INTEGER NOT NULL REFERENCES bf_jobs(id),
        action TEXT NOT NULL, actor TEXT NOT NULL, user_id INTEGER REFERENCES users(id),
        notes TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL)''')
    conn.execute('CREATE INDEX IF NOT EXISTS bf_allocation_review ON bf_jobs(approval_status,submitted_at)')


def ensure_approved(job):
    if job['approval_status'] != 'approved':
        raise ValueError('This allocation must be approved by an administrator before it can be scheduled or moved.')


def allocation_history(conn, jid, action, actor, uid, stamp, notes=''):
    conn.execute('''INSERT INTO bf_allocation_history(job_id,action,actor,user_id,notes,created_at)
        VALUES(?,?,?,?,?,?)''', (jid,action,actor,uid,notes,stamp))


def register_approvals(app,db,access,require,allowed,render,job,items,bins,event,success,failure,now,actor,reopen_requests,approve_requests):
    app.jinja_env.globals['bf_job_label'] = lambda j: (
        'Awaiting approval' if j['approval_status']=='pending' else
        'Disapproved' if j['approval_status']=='rejected' else
        ('Draft week' if j['schedule_week_id'] else 'Unscheduled') if not j['schedule_live'] else
        {'scheduled':'Scheduled','collected':'On the road','received':'Weighing','complete':'Complete','cancelled':'Cancelled'}[j['status']])

    @app.get('/binflow/allocation-approvals')
    @access
    def bf_allocation_approvals():
        state=request.args.get('state','')
        if state not in ('','QLD','NSW','VIC','SA','WA','TAS','ACT','NT'): abort(400)
        return redirect(url_for('bf_collection_calendar',state=state))

    def apply_review(job_id,decision,notes=''):
        # Caller owns the transaction; both review paths use the same stock checks.
        if not g.user or g.user['role']!='admin': abort(403)
        j=job(job_id);rows=items(job_id)
        target='approved' if decision=='approve' else 'rejected'
        if j['approval_status']==target:
            return False
        if j['approval_status']!='pending' or j['status']!='scheduled':
            raise ValueError('This allocation has already been reviewed. Create a new allocation for changes.')
        if not rows or any(i['loaded_at'] or i['delivered_at'] or i['picked_up_at'] or i['weighed_at'] for i in rows):
            raise ValueError('This allocation has unexpected movements. Review its docket before continuing.')
        if decision=='approve':
            from binflow_permissions import eligible_driver
            if j['driver_id'] and not eligible_driver(db(),j['driver_id'],j['state']):
                raise ValueError('The proposed driver no longer has transport access for this state. Reject and revise this allocation.')
            for item in rows:
                b=bins('b.id=?',(item['bin_id'],))[0]
                location=j['site_id'] if item['direction']=='collect' else j['warehouse_id']
                states=('at_client','full') if item['direction']=='collect' else ('empty',)
                if not item['active'] or b['reserved_job']!=job_id or b['location_id']!=location or b['status'] not in states or b['state']!=j['state'] or b['truck_id']:
                    raise ValueError(f'{b["code"]} is no longer available for this allocation. Reject it and prepare a revised allocation.')
        stamp=now();who=actor()
        db().execute('UPDATE bf_jobs SET approval_status=?,reviewed_at=?,reviewed_by=?,review_note=? WHERE id=?',
            (target,stamp,who,notes,job_id))
        if decision=='reject':
            db().execute("UPDATE bf_jobs SET status='cancelled' WHERE id=?",(job_id,))
            db().execute('UPDATE bf_job_bins SET active=0 WHERE job_id=?',(job_id,))
            reopen_requests(db(),job_id,who,stamp)
        else:
            approve_requests(db(),job_id,who,stamp)
        label='Allocation approved' if decision=='approve' else 'Allocation rejected; reservation released'
        allocation_history(db(),job_id,label,who,g.user['id'],stamp,notes)
        for item in rows:event(item['bin_id'],label,job_id,notes=notes)
        return True

    app.extensions['bf_review_allocation']=apply_review

    @app.post('/binflow/pickups/<int:job_id>/review')
    @require('admin')
    def bf_review_allocation(job_id):
        try:
            decision=request.form.get('decision','')
            notes=request.form.get('notes','').strip()
            if decision not in ('approve','reject') or len(notes)>1000:
                raise ValueError('Choose approve or reject, with notes up to 1,000 characters.')
            if decision=='reject' and not notes:
                raise ValueError('Enter a reason for rejecting this allocation.')
            db().execute('BEGIN IMMEDIATE')
            if not apply_review(job_id,decision,notes):
                return success('This allocation has already been '+('approved' if decision=='approve' else 'rejected')+'.','bf_job',job_id=job_id)
            return success('Allocation approved. Choose a truck to schedule the run.' if decision=='approve' else
                'Allocation rejected. Bins are available again; the reason is saved.','bf_job',job_id=job_id)
        except ValueError as error:
            return failure(error,'bf_job',job_id=job_id)
