"""Admin invoice documents and selected invoice / POD register exports."""
import io
import json
from datetime import date
from xml.sax.saxutils import escape

from flask import abort,request
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import Paragraph,SimpleDocTemplate,Spacer
from werkzeug.utils import secure_filename

import binflow_period_reports as reports
import binflow_invoice_pods as invoice_pods
import binflow_xero as xero

# A prepared invoice has an issue date. An unissued operational draft belongs to its period end.
INVOICE_DATE = "COALESCE(NULLIF(substr(json_extract(i.payload_json,'$.Invoices[0].Date'),1,10),''),i.period_end,d.end)"


def invoice_data(conn,key):
    item=conn.execute("""SELECT i.*,c.name AS category FROM bf_rental_invoices i
        JOIN bf_invoice_categories c ON c.id=i.category_id WHERE i.id=?""",(key,)).fetchone()
    draft=conn.execute('SELECT * FROM bf_auto_drafts WHERE id=?',(key,)).fetchone()
    if not item and not draft: abort(404)
    if item:
        snap=json.loads(item['snapshot_json'])
        payload=json.loads(item['payload_json']).get('Invoices') or [{}]
        return dict(id=key,item=item,lines=snap.get('lines',[]),number=item['invoice_number'] or item['reference'],
                    client=snap.get('contact',{}).get('name',''),kind=item['category'],
                    date=payload[0].get('Date') or item['period_end'],status=item['xero_status'] or item['status'],
                    issue=draft['issue'] if draft else '',start=item['period_start'],end=item['period_end'])
    return dict(id=key,item=None,lines=json.loads(draft['lines']),number='Draft '+key[:8],client=draft['client'],
                kind=draft['kind'],date=draft['end'],status=draft['state'],issue=draft['issue'],start=draft['start'],end=draft['end'])


def linked_pods(conn,invoice):
    ids=list(dict.fromkeys(line['job_id'] for line in invoice['lines'] if line.get('job_id')))
    if not ids: return {}
    rows=conn.execute("""SELECT id,code,site_name,state,pod_signed_at,pod_deleted_at,status FROM bf_jobs
        WHERE id IN (SELECT value FROM json_each(?))""",(json.dumps(ids),))
    by_id={r['id']:r for r in rows}
    if any(k not in by_id or by_id[k]['pod_deleted_at'] or not by_id[k]['pod_signed_at'] or by_id[k]['status']!='complete' for k in ids):
        raise ValueError('A supporting POD is missing or was withdrawn. Review the invoice before downloading.')
    return {k:by_id[k] for k in ids}


def local_preview(invoice):
    styles=getSampleStyleSheet()
    output=io.BytesIO()
    body=[Paragraph('BinFlow draft invoice preview',styles['Title']),
          Paragraph(escape(invoice['number']+' · '+invoice['client']),styles['Heading2']),
          Paragraph(escape(f"{invoice['kind']} · {invoice['start']} to {invoice['end']}"),styles['Normal'])]
    if invoice['issue']: body.append(Paragraph(escape(invoice['issue']),styles['Normal']))
    total=0
    for line in invoice['lines']:
        net,tax=line.get('net',0),line.get('tax',0)
        description=line.get('description') or line.get('size') or 'Invoice line'
        body.extend([Spacer(1,3*mm),Paragraph(escape(str(description)),styles['BodyText']),
                     Paragraph(f'Ex GST ${net/100:,.2f} · GST ${tax/100:,.2f}',styles['Normal'])])
        total+=net+tax
    body.append(Paragraph(f'Total incl GST · AUD ${total/100:,.2f}',styles['Heading2']))
    SimpleDocTemplate(output).build(body)
    return output.getvalue()


def combined_pdf(app,conn,invoice):
    from binflow_invoices import pdf_bytes
    pods=linked_pods(conn,invoice)
    item=invoice['item']
    if item and item['status']=='created' and item['invoice_id']:
        contact=json.loads(item['snapshot_json']).get('contact',{}).get('id')
        if xero.config(conn)['tenant_id']!=item['tenant_id']:
            raise ValueError('Reconnect the Xero organisation for this invoice.')
        result=xero.records(xero.api(app,conn,'Invoices/'+item['invoice_id'],tenant_id=item['tenant_id']),'Invoices')
        if (len(result)!=1 or result[0].get('InvoiceID')!=item['invoice_id'] or result[0].get('Type')!='ACCREC'
                or result[0].get('Contact',{}).get('ContactID')!=contact or result[0].get('Status') not in ('AUTHORISED','PAID','DRAFT','SUBMITTED')):
            raise ValueError('The invoice is unavailable in Xero. Refresh its status before downloading.')
        source=pdf_bytes(app,conn,item['tenant_id'],item['invoice_id'])
    elif item and item['status']!='preview':
        raise ValueError('Resolve the invoice’s Xero status before downloading a PDF.')
    else:
        source=local_preview(invoice)
    docs=invoice_pods.documents(conn,[invoice['id']]).get(invoice['id'],[]) if item else []
    job_ids=dict.fromkeys([*pods,*(p['id'] for p in docs if p['kind']=='job')])
    def attachments():
        for pid in job_ids:
            yield reports.pdf_data(app.view_functions['bf_pod_pdf'](job_id=pid))
        for doc in docs:
            if doc['kind']=='file':
                yield invoice_pods.file_content(conn,invoice['id'],doc['id'])
    return invoice_pods.bundle(source,attachments())


def filename(invoice):
    return (secure_filename(invoice['number']) or 'Invoice')+'-'+invoice['id']+('.pdf')


def table_rows(conn,invoices):
    for invoice in invoices:
        pods=linked_pods(conn,invoice)
        for line in invoice['lines'] or [{}]:
            pod=pods.get(line.get('job_id'))
            yield (invoice['number'],date.fromisoformat(invoice['date']),invoice['client'],invoice['kind'],invoice['status'],
                   date.fromisoformat(invoice['start']),date.fromisoformat(invoice['end']),line.get('description') or line.get('size',''),
                   pod['code'] if pod else '',pod['site_name'] if pod else '',pod['state'] if pod else '',
                   line['net_g']/1000 if line.get('net_g') is not None else None,
                   line.get('net',0)/100,line.get('tax',0)/100,(line.get('net',0)+line.get('tax',0))/100)


def register(app,db,require):
    @app.get('/binflow/invoices/<identifier>/document.pdf')
    @require('admin')
    def bf_invoice_document(identifier):
        invoice=invoice_data(db(),identifier)
        try:
            return reports.download(io.BytesIO(combined_pdf(app,db(),invoice)),'application/pdf',filename(invoice))
        except (ValueError,xero.XeroError) as exc: abort(409,str(exc))

    @app.post('/binflow/invoices/batch')
    @require('admin')
    def bf_invoice_batch():
        ids=reports.selected_ids()
        kind=request.form.get('format')
        if kind not in ('xls','zip'): abort(400)
        invoices=[invoice_data(db(),key) for key in sorted(ids)]
        invoices.sort(key=lambda i:(i['date'],i['client'],i['number']))
        period=reports.period_view(request.form.get('date',''),request.form.get('period','day'))
        if period['first'] and any(not period['first']<=i['date']<=period['last'] for i in invoices):
            abort(409,'The selected invoices have changed reporting period. Refresh the list.')
        name='BinFlow-Invoices-'+(period['first'] or 'selected')
        try:
            if kind=='zip':
                return reports.zip_download(((filename(i),combined_pdf(app,db(),i)) for i in invoices),name)
            return reports.xls('Invoice lines',['Invoice','Invoice or draft date','Billing client','Category','Stage','Period start','Period end',
                'Description','Signed POD','Site','State','Net kg','Ex GST AUD','GST AUD','Total AUD'],table_rows(db(),invoices),name)
        except (ValueError,xero.XeroError) as exc: abort(409,str(exc))
