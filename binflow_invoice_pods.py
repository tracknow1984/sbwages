"""Signed PODs linked by invoice source or explicitly attached by the office."""
import hashlib
import io
import json

from flask import abort, flash, g, redirect, request, send_file, url_for
from pypdf import PdfReader, PdfWriter
from pypdf.errors import PyPdfError

import binflow_client_access as access
import binflow_transfer_pods as transfer_pods
from binflow_management import pod_scope
from document_uploads import read_document


def migrate(conn):
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS bf_invoice_pod_files (
            id INTEGER PRIMARY KEY, invoice_id TEXT NOT NULL REFERENCES bf_rental_invoices(id) ON DELETE CASCADE,
            filename TEXT NOT NULL, digest TEXT NOT NULL, document BLOB NOT NULL, added_by TEXT NOT NULL,
            added_at TEXT NOT NULL, UNIQUE(invoice_id,digest));
        CREATE TABLE IF NOT EXISTS bf_invoice_pod_jobs (
            invoice_id TEXT NOT NULL REFERENCES bf_rental_invoices(id) ON DELETE CASCADE,
            job_id INTEGER NOT NULL REFERENCES bf_jobs(id), PRIMARY KEY(invoice_id,job_id));
        CREATE TABLE IF NOT EXISTS bf_invoice_pod_transfers (
            invoice_id TEXT NOT NULL REFERENCES bf_rental_invoices(id) ON DELETE CASCADE,
            booking_id INTEGER NOT NULL REFERENCES bf_interstate_bookings(id), PRIMARY KEY(invoice_id,booking_id));
    """)


def documents(conn, keys, client=None):
    """Fetch labels and identifiers only. Live PODs retain the client's existing site permissions."""
    if not keys:
        return {}
    wanted=json.dumps(list(keys))
    scope,params=pod_scope(client,g.client_rights) if client is not None else ("j.status='complete' AND j.pod_signed_at IS NOT NULL AND j.pod_deleted_at IS NULL",())
    rows=conn.execute(f"""
        WITH invoices AS (SELECT id,snapshot_json FROM bf_rental_invoices WHERE id IN (SELECT value FROM json_each(?))),
        jobs AS (
            SELECT i.id AS invoice_id,json_extract(l.value,'$.job_id') AS job_id FROM invoices i,json_each(i.snapshot_json,'$.lines') l
            UNION SELECT m.invoice_id,m.job_id FROM bf_invoice_pod_jobs m JOIN invoices i ON i.id=m.invoice_id)
        SELECT jobs.invoice_id,'job' AS kind,j.id AS id,j.code AS label FROM jobs JOIN bf_jobs j ON j.id=jobs.job_id WHERE {scope}
        UNION ALL SELECT f.invoice_id,'file',f.id,f.filename FROM bf_invoice_pod_files f JOIN invoices i ON i.id=f.invoice_id
        ORDER BY invoice_id,kind DESC,id""",(wanted,*params))
    result={}
    for row in rows:
        result.setdefault(row['invoice_id'],[]).append(dict(row))
    # Exact booking/source IDs also cover invoices prepared before their POD was signed.
    transfer_scope = ' AND COALESCE(b.account_id,i.portal_account_id)=?' if client is not None else ''
    transfers=conn.execute("""
        WITH invoices AS (SELECT id,booking_id,portal_account_id,snapshot_json FROM bf_rental_invoices
            WHERE id IN (SELECT value FROM json_each(?))),
        links AS (
            SELECT id AS invoice_id,booking_id FROM invoices
            UNION SELECT i.id,b.id FROM invoices i,json_each(i.snapshot_json,'$.lines') l
                JOIN bf_interstate_bookings b ON json_extract(l.value,'$.source')='transfer:'||b.id
            UNION SELECT m.invoice_id,m.booking_id FROM bf_invoice_pod_transfers m JOIN invoices i ON i.id=m.invoice_id)
        SELECT links.invoice_id,'transfer' AS kind,p.booking_id AS id,
            'Transfer POD #'||p.booking_id AS label
        FROM links JOIN bf_transfer_pods p ON p.booking_id=links.booking_id
        JOIN bf_interstate_bookings b ON b.id=p.booking_id
        JOIN invoices i ON i.id=links.invoice_id
        WHERE p.signed_at IS NOT NULL AND b.portal='binflow'"""+transfer_scope+
        ' ORDER BY links.invoice_id,p.booking_id',(wanted,client['id']) if client is not None else (wanted,))
    for row in transfers:
        result.setdefault(row['invoice_id'],[]).append(dict(row))
    return result


def transfer_content(app,conn,booking_id):
    pod=transfer_pods.record(conn,booking_id)
    if not pod or not pod['signed_at']:
        abort(404)
    return transfer_pods.pdf_bytes(pod,app.static_folder)


def file_content(conn,key,document_id):
    row=conn.execute('SELECT document FROM bf_invoice_pod_files WHERE invoice_id=? AND id=?',(key,document_id)).fetchone()
    if not row:
        abort(404)
    return row[0]


def document_response(data,name):
    response=send_file(io.BytesIO(data),mimetype='application/pdf',download_name=name,
                       as_attachment=request.args.get('download')=='1')
    response.cache_control.no_store=True
    return response


def bundle(source,parts):
    parts=iter(parts)
    first=next(parts,None)
    if first is None:
        return source
    writer=PdfWriter()
    try:
        for data in (source,first):
            writer.append(PdfReader(io.BytesIO(data)))
        for data in parts:
            writer.append(PdfReader(io.BytesIO(data)))
        out=io.BytesIO()
        writer.write(out)
        return out.getvalue()
    except (PyPdfError,ValueError,TypeError,KeyError,OSError,OverflowError,RecursionError) as exc:
        raise ValueError('A supporting document could not be combined. Review the attached PDFs.') from exc
    finally:
        writer.close()


def register(app,db,require,render,now,actor):
    def invoice(key):
        row=db().execute("""SELECT i.*,json_extract(i.snapshot_json,'$.contact.name') AS client,
            json_extract(i.snapshot_json,'$.contact.id') AS contact FROM bf_rental_invoices i WHERE id=?""",(key,)).fetchone()
        if not row:
            abort(404)
        return row

    def billing_account(item):
        aid=item['portal_account_id']
        if aid is None:
            matches=db().execute('SELECT account_id FROM bf_invoice_contacts WHERE tenant_id=? AND contact_id=?',
                                 (item['tenant_id'],item['contact'])).fetchall()
            aid=matches[0][0] if len(matches)==1 else None
        return aid

    def eligible_scope(item):
        aid=billing_account(item)
        return pod_scope({'id':aid},access.rights_for(db(),aid)) if aid else ('0',())

    @app.route('/binflow/invoices/<identifier>/pods',methods=['GET','POST'])
    @require('admin')
    def bf_invoice_pods(identifier):
        conn=db()
        if request.method=='POST':
            conn.execute('BEGIN IMMEDIATE')
        item=invoice(identifier)
        scope,params=eligible_scope(item)
        if request.method=='POST':
            try:
                if request.form.get('action')=='upload':
                    upload=request.files.get('document')
                    if not upload or not (upload.filename or '').lower().endswith('.pdf'):
                        raise ValueError('Choose a POD in PDF format.')
                    name,_,data=read_document(upload,app.config['DOCUMENT_MAX_BYTES'],verify_contents=True)
                    try:
                        reader=PdfReader(io.BytesIO(data))
                        if reader.is_encrypted or not len(reader.pages):
                            raise ValueError()
                    except (PyPdfError,ValueError,TypeError,KeyError,OSError,RecursionError) as exc:
                        raise ValueError('Choose a readable PDF with pages and no password.') from exc
                    cursor=conn.execute("""INSERT INTO bf_invoice_pod_files(invoice_id,filename,digest,document,added_by,added_at)
                        VALUES(?,?,?,?,?,?) ON CONFLICT(invoice_id,digest) DO NOTHING""",
                        (identifier,name,hashlib.sha256(data).hexdigest(),data,actor(),now()))
                elif request.form.get('action')=='link_transfer':
                    bid=request.form.get('booking_id',type=int)
                    pod=conn.execute("""SELECT p.booking_id FROM bf_transfer_pods p
                        JOIN bf_interstate_bookings b ON b.id=p.booking_id
                        WHERE p.booking_id=? AND p.signed_at IS NOT NULL AND b.portal='binflow' AND b.account_id=?""",
                        (bid,billing_account(item))).fetchone()
                    if not pod:
                        raise ValueError('Choose a signed transfer POD belonging to this billing client.')
                    cursor=conn.execute('INSERT OR IGNORE INTO bf_invoice_pod_transfers VALUES(?,?)',(identifier,bid))
                elif request.form.get('action')=='link':
                    code=request.form.get('code','').strip().upper()
                    pod=conn.execute('SELECT j.id FROM bf_jobs j WHERE j.code=? AND '+scope,(code,*params)).fetchone()
                    if not pod:
                        raise ValueError('Choose a completed, signed POD available to this billing client.')
                    cursor=conn.execute('INSERT OR IGNORE INTO bf_invoice_pod_jobs VALUES(?,?)',(identifier,pod['id']))
                elif request.form.get('action')=='remove':
                    doc=request.form.get('document_id',type=int)
                    if request.form.get('kind')=='file':
                        conn.execute('DELETE FROM bf_invoice_pod_files WHERE invoice_id=? AND id=?',(identifier,doc))
                    elif request.form.get('kind')=='job':
                        conn.execute('DELETE FROM bf_invoice_pod_jobs WHERE invoice_id=? AND job_id=?',(identifier,doc))
                    elif request.form.get('kind')=='transfer':
                        conn.execute('DELETE FROM bf_invoice_pod_transfers WHERE invoice_id=? AND booking_id=?',(identifier,doc))
                    else:
                        abort(400)
                    conn.commit()
                    flash('POD attachment removed.','success')
                    return redirect(url_for('bf_invoice_pods',identifier=identifier))
                else:
                    abort(400)
                conn.commit()
                flash('POD attached to the invoice.' if cursor.rowcount else 'This POD is already attached.','success')
            except ValueError as exc:
                conn.rollback()
                flash(str(exc),'error')
            return redirect(url_for('bf_invoice_pods',identifier=identifier))
        choices=conn.execute('SELECT j.code,j.site_name,j.scheduled_date FROM bf_jobs j WHERE '+scope+
            ' ORDER BY j.scheduled_date DESC,j.id DESC LIMIT 100',params).fetchall()
        automatic={line['job_id'] for line in json.loads(item['snapshot_json']).get('lines',[]) if line.get('job_id')}
        transfer_choices=conn.execute("""SELECT p.booking_id,p.service_date,b.pickup_company,b.delivery_company
            FROM bf_transfer_pods p JOIN bf_interstate_bookings b ON b.id=p.booking_id
            WHERE p.signed_at IS NOT NULL AND b.portal='binflow' AND b.account_id=?
            ORDER BY p.service_date DESC,p.booking_id DESC""",(billing_account(item),)).fetchall()
        sources={line.get('source') for line in json.loads(item['snapshot_json']).get('lines',[])}
        attached=documents(conn,[identifier]).get(identifier,[])
        transfer_automatic={doc['id'] for doc in attached if doc['kind']=='transfer'
                            and (doc['id']==item['booking_id'] or f"transfer:{doc['id']}" in sources)}
        return render('invoice_pods',section='invoices',invoice=item,choices=choices,automatic=automatic,
            transfer_choices=transfer_choices,transfer_automatic=transfer_automatic,
            documents=attached)

    @app.get('/binflow/invoices/<identifier>/pods/files/<int:doc_id>')
    @require('admin')
    def bf_invoice_pod_file(identifier,doc_id):
        row=db().execute('SELECT filename,document FROM bf_invoice_pod_files WHERE invoice_id=? AND id=?',(identifier,doc_id)).fetchone()
        if not row: abort(404)
        return document_response(row['document'],row['filename'])

