"""Vehicle details, dated service entries and their supporting documents."""
import io
import re
import secrets
import sqlite3
from datetime import date
from flask import abort, flash, g, redirect, render_template, request, send_file, url_for
from binflow_schedule import STATES
from document_uploads import MAX_STAFF_FILES, read_document


# Additional profile columns are optional so trucks already used on prestarts remain editable.
ASSET_PROFILE_COLUMNS = {
    'vin': "TEXT NOT NULL DEFAULT ''",
    'description': "TEXT NOT NULL DEFAULT ''",
    'gvm_kg': 'INTEGER',
    'tare_kg': 'INTEGER',
    'nhv_charging_code': "TEXT NOT NULL DEFAULT ''",
    'purpose_of_use': "TEXT NOT NULL DEFAULT ''",
    'rego_status': "TEXT NOT NULL DEFAULT ''",
    'inspection_certificate_number': "TEXT NOT NULL DEFAULT ''",
    'inspection_certificate_due': 'TEXT',
    'next_service_date': 'TEXT',
    'service_notice_days': 'INTEGER NOT NULL DEFAULT 30',
    'coc_due_date': 'TEXT',
    'coc_notice_days': 'INTEGER NOT NULL DEFAULT 30',
}

PROFILE_FIELDS = ('make', 'model', 'rego', 'rego_expiry', 'next_inspection', 'last_service',
                  *ASSET_PROFILE_COLUMNS)


def profile_values(form, asset):
    values = {}
    for field in PROFILE_FIELDS:
        previous = asset['baseline' if field == 'last_service' else field] if asset else None
        value = form.get(field, previous)
        values[field] = '' if value is None else str(value).strip()
    for field, limit, label in (
        ('make', 120, 'Make'), ('model', 120, 'Model'), ('rego', 20, 'Registration number'),
        ('description', 240, 'Description'), ('nhv_charging_code', 20, 'Charging code'),
        ('purpose_of_use', 80, 'Purpose of use'), ('rego_status', 60, 'Registration status'),
        ('inspection_certificate_number', 80, 'Certificate number'),
    ):
        if len(values[field]) > limit:
            raise ValueError(f'{label} must be {limit} characters or fewer.')
    for field in ('rego', 'vin', 'nhv_charging_code'):
        values[field] = values[field].upper()
    if values['vin'] and not re.fullmatch(r'[A-HJ-NPR-Z0-9]{17}', values['vin']):
        raise ValueError('Enter a 17-character VIN using digits and letters other than I, O or Q.')
    for field, label in (('gvm_kg', 'Gross vehicle mass'), ('tare_kg', 'Tare')):
        raw = values[field]
        if raw and (not re.fullmatch(r'[0-9]{1,9}', raw) or int(raw) == 0):
            raise ValueError(f'{label} must be a positive whole number in kilograms.')
        values[field] = int(raw) if raw else None
    if values['gvm_kg'] and values['tare_kg'] and values['tare_kg'] > values['gvm_kg']:
        raise ValueError('Tare cannot exceed gross vehicle mass.')
    for field in ('service_notice_days', 'coc_notice_days'):
        values[field] = notice_days(values[field])
    for field in ('rego_expiry', 'next_inspection', 'last_service', 'inspection_certificate_due',
                  'next_service_date', 'coc_due_date'):
        values[field] = iso_date(values[field])
    return values


def notice_days(value):
    if value == '':
        return 30
    if not re.fullmatch(r'[0-9]{1,3}', value) or int(value) > 365:
        raise ValueError('Choose a notice period from 0 to 365 days.')
    return int(value)


def due_notices(conn, day):
    # A notice appears on the first day of its window and stays until the date is updated.
    return conn.execute("""WITH deadlines AS (
        SELECT truck_id, 'Service' AS kind, next_service_date AS due, service_notice_days AS days FROM vehicle_profiles
        UNION ALL
        SELECT truck_id, 'COC', coc_due_date, coc_notice_days FROM vehicle_profiles
    )
    SELECT t.id, t.name, p.rego, d.kind, d.due, CAST(julianday(d.due)-julianday(?) AS INTEGER) AS days_left
    FROM deadlines d JOIN bf_trucks t ON t.id=d.truck_id JOIN vehicle_profiles p ON p.truck_id=t.id
    WHERE d.due IS NOT NULL AND julianday(d.due)-julianday(?) <= d.days
    ORDER BY d.due, t.name, d.kind""", (day, day)).fetchall()


def iso_date(value):
    if not value:
        return None
    try:
        if date.fromisoformat(value).isoformat() == value:
            return value
    except ValueError:
        pass
    raise ValueError('Enter a valid date.')


def save_history(conn, tid, kind, day, summary, key, uploads, max_bytes):
    hid = conn.execute('''INSERT INTO vehicle_history
        (truck_id,kind,event_date,summary,created_by,request_key) VALUES(?,?,?,?,?,?)''',
        (tid, kind, day, summary, g.user['id'], key)).lastrowid
    for upload in uploads:
        document = read_document(upload, max_bytes, verify_contents=True)
        conn.execute('INSERT INTO vehicle_files(history_id,filename,mime,data) VALUES(?,?,?,?)',
                     (hid, *document))


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
    conn.execute('BEGIN IMMEDIATE')
    existing = {row[1] for row in conn.execute('PRAGMA table_info(vehicle_profiles)')}
    for name, definition in ASSET_PROFILE_COLUMNS.items():
        if name not in existing:
            conn.execute(f'ALTER TABLE vehicle_profiles ADD COLUMN {name} {definition}')
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS vehicle_profiles_vin ON vehicle_profiles(vin COLLATE NOCASE) WHERE vin != ''")
    if 'vehicle_state' not in {r[1] for r in conn.execute('PRAGMA table_info(bf_run_inspections)')}:
        conn.execute("ALTER TABLE bf_run_inspections ADD COLUMN vehicle_state TEXT")
    conn.execute("""UPDATE bf_run_inspections SET vehicle_state=(SELECT state FROM bf_trucks WHERE id=truck_id)
                    WHERE vehicle_state IS NULL""")


VEHICLES="""SELECT t.id,t.name,t.state,p.make,p.model,p.rego,p.rego_expiry,p.next_inspection,
    p.vin,p.description,p.gvm_kg,p.tare_kg,p.nhv_charging_code,p.purpose_of_use,p.rego_status,
    p.inspection_certificate_number,p.inspection_certificate_due,p.next_service_date,p.coc_due_date,
    COALESCE(p.service_notice_days,30) AS service_notice_days,COALESCE(p.coc_notice_days,30) AS coc_notice_days,
    (SELECT MAX(d) FROM (SELECT p.last_service AS d UNION ALL SELECT MAX(h.event_date) FROM vehicle_history h
        WHERE h.truck_id=t.id AND h.kind='Service')) AS last_service,p.last_service AS baseline,
    COALESCE(p.version,0) AS version FROM bf_trucks t LEFT JOIN vehicle_profiles p ON p.truck_id=t.id"""


def register(app,db,require,now):
    def fetch(tid):
        row=db().execute(VEHICLES+' WHERE t.id=?',(tid,)).fetchone()
        if not row:abort(404)
        return row

    @app.get('/admin/prestart/assets')
    @require('admin')
    def fleet_assets():
        return register_page()

    def register_page(submitted=None):
        return render_template('fleet_assets.html', tab='prestart', states=STATES, submitted=submitted,
                               assets=db().execute(VEHICLES+' ORDER BY t.state,t.name').fetchall(),
                               notices=due_notices(db(), now()[:10]))

    @app.post('/admin/prestart/assets')
    @app.post('/admin/prestart/assets/<int:tid>/edit')
    @require('admin')
    def fleet_save(tid=None):
        editing_tid = tid
        try:
            registration_files = [f for f in request.files.getlist('registration_files') if f.filename]
            inspection_files = [f for f in request.files.getlist('inspection_files') if f.filename]
            coc_files = [f for f in request.files.getlist('coc_files') if f.filename]
            if len(registration_files) + len(inspection_files) + len(coc_files) > MAX_STAFF_FILES:
                raise ValueError(f'Choose up to {MAX_STAFF_FILES} documents per save.')
            conn=db()
            with conn:
                conn.execute('BEGIN IMMEDIATE')
                asset=fetch(tid) if tid else None
                if asset and asset['version']!=request.form.get('version',type=int):
                    raise ValueError('This vehicle has changed. Review its details and save again.')
                name = request.form.get('name', '').strip()
                state = request.form.get('state')
                if not name or len(name) > 120 or state not in STATES:
                    raise ValueError('Enter a vehicle name and home state.')
                values = profile_values(request.form, asset)
                rego = values['rego']
                if rego and conn.execute("""SELECT 1 FROM vehicle_profiles p JOIN bf_trucks t ON t.id=p.truck_id
                        WHERE t.state=? AND replace(upper(p.rego),' ','')=replace(?,' ','') AND t.id!=?""",
                        (state, rego, tid or 0)).fetchone():
                    raise ValueError('That registration is already recorded for this state.')
                if values['vin'] and conn.execute(
                        'SELECT 1 FROM vehicle_profiles WHERE vin=? COLLATE NOCASE AND truck_id!=?',
                        (values['vin'], tid or 0)).fetchone():
                    raise ValueError('That VIN is already recorded against another vehicle.')
                if asset:
                    conn.execute('UPDATE bf_trucks SET name=?,state=? WHERE id=?',(name,state,tid))
                else:
                    tid=conn.execute('INSERT INTO bf_trucks(name,state,created_at) VALUES(?,?,?)',(name,state,now())).lastrowid
                columns = ', '.join(PROFILE_FIELDS)
                placeholders = ', '.join('?' for _ in PROFILE_FIELDS)
                updates = ', '.join(f'{field}=excluded.{field}' for field in PROFILE_FIELDS)
                conn.execute(f"""INSERT INTO vehicle_profiles(truck_id, {columns}, version)
                    VALUES(?, {placeholders}, 1) ON CONFLICT(truck_id) DO UPDATE SET
                    {updates}, version=version+1""", (tid, *(values[field] for field in PROFILE_FIELDS)))
                for kind, label, number, uploads in (
                    ('Registration', 'Registration papers', rego, registration_files),
                    ('Inspection', 'Certificate of inspection', values['inspection_certificate_number'], inspection_files),
                    ('COC', 'COC document', values['coc_due_date'], coc_files),
                ):
                    if uploads:
                        summary = f'{label} · {number}' if number else label
                        save_history(conn, tid, kind, now()[:10], summary, secrets.token_urlsafe(24),
                                     uploads, app.config['DOCUMENT_MAX_BYTES'])
            flash('Vehicle details saved.','success')
        except (ValueError,sqlite3.IntegrityError) as exc:
            message = str(exc) if isinstance(exc, ValueError) else 'That vehicle name is already in use.'
            if any(f.filename for _, f in request.files.items(multi=True)):
                message += ' Select your files again before saving.'
            flash(message,'error')
            # Reload a stale edit before permitting a retry. Other validation errors retain the form.
            if editing_tid and fetch(editing_tid)['version'] != request.form.get('version', type=int):
                return redirect(url_for('fleet_asset', tid=editing_tid), 303)
            if editing_tid:
                return asset_page(editing_tid, submitted=request.form), 422
            return register_page(submitted=request.form), 422
        return redirect(url_for('fleet_asset',tid=tid) if tid else url_for('fleet_assets'),303)

    @app.get('/admin/prestart/assets/<int:tid>')
    @require('admin')
    def fleet_asset(tid):
        return asset_page(tid)

    def asset_page(tid, submitted=None, history_form=None):
        asset=fetch(tid)
        entries=db().execute('SELECT * FROM vehicle_history WHERE truck_id=? ORDER BY event_date DESC,id DESC',(tid,)).fetchall()
        files=db().execute("""SELECT f.id,f.history_id,f.filename,f.mime FROM vehicle_files f JOIN vehicle_history h ON h.id=f.history_id
            WHERE h.truck_id=? ORDER BY f.id""",(tid,)).fetchall()
        inspections=db().execute("""SELECT p.id,p.submitted_at,p.employee_name,p.failure_count,i.passed FROM bf_run_inspections i
            JOIN prestarts p ON p.id=i.prestart_id WHERE i.truck_id=? ORDER BY p.id DESC""",(tid,)).fetchall()
        attachments = {}
        for file in files:
            attachments.setdefault(file['history_id'], []).append(file)
        return render_template('fleet_asset.html',asset=asset,states=STATES,entries=entries,attachments=attachments,
            inspections=inspections,submitted=submitted,history_form=history_form,tab='prestart',key=secrets.token_urlsafe(24),day=now()[:10])

    @app.post('/admin/prestart/assets/<int:tid>/history')
    @require('admin')
    def fleet_history(tid):
        fetch(tid)
        try:
            kind = request.form.get('kind')
            summary = request.form.get('summary','').strip()
            day = iso_date(request.form.get('event_date', '').strip())
            key = request.form.get('key','')
            uploads=[u for u in request.files.getlist('files') if u.filename]
            if kind not in ('Service','Inspection','Registration','COC','Other document') or not day or not 1<=len(summary)<=2000 or not 20<=len(key)<=100:
                raise ValueError('Choose the record type, date and description.')
            if len(uploads)>MAX_STAFF_FILES:raise ValueError(f'Choose up to {MAX_STAFF_FILES} files per entry.')
            conn=db()
            with conn:
                conn.execute('BEGIN IMMEDIATE')
                existing = conn.execute('SELECT truck_id FROM vehicle_history WHERE request_key=?',(key,)).fetchone()
                if existing and existing['truck_id'] != tid:
                    raise ValueError('Open this vehicle’s service history and try again.')
                if not existing:
                    if kind == 'Service' and request.form.get('following_service'):
                        asset = fetch(tid)
                        if asset['version'] != request.form.get('version', type=int):
                            raise ValueError('The vehicle schedule changed. Review it before adding this service.')
                        due = iso_date(request.form['following_service'])
                        if due <= day:
                            raise ValueError('The next service must be after the service record date.')
                        days = notice_days(request.form.get('following_notice', ''))
                        conn.execute("""INSERT INTO vehicle_profiles(truck_id,make,model,rego,version,next_service_date,service_notice_days)
                            VALUES(?, '', '', '', 1, ?, ?) ON CONFLICT(truck_id) DO UPDATE SET
                            next_service_date=excluded.next_service_date,service_notice_days=excluded.service_notice_days,
                            version=version+1""", (tid, due, days))
                    save_history(conn, tid, kind, day, summary, key, uploads, app.config['DOCUMENT_MAX_BYTES'])
            flash('Vehicle record saved.','success')
        except ValueError as exc:
            message = str(exc)
            if any(f.filename for f in request.files.getlist('files')):
                message += ' Select your files again before saving.'
            flash(message,'error')
            return asset_page(tid, history_form=request.form), 422
        return redirect(url_for('fleet_asset', tid=tid, _anchor='service-history' if kind == 'Service' else 'asset-documents'),303)

    @app.get('/admin/prestart/assets/<int:tid>/files/<int:fid>')
    @require('admin')
    def fleet_file(tid,fid):
        row=db().execute('''SELECT f.* FROM vehicle_files f JOIN vehicle_history h ON h.id=f.history_id
            WHERE h.truck_id=? AND f.id=?''',(tid,fid)).fetchone()
        if not row:abort(404)
        result=send_file(io.BytesIO(row['data']),download_name=row['filename'],mimetype=row['mime'],
                         as_attachment=request.args.get('download') == '1')
        result.headers['Cache-Control']='private, no-store'
        return result
