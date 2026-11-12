import io
import json
import sqlite3
import unittest
import zipfile
from unittest.mock import patch

import test_binflow as legacy
import test_binflow_invoices as invoices
from test_binflow_invoices import BASE
from test_binflow_interstate import CLIENT, data
import test_binflow_xero as billing
from pod_helpers import sign_pod


class AutoInvoiceTests(unittest.TestCase):
    setUp=legacy.BinFlowTests.setUp
    tearDown=legacy.BinFlowTests.tearDown
    rows=legacy.BinFlowTests.rows
    login=legacy.BinFlowTests.login
    post=legacy.BinFlowTests.post
    job=legacy.BinFlowTests.job
    exchange=legacy.BinFlowTests.exchange
    initial_delivery=legacy.BinFlowTests.initial_delivery
    connect=invoices.ClientInvoiceTests.connect
    connection=invoices.ClientInvoiceTests.connection
    account=invoices.ClientInvoiceTests.account
    link=invoices.ClientInvoiceTests.link
    visit=invoices.ClientInvoiceTests.visit
    send=invoices.ClientInvoiceTests.send

    def configure(self):
        self.connect()
        aid=self.account('billing-client')
        self.link(aid)
        url='/binflow/setup/invoice-billing'
        for fields in (dict(action='defaults',account_code='200',tax_code='OUTPUT'),
                       dict(action='routing',client_id=1),dict(action='email',email='billing@example.com')):
            self.post(url,dict(account_id=aid,tenant=billing.TENANT,**fields))
        return aid

    def test_signed_pod_rate_snapshot_approval_and_complete_batch(self):
        aid=self.configure()
        self.initial_delivery()
        self.job(collect=(1,2))
        self.exchange(2,(1,2))
        rate='/binflow/setup/pickup-pricing'
        self.assertIn('Per Kg Pricing Pick Up',self.client.get('/binflow/setup').text)
        self.post(rate,dict(rate='1.6255',version=0))
        self.post(rate,dict(rate='2',version=1))
        self.post('/binflow/pickups/2/receive',dict(confirmed=[1,2]))
        for item in self.rows("SELECT id FROM bf_job_bins WHERE job_id=2"):
            self.post(f"/binflow/pickups/2/weigh/{item['id']}",dict(gross='15.125',tare='5',confirm_tare='yes'))
        self.client.get(BASE)
        self.assertFalse(self.rows("SELECT * FROM bf_auto_drafts WHERE kind='POD Collections'"))
        sign_pod(self,2)
        # Other sites can share a payer. A different, unmapped customer must stay out of the packet.
        for delivery,collection,bin_id,site in ((3,4,3,3),(5,6,4,4)):
            self.job(deliver=(bin_id,),site=site)
            self.exchange(delivery,(bin_id,))
            self.job(collect=(bin_id,),site=site)
            self.exchange(collection,(bin_id,))
            self.post(f'/binflow/pickups/{collection}/receive',dict(confirmed=[bin_id]))
            item=self.rows('SELECT id FROM bf_job_bins WHERE job_id=?',(collection,))[0]['id']
            self.post(f'/binflow/pickups/{collection}/weigh/{item}',dict(gross=15,tare=5,confirm_tare='yes'))
            sign_pod(self,collection)
        # Close this billing period; the fixture clock is Monday 19 October.
        with sqlite3.connect(self.path) as c:
            c.execute("UPDATE bf_jobs SET pod_signed_at='2026-10-02T12:00:00+10:00' WHERE id IN (2,4,6)")
        self.client.get(BASE);self.client.get(BASE)
        draft=self.rows("SELECT * FROM bf_auto_drafts WHERE kind='POD Collections' AND account_id=?",(aid,))[0]
        self.assertEqual([i['job_id'] for i in json.loads(draft['lines'])],[2,4])
        line=json.loads(draft['lines'])[0]
        self.assertEqual((line['net'],line['tax']),(3292,329))
        self.assertIn('1.6255',line['description'])
        path=BASE+'/review/'+draft['id']
        form=dict(version=draft['version'],tenant=billing.TENANT,contact=billing.CONTACT)
        remote=dict(InvoiceID=billing.INVOICE,InvoiceNumber='INV-POD',Reference='BF-'+draft['id'],Type='ACCREC',
                    Status='AUTHORISED',Contact=dict(ContactID=billing.CONTACT),Total=58.21,CurrencyCode='AUD')
        with patch('binflow_xero.api',return_value=dict(Invoices=[remote])) as api:
            self.post(path+'/approve',dict(form,version='stale'))
            api.assert_not_called()
            self.assertIn('Approved in Xero',self.post(path+'/approve',form).text)
            self.post(path+'/approve',form)
            api.assert_called_once()
            self.assertEqual(api.call_args.args[4]['Invoices'][0]['Status'],'AUTHORISED')
        self.client.get(BASE)
        self.assertEqual(len(self.rows("SELECT * FROM bf_auto_drafts WHERE kind='POD Collections' AND account_id=?",(aid,))),1)
        with patch('binflow_xero.api',return_value=dict(Invoices=[remote])),patch('binflow_invoices.pdf_bytes',return_value=b'%PDF-invoice'):
            response=self.client.get(path+'/batch.zip')
            self.assertEqual(response.mimetype,'application/zip',response.text if response.mimetype!='application/zip' else '')
            with zipfile.ZipFile(io.BytesIO(response.data)) as archive:
                self.assertEqual(set(archive.namelist()),{'Invoice.pdf','PODs/BF-000002.pdf','PODs/BF-000004.pdf'})
                self.assertTrue(archive.read('PODs/BF-000002.pdf').startswith(b'%PDF'))
            self.app.config.update(SMTP_HOST='mail.example.com',SMTP_FROM='ops@example.com')
            with patch('binflow_auto_invoices.send_smtp') as smtp:
                self.post(path+'/email',dict(email='other@example.com'))
                smtp.assert_not_called()
                self.post(path+'/email',dict(email='billing@example.com'))
                self.post(path+'/email',dict(email='billing@example.com'))
                smtp.assert_called_once()
                self.assertEqual(smtp.call_args.args[1]['To'],'billing@example.com')


    def test_rental_and_delivered_transfer_create_review_drafts(self):
        aid=self.configure()
        self.initial_delivery()
        client=self.visit('billing-client')
        self.send(client,CLIENT+'/new',data(handling='bulk',vehicle='SEMI TRAILER',pickup_company='Pickup',delivery_company='Delivery'))
        with sqlite3.connect(self.path) as c:
            c.execute("UPDATE bf_interstate_bookings SET locked_at='2026-10-16',rate_cents=10000,fuel_bps=2500")
        self.client.get(BASE)
        self.assertFalse(self.rows("SELECT * FROM bf_auto_drafts WHERE kind='Transport'"))
        with sqlite3.connect(self.path) as c:
            c.execute("UPDATE bf_interstate_bookings SET bulk_delivered_at='2026-10-17'")
        page=self.client.get(BASE).text
        self.assertNotIn('Prepare transport or other invoice',page)
        self.assertIn('POD Invoices',page)
        records=self.rows('SELECT * FROM bf_auto_drafts ORDER BY kind')
        self.assertEqual([r['kind'] for r in records],['Bin Rental','Transport'])
        self.assertEqual(sum(i['net'] for i in json.loads(records[1]['lines'])),12500)
