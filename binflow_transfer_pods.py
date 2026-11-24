"""Transfer work records and signed PODs, including retrospective service dates."""
import io
import json
from datetime import date, datetime
from pathlib import Path
from xml.sax.saxutils import escape
from zoneinfo import ZoneInfo

from flask import abort, flash, g, redirect, request, send_file
from prestart_media import signature_png
import sbtx

ROLES = {'recipient': 'Customer / recipient', 'driver': 'Driver', 'office': 'Office retrospective record'}
FIELDS = {'service_date': 10, 'work_done': 6000, 'driver_name': 150, 'registration': 100,
          'exceptions': 3000, 'signed_name': 150, 'signer_role': 20, 'signed_place': 180}


def migrate(conn):
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS bf_transfer_pods (
            booking_id INTEGER PRIMARY KEY REFERENCES bf_interstate_bookings(id),
            revision INTEGER NOT NULL, service_date TEXT NOT NULL, snapshot TEXT NOT NULL,
            created_at TEXT NOT NULL, created_by TEXT NOT NULL,
            updated_at TEXT NOT NULL, updated_by TEXT NOT NULL,
            signed_at TEXT, signed_by INTEGER REFERENCES users(id), signature BLOB);
        CREATE TABLE IF NOT EXISTS bf_transfer_pod_history (
            id INTEGER PRIMARY KEY, booking_id INTEGER NOT NULL REFERENCES bf_interstate_bookings(id),
            revision INTEGER NOT NULL, action TEXT NOT NULL, snapshot TEXT NOT NULL,
            recorded_at TEXT NOT NULL, recorded_by TEXT NOT NULL);
    """)


def record(conn, bid):
    return conn.execute('SELECT * FROM bf_transfer_pods WHERE booking_id=?', (bid,)).fetchone()


def client_records(conn, account_id, first='', last=''):
    scope, args = '', [account_id]
    if first:
        scope = ' AND p.service_date BETWEEN ? AND ?'
        args += [first, last]
    return conn.execute("""SELECT p.booking_id,p.service_date,p.signed_at,b.portal FROM bf_transfer_pods p
        JOIN bf_interstate_bookings b ON b.id=p.booking_id
        WHERE b.account_id=? AND b.portal='binflow' AND p.signed_at IS NOT NULL""" + scope +
        ' ORDER BY p.service_date DESC,p.booking_id DESC', args).fetchall()


def snapshot(conn, booking, values):
    po = conn.execute('SELECT number FROM bf_interstate_purchase_orders WHERE booking_id=?', (booking['id'],)).fetchone()
    result = {key: booking[key] for key in ('id', 'portal', 'client_name', 'pickup_company', 'pickup_address',
              'delivery_company', 'delivery_address', 'pickup_date', 'delivery_date', 'vehicle', 'goods')}
    result.update(values)
    result['po_number'] = po['number'] if po else ''
    return result


def pdf_bytes(pod, static_folder):
    import reportlab
    from reportlab.lib import colors
    from reportlab.lib.styles import getSampleStyleSheet
    from reportlab.lib.units import mm
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont
    from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Image, Table, TableStyle
    data = json.loads(pod['snapshot'])
    fonts = Path(reportlab.__file__).parent / 'fonts'
    for name, filename in [('TransferPOD', 'Vera.ttf'), ('TransferPOD-Bold', 'VeraBd.ttf')]:
        if name not in pdfmetrics.getRegisteredFontNames():
            pdfmetrics.registerFont(TTFont(name, str(fonts / filename)))
    styles = getSampleStyleSheet()
    for name, style in styles.byName.items():
        style.fontName = 'TransferPOD-Bold' if name.startswith('Heading') or name == 'Title' else 'TransferPOD'
        style.textColor = colors.HexColor('#183552')
    styles['BodyText'].fontSize = 9
    styles['BodyText'].leading = 13
    def p(value, style='BodyText'):
        return Paragraph(escape(str(value)).replace('\n', '<br/>'), styles[style])
    def day(value):
        return date.fromisoformat(value).strftime('%d/%m/%Y') if value else 'Not recorded'
    def stamp(value):
        return datetime.fromisoformat(value).astimezone(ZoneInfo('Australia/Brisbane')).strftime('%d/%m/%Y %H:%M AEST')
    reference = f"IT-{data['id']:06d}"
    output = io.BytesIO()
    document = SimpleDocTemplate(output, pagesize=(210*mm,297*mm), leftMargin=18*mm, rightMargin=18*mm,
                                 topMargin=15*mm, bottomMargin=18*mm, title=reference+' Proof of delivery / work')
    empire = Image(str(Path(static_folder) / 'sb-empire-black.webp'))
    empire._restrictSize(48*mm, 28*mm)
    empire.hAlign = 'LEFT'
    partner = ''
    if data['portal'] == 'binflow':
        mark = Image(str(Path(static_folder) / 'binflow-touch.png'), width=14*mm, height=14*mm)
        wordmark = Paragraph('<font name="TransferPOD-Bold" size="19" color="#102c4c">BINFLOW</font>', styles['BodyText'])
        partner = Table([[mark, wordmark]], colWidths=[18*mm, 39*mm])
        partner.setStyle(TableStyle([('VALIGN',(0,0),(-1,-1),'MIDDLE'),
                                    ('LEFTPADDING',(0,0),(-1,-1),0),('RIGHTPADDING',(0,0),(-1,-1),0)]))
    header = Table([[empire, partner]], colWidths=[117*mm, 57*mm])
    header.setStyle(TableStyle([('VALIGN',(0,0),(-1,-1),'MIDDLE'),
                               ('LEFTPADDING',(0,0),(-1,-1),0),('RIGHTPADDING',(0,0),(-1,-1),0),
                               ('BOTTOMPADDING',(0,0),(-1,-1),10),
                               ('LINEBELOW',(0,0),(-1,-1),0.6,colors.HexColor('#d6deeb'))]))
    story = [header, Spacer(1, 4*mm)]
    story += [p('PROOF OF DELIVERY / WORK COMPLETED', 'Heading1'),
              p(reference+' · '+('SIGNED' if pod['signed_at'] else 'DRAFT — NOT SIGNED'), 'Heading2')]
    for label, value in [('Customer',data['client_name']), ('PO number',data['po_number'] or 'Not provided'),
                         ('Work / service date',day(data['service_date'])),
                         ('Pickup',data['pickup_company']+' · '+data['pickup_address']),
                         ('Delivery',data['delivery_company']+' · '+data['delivery_address']),
                         ('Booked pickup / delivery',day(data['pickup_date'])+' / '+day(data['delivery_date'])),
                         ('Vehicle / registration',(data['vehicle'] or 'Not recorded')+' / '+(data['registration'] or 'Not recorded')),
                         ('Driver / operator',data['driver_name'] or 'Not recorded')]:
        story.append(p(label+': '+value))
    story += [p('Work conducted and quantities', 'Heading2'),p(data['work_done']),
              p('Exceptions / supporting details', 'Heading2'),p(data['exceptions'] or 'None recorded')]
    if pod['signed_at']:
        story += [p('Sign-off', 'Heading2'),p(data['signed_name']+' · '+ROLES[data['signer_role']]),
                  p('Signing place: '+data['signed_place']+' (entered by signer)'),
                  p('Signed on: '+stamp(pod['signed_at'])),
                  p('Recorded by: '+pod['updated_by']),
                  Image(io.BytesIO(pod['signature']), width=75*mm, height=25*mm),
                  p('I confirm the work recorded above was conducted on the stated service date, subject to the exceptions listed.')]
        if data['signer_role'] == 'office':
            story.append(p('Office retrospective record. This is an office attestation, not a customer receipt signature.'))
        story.append(p('The work date and the date this record was signed are recorded separately.'))
    else:
        story.append(p('DRAFT — This record has not been signed and does not certify completion.', 'Heading2'))
    story += [Spacer(1,5*mm),p('POD raised: '+stamp(pod['created_at'])+' · Revision '+str(pod['revision']))]
    def footer(canvas, doc):
        canvas.setFont('TransferPOD', 8)
        canvas.drawString(18*mm, 10*mm, 'SB EMPIRE · '+reference+' · '+('SIGNED POD' if pod['signed_at'] else 'DRAFT / UNSIGNED'))
        canvas.drawRightString(192*mm, 10*mm, 'Page '+str(doc.page))
    document.build(story, onFirstPage=footer, onLaterPages=footer)
    return output.getvalue()


def register(app, db, access, client_access, page, booking, office, client, now, actor):
    def show(bid, external=False, error=None, status=200):
        b = booking(bid, external)
        pod = record(db(), bid)
        if external and (not pod or not pod['signed_at']):
            abort(404)
        values = json.loads(pod['snapshot']) if pod else dict(service_date=b['delivery_date'] or b['pickup_date'],
                  work_done=b['goods'], driver_name='', registration='', exceptions='', signed_name='', signer_role='recipient', signed_place='')
        if error:
            values.update({key: request.form.get(key, '') for key in FIELDS})
        return page('transfer_pod', external, b=b, pod=pod, values=values, error=error, roles=ROLES), status

    @office.route('/transfers/<int:bid>/pod', endpoint='pod', methods=['GET','POST'])
    @app.route('/binflow/interstate-transfers/<int:bid>/pod', methods=['GET','POST'])
    @access
    def bf_interstate_pod(bid):
        if request.method == 'GET':
            return show(bid)
        conn = db()
        conn.execute('BEGIN IMMEDIATE')
        b = booking(bid)
        pod = record(conn, bid)
        if b['cancelled_at']:
            abort(409, 'This transfer is cancelled. Existing PODs remain available to view.')
        if pod and pod['signed_at']:
            abort(409, 'This POD is already signed. The signed record cannot be overwritten.')
        if request.form.get('revision', type=int) != (pod['revision'] if pod else 0) or request.form.get('booking_version', type=int) != b['version']:
            abort(409, 'This booking or POD changed. Reopen it before saving.')
        try:
            action = request.form.get('action')
            if action not in ('draft','sign'):
                raise ValueError('Choose Save draft or Sign off POD.')
            values = {key: request.form.get(key, '').strip() for key in FIELDS}
            if any(len(values[key]) > maximum for key,maximum in FIELDS.items()):
                raise ValueError('One of the fields is too long. Shorten it before saving.')
            try:
                service = date.fromisoformat(values['service_date'])
            except ValueError:
                raise ValueError('Enter the actual work / service date.') from None
            if not values['work_done']:
                raise ValueError('Describe the work conducted and the quantities handled.')
            if values['signer_role'] not in ROLES:
                raise ValueError('Choose who is signing this POD.')
            stamp = now()
            signature = None
            if action == 'sign':
                if service > datetime.fromisoformat(stamp).astimezone(ZoneInfo('Australia/Brisbane')).date():
                    raise ValueError('Completed work cannot have a future service date. Save a draft until the work is done.')
                if not values['signed_name'] or not values['signed_place']:
                    raise ValueError('Enter the signer’s name and current signing place.')
                if request.form.get('confirm') != 'yes':
                    raise ValueError('Confirm that the work was conducted on the stated service date.')
                if values['signer_role'] == 'office' and not values['exceptions']:
                    raise ValueError('For an office retrospective record, explain the records or information used to confirm the work.')
                signature = signature_png(request.form.get('signature',''))
            payload = json.dumps(snapshot(conn, b, values))
            revision = pod['revision'] + 1 if pod else 1
            signed = stamp if action == 'sign' else None
            conn.execute("""INSERT INTO bf_transfer_pods(booking_id,revision,service_date,snapshot,created_at,created_by,
                updated_at,updated_by,signed_at,signed_by,signature) VALUES(?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(booking_id) DO UPDATE SET revision=excluded.revision,service_date=excluded.service_date,
                snapshot=excluded.snapshot,updated_at=excluded.updated_at,updated_by=excluded.updated_by,
                signed_at=excluded.signed_at,signed_by=excluded.signed_by,signature=excluded.signature""",
                (bid,revision,service.isoformat(),payload,stamp,actor(),stamp,actor(),signed,g.user['id'] if signed else None,signature))
            conn.execute('INSERT INTO bf_transfer_pod_history(booking_id,revision,action,snapshot,recorded_at,recorded_by) VALUES(?,?,?,?,?,?)',
                         (bid,revision,'Signed' if signed else 'Draft saved',payload,stamp,actor()))
            conn.commit()
        except ValueError as error:
            conn.rollback()
            return show(bid, error=str(error), status=400)
        flash('POD signed off and saved.' if signed else 'POD draft saved. You can return to sign it off.', 'success')
        if signed:
            return redirect(sbtx.transfer_url('calendar', False, date=b['pickup_date']), code=303)
        return redirect(sbtx.transfer_url('pod', False, bid=bid), code=303)

    @client.get('/transfers/<int:bid>/pod', endpoint='pod')
    @app.get('/binflow/client/interstate-transfers/<int:bid>/pod')
    @client_access
    def bf_client_interstate_pod(bid):
        return show(bid, True)

    def document(bid, external):
        booking(bid, external)
        pod = record(db(), bid)
        if not pod or external and not pod['signed_at']:
            abort(404)
        response = send_file(io.BytesIO(pdf_bytes(pod, app.static_folder)), mimetype='application/pdf',
                             download_name=f"IT-{bid:06d}-POD{'-DRAFT' if not pod['signed_at'] else ''}.pdf",
                             as_attachment=request.args.get('download') == '1')
        response.cache_control.no_store = True
        return response

    @office.get('/transfers/<int:bid>/pod.pdf', endpoint='pod_pdf')
    @app.get('/binflow/interstate-transfers/<int:bid>/pod.pdf')
    @access
    def bf_interstate_pod_pdf(bid):
        return document(bid, False)

    @client.get('/transfers/<int:bid>/pod.pdf', endpoint='pod_pdf')
    @app.get('/binflow/client/interstate-transfers/<int:bid>/pod.pdf')
    @client_access
    def bf_client_interstate_pod_pdf(bid):
        return document(bid, True)
