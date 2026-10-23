import base64
import gzip
import json
import sqlite3
import unittest
from html.parser import HTMLParser

import test_binflow as legacy
from binflow_allocation_import import replace_from_workbook
from binflow_client_profiles import KEYS
from binflow_requirements import site_plan


class Tags(HTMLParser):
    def __init__(self, html):
        super().__init__()
        self.tags = []
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        self.tags.append((tag, dict(attrs)))


class AllocationWorkbookTests(unittest.TestCase):
    setUp = legacy.BinFlowTests.setUp
    tearDown = legacy.BinFlowTests.tearDown
    post = legacy.BinFlowTests.post
    rows = legacy.BinFlowTests.rows
    login = legacy.BinFlowTests.login
    job = legacy.BinFlowTests.job
    initial_delivery = legacy.BinFlowTests.initial_delivery
    exchange = legacy.BinFlowTests.exchange

    def connection(self):
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        conn.execute('PRAGMA foreign_keys=ON')
        return conn

    def payload(self):
        rows = []
        for number, name, state, address, counts in (
                (5, 'Alpha QLD', 'QLD', '3 Client Road', (3, 2, 1, 0, 0)),
                (6, 'New Fabric Co', 'WA', '8 Example Street', (0, 0, 0, 7, 4)),
                (7, 'Missing Details Co', '', '', (0, 0, 0, 0, 0))):
            profile = dict(group_name='Tier 3', customer_site=name, state=state, address=address)
            rows.append(dict(profile=profile, source_row=number, profile_source_key=f'contacts-test:{number}',
                             equipment=dict(zip(('bin660', 'bin240', 'cage', 'pallecon', 'bulkbag'), counts))))
        return dict(id='allocation-test-v1', rows=rows, expected_rows=3,
                    expected_equipment=dict(bin660=3, bin240=2, cage=1, pallecon=7, bulkbag=4))

    def encoded(self, payload):
        return base64.b64encode(gzip.compress(json.dumps(payload).encode())).decode()

    def replace(self, payload=None):
        with self.connection() as conn:
            return replace_from_workbook(conn, self.encoded(payload or self.payload()), '2026-10-23T09:00:00')

    def save_target(self, site, small, large, version=0):
        return self.post(f'/binflow/locations/{site}/requirements', dict(
            version=version, quantity_1=small, quantity_2=0, quantity_3=large, notes='Confirmed quantities'))

    def test_replacement_preserves_operations_contacts_history_and_rotation(self):
        self.save_target(3, 9, 8)
        self.save_target(4, 6, 5)
        self.initial_delivery()
        self.post('/binflow/allocations', {'rotation_QLD_1': 12})
        with self.connection() as conn:
            conn.execute("UPDATE bf_client_site_profiles SET source_key='contacts-test:5',contact_name='Verified Person',contact_verified='yes',version=4 WHERE location_id=3")
        tables = ('bf_bins', 'bf_jobs', 'bf_job_bins', 'bf_events', 'bf_state_bin_pools')
        before = {t: self.rows('SELECT * FROM '+t) for t in tables}
        contacts = self.rows('SELECT * FROM bf_client_site_profiles WHERE location_id=3')
        summary, changed = self.replace()
        self.assertTrue(changed)
        self.assertEqual((summary['sites'], summary['linked'], summary['awaiting_location']), (3, 2, 1))
        self.assertEqual(summary['states']['QLD'], {'240 L': 2, '1100 L': 3})
        self.assertEqual({t: self.rows('SELECT * FROM '+t) for t in tables}, before)
        self.assertEqual(self.rows('SELECT * FROM bf_client_site_profiles WHERE location_id=3'), contacts)
        self.assertFalse(self.rows('SELECT * FROM bf_site_requirements WHERE location_id=4'))
        self.assertEqual(self.rows('SELECT active FROM bf_site_profiles WHERE location_id=4'), [{'active': 0}])
        with self.connection() as conn:
            self.assertEqual(site_plan(conn, 4, 1), (None, []))
        previous = json.loads(self.rows('SELECT previous_plan FROM bf_allocation_imports')[0]['previous_plan'])
        self.assertEqual(len(previous['bf_site_profiles']), 2)
        self.assertEqual(len(previous['bf_site_requirements']), 6)
        self.assertEqual(self.save_target(3, 99, 99, version=1).status_code, 400)
        self.assertEqual(self.save_target(3, 4, 6, version=3).status_code, 200)
        self.assertFalse(self.replace()[1])
        self.assertEqual(self.rows('SELECT quantity FROM bf_site_requirements WHERE location_id=3 AND size_id=1'), [{'quantity': 4}])

    def test_render_pending_rows_source_counts_state_filter_and_closed_stock_panel(self):
        self.replace()
        response = self.client.get('/binflow/allocations')
        self.assertEqual(response.status_code, 200)
        html = response.data.decode()
        self.assertIn('3 sites', html)
        self.assertNotIn('Beta QLD', html)
        self.assertIn('7 × pallecons, 4 × bulk bags', html)
        self.assertIn('Missing Details Co', html)
        self.assertIn('Complete site profile', html)
        attrs = next(attrs for tag, attrs in Tags(html).tags if tag == 'details' and 'bf-stock-plan' in attrs.get('class', ''))
        self.assertNotIn('open', attrs)
        wa = self.client.get('/binflow/allocations?state=WA').data
        self.assertIn(b'New Fabric Co', wa)
        self.assertNotIn(b'Alpha QLD', wa)
        self.assertIn('3 × 660L bins'.encode(), self.client.get('/binflow/locations/3/requirements').data)
        self.post('/logout')
        self.assertEqual(self.client.get('/binflow/allocations').status_code, 302)

    def test_invalid_payload_and_duplicate_mapping_roll_back_every_change(self):
        self.save_target(3, 8, 7)
        tables = ('bf_site_profiles', 'bf_site_requirements', 'bf_profile_history',
                  'bf_client_site_profiles', 'bf_locations', 'bf_clients', 'bf_allocation_workbook_rows')
        before = {t: self.rows('SELECT * FROM '+t) for t in tables}
        invalid = self.payload()
        invalid['expected_equipment']['bin660'] = 99
        with self.assertRaises(ValueError):
            self.replace(invalid)
        duplicate = self.payload()
        duplicate['rows'][1]['profile_source_key'] = duplicate['rows'][0]['profile_source_key']
        with self.assertRaises(ValueError):
            self.replace(duplicate)
        self.assertEqual({t: self.rows('SELECT * FROM '+t) for t in tables}, before)
        self.assertFalse(self.rows('SELECT * FROM bf_allocation_imports'))

    def test_completing_pending_profile_activates_once_and_preserves_later_targets(self):
        self.replace()
        profile = self.rows("SELECT * FROM bf_client_site_profiles WHERE source_key='contacts-test:7'")[0]
        values = {k: profile[k] for k in KEYS}
        values.update(state='SA', address='10 Newly Confirmed Road', version=profile['version'])
        result = self.post(f'/binflow/client-profiles/{profile["id"]}', values)
        self.assertEqual(result.status_code, 200)
        updated = self.rows('SELECT * FROM bf_client_site_profiles WHERE id=?', (profile['id'],))[0]
        lid = updated['location_id']
        self.assertIsNotNone(lid)
        self.assertEqual(self.rows('SELECT active FROM bf_site_profiles WHERE location_id=?', (lid,)), [{'active': 1}])
        self.save_target(lid, 3, 2, version=1)
        values.update(version=updated['version'], contact_name='Updated contact')
        self.assertEqual(self.post(f'/binflow/client-profiles/{profile["id"]}', values).status_code, 200)
        self.assertEqual(self.rows('SELECT quantity FROM bf_site_requirements WHERE location_id=? AND size_id=1', (lid,)), [{'quantity': 3}])


if __name__ == '__main__':
    unittest.main()
