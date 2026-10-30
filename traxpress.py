"""Traxpress stock counts and handling records. All writes use the existing admin gate."""
import csv
import io
import json
import re
import secrets
from datetime import datetime
from decimal import Decimal, ROUND_HALF_UP
from xml.sax.saxutils import escape
from zoneinfo import ZoneInfo

from flask import abort, flash, g, redirect, render_template, request, send_file, url_for
from itsdangerous import BadSignature, URLSafeTimedSerializer

from prestart_media import photo_jpeg

RATE_FIELDS = (
    ('unload', 'Bulk unloading / hour', 'Includes forklift'),
    ('prepare', 'Preparation and loading / hour', 'Time spent preparing and loading a stock pick'),
    ('loading', 'Loading / pallet', 'Per pallet loaded'),
    ('pallet', 'Pallet supplied / each', 'Only pallets supplied by SB Empire'),
    ('strap', 'Strapping / strap', 'Per strap used'),
)
KINDS = {'bulk_unload': 'Bulk unload', 'stock_pick': 'Stock pick'}
SCHEMA = '''
CREATE TABLE IF NOT EXISTS tx_rates (
 id INTEGER PRIMARY KEY, unload INTEGER NOT NULL CHECK(unload>=0),
 prepare INTEGER NOT NULL CHECK(prepare>=0), loading INTEGER NOT NULL CHECK(loading>=0),
 pallet INTEGER NOT NULL CHECK(pallet>=0), strap INTEGER NOT NULL CHECK(strap>=0),
 created_at TEXT NOT NULL, created_by INTEGER NOT NULL REFERENCES users(id)
);
CREATE TABLE IF NOT EXISTS tx_stocktakes (
 id INTEGER PRIMARY KEY, stock_date TEXT NOT NULL, notes TEXT NOT NULL,
 created_at TEXT NOT NULL, created_by INTEGER NOT NULL REFERENCES users(id),
 recorded_by TEXT NOT NULL, submission_token TEXT NOT NULL UNIQUE
);
CREATE TABLE IF NOT EXISTS tx_stock_lines (
 id INTEGER PRIMARY KEY, stocktake_id INTEGER NOT NULL REFERENCES tx_stocktakes(id),
 tyre_size TEXT NOT NULL, quantity INTEGER NOT NULL CHECK(quantity>=0),
 UNIQUE(stocktake_id,tyre_size)
);
CREATE TABLE IF NOT EXISTS tx_handling (
 id INTEGER PRIMARY KEY, kind TEXT NOT NULL CHECK(kind IN ('bulk_unload','stock_pick')),
 work_date TEXT NOT NULL, reference TEXT NOT NULL, employee_id INTEGER NOT NULL REFERENCES users(id),
 employee_name TEXT NOT NULL, hours_units INTEGER NOT NULL CHECK(hours_units>0),
 pallets_loaded INTEGER NOT NULL CHECK(pallets_loaded>=0),
 pallets_supplied INTEGER NOT NULL CHECK(pallets_supplied>=0),
 straps INTEGER NOT NULL CHECK(straps>=0), notes TEXT NOT NULL,
 rate_id INTEGER NOT NULL REFERENCES tx_rates(id), charges_json TEXT NOT NULL,
 total_cents INTEGER NOT NULL CHECK(total_cents>=0),
 created_at TEXT NOT NULL, created_by INTEGER NOT NULL REFERENCES users(id),
 recorded_by TEXT NOT NULL, submission_token TEXT NOT NULL UNIQUE
);
CREATE TABLE IF NOT EXISTS tx_delivery_photos (
 handling_id INTEGER PRIMARY KEY REFERENCES tx_handling(id), data BLOB NOT NULL
);
CREATE INDEX IF NOT EXISTS tx_stock_lines_parent ON tx_stock_lines(stocktake_id);
CREATE INDEX IF NOT EXISTS tx_handling_date ON tx_handling(work_date,id);
'''


def stamp():
    return datetime.now(ZoneInfo('Australia/Brisbane')).isoformat(timespec='seconds')


def whole(raw, label, maximum=1000000):
    if not re.fullmatch(r'\d{1,7}', str(raw)) or int(raw) > maximum:
        raise ValueError(f'{label} must be a whole number between 0 and {maximum:,}.')
    return int(raw)


def clean_text(raw, label, maximum, required=False):
    value = raw.strip()
    if len(value) > maximum or (required and not value) or any(ord(c) < 32 and c not in '\n\t' for c in value):
        raise ValueError(f'Enter {label.lower()} using {1 if required else 0}–{maximum} characters.')
    return value


def charges(kind, hours_units, loaded, supplied, straps, rates):
    """Round each billable line once using decimal arithmetic, never binary floats."""
    lines = []
    def line(label, quantity, rate, unit):
        total = int((quantity * rate).quantize(Decimal('1'), rounding=ROUND_HALF_UP))
        lines.append(dict(label=label, quantity=format(quantity.normalize(), 'f'), unit=unit, rate_cents=rate, total_cents=total))
    line('Bulk unloading (forklift included)' if kind == 'bulk_unload' else 'Preparation and loading',
         Decimal(hours_units) / 100, rates['unload' if kind == 'bulk_unload' else 'prepare'], 'hour')
    if kind == 'stock_pick':
        for label, quantity, rate, unit in [('Pallet loading', loaded, rates['loading'], 'pallet'),
                                           ('Pallets supplied', supplied, rates['pallet'], 'pallet'),
                                           ('Strapping', straps, rates['strap'], 'strap')]:
            line(label, Decimal(quantity), rate, unit)
    return lines, sum(item['total_cents'] for item in lines)


def csv_response(filename, rows):
    output = io.StringIO(newline='')
    writer = csv.writer(output)
    # Keep user text inert when the file is opened in spreadsheet software.
    def cell(value):
        value = str(value if value is not None else '')
        return "'" + value if value.lstrip().startswith(('=', '+', '-', '@')) or value.startswith(('\t', '\r', '\n')) else value
    for row in rows:
        writer.writerow([cell(value) for value in row])
    return send_file(io.BytesIO(('\ufeff' + output.getvalue()).encode('utf-8')), mimetype='text/csv',
                     as_attachment=True, download_name=filename)


def record_pdf(kind, record, rows):
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import getSampleStyleSheet
    from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle
    styles = getSampleStyleSheet()
    output = io.BytesIO()
    doc = SimpleDocTemplate(output, pagesize=A4, rightMargin=38, leftMargin=38, topMargin=38, bottomMargin=38)
    title = f"Stock take ST-{record['id']:05d}" if kind == 'stock' else f"{KINDS[record['kind']]} H-{record['id']:05d}"
    p = lambda text: Paragraph(escape(str(text)).replace('\n', '<br/>'), styles['BodyText'])
    story = [Paragraph('SB EMPIRE / TRAXPRESS', styles['Title']), Paragraph(escape(title), styles['Heading1'])]
    info = [('Work date', record['stock_date' if kind == 'stock' else 'work_date']),
            ('Recorded', record['created_at'] + ' (Brisbane)'), ('Recorded by', record['recorded_by'])]
    if kind != 'stock':
        info += [('Employee', record['employee_name']), ('Delivery / order reference', record['reference'] or '—')]
    for key, value in info:
        story.append(p(f'{key}: {value}'))
    story.append(Spacer(1, 16))
    if kind == 'stock':
        data = [['Tyre size', 'Quantity']] + [[r['tyre_size'], r['quantity']] for r in rows]
        data.append(['TOTAL TYRES', sum(r['quantity'] for r in rows)])
        widths = [340, 179]
    else:
        data = [['Charge', 'Quantity', 'Rate AUD', 'Amount AUD']]
        data += [[r['label'], r['quantity'] + ' ' + r['unit'], f"${r['rate_cents']/100:,.2f}", f"${r['total_cents']/100:,.2f}"] for r in rows]
        data.append(['TOTAL EX GST', '', '', f"${record['total_cents']/100:,.2f}"])
        widths = [230, 95, 90, 104]
    table = Table([[p(v) for v in row] for row in data], colWidths=widths, repeatRows=1, hAlign='LEFT')
    table.setStyle(TableStyle([('BACKGROUND', (0,0), (-1,0), colors.HexColor('#eaf2ff')),
                              ('BACKGROUND', (0,-1), (-1,-1), colors.HexColor('#f1f5f9')),
                              ('VALIGN', (0,0), (-1,-1), 'TOP'), ('BOTTOMPADDING', (0,0), (-1,-1), 10),
                              ('TOPPADDING', (0,0), (-1,-1), 10),
                              ('LINEBELOW', (0,0), (-1,-1), .3, colors.HexColor('#e2e8f0'))]))
    story.append(table)
    if record['notes']:
        story += [Spacer(1,16), p('Notes: ' + record['notes'])]
    if kind != 'stock':
        story += [Spacer(1,16), p('All amounts are AUD excluding GST. Handling record — not a tax invoice.')]
    doc.build(story)
    output.seek(0)
    return send_file(output, mimetype='application/pdf', as_attachment=True,
                     download_name=f'traxpress-{kind}-{record["id"]:05d}.pdf')


def register_traxpress(app, db, require):
    from app import decimal_units, parse_calendar_date
    with app.app_context():
        db().executescript(SCHEMA)
        db().commit()
    signer = URLSafeTimedSerializer(app.secret_key, salt='traxpress-submission')

    def token(scope):
        return signer.dumps([g.user['id'], scope, secrets.token_urlsafe(16)])

    def checked_token(scope):
        value = request.form.get('submission_token', '')
        try:
            payload = signer.loads(value, max_age=86400)
            if payload[:2] != [g.user['id'], scope]:
                raise ValueError()
        except (BadSignature, ValueError, TypeError):
            raise ValueError('This form has expired. Refresh the page and try again.') from None
        return value

    def name(user):
        return (user['first_name'] + ' ' + user['last_name']).strip()

    def rate_card():
        return db().execute('SELECT * FROM tx_rates ORDER BY id DESC LIMIT 1').fetchone()

    def day(field):
        try:
            value = parse_calendar_date(request.form.get(field, ''))
            if value > datetime.now(ZoneInfo('Australia/Brisbane')).date():
                raise ValueError()
            return value.isoformat()
        except ValueError:
            raise ValueError('Choose a valid work date, today or earlier.') from None

    def page(template, section, **kwargs):
        return render_template('traxpress/' + template, tab='traxpress', tx_section=section, kinds=KINDS, **kwargs)

    def current_page():
        try:
            return max(1, min(int(request.args.get('page', '1')), 100000))
        except ValueError:
            return 1

    @app.route('/traxpress/stock-take', methods=['GET','POST'])
    @require('admin')
    def tx_stock():
        status = 200
        values = request.form if request.method == 'POST' else {}
        inputs = list(zip(request.form.getlist('tyre_size'), request.form.getlist('quantity'))) if values else [('', '')]
        if request.method == 'POST':
            try:
                submission = checked_token('stock')
                existing = db().execute('SELECT id FROM tx_stocktakes WHERE submission_token=?', (submission,)).fetchone()
                if existing:
                    return redirect(url_for('tx_stock_record', record_id=existing['id']))
                sizes, quantities = request.form.getlist('tyre_size'), request.form.getlist('quantity')
                if len(sizes) != len(quantities) or not 1 <= len(sizes) <= 200:
                    raise ValueError('Enter between 1 and 200 tyre sizes.')
                lines, seen = [], set()
                for size, qty in zip(sizes, quantities):
                    if not size.strip() and not qty.strip():
                        continue
                    size = clean_text(size, 'Tyre size', 80, True).upper()
                    size = re.sub(r'\s+', ' ', size)
                    key = re.sub(r'\s+', '', size)
                    if key in seen:
                        raise ValueError(f'{size} appears more than once. Combine its quantities into one row.')
                    seen.add(key)
                    lines.append((size, whole(qty, 'Quantity')))
                if not lines:
                    raise ValueError('Add at least one tyre size and quantity. Zero stock is allowed.')
                stock_date = day('stock_date')
                notes = clean_text(request.form.get('notes', ''), 'Notes', 2000)
                with db():
                    result = db().execute('''INSERT OR IGNORE INTO tx_stocktakes
                        (stock_date,notes,created_at,created_by,recorded_by,submission_token) VALUES(?,?,?,?,?,?)''',
                        (stock_date,notes,stamp(),g.user['id'],name(g.user),submission))
                    saved = db().execute('SELECT id FROM tx_stocktakes WHERE submission_token=?', (submission,)).fetchone()['id']
                    if result.rowcount:
                        db().executemany('INSERT INTO tx_stock_lines(stocktake_id,tyre_size,quantity) VALUES(?,?,?)',
                                         [(saved, size, qty) for size,qty in lines])
                flash('Stock take saved. Download the record as PDF or CSV.', 'success')
                return redirect(url_for('tx_stock_record', record_id=saved))
            except ValueError as error:
                flash(str(error), 'error')
                status = 400
        number = current_page()
        records = db().execute('''SELECT s.*,COUNT(l.id) AS size_count,SUM(l.quantity) AS tyre_count
            FROM tx_stocktakes s JOIN tx_stock_lines l ON l.stocktake_id=s.id GROUP BY s.id
            ORDER BY s.id DESC LIMIT 51 OFFSET ?''', ((number-1)*50,)).fetchall()
        return page('stock.html', 'stock', records=records[:50], more=len(records)>50, page_number=number,
                    values=values, inputs=inputs, submission_token=token('stock')), status

    @app.get('/traxpress/stock-take/<int:record_id>')
    @require('admin')
    def tx_stock_record(record_id):
        record = db().execute('SELECT * FROM tx_stocktakes WHERE id=?', (record_id,)).fetchone()
        if not record:
            abort(404)
        lines = db().execute('SELECT * FROM tx_stock_lines WHERE stocktake_id=? ORDER BY id', (record_id,)).fetchall()
        fmt = request.args.get('download')
        if fmt == 'csv':
            rows = [['Record', 'Stock take date', 'Recorded at (Brisbane)', 'Recorded by', 'Tyre size', 'Quantity', 'Notes']]
            rows += [[f'ST-{record_id:05d}', record['stock_date'], record['created_at'], record['recorded_by'], r['tyre_size'], r['quantity'],record['notes']] for r in lines]
            return csv_response(f'traxpress-stock-{record_id:05d}.csv', rows)
        if fmt == 'pdf':
            return record_pdf('stock', record, lines)
        return page('record.html', 'stock', record=record, lines=lines, stock=True,
                    total=sum(r['quantity'] for r in lines))

    @app.route('/traxpress/setup', methods=['GET','POST'])
    @require('admin')
    def tx_setup():
        rates = rate_card()
        values = request.form if request.method == 'POST' else {key: f'{rates[key]/100:.2f}' if rates else '' for key,_,_ in RATE_FIELDS}
        status = 200
        if request.method == 'POST':
            try:
                parsed = [decimal_units(request.form.get(key, ''), label, 100000) for key,label,_ in RATE_FIELDS]
                with db():
                    db().execute('BEGIN IMMEDIATE')
                    latest = rate_card()
                    if str(latest['id'] if latest else 0) != request.form.get('rate_version'):
                        raise ValueError('Rates changed while this form was open. Review the latest rates before saving.')
                    db().execute('INSERT INTO tx_rates(unload,prepare,loading,pallet,strap,created_at,created_by) VALUES(?,?,?,?,?,?,?)',
                                 (*parsed,stamp(),g.user['id']))
                flash('Traxpress rates saved. Existing handling records keep their original charges.', 'success')
                return redirect(url_for('tx_setup'))
            except ValueError as error:
                flash(str(error), 'error')
                status = 400
                rates = rate_card()
        return page('setup.html', 'setup', values=values, rates=rates, fields=RATE_FIELDS), status

    @app.route('/traxpress/handling', methods=['GET','POST'])
    @require('admin')
    def tx_handling():
        kind = request.form.get('kind', request.args.get('kind', 'bulk_unload'))
        if kind not in KINDS:
            abort(400, 'Choose Bulk unload or Stock pick.')
        rates = rate_card()
        status = 200
        if request.method == 'POST':
            try:
                submission = checked_token('handling')
                existing = db().execute('SELECT id FROM tx_handling WHERE submission_token=?', (submission,)).fetchone()
                if existing:
                    return redirect(url_for('tx_handling_record', record_id=existing['id']))
                if not rates:
                    raise ValueError('Save your handling rates in Setup before recording a job.')
                if request.form.get('rate_version') != str(rates['id']):
                    raise ValueError('Rates changed while this form was open. Review the current estimate and submit again.')
                employee_id = whole(request.form.get('employee_id',''), 'Employee ID')
                employee = db().execute("SELECT * FROM users WHERE id=? AND role='employee' AND status='active'", (employee_id,)).fetchone()
                if not employee:
                    raise ValueError('Choose an active employee from the SB Empire staff list.')
                work_date = day('work_date')
                reference = clean_text(request.form.get('reference',''), 'Reference', 120)
                notes = clean_text(request.form.get('notes',''), 'Notes', 2000)
                hours = decimal_units(request.form.get('hours',''), 'Hours taken', 24)
                if hours == 0:
                    raise ValueError('Hours taken must be greater than zero.')
                loaded = whole(request.form.get('pallets_loaded',''), 'Pallets loaded', 10000) if kind == 'stock_pick' else 0
                supplied = whole(request.form.get('pallets_supplied',''), 'Pallets supplied', 10000) if kind == 'stock_pick' else 0
                straps = whole(request.form.get('straps',''), 'Straps used', 100000) if kind == 'stock_pick' else 0
                if supplied > loaded:
                    raise ValueError('Pallets supplied cannot exceed the number of pallets loaded.')
                photo = None
                upload = request.files.get('delivery_note')
                if kind == 'bulk_unload':
                    if not upload or not upload.filename:
                        raise ValueError('Take or upload a photo of the delivery note.')
                    photo = photo_jpeg(upload)
                line_items, total = charges(kind, hours, loaded, supplied, straps, rates)
                with db():
                    result = db().execute('''INSERT OR IGNORE INTO tx_handling
                        (kind,work_date,reference,employee_id,employee_name,hours_units,pallets_loaded,pallets_supplied,
                         straps,notes,rate_id,charges_json,total_cents,created_at,created_by,recorded_by,submission_token)
                        VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                        (kind,work_date,reference,employee_id,name(employee),hours,loaded,supplied,straps,notes,
                         rates['id'],json.dumps(line_items),total,stamp(),g.user['id'],name(g.user),submission))
                    saved = db().execute('SELECT id FROM tx_handling WHERE submission_token=?', (submission,)).fetchone()['id']
                    if result.rowcount and photo:
                        db().execute('INSERT INTO tx_delivery_photos(handling_id,data) VALUES(?,?)', (saved,photo))
                flash(KINDS[kind] + ' saved with the employee, timestamp and charges.', 'success')
                return redirect(url_for('tx_handling_record', record_id=saved))
            except ValueError as error:
                flash(str(error), 'error')
                status = 400
        number = current_page()
        records = db().execute('SELECT * FROM tx_handling ORDER BY id DESC LIMIT 51 OFFSET ?', ((number-1)*50,)).fetchall()
        employees = db().execute("SELECT id,first_name,last_name FROM users WHERE role='employee' AND status='active' ORDER BY first_name,last_name").fetchall()
        return page('handling.html', 'handling', kind=kind, rates=rates, values=request.form,
                    employees=employees, records=records[:50], more=len(records)>50, page_number=number,
                    submission_token=token('handling')), status

    @app.get('/traxpress/handling/<int:record_id>')
    @require('admin')
    def tx_handling_record(record_id):
        record = db().execute('SELECT * FROM tx_handling WHERE id=?', (record_id,)).fetchone()
        if not record:
            abort(404)
        lines = json.loads(record['charges_json'])
        if request.args.get('download') == 'pdf':
            return record_pdf('handling', record, lines)
        if request.args.get('download') == 'csv':
            rows = [['Record','Type','Work date','Reference','Employee','Recorded at (Brisbane)','Recorded by','Charge','Quantity','Unit','Rate AUD ex GST','Amount AUD ex GST','Notes']]
            rows += [[f'H-{record_id:05d}', KINDS[record['kind']],record['work_date'],record['reference'],record['employee_name'],
                      record['created_at'],record['recorded_by'],r['label'],r['quantity'],r['unit'],f"{r['rate_cents']/100:.2f}",f"{r['total_cents']/100:.2f}",record['notes']] for r in lines]
            return csv_response(f'traxpress-handling-{record_id:05d}.csv', rows)
        return page('record.html', 'handling', stock=False, record=record, lines=lines, total=record['total_cents'])

    @app.get('/traxpress/handling/<int:record_id>/delivery-note')
    @require('admin')
    def tx_delivery_note(record_id):
        row = db().execute('SELECT data FROM tx_delivery_photos WHERE handling_id=?', (record_id,)).fetchone()
        if not row:
            abort(404)
        return send_file(io.BytesIO(row['data']), mimetype='image/jpeg',
                         as_attachment=request.args.get('download') == '1', download_name=f'traxpress-delivery-note-{record_id:05d}.jpg')

    @app.get('/traxpress/handling/export.csv')
    @require('admin')
    def tx_handling_export():
        rows = [['Record','Type','Work date','Reference','Employee','Hours','Pallets loaded','Pallets supplied','Straps',
                 'Total AUD ex GST','Recorded at (Brisbane)','Recorded by','Notes']]
        rows += [[f'H-{r["id"]:05d}', KINDS[r['kind']],r['work_date'],r['reference'],r['employee_name'],
                  f"{r['hours_units']/100:.2f}",r['pallets_loaded'],r['pallets_supplied'],r['straps'],
                  f"{r['total_cents']/100:.2f}",r['created_at'],r['recorded_by'],r['notes']]
                 for r in db().execute('SELECT * FROM tx_handling ORDER BY id DESC')]
        return csv_response('traxpress-handling-register.csv', rows)
