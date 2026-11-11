"""Physical bin condition, tracked independently of custody/movement status."""
from flask import abort, request

CONDITIONS = {'in_service':'IN SERVICE','damaged':'DAMAGED','lost':'LOST','archived':'ARCHIVED'}


def migrate(conn):
    columns={r['name'] for r in conn.execute('PRAGMA table_info(bf_bins)')}
    if 'service_status' not in columns:
        conn.execute("ALTER TABLE bf_bins ADD COLUMN service_status TEXT NOT NULL DEFAULT 'in_service' CHECK(service_status IN ('in_service','damaged','lost'))")
    if 'archived' not in columns:
        conn.execute('ALTER TABLE bf_bins ADD COLUMN archived INTEGER NOT NULL DEFAULT 0 CHECK(archived IN (0,1))')
    if 'service_version' not in columns:
        conn.execute('ALTER TABLE bf_bins ADD COLUMN service_version INTEGER NOT NULL DEFAULT 1')


def register(app,db,require,success,failure,event):
    app.context_processor(lambda:dict(bin_conditions=CONDITIONS))

    @app.post('/binflow/bins/<int:bin_id>/condition')
    @require('admin')
    def bf_bin_condition(bin_id):
        filters={key:request.form.get(key,'')[:100] for key in ('q','state','status','client','size','condition') if request.form.get(key)}
        try:
            selected=request.form.get('service_status')
            if selected not in (*CONDITIONS, 'restore'):raise ValueError('Choose a listed bin status.')
            db().execute('BEGIN IMMEDIATE')
            b=db().execute('SELECT * FROM bf_bins WHERE id=?',(bin_id,)).fetchone()
            if not b:abort(404)
            if request.form.get('service_version')!=str(b['service_version']):
                raise ValueError('This bin status changed in another session. Review the current status and try again.')
            if selected == 'archived' and not b['archived']:
                in_use = b['truck_id'] or db().execute('''SELECT 1 FROM bf_job_bins WHERE bin_id=? AND active=1
                    UNION ALL SELECT 1 FROM bf_collection_requests WHERE bin_id=? AND status IN ('new','scheduled') LIMIT 1''',(bin_id,bin_id)).fetchone()
                if in_use:
                    raise ValueError('Finish the current movement or resolve the open collection request before archiving this bin.')
                db().execute('UPDATE bf_bins SET archived=1,service_version=service_version+1 WHERE id=?',(bin_id,))
                event(bin_id,'Bin archived',notes=CONDITIONS[b['service_status']])
            elif selected == 'restore' and b['archived']:
                db().execute('UPDATE bf_bins SET archived=0,service_version=service_version+1 WHERE id=?',(bin_id,))
                event(bin_id,'Bin restored',notes=CONDITIONS[b['service_status']])
                selected=b['service_status']
            elif selected == 'restore' or (b['archived'] and selected != 'archived'):
                raise ValueError('Review the current bin status before changing it.')
            elif selected != 'archived' and selected!=b['service_status']:
                db().execute('UPDATE bf_bins SET service_status=?,service_version=service_version+1 WHERE id=?',(selected,bin_id))
                event(bin_id,'Bin status updated',notes=f'{CONDITIONS[b["service_status"]]} → {CONDITIONS[selected]}')
            return success(f'{b["code"]}: {CONDITIONS[selected]} saved.','bf_inventory',**filters)
        except ValueError as error:return failure(error,'bf_inventory',**filters)
