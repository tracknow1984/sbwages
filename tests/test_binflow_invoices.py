import json
from unittest import TestCase
from unittest.mock import patch
from uuid import uuid4

import test_binflow_client_permissions as portal
import test_binflow_xero as billing
from test_binflow_interstate import CLIENT, data

BASE = '/binflow/invoices'
PORTAL = '/binflow/client/invoices'


class ClientInvoiceTests(TestCase):
    setUp = billing.XeroRentalTests.setUp
    tearDown = billing.XeroRentalTests.tearDown
    connect = billing.XeroRentalTests.connect
    connection = billing.XeroRentalTests.connection
    post = billing.XeroRentalTests.post
    login = billing.XeroRentalTests.login
    rows = billing.XeroRentalTests.rows
    account = portal.PortalPermissionTests.account
    visit = portal.PortalPermissionTests.visit
    send = portal.PortalPermissionTests.send
    job = portal.PortalPermissionTests.job
    exchange = portal.PortalPermissionTests.exchange
    initial_delivery = portal.PortalPermissionTests.initial_delivery

    def link(self, aid):
        self.assertEqual(self.post(BASE+'/contact',dict(client=aid,contact=billing.CONTACT,tenant=billing.TENANT)).status_code,200)

    @billing.connected_organisation
    def test_attached_pods_view_merge_and_account_isolation(self):
        import io
        import zipfile
        from pypdf import PdfReader,PdfWriter
        self.connect()
        owner=self.account('blocktexx1')
        self.link(owner)
        self.link(self.account('other-invoice-client'))
        client=self.visit('blocktexx1')
        other=self.visit('other-invoice-client')
        data=self.values(owner)
        self.post(BASE+'/new',data)
        key=data['draft_key']
        manage=f'{BASE}/{key}/pods'
        self.assertIn('Attach PDF',self.client.get(manage).text)
        with self.connection() as conn:
            conn.execute("UPDATE bf_rental_invoices SET status='created',invoice_id=? WHERE id=?",(billing.INVOICE,key))
        pdf=io.BytesIO()
        writer=PdfWriter()
        writer.add_blank_page(width=300,height=200)
        writer.write(pdf)
        raw=pdf.getvalue()
        for _ in range(2):
            self.post(manage,dict(action='upload',document=(io.BytesIO(raw),'signed-delivery.pdf')))
        invalid=self.post(manage,dict(action='upload',document=(io.BytesIO(b'%PDF-corrupt'),'bad.pdf')))
        self.assertIn('This file could not be read',invalid.text)
        files=self.rows('SELECT id FROM bf_invoice_pod_files')
        self.assertEqual(len(files),1)
        file_id=files[0]['id']
        route=f'{PORTAL}/{key}/pods/file/{file_id}'
        remote=dict(InvoiceID=billing.INVOICE,InvoiceNumber='INV-POD',Type='ACCREC',Status='AUTHORISED',
            Contact={'ContactID':billing.CONTACT},DateString='2026-11-20',DueDateString='2026-12-04',Total=100,AmountDue=100)
        with patch('binflow_xero.api',return_value={'Invoices':[remote]}),patch('binflow_invoices.pdf_bytes',return_value=raw):
            self.assertIn(route,client.get(PORTAL).text)
            self.assertEqual(client.get(route).data,raw)
            self.assertIn('inline',client.get(route).headers['Content-Disposition'])
            self.assertEqual(other.get(route).status_code,404)
            self.assertEqual(len(PdfReader(io.BytesIO(client.get(f'{PORTAL}/{key}/pdf').data)).pages),2)
            self.assertEqual(len(PdfReader(io.BytesIO(self.client.get(f'{BASE}/{key}/document.pdf').data)).pages),2)
            response=self.send(client,PORTAL,dict(selected=key,format='zip'))
            with zipfile.ZipFile(io.BytesIO(response.data)) as archive:
                self.assertEqual(len(PdfReader(io.BytesIO(archive.read(archive.namelist()[0]))).pages),2)
            remote['Status']='DRAFT'
            self.assertEqual(client.get(route).status_code,404)
            remote['Status']='PAID'
            self.post(manage,dict(action='remove',kind='file',document_id=file_id))
            self.assertEqual(client.get(route).status_code,404)
            self.assertIn('No POD attached',client.get(PORTAL).text)

    def values(self, aid, **updates):
        return dict(dict(draft_key=str(uuid4()),client=aid,category=2,invoice_date='2026-11-11',due_date='2026-11-25',
                         po_number='PO-Manual',account_code='200',tax_type='OUTPUT',description='Local transport',quantity='2.5',unit_rate='125.30'),**updates)

    @billing.connected_organisation
    def test_link_signed_system_pod_and_withdrawal(self):
        from pod_helpers import sign_pod
        self.connect()
        aid=self.account('blocktexx1')
        self.link(aid)
        client=self.visit('blocktexx1')
        self.initial_delivery()
        sign_pod(self,1)
        values=self.values(aid)
        self.post(BASE+'/new',values)
        key=values['draft_key']
        manage=f'{BASE}/{key}/pods'
        self.assertIn('BF-000001',self.client.get(manage).text)
        for _ in range(2):
            self.post(manage,dict(action='link',code='BF-000001'))
        self.assertEqual(len(self.rows('SELECT job_id FROM bf_invoice_pod_jobs')),1)
        with self.connection() as conn:
            conn.execute("UPDATE bf_rental_invoices SET status='created',invoice_id=? WHERE id=?",(billing.INVOICE,key))
        remote=dict(InvoiceID=billing.INVOICE,Type='ACCREC',Status='PAID',Contact={'ContactID':billing.CONTACT},Total=3,AmountDue=0)
        route=f'{PORTAL}/{key}/pods/job/1'
        with patch('binflow_xero.api',return_value={'Invoices':[remote]}):
            self.assertIn(route,client.get(PORTAL).text)
            self.assertEqual(client.get(route).mimetype,'application/pdf')
            with self.connection() as conn:
                conn.execute("UPDATE bf_jobs SET pod_deleted_at='withdrawn' WHERE id=1")
            self.assertNotIn(route,client.get(PORTAL).text)
            self.assertEqual(client.get(route).status_code,404)

    @billing.connected_organisation
    def test_categories_billing_draft_and_isolated_issued_documents(self):
        self.connect()
        aid = self.account('freight-buyer')
        self.account('another-buyer')
        self.account('no-invoices',custom_permissions='yes',permission=['bookings.view'])
        client, other, restricted = [self.visit(n) for n in ('freight-buyer','another-buyer','no-invoices')]
        self.assertEqual(restricted.get(PORTAL).status_code,403)
        self.assertIn('Bin Rental invoices',client.get(PORTAL).text)
        self.assertIn('Transport invoices',client.get(PORTAL).text)
        self.assertNotIn('Add category',self.client.get(BASE).text)
        self.assertIn('Add category',self.client.get('/binflow/setup/invoice-billing').text)
        self.assertEqual(self.client.post(BASE+'/categories',data={'name':'Storage'}).status_code,400)
        self.post(BASE+'/categories',dict(name='Storage'))
        self.post(BASE+'/categories',dict(name='storage'))
        cat=self.rows("SELECT * FROM bf_invoice_categories WHERE name='Storage'")
        self.assertEqual(len(cat),1)
        self.assertIn('Storage invoices',client.get(PORTAL).text)
        self.link(aid)
        vals=self.values(aid,category=cat[0]['id'])
        self.assertIn('BlockTexx',self.client.get(BASE+'/new').text)
        self.assertEqual(self.post(BASE+'/new',vals,follow=False).status_code,302)
        self.post(BASE+'/new',vals)
        rows=self.rows('SELECT * FROM bf_rental_invoices')
        self.assertEqual(len(rows),1)
        item=rows[0]
        invoice=json.loads(item['payload_json'])['Invoices'][0]
        snap=json.loads(item['snapshot_json'])
        self.assertEqual((snap['subtotal'],snap['gst']),(31325,3133))
        self.assertEqual((invoice['Status'],invoice['Contact']['ContactID'],invoice['LineAmountTypes']),('DRAFT',billing.CONTACT,'Exclusive'))
        self.assertEqual(invoice['LineItems'][0]['Quantity'],2.5)
        self.assertNotIn('Local transport',client.get(PORTAL).text)
        num='INV-0099'
        remote=dict(Reference='PO-Manual',InvoiceNumber=num,InvoiceID=billing.INVOICE,Status='DRAFT',Contact={'ContactID':billing.CONTACT},Type='ACCREC',
                    DateString='2026-11-11T00:00:00',DueDateString='2026-11-25T00:00:00',Total=344.58,AmountDue=344.58)
        with patch('binflow_xero.request_json',return_value={'Invoices':[remote]}) as api:
            self.post('/binflow/rental-invoices/'+item['id']+'/create',{'confirm_draft':'yes'})
            self.post('/binflow/rental-invoices/'+item['id']+'/create',{'confirm_draft':'yes'})
            self.assertEqual(api.call_count,1)
            self.assertNotIn(num,client.get(PORTAL).text)
        path=PORTAL+'/'+item['id']+'/pdf'
        with patch('binflow_xero.api',return_value={'Invoices':[remote]}),patch('binflow_invoices.pdf_bytes',return_value=b'%PDF-1.7') as pdf:
            self.assertEqual(client.get(path).status_code,404)
            self.assertEqual(other.get(path).status_code,404)
            remote['Status']='AUTHORISED'
            self.assertIn(num,client.get(PORTAL).text)
            self.assertIn('344.58',client.get(PORTAL).text)
            self.assertEqual(client.get(path).data,b'%PDF-1.7')
            remote.update(Status='PAID',AmountDue=0)
            self.assertIn('Paid',client.get(PORTAL).text)
            self.assertNotIn(num,other.get(PORTAL).text)
            remote['Contact']['ContactID']=billing.INVOICE
            self.assertNotIn(num,client.get(PORTAL).text)
            self.assertEqual(client.get(path).status_code,404)
            pdf.assert_called_once()
        self.assertIn('All invoices',self.client.get('/binflow/rental-invoices/'+item['id']).text)

    @billing.connected_organisation
    def test_client_invoice_periods_and_selected_exports(self):
        import io
        import zipfile
        from urllib.parse import parse_qs,urlsplit
        self.connect()
        aid=self.account('report-buyer')
        other=self.account('separate-buyer')
        self.link(aid)
        self.link(other)
        client=self.visit('report-buyer')
        remote={}
        keys={}
        for number,day,owner,state in [
            ('INV-A','2026-11-09',aid,'AUTHORISED'),('INV-B','2026-11-15',aid,'PAID'),
            ('INV-C','2026-11-22',aid,'AUTHORISED'),('INV-D','2026-11-30',aid,'AUTHORISED'),
            ('INV-E','2026-12-01',aid,'AUTHORISED'),('INV-OTHER','2026-11-09',other,'AUTHORISED'),
            ('INV-DRAFT','2026-11-10',aid,'DRAFT')]:
            vals=self.values(owner)
            self.assertEqual(self.post(BASE+'/new',vals,follow=False).status_code,302)
            iid=str(uuid4())
            keys[number]=vals['draft_key']
            with self.connection() as conn:
                conn.execute("UPDATE bf_rental_invoices SET status='created',invoice_id=? WHERE id=?",(iid,vals['draft_key']))
            remote[iid]=dict(InvoiceID=iid,InvoiceNumber=number,Contact={'ContactID':billing.CONTACT},Type='ACCREC',
                Status=state,DateString=day,DueDateString='2026-12-15',Total=120.75,AmountDue=0 if state=='PAID' else 120.75)
        def live_rows(app,conn,path,tenant_id):
            wanted=parse_qs(urlsplit(path).query)['IDs'][0].split(',')
            return {'Invoices':[remote[i] for i in wanted]}
        def pdf(app,conn,tenant,iid):
            return b'%PDF-'+remote[iid]['InvoiceNumber'].encode()
        week=PORTAL+'?period=week&date=2026-11-13'
        selected={'selected':[keys['INV-A'],keys['INV-B'],keys['INV-A']]}
        with patch('binflow_xero.api',side_effect=live_rows) as api,patch('binflow_invoices.pdf_bytes',side_effect=pdf) as docs:
            newest=client.get(week).text
            self.assertIn('09 Nov 2026 – 15 Nov 2026',newest)
            self.assertLess(newest.index('INV-B'),newest.index('INV-A'))
            self.assertIn('name="selected"',newest)
            self.assertNotIn('INV-C',newest)
            oldest=client.get(week+'&sort=oldest').text
            self.assertLess(oldest.index('INV-A'),oldest.index('INV-B'))
            fortnight=client.get(PORTAL+'?period=fortnight&date=2026-11-12').text
            self.assertIn('INV-C',fortnight)
            self.assertNotIn('INV-D',fortnight)
            month=client.get(PORTAL+'?period=month&date=2026-11-30').text
            self.assertIn('INV-D',month)
            for excluded in ('INV-E','INV-OTHER','INV-DRAFT'):
                self.assertNotIn(excluded,month)
            spreadsheet=self.send(client,week,dict(selected,format='xls'))
            self.assertEqual(spreadsheet.mimetype,'application/vnd.ms-excel')
            self.assertTrue(spreadsheet.data.startswith(bytes.fromhex('d0cf11e0a1b11ae1')))
            self.assertIn(b'INV-A',spreadsheet.data)
            self.assertIn(b'INV-B',spreadsheet.data)
            self.assertNotIn(b'INV-C',spreadsheet.data)
            archive=self.send(client,week,dict(selected,format='zip'))
            self.assertEqual(archive.mimetype,'application/zip')
            with zipfile.ZipFile(io.BytesIO(archive.data)) as files:
                self.assertEqual([files.read(f) for f in files.namelist()],[b'%PDF-INV-B',b'%PDF-INV-A'])
            self.assertEqual(docs.call_count,2)
            self.assertEqual(self.send(client,week,dict(selected=[keys['INV-A'],keys['INV-C']],format='zip')).status_code,409)
            self.assertEqual(self.send(client,week,dict(selected=keys['INV-DRAFT'],format='xls')).status_code,409)
            api.reset_mock()
            self.assertEqual(self.send(client,week,dict(selected=keys['INV-OTHER'],format='zip')).status_code,404)
            api.assert_not_called()
            self.assertEqual(self.send(client,week,dict(format='zip')).status_code,400)
            self.assertEqual(client.post(week,data=dict(selected,format='zip')).status_code,400)
            self.assertEqual(docs.call_count,2)
            docs.side_effect=[b'%PDF-first',billing.xero.XeroError('Unavailable')]
            self.assertEqual(self.send(client,week,dict(selected,format='zip')).status_code,503)

    @billing.connected_organisation
    def test_approved_booking_uses_frozen_rate_po_and_one_invoice_per_transfer(self):
        self.connect()
        aid=self.account('booker')
        self.link(aid)
        client=self.visit('booker')
        submission=data(handling='bulk',vehicle='SEMI TRAILER',pickup_company='Pickup',delivery_company='Delivery')
        self.send(client,CLIENT+'/new',submission)
        bid=self.rows('SELECT id FROM bf_interstate_bookings')[0]['id']
        with self.connection() as conn:
            conn.execute("UPDATE bf_interstate_bookings SET locked_at='2026-11-07',rate_cents=10000,fuel_bps=2500 WHERE id=?",(bid,))
            conn.execute("INSERT INTO bf_interstate_purchase_orders(booking_id,number,revision,updated_at,updated_by) VALUES(?,'PO-50',1,'now','Admin')",(bid,))
        self.assertIn('PO-50',self.client.get(BASE+'/new?booking='+str(bid)).text)
        for i in range(2):
            result=self.post(BASE+'/new',self.values(999,booking=str(bid),unit_rate='0.01'))
            self.assertIn('PO-50',result.text)
        items=self.rows('SELECT * FROM bf_rental_invoices')
        self.assertEqual(len(items),1)
        item=items[0]
        self.assertEqual((item['portal_account_id'],item['category_id'],item['booking_id']),(aid,2,bid))
        self.assertEqual(json.loads(item['payload_json'])['Invoices'][0]['Reference'],'PO-50')
        snap=json.loads(item['snapshot_json'])
        self.assertEqual([x['net'] for x in snap['lines']],[10000,2500])
        self.assertEqual((snap['subtotal'],snap['gst']),(12500,1250))
        self.post('/binflow/rental-invoices/'+item['id']+'/cancel')
        self.post(BASE+'/new',self.values(aid,booking=str(bid)))
        self.assertEqual(len(self.rows('SELECT * FROM bf_rental_invoices WHERE status="preview"')),1)

    @billing.connected_organisation
    def test_existing_actual_rentals_are_matched_by_tenant_and_xero_contact(self):
        self.connect()
        aid=self.account('rental-buyer')
        self.link(aid)
        client=self.visit('rental-buyer')
        preview=billing.XeroRentalTests.preview
        with self.connection() as conn:
            conn.execute("UPDATE bf_bins SET location_id=3,status='at_client' WHERE id=1")
        preview(self,mode='actual',reviewed_period='yes')
        preview(self)
        with self.connection() as conn:
            conn.execute("UPDATE bf_rental_invoices SET status='created',invoice_id=? WHERE mode='actual'",(billing.INVOICE,))
            conn.execute("UPDATE bf_rental_invoices SET status='created',invoice_id=? WHERE mode='test'",(billing.CONTACT,))
        remote=dict(InvoiceID=billing.INVOICE,InvoiceNumber='INV-RENT',Type='ACCREC',Status='PAID',
            Contact={'ContactID':billing.CONTACT},CurrencyCode='AUD',Total=4,AmountDue=0)
        with patch('binflow_xero.api',return_value={'Invoices':[remote]}) as api:
            result=client.get(PORTAL)
            self.assertIn('INV-RENT',result.text)
            self.assertNotIn('INV-RENT',result.text.split('Transport invoices')[1])
            from urllib.parse import urlsplit,parse_qs
            self.assertEqual(parse_qs(urlsplit(api.call_args.args[2]).query)['IDs'],[billing.INVOICE])
            self.post(BASE+'/contact',dict(client=aid,contact='unlink',tenant=billing.TENANT))
            self.assertNotIn('INV-RENT',client.get(PORTAL).text)
            api.assert_called_once()
