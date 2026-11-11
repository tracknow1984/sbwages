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

    def link(self, aid):
        self.assertEqual(self.post(BASE+'/contact',dict(client=aid,contact=billing.CONTACT,tenant=billing.TENANT)).status_code,200)

    def values(self, aid, **updates):
        return dict(dict(draft_key=str(uuid4()),client=aid,category=2,invoice_date='2026-11-11',due_date='2026-11-25',
                         account_code='200',tax_type='OUTPUT',description='Local transport',quantity='2.5',unit_rate='125.30'),**updates)

    def test_categories_billing_draft_and_isolated_issued_documents(self):
        self.connect()
        aid = self.account('freight-buyer')
        self.account('another-buyer')
        self.account('no-invoices',custom_permissions='yes',permission=['bookings.view'])
        client, other, restricted = [self.visit(n) for n in ('freight-buyer','another-buyer','no-invoices')]
        self.assertEqual(restricted.get(PORTAL).status_code,403)
        self.assertIn('Bin Rental invoices',client.get(PORTAL).text)
        self.assertIn('Transport invoices',client.get(PORTAL).text)
        self.assertIn('Add category',self.client.get(BASE).text)
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
        remote=dict(InvoiceNumber=num,InvoiceID=billing.INVOICE,Status='DRAFT',Contact={'ContactID':billing.CONTACT},Type='ACCREC',
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
        snap=json.loads(item['snapshot_json'])
        self.assertEqual([x['net'] for x in snap['lines']],[10000,2500])
        self.assertEqual((snap['subtotal'],snap['gst']),(12500,1250))
        self.post('/binflow/rental-invoices/'+item['id']+'/cancel')
        self.post(BASE+'/new',self.values(aid,booking=str(bid)))
        self.assertEqual(len(self.rows('SELECT * FROM bf_rental_invoices WHERE status="preview"')),1)

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
