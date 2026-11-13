"""Client portal permissions, attached to an account ID and checked for each request."""
import hashlib
import json
from flask import abort, flash, g, redirect, request, url_for

MODULES = (
    ('bookings','Own interstate bookings',('view','add','edit')),
    ('national','National local pickups',('view',)),
    ('profiles','National client profiles',('view','add','edit')),
    ('invoices','Own client invoices',('view',)),
    ('pods','Completed PODs',('view',)),
    ('calendar','Approved-site collection calendar',('view',)),
    ('requests','Approved-site collection requests',('view',)),
)
ENDPOINTS = {
    'bf_client_interstate_reschedule':'bookings.view',
    'bf_client_rigid_quote':'bookings.view', 'bf_client_interstate':'bookings.view', 'bf_client_interstate_detail':'bookings.view',
    'bf_client_interstate_po':'bookings.view','bf_client_interstate_po_file':'bookings.view',
    'bf_client_interstate_new':'bookings.add','bf_client_interstate_edit':'bookings.edit',
    'bf_client_national':'national.view','bf_client_national_progress':'national.view',
    'bf_client_national_weeks':'national.view','bf_client_profiles_portal':'profiles.view',
    'bf_client_site_portal':'profiles.view','bf_client_site_new':'profiles.add','bf_client_site_edit':'profiles.edit',
    'bf_pod_record':'pods.view', 'bf_pod_journey':'pods.view',
    'bf_client_pods':'pods.view','bf_client_pdf':'pods.view','bf_client_calendar':'calendar.view',
    'bf_client_requests':'requests.view', 'bf_client_invoices':'invoices.view', 'bf_client_invoice_pdf':'invoices.view',
}

def migrate(conn):
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS bf_client_permissions (
          account_id INTEGER PRIMARY KEY REFERENCES bf_client_accounts(id), rights TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS bf_client_named_defaults (
          name TEXT PRIMARY KEY, account_id INTEGER NOT NULL REFERENCES bf_client_accounts(id));
    """)
    bind_named_default(conn)

def bind_named_default(conn):
    conn.execute("""INSERT OR IGNORE INTO bf_client_named_defaults(name,account_id)
        SELECT 'blocktexx1',id FROM bf_client_accounts WHERE username='blocktexx1'""")

def defaults(booking_login, national_reader=False):
    rights = {'bookings.view','bookings.add','invoices.view'}
    if not booking_login:
        rights.update(('calendar.view','requests.view','pods.view'))
    if national_reader:
        rights.update(('pods.view','pods.all','profiles.view','profiles.add','national.view'))
    return rights

def rights_for(conn, aid):
    row = conn.execute("""SELECT p.rights,i.account_id IS NOT NULL AS booking_login,
        n.account_id IS NOT NULL AS national_reader FROM bf_client_accounts a
        LEFT JOIN bf_client_permissions p ON p.account_id=a.id
        LEFT JOIN bf_interstate_accounts i ON i.account_id=a.id
        LEFT JOIN bf_client_named_defaults n ON n.account_id=a.id WHERE a.id=?""",(aid,)).fetchone()
    return set(json.loads(row['rights'])) if row['rights'] is not None else defaults(row['booking_login'],row['national_reader'])

def set_context(conn, account):
    g.bf_client=account
    g.client_site_approval = conn.execute(
        "SELECT 1 FROM bf_client_named_defaults WHERE name='blocktexx1' AND account_id=?",
        (account['id'],)).fetchone() is not None
    g.client_rights=rights_for(conn,account['id'])
    g.client_section = ENDPOINTS.get(request.endpoint, '').partition('.')[0]
    return g.client_rights

def revision(rights):
    return hashlib.sha256(json.dumps(sorted(rights)).encode()).hexdigest()

def save_rights(conn, aid, form):
    rights=set(form.getlist('permission'))
    allowed={module+'.'+action for module,_,actions in MODULES for action in actions}
    if rights-allowed or form.get('pod_scope','scoped') not in ('scoped','national'):
        raise ValueError('Choose valid portal permissions.')
    rights.update(key.split('.')[0]+'.view' for key in list(rights))
    if 'pods.view' in rights and form.get('pod_scope')=='national':
        rights.add('pods.all')
    conn.execute("""INSERT INTO bf_client_permissions VALUES(?,?) ON CONFLICT(account_id)
        DO UPDATE SET rights=excluded.rights""",(aid,json.dumps(sorted(rights))))

def register_site_scope(app, db, require, render, now, actor):
    @app.route('/binflow/interstate-transfers/access/<int:aid>/sites',methods=['GET','POST'])
    @require('admin')
    def bf_portal_sites(aid):
        conn=db()
        if request.method=='POST':
            conn.execute('BEGIN IMMEDIATE')
        account=conn.execute("""SELECT a.id,a.name FROM bf_client_accounts a
            JOIN bf_interstate_accounts i ON i.account_id=a.id WHERE a.id=?""",(aid,)).fetchone()
        if not account:
            abort(404)
        sites=conn.execute("""SELECT id,name,state FROM bf_locations WHERE kind='client'
            ORDER BY state,name,id""").fetchall()
        selected={row[0] for row in conn.execute('SELECT site_id FROM bf_client_site_scope WHERE account_id=?',(aid,))}
        version=revision(selected)
        if request.method=='POST':
            if request.form.get('version')!=version:
                abort(409,'Approved sites have changed. Reopen the login before saving.')
            try:
                selected={int(value) for value in request.form.getlist('site_id')}
            except ValueError:
                abort(400)
            if selected-{row['id'] for row in sites}:
                abort(400)
            conn.execute('DELETE FROM bf_client_site_scope WHERE account_id=?',(aid,))
            conn.executemany('INSERT INTO bf_client_site_scope VALUES(?,?)',[(aid,sid) for sid in selected])
            conn.execute('DELETE FROM bf_client_account_scope WHERE account_id=?',(aid,))
            conn.execute("""INSERT INTO bf_client_account_scope SELECT DISTINCT ?,l.client_id FROM bf_locations l
                JOIN bf_client_site_scope s ON s.site_id=l.id WHERE s.account_id=?""",(aid,aid))
            conn.execute('INSERT INTO bf_client_access_history(account_id,action,scopes,actor,created_at) VALUES(?,?,?,?,?)',
                         (aid,'Approved sites saved',','.join(map(str,sorted(selected))),actor(),now()))
            conn.commit()
            flash('Approved sites saved.','success')
            return redirect(url_for('bf_interstate_accounts',_anchor='login-'+str(aid)),code=303)
        return render('portal_sites',section='interstate',client=account,sites=sites,selected=selected,version=version)
