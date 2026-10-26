import json
import re
import sqlite3
import unittest
from html import unescape

from werkzeug.security import generate_password_hash

import test_binflow as fixtures
from test_binflow_client_profiles import row
from binflow_client_profiles import FIELDS
from binflow_supplier_profiles import CATEGORIES, migrate


class SupplierProfileTests(unittest.TestCase):
    setUp = fixtures.BinFlowTests.setUp
    tearDown = fixtures.BinFlowTests.tearDown
    rows = fixtures.BinFlowTests.rows
    post = fixtures.BinFlowTests.post
    login = fixtures.BinFlowTests.login

    def add(self, category='decommission-partners', **values):
        return self.post(f'/binflow/supplier-profiles/{category}/new', row(**values))

    def test_three_registers_and_all_fields_are_independent_and_persistent(self):
        self.client.get('/binflow/client-profiles')
        tables = ('bf_clients', 'bf_locations', 'bf_client_site_profiles', 'bf_bins', 'bf_jobs')
        before = {table: self.rows('SELECT * FROM ' + table) for table in tables}
        for category, label in CATEGORIES.items():
            form = self.client.get(f'/binflow/supplier-profiles/{category}/new')
            for key, _, _ in FIELDS:
                self.assertIn(f'name="{key}"'.encode(), form.data)
            result = self.add(category, customer_site=label + ' Test site')
            self.assertEqual(result.status_code, 200)
            self.assertIn(b'Supplier site profile added', result.data)
            saved = self.rows('SELECT * FROM bf_supplier_site_profiles WHERE category=?', (category,))[0]
            for key, _, _ in FIELDS:
                self.assertEqual(saved[key], row(customer_site=label + ' Test site')[key])
            self.assertEqual(saved['updated_by'], 'SB Empire')
        with sqlite3.connect(self.path) as conn:
            migrate(conn)
            migrate(conn)
        self.assertEqual(len(self.rows('SELECT * FROM bf_supplier_site_profiles')), 2)
        main = self.client.get('/binflow/client-profiles').data
        self.assertLess(main.index(b'Customer site register</h2>'), main.index(b'id="supplier-register-title"'))
        for label in CATEGORIES.values():
            self.assertIn((label + ' Test site').encode(), main)
        for table, records in before.items():
            self.assertEqual(self.rows('SELECT * FROM ' + table), records)
        decomm = self.client.get('/binflow/supplier-profiles/decommission-partners').data
        self.assertIn(b'Decommission Partners Test site', decomm)
        self.assertNotIn(b'Industry Suppliers Test site', decomm)

    def test_edit_audit_stale_save_and_duplicate_rollback(self):
        self.add(customer_site='Partner first')
        self.add(customer_site='Partner second')
        saved = self.rows('SELECT * FROM bf_supplier_site_profiles ORDER BY id')[0]
        url = f'/binflow/supplier-profiles/site/{saved["id"]}'
        result = self.post(url, {**row(customer_site='Partner first', contact_name='Reviewed contact'), 'version': '1'})
        self.assertIn(b'Supplier site profile saved', result.data)
        edited = self.rows('SELECT * FROM bf_supplier_site_profiles WHERE id=?', (saved['id'],))[0]
        self.assertEqual((edited['version'], edited['contact_name']), (2, 'Reviewed contact'))
        history = self.rows('SELECT * FROM bf_supplier_profile_history')[0]
        self.assertEqual(json.loads(history['snapshot']), saved)
        stale = self.post(url, {**row(customer_site='Partner first'), 'version': '1'})
        self.assertEqual(stale.status_code, 400)
        self.assertIn(b'changed while you were editing', stale.data)
        duplicate = self.post(url, {**row(customer_site='Partner second'), 'version': '2'})
        self.assertEqual(duplicate.status_code, 400)
        self.assertEqual(len(self.rows('SELECT * FROM bf_supplier_profile_history')), 1)
        self.assertEqual(self.rows('SELECT * FROM bf_supplier_site_profiles WHERE id=?', (saved['id'],))[0], edited)

    def test_validation_and_category_are_enforced_on_server(self):
        for change in ({'customer_site': ''}, {'address': ''}, {'state': 'BAD'}, {'postcode': '123'},
                       {'asic_required': 'maybe'}, {'contact_verified': 'maybe'}, {'contact_name': 'x' * 501}):
            with self.subTest(change=change):
                self.assertEqual(self.add(**change).status_code, 400)
        self.assertFalse(self.rows('SELECT * FROM bf_supplier_site_profiles'))
        self.assertEqual(self.client.get('/binflow/supplier-profiles/unknown').status_code, 404)
        self.assertEqual(self.add('unknown').status_code, 404)
        self.assertEqual(self.client.get('/binflow/supplier-profiles/site/999').status_code, 404)
        self.assertEqual(self.add(customer_site='Duplicate').status_code, 200)
        self.assertEqual(self.add(customer_site='duplicate').status_code, 400)
        self.assertEqual(self.add('industry-suppliers', customer_site='Duplicate').status_code, 200)
        self.assertEqual(len(self.rows('SELECT * FROM bf_supplier_site_profiles')), 2)

    def test_filters_search_sort_and_pagination_stay_in_category(self):
        for number in range(12):
            self.add(customer_site=f'Supplier {number:02d}', schedule='Weekly', contact_verified='yes', asic_required='yes')
        self.add('industry-suppliers', customer_site='Wrong category', schedule='Weekly', contact_verified='yes', asic_required='yes')
        url = '/binflow/supplier-profiles/decommission-partners?q=Supplier&state=QLD&group=Tier+1&schedule=Weekly&verified=yes&asic=yes&sort=state&per_page=10'
        first = self.client.get(url).data
        self.assertIn(b'of 12 matching', first)
        self.assertIn(b'Supplier 00', first)
        self.assertNotIn(b'Supplier 10', first)
        self.assertNotIn(b'Wrong category', first)
        next_url = unescape(re.search(r'href="([^"]+)">Next', first.decode())[1])
        for expected in ('decommission-partners', 'state=QLD', 'schedule=Weekly', 'verified=yes', 'asic=yes', 'sort=state', 'page=2'):
            self.assertIn(expected, next_url)
        second = self.client.get(next_url).data
        self.assertIn(b'Supplier 10', second)
        self.assertNotIn(b'Supplier 00', second)
        self.assertIn(b'No profiles match', self.client.get(url + '&status=review').data)
        self.assertIn(b'Page 2 of 2', self.client.get(url + '&page=999').data)

    def test_admin_only_csrf_and_escaped_contacts(self):
        url = '/binflow/supplier-profiles/decommission-partners/new'
        self.assertEqual(self.client.post(url, data=row()).status_code, 400)
        self.add(contact_name='<script>alert(1)</script>')
        page = self.client.get('/binflow/supplier-profiles/decommission-partners').data
        self.assertIn(b'&lt;script&gt;', page)
        self.assertNotIn(b'<script>alert', page)
        with sqlite3.connect(self.path) as conn:
            conn.execute("INSERT INTO users(role,first_name,last_name,email,username,password_hash) VALUES('employee','Test','Driver','driver@example.com','driver',?)",
                         (generate_password_hash('test-admin-password'),))
        self.post('/logout')
        self.login('employee', 'driver')
        for route in ('/binflow/supplier-profiles', '/binflow/supplier-profiles/decommission-partners',
                      url, '/binflow/supplier-profiles/site/1'):
            self.assertEqual(self.client.get(route).status_code, 403)
        self.assertEqual(self.post(url, row()).status_code, 403)
        self.assertEqual(self.post('/binflow/supplier-profiles/site/1', {**row(), 'version': '1'}).status_code, 403)
