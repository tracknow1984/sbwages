import json
import sqlite3
import unittest

from werkzeug.security import generate_password_hash

import test_binflow as fixtures
from app import create_app
from binflow_sizes import migrate


class BinSizeTests(unittest.TestCase):
    setUp = fixtures.BinFlowTests.setUp
    tearDown = fixtures.BinFlowTests.tearDown
    rows = fixtures.BinFlowTests.rows
    post = fixtures.BinFlowTests.post
    login = fixtures.BinFlowTests.login
    job = fixtures.BinFlowTests.job

    def values(self, **changes):
        values = dict(name='Renamed wheelie bin', litres='250', tare='14.125',
                      weekly_rental='1.81', capital_purchase='216.90', version='1')
        return {**values, **changes}

    def test_edit_persists_prices_defaults_and_identity_across_restart(self):
        self.job(deliver=(1,))
        original = self.rows('SELECT * FROM bf_sizes WHERE id=1')[0]
        bins = self.rows('SELECT * FROM bf_bins')
        dockets = self.rows('SELECT * FROM bf_job_bins')
        response = self.post('/binflow/setup/bin-sizes/1/edit', self.values())
        self.assertEqual(response.status_code, 200)
        self.assertIn(b'Bin size and pricing saved', response.data)
        edited = self.rows('SELECT * FROM bf_sizes WHERE id=1')[0]
        self.assertEqual((edited['name'], edited['litres'], edited['default_tare_g'],
                          edited['weekly_rental_cents'], edited['capital_purchase_cents'], edited['version']),
                         ('Renamed wheelie bin', 250, 14125, 181, 21690, 2))
        history = self.rows('SELECT * FROM bf_size_history WHERE size_id=1')[0]
        self.assertEqual(json.loads(history['snapshot']), original)
        self.assertEqual(history['changed_by'], 'SB Empire')
        create_app(dict(self.app.config))
        self.assertEqual(len(self.rows('SELECT * FROM bf_sizes')), 3)
        self.assertEqual(self.rows('SELECT * FROM bf_sizes WHERE id=1')[0], edited)
        self.assertEqual(self.rows('SELECT * FROM bf_bins'), bins)
        self.assertEqual(self.rows('SELECT * FROM bf_job_bins'), dockets)
        self.post('/binflow/bins', dict(warehouse_id=1, size_id=1, quantity=1, tare=''))
        self.assertEqual(self.rows('SELECT tare_g FROM bf_bins ORDER BY id DESC LIMIT 1')[0]['tare_g'], 14125)
        page = self.client.get('/binflow/setup').data
        for label in (b'$1.81', b'$216.90', b'Renamed wheelie bin', b'Weekly rental', b'Capital purchase'):
            self.assertIn(label, page)

    def test_add_prices_and_distinguish_unset_from_zero(self):
        before = self.rows('SELECT * FROM bf_sizes')
        self.assertIsNone(before[0]['weekly_rental_cents'])
        self.assertIsNone(before[0]['capital_purchase_cents'])
        response = self.post('/binflow/setup', dict(action='size', **self.values(name='New 120 L', litres='120', weekly_rental='0', capital_purchase='99.95')))
        self.assertIn(b'Bin size and pricing added', response.data)
        added = self.rows('SELECT * FROM bf_sizes ORDER BY id DESC LIMIT 1')[0]
        self.assertEqual((added['weekly_rental_cents'], added['capital_purchase_cents']), (0, 9995))
        edit = self.client.get(f'/binflow/setup/bin-sizes/{added["id"]}/edit').data
        self.assertIn(b'value="0.00"', edit)
        self.post(f'/binflow/setup/bin-sizes/{added["id"]}/edit', self.values(name='New 120 L', litres='120', weekly_rental='', capital_purchase=''))
        blank = self.rows('SELECT * FROM bf_sizes WHERE id=?', (added['id'],))[0]
        self.assertIsNone(blank['weekly_rental_cents'])
        self.assertIsNone(blank['capital_purchase_cents'])
        self.assertEqual(self.rows('SELECT * FROM bf_sizes WHERE id<4'), before)

    def test_invalid_money_capacity_duplicates_and_stale_edits_are_atomic(self):
        before = self.rows('SELECT * FROM bf_sizes')
        invalid = [dict(weekly_rental=value) for value in ('-1', '1.001', 'NaN', 'Infinity', '1000000.01')]
        invalid += [dict(capital_purchase='10.999'), dict(litres='0'), dict(litres='50001'),
                    dict(litres='240.5'), dict(tare='-1'), dict(tare='1.0001'), dict(name=''), dict(name='660 l'), dict(version='0')]
        for changes in invalid:
            with self.subTest(changes=changes):
                response = self.post('/binflow/setup/bin-sizes/1/edit', self.values(**changes))
                self.assertEqual(response.status_code, 400)
                self.assertEqual(self.rows('SELECT * FROM bf_sizes'), before)
                self.assertFalse(self.rows('SELECT * FROM bf_size_history'))
        self.post('/binflow/setup/bin-sizes/1/edit', self.values())
        stale = self.post('/binflow/setup/bin-sizes/1/edit', self.values(weekly_rental='2.00'))
        self.assertEqual(stale.status_code, 400)
        self.assertIn(b'changed while you were editing', stale.data)
        self.assertEqual(self.rows('SELECT weekly_rental_cents FROM bf_sizes WHERE id=1')[0]['weekly_rental_cents'], 181)

    def test_access_csrf_and_missing_size(self):
        route = '/binflow/setup/bin-sizes/1/edit'
        self.assertEqual(self.client.post(route, data=self.values()).status_code, 400)
        self.assertEqual(self.client.get('/binflow/setup/bin-sizes/999/edit').status_code, 404)
        with sqlite3.connect(self.path) as conn:
            conn.execute("INSERT INTO users(role,first_name,last_name,email,username,password_hash) VALUES('employee','Test','Driver','driver@example.com','driver',?)",
                         (generate_password_hash('test-admin-password'),))
        self.post('/logout')
        self.login('employee', 'driver')
        self.assertEqual(self.client.get(route).status_code, 403)
        self.assertEqual(self.post(route, self.values()).status_code, 403)

    def test_old_database_migration_is_additive_and_repeat_safe(self):
        conn = sqlite3.connect(':memory:')
        self.addCleanup(conn.close)
        conn.row_factory = sqlite3.Row
        conn.execute('CREATE TABLE bf_sizes(id INTEGER PRIMARY KEY,name TEXT NOT NULL UNIQUE,litres INTEGER NOT NULL,default_tare_g INTEGER NOT NULL)')
        conn.execute("INSERT INTO bf_sizes VALUES(11,'240 L',240,13000)")
        migrate(conn)
        original = dict(conn.execute('SELECT * FROM bf_sizes WHERE id=11').fetchone())
        self.assertEqual((original['name'], original['litres'], original['default_tare_g']), ('240 L', 240, 13000))
        self.assertIsNone(original['weekly_rental_cents'])
        conn.execute("UPDATE bf_sizes SET name='250 L',litres=250,weekly_rental_cents=181 WHERE id=11")
        migrate(conn)
        self.assertEqual(conn.execute('SELECT COUNT(*) FROM bf_sizes').fetchone()[0], 3)
        self.assertEqual(conn.execute('SELECT COUNT(*) FROM bf_sizes WHERE litres=240').fetchone()[0], 0)
        self.assertEqual(conn.execute('SELECT weekly_rental_cents FROM bf_sizes WHERE id=11').fetchone()[0], 181)
