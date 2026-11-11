"""Client-facing national site directory. Permission checks are shared with the portal."""
import sqlite3
from collections import defaultdict
from flask import abort, g, redirect, render_template, request, url_for

from binflow_client_profiles import (FIELDS, KEYS, STATES, create_site, filter_register,
    register_rows, seed_locations, update_site, validate)


def register(app, db, client_access, now):
    def show(template, **context):
        return render_template('binflow/'+template+'.html', account=g.bf_client, **context)

    def profile(pid):
        row=db().execute('SELECT * FROM bf_client_site_profiles WHERE id=?',(pid,)).fetchone()
        if not row:
            abort(404)
        return row

    @app.get('/binflow/client/profiles')
    @client_access
    def bf_client_profiles_portal():
        seed_locations(db(),now())
        db().commit()
        view=request.args.get('view','current')
        if view not in ('current','archived','all'):
            abort(400)
        rows=[row for row in register_rows(db()) if view=='all' or bool(row['archived'])==(view=='archived')]
        data=filter_register(rows,request.args,'bf_client_profiles_portal',allow_all=True)
        location_ids={r['location_id'] for r in data['profiles'] if r['location_id'] is not None}
        bins=defaultdict(list)
        # Query requirements in batches to handle both paginated and full-register views.
        ids=sorted(location_ids)
        for start in range(0,len(ids),500):
            batch=ids[start:start+500]
            marks=','.join('?' for _ in batch)
            for item in db().execute(f"""SELECT r.location_id,r.quantity,s.name FROM bf_site_requirements r
                JOIN bf_sizes s ON s.id=r.size_id WHERE r.quantity>0 AND r.location_id IN ({marks})
                ORDER BY r.location_id,s.litres,s.name""",batch):
                bins[item['location_id']].append(item)
        return show('client_directory',states=STATES,view=view,bins=bins,
                    show_all=request.args.get('per_page')=='all',**data)

    @app.get('/binflow/client/profiles/<int:pid>')
    @client_access
    def bf_client_site_portal(pid):
        row=profile(pid)
        bins=db().execute("""SELECT s.name,r.quantity FROM bf_site_requirements r JOIN bf_sizes s ON s.id=r.size_id
            WHERE r.location_id=? AND r.quantity>0 ORDER BY s.litres""",(row['location_id'],)).fetchall()
        return show('client_directory_detail',profile=row,fields=FIELDS,bins=bins)

    def edit(pid=None):
        if request.method=='POST':
            db().execute('BEGIN IMMEDIATE')
        row=profile(pid) if pid else None
        values=dict(row) if row else dict.fromkeys(KEYS,'')
        error=None
        if request.method=='POST':
            try:
                values.update({key:request.form.get(key,'').strip() for key in (*KEYS,'client_name','client_id')})
                details=validate(values)
                who='Client login '+g.bf_client['username']
                if row:
                    if request.form.get('version')!=str(row['version']):
                        abort(409,'This profile has changed. Reopen it before saving.')
                    update_site(db(),row,details,row['location_id'],now(),who)
                else:
                    if not details['address'] or not details['state']:
                        raise ValueError('Enter a street address and state for the collection site.')
                    pid=create_site(db(),values,details,now(),who)
                db().commit()
                return redirect(url_for('bf_client_site_portal',pid=pid),code=303)
            except (ValueError,sqlite3.IntegrityError) as exc:
                db().rollback()
                error=str(exc) if isinstance(exc,ValueError) else 'This client site already exists.'
        clients=[] if row else db().execute('SELECT id,name FROM bf_clients ORDER BY name').fetchall()
        return show('client_directory_form',values=values,profile=row,clients=clients,creating=not row,
                    fields=FIELDS,states=STATES,error=error),400 if error else 200

    @app.route('/binflow/client/profiles/new',methods=['GET','POST'])
    @client_access
    def bf_client_site_new():
        return edit()

    @app.route('/binflow/client/profiles/<int:pid>/edit',methods=['GET','POST'])
    @client_access
    def bf_client_site_edit(pid):
        return edit(pid)
