import io
import re
import sqlite3
import unittest
from unittest.mock import patch

from openpyxl import load_workbook
from pypdf import PdfWriter

import test_binflow as fixtures
from test_binflow_interstate import data as transfer_data
from binflow_client_access import SBTX_RIGHTS, revision
from sbtx import migrate


CLIENT = '/sbtx/client'
OFFICE = '/sbtx'
BINFLOW = '/binflow/interstate-transfers'


def pdf_bytes():
    file = io.BytesIO()
    writer = PdfWriter()
    writer.add_blank_page(width=300, height=300)
    writer.add_metadata({'/Subject': 'A' * 180000})
    writer.write(file)
    return file.getvalue()


class SBTXTests(unittest.TestCase):
    setUp = fixtures.BinFlowTests.setUp
    tearDown = fixtures.BinFlowTests.tearDown
    post = fixtures.BinFlowTests.post
    rows = fixtures.BinFlowTests.rows
    login = fixtures.BinFlowTests.login

    def customer(self, name):
        response = self.post('/sbtx/customers', dict(name=name, username=name.lower(),
                             password='customer-test-password', active='yes'))
        self.assertEqual(response.status_code, 200)
        return self.rows('SELECT id FROM bf_client_accounts WHERE username=?', (name.lower(),))[0]['id']

    def customer_sign_in(self, name):
        self.client = self.app.test_client()
        return self.post('/sbtx/client/login', dict(username=name.lower(), password='customer-test-password'))

    def test_booking_to_delivery_with_po_and_date_review(self):
        office = self.client
        alice = self.customer('Alice')
        bob = self.customer('Bob')
        self.post(BINFLOW+'/new', transfer_data(client_name='Separate BinFlow load'))
        response = self.customer_sign_in('Alice')
        self.assertIn('Transport calendar', response.text)
        self.assertIn('Request a transfer', response.text)
        customer = self.client
        new = self.client.get(CLIENT+'/jobs/new?pickup=2026-11-19')
        self.assertIn('2026-11-19', new.text)
        self.assertIn('value="Alice"', new.text)
        self.assertIn('photon.komoot.io', new.headers['Content-Security-Policy'])
        key = re.search(r'name="request_key" value="([^"]+)"', new.text)[1]
        data = transfer_data(request_key=key, account_id=bob, handling='scanned', pickup_date='2026-11-19')
        result = self.post(CLIENT+'/jobs/new', data)
        self.assertEqual(result.status_code, 200)
        self.assertIn('Requested', result.text)
        detail = '/transfers/2'
        self.assertEqual(result.request.path, CLIENT+detail)
        self.post(CLIENT+'/jobs/new', data)
        self.assertEqual(self.rows("SELECT account_id,handling,delivery_date FROM bf_interstate_bookings WHERE portal='sbtx'"),
                         [dict(account_id=alice, handling='bulk', delivery_date='2026-11-22')])
        cal = '/?date=2026-11-19'
        pending = self.client.get(CLIENT+cal).text
        self.assertIn('Awaiting confirmation', pending)
        self.assertNotIn('class="it-event pickup"', pending)
        self.assertNotIn('Separate BinFlow load', pending)
        self.assertEqual(self.post(CLIENT+detail+'/purchase-order', dict(po_revision=0,po_number='PO')).status_code, 409)
        self.client = office
        self.assertEqual(self.client.get(BINFLOW+'/2').status_code, 404)
        self.assertEqual(self.client.get(OFFICE+'/transfers/1').status_code, 404)
        self.assertEqual(self.post(BINFLOW+'/2/lock',dict(version=1)).status_code,404)
        self.app.config.update(SMTP_HOST='smtp.example.invalid',SMTP_FROM='office@example.invalid')
        with patch('binflow_interstate.send_smtp') as mail:
            approved = self.post(OFFICE+detail+'/lock', dict(version=1))
            self.assertIn('Scheduled', approved.text)
            mail.assert_called_once()
        self.client = customer
        self.assertIn('data-po-status="required"', self.client.get(CLIENT+cal).text)
        document = pdf_bytes()
        self.assertEqual(self.post(CLIENT+detail+'/purchase-order', dict(po_revision=0,po_number='PO-237',
                                po_file=(io.BytesIO(document),'Order.pdf'))).status_code, 200)
        self.assertEqual(self.post(CLIENT+detail+'/purchase-order', dict(po_revision=0,po_number='STALE')).status_code,409)
        preview = self.client.get(CLIENT+detail+'/purchase-order/file?preview=1')
        self.assertEqual(preview.data,document)
        self.assertEqual(preview.headers['X-Frame-Options'], 'SAMEORIGIN')
        confirmed = self.client.get(CLIENT+cal).text
        self.assertIn('data-po-status="received"', confirmed)
        self.assertIn('Quick view PO', confirmed)
        self.assertNotIn('data-po-status="received"', self.client.get(CLIENT+cal+'&state=WA').text)
        self.assertNotIn('data-po-status="received"', self.client.get(CLIENT+cal+'&vehicle=RIGID').text)
        self.assertNotIn('/binflow/client/interstate-transfers', confirmed)
        self.assertIn('Share by email', confirmed)
        exported = self.client.get(CLIENT+cal+'&export=month')
        self.assertIn('SBTX-All-Transfers-2026-11.xlsx', exported.headers['Content-Disposition'])
        book = load_workbook(io.BytesIO(exported.data))
        self.assertEqual(book.active.max_row,5)
        self.assertIn('PO-237', [c.value for c in book.active[5]])
        self.assertIn('SBTX', book.active['A1'].value)
        reschedule = self.post(CLIENT+detail+'/reschedule', dict(version=2,pickup_date='2026-11-25'))
        self.assertIn('Date change awaiting confirmation',reschedule.text)
        self.assertIn('PO-237',reschedule.text)
        self.assertNotIn('class="it-event pickup"',self.client.get(CLIENT+cal).text)
        self.client = office
        with patch('binflow_interstate.send_smtp'):
            approved = self.post(OFFICE+detail+'/lock',dict(version=3, decision='original'))
        self.assertIn('19.11.2026',approved.text)
        self.assertEqual(self.post(OFFICE+detail+'/bulk',dict(action='dispatch')).status_code,200)
        self.assertEqual(self.post(OFFICE+detail+'/bulk',dict(action='deliver')).status_code,200)
        self.assertIn('data-delivered="true"', self.client.get(OFFICE+cal).text)
        self.client = customer
        self.assertEqual(self.post(CLIENT+detail+'/reschedule', dict(version=4,pickup_date='2026-11-28')).status_code,409)
        self.assertIn('Delivered',self.client.get(CLIENT+detail).text)
        self.customer_sign_in('Bob')
        for path in (detail,detail+'/purchase-order/file'):
            self.assertEqual(self.client.get(CLIENT+path).status_code,404)
        self.assertEqual(self.post(CLIENT+detail+'/purchase-order', dict(po_revision=1,po_number='PO')).status_code,404)
        self.assertNotIn('PO-237', self.client.get(CLIENT+cal).text)
        self.assertEqual(load_workbook(io.BytesIO(self.client.get(CLIENT+cal+'&export=month').data)).active.max_row,4)
        for path in ['/binflow/client/interstate-transfers', '/binflow/client/warehouses']:
            self.assertEqual(self.client.get(path).status_code,403)
        self.assertEqual(self.client.get(OFFICE+detail).status_code,302)

    def test_office_assignment_validation_and_read_only(self):
        office = self.client
        aid = self.customer('Alice')
        data = transfer_data()
        self.assertEqual(self.post(OFFICE+'/jobs/new', dict(data, pickup_date='bad')).status_code,400)
        self.assertEqual(self.post(OFFICE+'/jobs/new?bill_to=9999', data).status_code,400)
        self.post(OFFICE+'/jobs/new',data)
        self.customer_sign_in('Alice')
        self.assertEqual(self.client.get(CLIENT+'/transfers/1').status_code,404)
        self.client = office
        self.assertEqual(self.post(OFFICE+f'/transfers/1/edit?bill_to={aid}',dict(data,version=1,reason='Assign account')).status_code,200)
        self.post(f'/binflow/interstate-transfers/access/{aid}/permissions?area=sbtx',
                  dict(permission=['sbtx.view'], version=revision(SBTX_RIGHTS)))
        self.customer_sign_in('Alice')
        self.assertIn('12 pallets linen', self.client.get(CLIENT+'/transfers/1').text)
        self.assertEqual(self.client.get(CLIENT+'/jobs/new').status_code,403)
        for path in ('edit','purchase-order','reschedule'):
            self.assertEqual(self.post(CLIENT+'/transfers/1/'+path, dict(data,version=2)).status_code,403)
        self.assertIn('SBTX customer sign in',self.post(CLIENT+'/logout').text)

    def test_dual_portal_account_has_separate_queues_exports_and_purchase_orders(self):
        aid = self.customer('Alice')
        rights = sorted(SBTX_RIGHTS | {'bookings.view','bookings.add','bookings.edit'})
        self.post(f'/binflow/interstate-transfers/access/{aid}/permissions?area=sbtx',
                  dict(permission=rights, version=revision(SBTX_RIGHTS)))
        self.post(BINFLOW+f'/new?bill_to={aid}',transfer_data(goods='BinFlow textiles'))
        self.post(OFFICE+f'/jobs/new?bill_to={aid}',transfer_data(goods='SBTX pallets'))
        self.post(BINFLOW+'/1/lock',dict(version=1))
        self.post(OFFICE+'/transfers/2/lock',dict(version=1))
        self.customer_sign_in('Alice')
        for calendar, detail, other, expected, hidden in (
            (CLIENT+'/', CLIENT+'/transfers/2', CLIENT+'/transfers/1', 'SBTX pallets', 'BinFlow textiles'),
            ('/binflow/client/interstate-transfers', '/binflow/client/interstate-transfers/1',
             '/binflow/client/interstate-transfers/2', 'BinFlow textiles', 'SBTX pallets'),
        ):
            month = calendar+'?date=2026-11-13'
            html = self.client.get(month).text
            self.assertIn(expected,html)
            self.assertNotIn(hidden,html)
            book = load_workbook(io.BytesIO(self.client.get(month+'&export=month').data))
            self.assertEqual(book.active.max_row,5)
            self.assertIn(expected,[c.value for c in book.active[5]])
            self.assertEqual(self.client.get(other).status_code,404)
            self.assertEqual(self.post(other+'/purchase-order',dict(po_revision=0,po_number='Wrong portal')).status_code,404)
            self.assertEqual(self.post(detail+'/purchase-order',dict(po_revision=0,po_number=expected)).status_code,200)

    def test_legacy_pickup_and_file_survive_repeated_migration(self):
        office = self.client
        aid = self.customer('Alice')
        self.customer('Bob')
        self.post(BINFLOW+'/new',transfer_data())
        document = pdf_bytes()
        with sqlite3.connect(self.path) as conn:
            conn.execute("""CREATE TABLE sbtx_jobs(id INTEGER PRIMARY KEY,account_id,customer_name,
                contact_number,pickup_date,description,po_number,filename,mime_type,document,version)""")
            conn.execute('INSERT INTO sbtx_jobs VALUES(1,?,?,?,?,?,?,?,?,?,3)',
                         (aid,'Alice site','0422 333 444','2026-11-19','Original load','PO-OLD','Old.pdf','application/pdf',document))
            self.assertEqual(migrate(conn,'2026-11-18T09:00:00+10:00'),(1,1))
            self.assertEqual(migrate(conn,'2026-11-18T09:00:00+10:00'),(0,0))
        self.assertEqual(len(self.rows("SELECT id FROM bf_interstate_bookings WHERE portal='sbtx'")),1)
        self.customer_sign_in('Alice')
        self.assertIn('Original load',self.client.get(CLIENT+'/?date=2026-11-19').text)
        self.assertEqual(self.client.get(CLIENT+'/jobs/1/purchase-order',follow_redirects=True).data,document)
        result = self.client.get(CLIENT+'/jobs/1',follow_redirects=True)
        self.assertEqual(result.request.path,CLIENT+'/transfers/2')
        self.assertIn('Complete transfer details',result.text)
        self.assertIn('0422 333 444',result.text)
        self.assertIn('PO-OLD',result.text)
        export=self.client.get(CLIENT+'/?export=month&date=2026-11-19')
        self.assertEqual(load_workbook(io.BytesIO(export.data)).active['D5'].value,None)
        self.client = office
        self.assertEqual(self.client.get(OFFICE+'/jobs/1/purchase-order',follow_redirects=True).data,document)
        self.assertEqual(self.post(OFFICE+'/transfers/2/bulk',dict(action='dispatch')).status_code,409)
        self.customer_sign_in('Alice')
        self.assertEqual(self.post(CLIENT+'/transfers/2/edit',transfer_data(version=3)).status_code,200)
        self.assertEqual(self.client.get(CLIENT+'/jobs/1/purchase-order',follow_redirects=True).data,document)
        self.assertIsNone(self.rows('SELECT locked_at FROM bf_interstate_bookings WHERE id=2')[0]['locked_at'])
        self.customer_sign_in('Bob')
        self.assertEqual(self.client.get(CLIENT+'/jobs/1').status_code,404)
        self.assertEqual(self.client.get(CLIENT+'/jobs/1/purchase-order').status_code,404)
