"""Per-employee induction, with private, immutable editions and signing receipts."""
import hashlib
import io
import json
import secrets
from datetime import datetime, timezone
from pathlib import Path
from xml.sax.saxutils import escape
from zoneinfo import ZoneInfo

from flask import abort, flash, g, redirect, render_template, request, send_file, url_for
from pypdf import PdfReader, PdfWriter
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import Image, Paragraph, SimpleDocTemplate, Spacer

EDITION = '1.1'
# Edition 1.1 adds contractor guidance. The induction declarations are unchanged.
PREVIOUS_EQUIVALENT_EDITION = '1.0'
ROOT = Path(__file__).resolve().parent
TERMS = (ROOT / 'handbooks/acknowledgements.json').read_text()
# Only this exact release package can be installed. Private handbook bytes and
# employee contact details live on the application's persistent database.
PACKAGE_DIGEST = '48fc3a74cff581e9152840eefe7bdec3a5da5425b1a49e98c75193488bb15a33'
PUBLISH_KEY_DIGEST = '2d3e1fa14e6e56a57ed2544f4f31a2a906fa65e70a11267927afc82c972b4632'


def receipt_pdf(record, edition):
    terms = json.loads(edition['terms'])
    out = io.BytesIO()
    sheet = SimpleDocTemplate(out, pagesize=A4, leftMargin=22*mm, rightMargin=22*mm,
                              topMargin=15*mm, bottomMargin=15*mm)
    styles = getSampleStyleSheet()
    def para(value, style='BodyText'):
        return Paragraph(escape(value), styles[style])
    signed = datetime.fromisoformat(record['signed_at']).astimezone(ZoneInfo('Australia/Brisbane'))
    story = [Image(str(ROOT/'static/sb-empire-header.webp'),width=58*mm,height=23*mm,hAlign='LEFT'),
             para('Employee induction record','Title'),
             para('SB Empire Pty Ltd  |  Handbook edition '+edition['version']),
             para('Signed electronically by '+record['signature'],'Heading2'),
             para(signed.strftime('%d %B %Y at %I:%M %p')+' Brisbane time (AEST)'),
             para('Employee account '+record['username']+'  |  Record '+str(record['id']))]
    for key, title, content in terms['items']:
        story.extend([para(title,'Heading3'),para(content)])
    story.extend([Spacer(1,4*mm),para('Employee declaration','Heading3'),para(terms['consent'])])
    sheet.build(story)
    writer = PdfWriter()
    writer.append(PdfReader(io.BytesIO(edition['pdf'])))
    writer.append(PdfReader(io.BytesIO(out.getvalue())))
    writer.add_metadata({'/Title':'SB Empire signed employee handbook','/Author':'SB Empire Pty Ltd'})
    combined = io.BytesIO()
    writer.write(combined)
    combined.seek(0)
    return combined


def register_induction(app, db, require):
    with app.app_context():
        db().executescript("""
        CREATE TABLE IF NOT EXISTS handbook_editions (
            version TEXT PRIMARY KEY, pdf BLOB NOT NULL, contacts TEXT NOT NULL, terms TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS handbook_signatures (
            id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id),
            version TEXT NOT NULL REFERENCES handbook_editions(version), signature TEXT NOT NULL,
            username TEXT NOT NULL, signed_at TEXT NOT NULL, UNIQUE(user_id,version));
        """)

    def own_signature():
        return db().execute('''SELECT * FROM handbook_signatures WHERE user_id=? AND version IN (?,?)
            ORDER BY id DESC LIMIT 1''',(g.user['id'],EDITION,PREVIOUS_EQUIVALENT_EDITION)).fetchone()

    @app.context_processor
    def induction_status():
        if not g.user or g.user['role']!='employee' or g.get('bf_demo_token'):
            return {}
        return {'induction_pending':own_signature() is None}

    @app.post('/induction/install-edition')
    def install_induction_edition():
        # Narrow release credential only authorises installation of the pinned
        # document. This route cannot read data, replace an edition or sign for staff.
        bearer = request.headers.get('Authorization','')
        if not secrets.compare_digest(hashlib.sha256(bearer.encode()).hexdigest(),PUBLISH_KEY_DIGEST):
            abort(403)
        upload = request.files.get('handbook')
        if upload is None:
            abort(400)
        pdf = upload.read()
        contacts = request.form.get('contacts','')
        digest = hashlib.sha256(pdf+b'\0'+contacts.encode()+b'\0'+TERMS.encode()).hexdigest()
        if not secrets.compare_digest(digest,PACKAGE_DIGEST):
            abort(400)
        db().execute('INSERT OR IGNORE INTO handbook_editions VALUES(?,?,?,?)',(EDITION,pdf,contacts,TERMS))
        db().commit()
        return {'edition':EDITION,'installed':True}

    @app.route('/employee/induction',methods=['GET','POST'])
    @require('employee')
    def employee_induction():
        edition = db().execute('''SELECT version,contacts,terms FROM handbook_editions WHERE version IN (?,?)
            ORDER BY (version=?) DESC LIMIT 1''',(EDITION,PREVIOUS_EQUIVALENT_EDITION,EDITION)).fetchone()
        terms = json.loads(edition['terms']) if edition else None
        name = ' '.join((g.user['first_name']+' '+g.user['last_name']).split())
        if request.method=='POST':
            if not edition or request.form.get('version')!=edition['version']:
                abort(409,'The handbook edition changed. Reopen My induction before signing.')
            if not own_signature():
                typed = ' '.join(request.form.get('signature','').split())
                expected = {item[0] for item in terms['items']}
                if set(request.form.getlist('ack'))!=expected or request.form.get('consent')!='yes':
                    flash('Read and tick each acknowledgement and the electronic signature declaration.', 'error')
                elif not typed or typed.casefold()!=name.casefold():
                    flash('Enter your full name as shown on your account.', 'error')
                else:
                    db().execute("""INSERT INTO handbook_signatures(user_id,version,signature,username,signed_at)
                        VALUES(?,?,?,?,?) ON CONFLICT(user_id,version) DO NOTHING""",
                        (g.user['id'],edition['version'],typed,g.user['username'],datetime.now(timezone.utc).isoformat()))
                    db().commit()
                    return redirect(url_for('employee_induction'))
            else:
                return redirect(url_for('employee_induction'))
        records = db().execute('SELECT * FROM handbook_signatures WHERE user_id=? ORDER BY id DESC',(g.user['id'],)).fetchall()
        return render_template('induction.html',tab='induction',version=edition['version'] if edition else EDITION,terms=terms,
                               contacts=json.loads(edition['contacts']) if edition else [],
                               name=name,signed=own_signature(),records=records)

    @app.get('/employee/induction/handbook.pdf')
    @require('employee')
    def employee_handbook():
        row=db().execute('''SELECT pdf FROM handbook_editions WHERE version IN (?,?)
            ORDER BY (version=?) DESC LIMIT 1''',(EDITION,PREVIOUS_EQUIVALENT_EDITION,EDITION)).fetchone()
        if not row: abort(404)
        return send_file(io.BytesIO(row['pdf']),mimetype='application/pdf',download_name='SB-Empire-Employee-Handbook.pdf')

    @app.get('/employee/induction/records/<int:record_id>/signed.pdf')
    def induction_record(record_id):
        if not g.user:
            return redirect(url_for('login',audience='employee'))
        row=db().execute('SELECT * FROM handbook_signatures WHERE id=?',(record_id,)).fetchone()
        if not row or (g.user['role']!='admin' and g.user['id']!=row['user_id']):
            abort(404)
        edition=db().execute('SELECT * FROM handbook_editions WHERE version=?',(row['version'],)).fetchone()
        return send_file(receipt_pdf(row,edition),mimetype='application/pdf',as_attachment=True,
                         download_name=f'SB-Empire-Signed-Induction-{record_id}.pdf')
