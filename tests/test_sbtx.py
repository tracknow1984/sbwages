import io
import re
import secrets
import unittest

from pypdf import PdfWriter

import test_binflow as fixtures


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

    def job_data(self, **extra):
        return dict(dict(description='Collect two pallets', pickup_date='2026-11-19', customer_name='Customer',
                         contact_number='0412 345 678', request_key=secrets.token_hex(16)), **extra)

    def test_customer_booking_po_and_scope(self):
        office = self.client
        alice = self.customer('Alice')
        bob = self.customer('Bob')
        self.assertEqual(self.customer_sign_in('Alice').status_code, 200)
        self.assertIn('Transport calendar', self.client.get('/sbtx/client/').text)
        for path in ['/binflow/client/interstate-transfers', '/binflow/client/warehouses']:
            self.assertEqual(self.client.get(path).status_code, 403)

        new = self.client.get('/sbtx/client/jobs/new?pickup=2026-11-19').text
        self.assertIn('value="Alice"', new)
        data = self.job_data(request_key=re.search(r'name="request_key" value="([^"]+)"', new)[1],
                             account_id=bob)
        result = self.post('/sbtx/client/jobs/new', data)
        self.assertIn('data-po-status="required"', result.text)
        self.post('/sbtx/client/jobs/new', data)
        self.assertEqual(self.rows('SELECT account_id FROM sbtx_jobs'), [dict(account_id=alice)])
        pdf = io.BytesIO()
        writer = PdfWriter()
        writer.add_blank_page(width=300, height=300)
        writer.add_metadata({'/Subject': 'A' * 180000})
        writer.write(pdf)
        document = pdf.getvalue()
        result = self.post('/sbtx/client/jobs/1', dict(data, version=1, po_number='PO-237',
            po_file=(io.BytesIO(document), 'Order.pdf'), pickup_date='2026-11-27'))
        self.assertIn('data-po-status="received"', result.text)
        file_path = '/sbtx/client/jobs/1/purchase-order'
        self.assertEqual(self.client.get(file_path).data, document)
        self.assertEqual(self.post('/sbtx/client/jobs/1', dict(data, version=1)).status_code, 409)
        self.assertEqual(self.rows('SELECT pickup_date,po_number FROM sbtx_jobs')[0],
                         dict(pickup_date='2026-11-27', po_number='PO-237'))
        self.assertEqual(self.rows('SELECT id FROM bf_interstate_bookings'), [])

        self.customer_sign_in('Bob')
        self.assertNotIn('Collect two pallets', self.client.get('/sbtx/client/?date=2026-11-27').text)
        for path in ['/sbtx/client/jobs/1', file_path]:
            self.assertEqual(self.client.get(path).status_code, 404)
        self.assertEqual(self.post('/sbtx/client/jobs/1', dict(data, version=2)).status_code, 404)
        self.assertEqual(self.client.get('/sbtx/jobs/1').status_code, 302)
        self.client = office
        calendar = self.client.get('/sbtx/?date=2026-11-27&view=week').text
        self.assertLess(calendar.index('>SBTX</a>'), calendar.index('>BinFlow</a>'))
        self.assertIn('Collect two pallets', calendar)
        self.assertEqual(self.client.get('/sbtx/jobs/1/purchase-order').data, document)
        self.assertNotIn('Collect two pallets', self.client.get('/binflow/interstate-transfers?date=2026-11-27').text)

    def test_office_assignment_validation_and_read_only(self):
        office = self.client
        aid = self.customer('Alice')
        data = self.job_data()
        self.assertEqual(self.post('/sbtx/jobs/new', dict(data, contact_number='')).status_code, 400)
        self.assertEqual(self.post('/sbtx/jobs/new', dict(data, pickup_date='bad')).status_code, 400)
        self.assertEqual(self.post('/sbtx/jobs/new', dict(data, account_id=9999)).status_code, 400)
        self.assertEqual(self.post('/sbtx/jobs/new', dict(data, po_file=(io.BytesIO(b'fake'), 'test.pdf'))).status_code, 400)
        self.assertEqual(self.rows('SELECT id FROM sbtx_jobs'), [])
        self.post('/sbtx/jobs/new', data)
        self.customer_sign_in('Alice')
        self.assertEqual(self.client.get('/sbtx/client/jobs/1').status_code, 404)
        self.client = office
        self.post('/sbtx/jobs/1', dict(data, version=1, account_id=aid))
        from binflow_client_access import SBTX_RIGHTS, revision
        self.post(f'/binflow/interstate-transfers/access/{aid}/permissions?area=sbtx',
                  dict(permission=['sbtx.view'], version=revision(SBTX_RIGHTS)))
        self.customer_sign_in('Alice')
        self.assertIn('Collect two pallets', self.client.get('/sbtx/client/jobs/1').text)
        self.assertEqual(self.client.get('/sbtx/client/jobs/new').status_code, 403)
        self.assertEqual(self.post('/sbtx/client/jobs/1', dict(data, version=2)).status_code, 403)
        self.assertIn('SBTX customer sign in', self.post('/sbtx/client/logout').text)
