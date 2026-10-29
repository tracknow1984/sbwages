import json
import time
import unittest
from urllib.parse import parse_qs, urlparse
from unittest.mock import patch

from werkzeug.security import generate_password_hash

import binflow_xero as xero
import test_binflow_rental as fixtures


TENANT = '11111111-1111-4111-8111-111111111111'
CONTACT = '22222222-2222-4222-8222-222222222222'
INVOICE = '33333333-3333-4333-8333-333333333333'


class XeroRentalTests(unittest.TestCase):
    setUp = fixtures.RentalIncomeTests.setUp
    tearDown = fixtures.RentalIncomeTests.tearDown
    connection = fixtures.RentalIncomeTests.connection
    rows = fixtures.RentalIncomeTests.rows
    post = fixtures.RentalIncomeTests.post
    login = fixtures.RentalIncomeTests.login

    def connect(self):
        self.post('/binflow/xero/credentials', {'client_id': 'test-client-id', 'client_secret': 'private-client-secret'})
        with self.connection() as conn:
            xero.save_tokens(self.app, conn, {'access_token': 'private-access-token', 'refresh_token': 'private-refresh-token', 'expires_in': 1800})
            conn.execute('''UPDATE bf_xero_config SET tenant_id=?,tenant_name=?,choices_at=?,choices_json=? WHERE id=1''',
                         (TENANT, 'Test Organisation', int(time.time()), json.dumps({
                             'accounts': [{'code': '200', 'name': 'Rental income'}],
                             'contacts': [{'id': CONTACT, 'name': 'BlockTexx'}],
                             'taxes': [{'code': 'OUTPUT', 'name': 'GST on income', 'rate': '10'}]})))
            conn.execute('UPDATE bf_sizes SET weekly_rental_cents=181 WHERE id=1')

    def preview(self, **overrides):
        values = dict(period_start='2026-10-06', period_end='2026-10-19', mode='test', contact_id=CONTACT,
                      account_code='200', tax_type='OUTPUT', sample_size='1', sample_quantity='3')
        values.update(overrides)
        return self.post('/binflow/rental-invoices/preview', values, follow=False)

    def invoice(self):
        return self.rows('SELECT * FROM bf_rental_invoices ORDER BY rowid DESC')[0]

    def test_secrets_encrypted_and_pages_render_without_them(self):
        self.connect()
        cfg = self.rows('SELECT * FROM bf_xero_config')[0]
        self.assertNotIn(b'private-', cfg['secret_cipher'])
        self.assertNotIn(b'private-', cfg['token_cipher'])
        self.assertEqual(xero.decrypt(self.app, cfg['secret_cipher']), 'private-client-secret')
        for route in ('/binflow/setup', '/binflow/rental-income', '/binflow/settings/xero', '/binflow/rental-invoices'):
            response = self.client.get(route)
            self.assertEqual(response.status_code, 200)
            self.assertNotIn(b'private-', response.data)
        with self.client.session_transaction() as session:
            self.assertNotIn('private-', str(dict(session)))

    def test_oauth_state_is_bound_single_use_and_expiring(self):
        self.post('/binflow/xero/credentials', {'client_id': 'test-client-id', 'client_secret': 'private-client-secret'})
        result = self.post('/binflow/xero/connect', follow=False)
        query = parse_qs(urlparse(result.location).query)
        self.assertEqual(query['redirect_uri'], ['https://example.com/binflow/xero/callback'])
        self.assertIn('https://login.xero.com', result.headers['Content-Security-Policy'])
        self.assertEqual(set(query['scope'][0].split()), set(xero.SCOPES.split()))
        state = query['state'][0]
        with patch('binflow_xero.exchange', return_value={'access_token': 'new-access', 'refresh_token': 'new-refresh', 'expires_in': 1800}) as exchange:
            self.client.get('/binflow/xero/callback?state=wrong&code=secret-code')
            exchange.assert_not_called()
            self.client.get('/binflow/xero/callback?state='+state+'&code=secret-code')
            exchange.assert_called_once()
            self.client.get('/binflow/xero/callback?state='+state+'&code=secret-code')
            exchange.assert_called_once()
        self.assertFalse(self.rows('SELECT * FROM bf_xero_oauth'))
        self.post('/binflow/xero/disconnect')
        result = self.post('/binflow/xero/connect', follow=False)
        state = parse_qs(urlparse(result.location).query)['state'][0]
        with self.connection() as conn:
            conn.execute('UPDATE bf_xero_oauth SET created_at=0')
        with patch('binflow_xero.exchange') as exchange:
            self.client.get('/binflow/xero/callback?state='+state+'&code=secret-code')
            exchange.assert_not_called()

    def test_refresh_rotates_and_persists_token_once(self):
        self.connect()
        with self.connection() as conn:
            conn.execute('UPDATE bf_xero_config SET expires_at=0')
        conn = self.connection()
        with patch('binflow_xero.request_json', return_value={'access_token': 'rotated-access', 'refresh_token': 'rotated-refresh', 'expires_in': 1800}) as remote:
            self.assertEqual(xero.access_token(self.app, conn), 'rotated-access')
            self.assertEqual(xero.access_token(self.app, conn), 'rotated-access')
            remote.assert_called_once()
            self.assertIn(b'refresh_token=private-refresh-token', remote.call_args.args[3])
        saved = json.loads(xero.decrypt(self.app, xero.config(conn)['token_cipher']))
        self.assertEqual(saved['refresh_token'], 'rotated-refresh')

    def test_sample_invoice_math_payload_and_inventory_unchanged(self):
        self.connect()
        before = self.rows('SELECT * FROM bf_bins')
        response = self.preview()
        self.assertEqual(response.status_code, 302)
        item = self.invoice()
        snapshot = json.loads(item['snapshot_json'])
        payload = json.loads(item['payload_json'])['Invoices'][0]
        self.assertEqual((snapshot['subtotal'], snapshot['gst']), (1086, 109))
        self.assertEqual((payload['Status'], payload['Type'], payload['CurrencyCode']), ('DRAFT', 'ACCREC', 'AUD'))
        self.assertEqual(payload['Contact'], {'ContactID': CONTACT})
        self.assertEqual((payload['Date'], payload['DueDate']), ('2026-10-19', '2026-11-02'))
        self.assertEqual(payload['LineItems'][0]['Quantity'], 6)
        self.assertEqual(payload['LineItems'][0]['UnitAmount'], 1.81)
        self.assertIn('TEST ONLY', payload['LineItems'][0]['Description'])
        page = self.client.get(response.location)
        self.assertEqual(page.status_code, 200)
        self.assertIn(b'$11.95', page.data)
        self.assertEqual(self.rows('SELECT * FROM bf_bins'), before)

    def test_actual_requires_billable_inventory_review_and_complete_rates(self):
        self.connect()
        self.preview(mode='actual', reviewed_period='yes')
        self.assertFalse(self.rows('SELECT * FROM bf_rental_invoices'))
        with self.connection() as conn:
            conn.execute("UPDATE bf_bins SET location_id=3,status='at_client' WHERE id=1")
        self.preview(mode='actual')
        self.assertFalse(self.rows('SELECT * FROM bf_rental_invoices'))
        with self.connection() as conn:
            conn.execute('UPDATE bf_sizes SET weekly_rental_cents=NULL WHERE id=1')
        self.preview(mode='actual', reviewed_period='yes')
        self.assertFalse(self.rows('SELECT * FROM bf_rental_invoices'))
        with self.connection() as conn:
            conn.execute('UPDATE bf_sizes SET weekly_rental_cents=181 WHERE id=1')
            conn.execute("UPDATE bf_bins SET service_status='damaged' WHERE id=1")
        self.preview(mode='actual', reviewed_period='yes')
        self.assertFalse(self.rows('SELECT * FROM bf_rental_invoices'))
        with self.connection() as conn:
            conn.execute("UPDATE bf_bins SET service_status='in_service' WHERE id=1")
        self.preview(mode='actual', reviewed_period='yes')
        snapshot = json.loads(self.invoice()['snapshot_json'])
        self.assertEqual(snapshot['lines'][0]['bins'], 1)
        self.assertEqual(snapshot['subtotal'], 362)

    def test_period_overlap_blocks_until_unsent_preview_cancelled(self):
        self.connect()
        with self.connection() as conn:
            conn.execute("UPDATE bf_bins SET location_id=3,status='at_client' WHERE id=1")
        self.preview(mode='actual', reviewed_period='yes')
        item = self.invoice()
        self.preview(mode='actual', reviewed_period='yes', period_start='2026-10-19', period_end='2026-11-01')
        self.assertEqual(len(self.rows('SELECT * FROM bf_rental_invoices')), 1)
        self.post('/binflow/rental-invoices/'+item['id']+'/cancel')
        self.preview(mode='actual', reviewed_period='yes')
        self.assertEqual(len(self.rows('SELECT * FROM bf_rental_invoices')), 2)

    def test_preview_validation_rejects_dates_quantities_and_untrusted_choices(self):
        self.connect()
        for overrides in ({'period_end': '2026-10-20'}, {'sample_quantity': '0'}, {'sample_quantity': '11'},
                          {'contact_id': 'unselected'}, {'tax_type': 'unselected'}, {'account_code': 'unselected'}):
            self.preview(**overrides)
            self.assertFalse(self.rows('SELECT * FROM bf_rental_invoices'))
        with self.connection() as conn:
            conn.execute('UPDATE bf_xero_config SET choices_at=0')
        self.preview()
        self.assertFalse(self.rows('SELECT * FROM bf_rental_invoices'))

    def test_frozen_preview_and_double_click_create_only_one_draft(self):
        self.connect()
        self.preview()
        item = self.invoice()
        with self.connection() as conn:
            conn.execute('UPDATE bf_sizes SET weekly_rental_cents=999 WHERE id=1')
        path = '/binflow/rental-invoices/'+item['id']+'/create'
        with patch('binflow_xero.request_json', return_value={'Invoices': [{'InvoiceID': INVOICE, 'InvoiceNumber': 'INV-0042', 'Status': 'DRAFT'}]}) as remote:
            self.post(path)
            remote.assert_not_called()
            self.post(path, {'confirm_draft': 'yes'})
            self.post(path, {'confirm_draft': 'yes'})
            remote.assert_called_once()
            args = remote.call_args.args
            self.assertEqual(args[1], 'POST')
            self.assertEqual(args[2]['Idempotency-Key'], item['id'])
            self.assertEqual(args[2]['Xero-tenant-id'], TENANT)
            self.assertEqual(json.loads(args[3]), json.loads(item['payload_json']))
        self.assertEqual(self.invoice()['status'], 'created')
        self.assertEqual(self.invoice()['invoice_number'], 'INV-0042')
        self.post('/binflow/rental-invoices/'+item['id']+'/cancel')
        self.assertEqual(self.invoice()['status'], 'created')

    def test_timeout_is_reconciled_without_another_creation(self):
        self.connect()
        self.preview()
        item = self.invoice()
        base = '/binflow/rental-invoices/'+item['id']
        with patch('binflow_xero.request_json', side_effect=xero.XeroError('Xero could not be reached.')) as remote:
            self.post(base+'/create', {'confirm_draft': 'yes'})
            self.post(base+'/create', {'confirm_draft': 'yes'})
            remote.assert_called_once()
        self.assertEqual(self.invoice()['status'], 'uncertain')
        with patch('binflow_xero.request_json', return_value={'Invoices': [{'Reference': item['reference'], 'Type': 'ACCREC',
                   'InvoiceID': INVOICE, 'InvoiceNumber': 'INV-0042', 'Status': 'DRAFT'}]}) as remote:
            self.post(base+'/check')
            self.assertEqual(remote.call_args.args[1], 'GET')
        self.assertEqual(self.invoice()['status'], 'created')

    def test_organisation_change_cannot_reroute_saved_invoice(self):
        self.connect()
        self.preview()
        item = self.invoice()
        with self.connection() as conn:
            conn.execute('UPDATE bf_xero_config SET tenant_id=?', (CONTACT,))
        with patch('binflow_xero.request_json') as remote:
            self.post('/binflow/rental-invoices/'+item['id']+'/create', {'confirm_draft': 'yes'})
            remote.assert_not_called()
        self.assertEqual(self.invoice()['status'], 'preview')

    def test_invalid_remote_result_never_records_success_or_retries(self):
        self.connect()
        for response in ([], {'Invoices': None}, {'Invoices': [{'Status': 'AUTHORISED', 'InvoiceID': INVOICE}]}):
            self.preview()
            base = '/binflow/rental-invoices/'+self.invoice()['id']
            with patch('binflow_xero.request_json', return_value=response) as remote:
                self.assertEqual(self.post(base+'/create', {'confirm_draft': 'yes'}).status_code, 200)
                self.post(base+'/create', {'confirm_draft': 'yes'})
                remote.assert_called_once()
            self.assertEqual(self.invoice()['status'], 'uncertain')

    def test_connection_test_reads_only_and_rejects_foreign_currency(self):
        self.connect()
        with patch('binflow_xero.request_json', return_value={'Organisations': [{'Name': 'Test Organisation', 'BaseCurrency': 'AUD'}]}) as remote:
            response = self.post('/binflow/xero/test')
            self.assertIn(b'connection test passed', response.data)
            self.assertEqual(remote.call_args.args[1], 'GET')
            self.assertTrue(remote.call_args.args[0].endswith('/Organisation'))
        self.assertFalse(self.rows('SELECT * FROM bf_rental_invoices'))
        with patch('binflow_xero.request_json', return_value={'Organisations': [{'BaseCurrency': 'USD'}]}):
            self.assertIn(b'Australian-dollar', self.post('/binflow/xero/test').data)

    def test_all_actions_require_csrf_and_admin(self):
        self.connect()
        self.preview()
        base = '/binflow/rental-invoices/'+self.invoice()['id']
        paths = ['/binflow/xero/'+action for action in ('credentials', 'connect', 'organisation', 'test', 'disconnect')]
        paths += ['/binflow/rental-invoices/preview'] + [base+'/'+a for a in ('create', 'check', 'cancel')]
        for path in paths:
            self.assertEqual(self.client.post(path).status_code, 400, path)
        with self.connection() as conn:
            conn.execute("INSERT INTO users(role,first_name,last_name,email,username,password_hash) VALUES('employee','Test','Driver','driver@example.com','driver',?)",
                         (generate_password_hash('test-admin-password'),))
        self.post('/logout')
        self.login('employee', 'driver')
        for path in paths:
            self.assertEqual(self.post(path).status_code, 403, path)
        for path in ('/binflow/settings/xero', '/binflow/xero/callback', '/binflow/rental-invoices', base):
            self.assertEqual(self.client.get(path).status_code, 403, path)


if __name__ == '__main__':
    unittest.main()
