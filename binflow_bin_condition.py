"""Physical bin condition, tracked independently of custody/movement status."""
from flask import abort, request

CONDITIONS = {'in_service':'IN SERVICE','damaged':'DAMAGED','lost':'LOST'}


def migrate(conn):
    columns={r['name'] for r in conn.execute('PRAGMA table_info(bf_bins)')}
    if 'service_status' not in columns:
        conn.execute("ALTER TABLE bf_bins ADD COLUMN service_status TEXT NOT NULL DEFAULT 'in_service' CHECK(service_status IN ('in_service','damaged','lost'))")
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
            if selected not in CONDITIONS:raise ValueError('Choose IN SERVICE, DAMAGED or LOST.')
            db().execute('BEGIN IMMEDIATE')
            b=db().execute('SELECT * FROM bf_bins WHERE id=?',(bin_id,)).fetchone()
            if not b:abort(404)
            if request.form.get('service_version')!=str(b['service_version']):
                raise ValueError('This bin status changed in another session. Review the current status and try again.')
            if selected!=b['service_status']:
                db().execute('UPDATE bf_bins SET service_status=?,service_version=service_version+1 WHERE id=?',(selected,bin_id))
                event(bin_id,'Bin status updated',notes=f'{CONDITIONS[b["service_status"]]} → {CONDITIONS[selected]}')
            return success(f'{b["code"]}: {CONDITIONS[selected]} saved.','bf_inventory',**filters)
        except ValueError as error:return failure(error,'bf_inventory',**filters)
