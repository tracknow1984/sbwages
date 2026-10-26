import csv
import io
import sqlite3
import unittest

from werkzeug.security import generate_password_hash

import test_binflow as fixtures
from binflow_rental import report


class RentalIncomeTests(unittest.TestCase):
    setUp = fixtures.BinFlowTests.setUp
    tearDown = fixtures.BinFlowTests.tearDown
    rows = fixtures.BinFlowTests.rows
    post = fixtures.BinFlowTests.post
    login = fixtures.BinFlowTests.login
    job = fixtures.BinFlowTests.job
    exchange = fixtures.BinFlowTests.exchange

    def connection(self):
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        conn.execute('PRAGMA foreign_keys=ON')
        self.addCleanup(conn.close)
        return conn

    def prepare(self):
        with self.connection() as conn:
            conn.execute('UPDATE bf_sizes SET weekly_rental_cents=CASE litres WHEN 240 THEN 181 WHEN 1100 THEN 833 END')
            for location, small, large in ((3, 3, 2), (4, 0, 1), (5, 1, 0)):
                conn.execute("INSERT INTO bf_site_profiles(location_id,version,updated_at,updated_by) VALUES(?,1,'now','Test')", (location,))
                conn.executemany('INSERT INTO bf_site_requirements(location_id,size_id,quantity) VALUES(?,?,?)',
                                 [(location, 1, small), (location, 3, large)])
            conn.execute("INSERT INTO bf_state_bin_pools VALUES('QLD',1,999,'now','Test')")
        self.post('/binflow/bins', dict(warehouse_id=1, size_id=3, quantity=2, tare=''))
        with self.connection() as conn:
            conn.execute("UPDATE bf_bins SET location_id=3,status='at_client' WHERE id IN (1,2)")
            conn.execute("UPDATE bf_bins SET location_id=5,state='NSW',status='full' WHERE id=3")
            conn.execute("UPDATE bf_bins SET location_id=4,status='at_client' WHERE id=7")

    def snapshot(self):
        return report(self.connection())

    def test_client_totals_use_saved_targets_and_current_custody_with_one_fortnight_charge(self):
        self.prepare()
        self.job(deliver=(5,))  # Reserved at warehouse: no actual rent until delivery.
        data = self.snapshot()
        totals = data['totals']
        self.assertEqual((totals['proposed_count'], totals['actual_count'], totals['billable_count']), (7, 4, 4))
        self.assertEqual(totals['proposed_cents'], 4*181 + 3*833)
        self.assertEqual(totals['actual_cents'], 3*181 + 833)
        self.assertEqual(totals['fortnight_cents'], 2752)
        self.assertEqual((totals['gst_cents'], totals['total_cents']), (275, 3027))
        alpha = next(c for c in data['clients'] if c['name']=='Client Alpha')
        self.assertEqual(alpha['summary']['proposed_cents'], 2390)
        self.assertEqual(alpha['summary']['actual_cents'], 543)
        self.assertEqual(len(alpha['sites']), 2)
        page = self.client.get('/binflow/rental-income')
        self.assertEqual(page.status_code, 200)
        for label in (b'Bill to BlockTexx', b'$27.52', b'$30.27', b'Client Alpha', b'Client Beta', b'Export national summary'):
            self.assertIn(label, page.data)
        self.exchange(1, (5,))
        self.assertEqual(self.snapshot()['totals']['actual_cents'], 1376+181)
        self.job(collect=(1,))
        self.exchange(2, (1,))
        self.assertEqual(self.snapshot()['totals']['actual_cents'], 1376)

    def test_missing_rates_do_not_become_zero_and_rate_edits_recalculate(self):
        self.prepare()
        with self.connection() as conn:
            conn.execute('UPDATE bf_sizes SET weekly_rental_cents=NULL WHERE id=3')
        totals = self.snapshot()['totals']
        self.assertIsNone(totals['proposed_cents'])
        self.assertIsNone(totals['actual_cents'])
        self.assertIsNone(totals['fortnight_cents'])
        self.assertEqual((totals['proposed_unpriced'], totals['actual_unpriced']), (3,1))
        self.assertEqual(totals['actual_known_cents'], 543)
        html = self.client.get('/binflow/rental-income').data
        self.assertIn(b'Missing weekly rental rates', html)
        self.assertIn(b'Incomplete', html)
        self.post('/binflow/setup/bin-sizes/3/edit', dict(name='1100 L',litres=1100,tare=42,
                  weekly_rental='0',capital_purchase='',version=1))
        totals = self.snapshot()['totals']
        self.assertEqual(totals['actual_cents'], 543)
        self.assertEqual(totals['fortnight_cents'], 1086)
        self.assertEqual(totals['actual_unpriced'], 0)

    def test_empty_pending_retired_sites_and_bins_needing_review_stay_visible(self):
        self.prepare()
        with self.connection() as conn:
            conn.execute("INSERT INTO bf_clients(name,created_at) VALUES('No Sites Yet','now')")
            conn.execute("INSERT INTO bf_client_site_profiles(customer_site,group_name,updated_at,updated_by) VALUES('Unlinked Test','Tier 1','now','Test')")
            conn.execute('UPDATE bf_site_profiles SET active=0 WHERE location_id=3')
            conn.execute("UPDATE bf_bins SET service_status='damaged' WHERE id=1")
            conn.execute("UPDATE bf_bins SET service_status='lost' WHERE id=2")
        data = self.snapshot()
        totals = data['totals']
        self.assertEqual(totals['proposed_count'], 2)  # Retired targets excluded.
        self.assertEqual((totals['actual_count'], totals['billable_count']), (3, 2))
        self.assertEqual(totals['review_count'], 2)
        self.assertIsNone(totals['actual_cents'])
        self.assertEqual(totals['actual_known_cents'], 181+833)
        self.assertEqual(totals['missing_targets'], 1)
        self.assertIsNone(totals['proposed_cents'])
        html = self.client.get('/binflow/rental-income').data
        for label in (b'No Sites Yet', b'Unlinked Test', b'Retired proposal', b'BF000001', b'BF000002'):
            self.assertIn(label, html)

    def test_search_and_state_scope_only_filter_table_and_export_is_national(self):
        self.prepare()
        from flask import template_rendered
        captured = []
        def capture(sender, template, context, **extra): captured.append(context)
        template_rendered.connect(capture, self.app)
        try:
            response = self.client.get('/binflow/rental-income?state=NSW&q=Alpha')
        finally:
            template_rendered.disconnect(capture, self.app)
        self.assertEqual(response.status_code, 200)
        context = captured[-1]
        self.assertEqual(context['totals']['actual_cents'], 1376)
        self.assertEqual(context['filtered_totals']['actual_cents'], 181)
        self.assertEqual(len(context['visible']), 1)
        self.assertEqual([s['state'] for s in context['visible'][0]['sites']], ['NSW'])
        export = self.client.get('/binflow/rental-income/export.csv?state=NSW&q=Alpha')
        self.assertEqual(export.status_code, 200)
        rows = list(csv.reader(io.StringIO(export.data.decode('utf-8-sig'))))
        self.assertIn(['BlockTexx fortnight ex GST','27.52'], rows)
        self.assertTrue(any(r and r[0]=='Client Beta' for r in rows))
        self.assertIn('attachment;', export.headers['Content-Disposition'])
        self.assertEqual(self.client.get('/binflow/rental-income?state=BAD').status_code, 400)

    def test_admin_only_export_and_escaping(self):
        self.prepare()
        with self.connection() as conn:
            conn.execute("UPDATE bf_clients SET name='=malicious <script>alert(1)</script>' WHERE id=1")
        html = self.client.get('/binflow/rental-income').data
        self.assertIn(b'&lt;script&gt;', html)
        self.assertNotIn(b'<script>alert', html)
        rows = list(csv.reader(io.StringIO(self.client.get('/binflow/rental-income/export.csv').data.decode('utf-8-sig'))))
        self.assertTrue(any(r and r[0].startswith("'=malicious") for r in rows))
        with self.connection() as conn:
            conn.execute("INSERT INTO users(role,first_name,last_name,email,username,password_hash) VALUES('employee','Test','Driver','driver@example.com','driver',?)",
                         (generate_password_hash('test-admin-password'),))
        self.post('/logout')
        self.login('employee','driver')
        for path in ('/binflow/rental-income','/binflow/rental-income/export.csv'):
            self.assertEqual(self.client.get(path).status_code, 403)
