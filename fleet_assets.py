"""Vehicle details, dated service entries and their supporting documents."""
import io
import secrets
import sqlite3
from datetime import date
from flask import abort, flash, g, redirect, render_template, request, send_file, url_for
from binflow_schedule import STATES
from document_uploads import MAX_STAFF_FILES, read_document


def migrate(conn):
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS vehicle_profiles (
            truck_id INTEGER PRIMARY KEY REFERENCES bf_trucks(id), make TEXT NOT NULL, model TEXT NOT NULL,
            rego TEXT NOT NULL, rego_expiry TEXT, next_inspection TEXT, last_service TEXT, version INTEGER NOT NULL);
        CREATE TABLE IF NOT EXISTS vehicle_history (
            id INTEGER PRIMARY KEY,truck_id INTEGER NOT NULL REFERENCES bf_trucks(id),kind TEXT NOT NULL,
            event_date TEXT NOT NULL,summary TEXT NOT NULL,created_by INTEGER NOT NULL REFERENCES users(id),
            request_key TEXT NOT NULL UNIQUE);
        CREATE TABLE IF NOT EXISTS vehicle_files (
            id INTEGER PRIMARY KEY,history_id INTEGER NOT NULL REFERENCES vehicle_history(id),
            filename TEXT NOT NULL,mime TEXT NOT NULL,data BLOB NOT NULL);
        CREATE INDEX IF NOT EXISTS vehicle_history_by_asset ON vehicle_history(truck_id,event_date);
    """)
    if 'vehicle_state' not in {r[1] for r in conn.execute('PRAGMA table_info(bf_run_inspections)')}:
        conn.execute("ALTER TABLE bf_run_inspections ADD COLUMN vehicle_state TEXT")
    conn.execute("""UPDATE bf_run_inspections SET vehicle_state=(SELECT state FROM bf_trucks WHERE id=truck_id)
                    WHERE vehicle_state IS NULL""")


VEHICLES="""SELECT t.id,t.name,t.state,p.make,p.model,p.rego,p.rego_expiry,p.next_inspection,
    (SELECT MAX(d) FROM (SELECT p.last_service AS d UNION ALL SELECT MAX(h.event_date) FROM vehicle_history h
        WHERE h.truck_id=t.id AND h.kind='Service')) AS last_service,p.last_service AS baseline,
    COALESCE(p.version,0) AS version FROM bf_trucks t LEFT JOIN vehicle_profiles p ON p.truck_id=t.id"""


def register(app,db,require,now):
    def fetch(tid):
        row=db().execute(VEHICLES+' WHERE t.id=?',(tid,)).fetchone()
        if not row:abort(404)
        return row

    def date_value(name):
        value=request.form.get(name,'').strip()
        if value and (len(value)!=10 or date.fromisoformat(value).isoformat()!=value):
            raise ValueError('Enter a valid date.')
        return value or None

    @app.get('/admin/prestart/assets')
    @require('admin')
    def fleet_assets():
        return render_template('fleet_assets.html',tab='prestart',assets=db().execute(VEHICLES+' ORDER BY t.state,t.name').fetchall(),states=STATES)

    @app.post('/admin/prestart/assets')
    @app.post('/admin/prestart/assets/<int:tid>/edit')
    @require('admin')
    def fleet_save(tid=None):
        try:
            conn=db()
            with conn:
                conn.execute('BEGIN IMMEDIATE')
                asset=fetch(tid) if tid else None
                if asset and asset['version']!=request.form.get('version',type=int):
                    raise ValueError('This vehicle has changed. Review its details and save again.')
                name,make,model,rego=[request.form.get(k,'').strip() for k in ('name','make','model','rego')]
                state=request.form.get('state')
                if not name or any(len(v)>120 for v in (name,make,model)) or len(rego)>20 or state not in STATES:
                    raise ValueError('Enter a vehicle name and home state, and check the registration.')
                dates=[date_value(k) for k in ('rego_expiry','next_inspection','last_service')]
                if rego and conn.execute("""SELECT 1 FROM vehicle_profiles p JOIN bf_trucks t ON t.id=p.truck_id
                        WHERE t.state=? AND replace(p.rego,' ','')=replace(?,' ','') AND t.id!=?""",(state,rego.upper(),tid or 0)).fetchone():
                    raise ValueError('That registration is already recorded for this state.')
                if asset:
                    conn.execute('UPDATE bf_trucks SET name=?,state=? WHERE id=?',(name,state,tid))
                else:
                    tid=conn.execute('INSERT INTO bf_trucks(name,state,created_at) VALUES(?,?,?)',(name,state,now())).lastrowid
                conn.execute("""INSERT INTO vehicle_profiles VALUES(?,?,?,?,?,?,?,1) ON CONFLICT(truck_id) DO UPDATE SET
                    make=excluded.make,model=excluded.model,rego=excluded.rego,rego_expiry=excluded.rego_expiry,
                    next_inspection=excluded.next_inspection,last_service=excluded.last_service,version=version+1""",(tid,make,model,rego.upper(),*dates))
            flash('Vehicle details saved.','success')
        except (ValueError,sqlite3.IntegrityError) as exc:
            flash(str(exc) if isinstance(exc,ValueError) else 'That vehicle name is already in use.','error')
            tid=request.view_args.get('tid')
        return redirect(url_for('fleet_asset',tid=tid) if tid else url_for('fleet_assets'),303)

    @app.get('/admin/prestart/assets/<int:tid>')
    @require('admin')
    def fleet_asset(tid):
        asset=fetch(tid)
        entries=db().execute('SELECT * FROM vehicle_history WHERE truck_id=? ORDER BY event_date DESC,id DESC',(tid,)).fetchall()
        files=db().execute("""SELECT f.id,f.history_id,f.filename FROM vehicle_files f JOIN vehicle_history h ON h.id=f.history_id
            WHERE h.truck_id=? ORDER BY f.id""",(tid,)).fetchall()
        inspections=db().execute("""SELECT p.id,p.submitted_at,p.employee_name,p.failure_count,i.passed FROM bf_run_inspections i
            JOIN prestarts p ON p.id=i.prestart_id WHERE i.truck_id=? ORDER BY p.id DESC""",(tid,)).fetchall()
        return render_template('fleet_asset.html',asset=asset,states=STATES,entries=entries,files=files,
            inspections=inspections,tab='prestart',key=secrets.token_urlsafe(24),day=now()[:10])

    @app.post('/admin/prestart/assets/<int:tid>/history')
    @require('admin')
    def fleet_history(tid):
        fetch(tid)
        try:
            kind=request.form.get('kind');summary=request.form.get('summary','').strip()
            day=date_value('event_date');key=request.form.get('key','')
            uploads=[u for u in request.files.getlist('files') if u.filename]
            if kind not in ('Service','Inspection','Registration','Other document') or not day or not 1<=len(summary)<=2000 or not 20<=len(key)<=100:
                raise ValueError('Choose the record type, date and description.')
            if len(uploads)>MAX_STAFF_FILES:raise ValueError(f'Choose up to {MAX_STAFF_FILES} files per entry.')
            conn=db()
            with conn:
                conn.execute('BEGIN IMMEDIATE')
                if not conn.execute('SELECT 1 FROM vehicle_history WHERE request_key=? AND truck_id=?',(key,tid)).fetchone():
                    hid=conn.execute('INSERT INTO vehicle_history(truck_id,kind,event_date,summary,created_by,request_key) VALUES(?,?,?,?,?,?)',
                        (tid,kind,day,summary,g.user['id'],key)).lastrowid
                    for upload in uploads:
                        file=read_document(upload,app.config['DOCUMENT_MAX_BYTES'],verify_contents=True)
                        conn.execute('INSERT INTO vehicle_files(history_id,filename,mime,data) VALUES(?,?,?,?)',(hid,*file))
            flash('Vehicle record saved.','success')
        except ValueError as exc:
            flash(str(exc),'error')
        return redirect(url_for('fleet_asset',tid=tid),303)

    @app.get('/admin/prestart/assets/<int:tid>/files/<int:fid>')
    @require('admin')
    def fleet_file(tid,fid):
        row=db().execute('''SELECT f.* FROM vehicle_files f JOIN vehicle_history h ON h.id=f.history_id
            WHERE h.truck_id=? AND f.id=?''',(tid,fid)).fetchone()
        if not row:abort(404)
        result=send_file(io.BytesIO(row['data']),download_name=row['filename'],mimetype=row['mime'])
        result.headers['Cache-Control']='private, no-store'
        return result
