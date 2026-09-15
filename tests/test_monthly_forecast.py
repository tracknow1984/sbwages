import sqlite3
import tempfile
import unittest
from app import create_app


class MonthlyForecastTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = self.temp.name + '/test.db'
        self.app = create_app({'TESTING': True, 'DATABASE': self.path, 'SECRET_KEY': 'test-' * 10,
                               'ADMIN_EMAIL': 'test@example.com', 'ADMIN_PASSWORD': 'test-password',
                               'SESSION_COOKIE_SECURE': False})
        self.client = self.app.test_client()
        with sqlite3.connect(self.path) as conn:
            self.uid, version = conn.execute("SELECT id,auth_version FROM users WHERE role='admin'").fetchone()
        with self.client.session_transaction() as session:
            session.update(uid=self.uid, version=version, csrf='test')
        import re
        self.revision = re.search(rb'data-revision="([^"]+)"', self.client.get('/admin/rent-calculator').data).group(1).decode()
        self.values = {'csrf': 'test', 'revision': self.revision}
        for month in range(1, 49):
            self.values[f'month_area_{month}'] = '0'
            self.values[f'month_rate_{month}'] = '15'

    def tearDown(self):
        self.temp.cleanup()

    def save(self, values):
        return self.client.post('/admin/rent-calculator/monthly', data=values,
                                headers={'Accept': 'application/json'})

    def test_month_five_onwards_and_reload(self):
        for month in [1, 5, 6, 12, 13, 24, 36, 48]:
            self.values[f'month_area_{month}'] = str(month * 10)
            self.values[f'month_rate_{month}'] = '25.50'
        response = self.save(self.values)
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json['ok'])
        self.assertNotEqual(response.json['revision'], self.revision)
        page = self.client.get('/admin/rent-calculator').data
        self.assertIn(b'name="month_area_48"', page)
        with sqlite3.connect(self.path) as conn:
            self.assertEqual(conn.execute('SELECT new_area,rate_cents FROM rent_months WHERE month=5').fetchone(), (50, 2550))
            self.assertEqual(conn.execute('SELECT new_area,rate_cents FROM rent_months WHERE month=48').fetchone(), (480, 2550))

    def test_stale_tab_cannot_overwrite_newer_forecast(self):
        self.values['month_area_5'] = '1000'
        saved = self.save(self.values)
        self.assertEqual(saved.status_code, 200)
        self.values['month_area_5'] = '2000'
        stale = self.save(self.values)
        self.assertEqual(stale.status_code, 409)
        with sqlite3.connect(self.path) as conn:
            self.assertEqual(conn.execute('SELECT new_area FROM rent_months WHERE month=5').fetchone()[0], 1000)
        self.values['revision'] = saved.json['revision']
        self.assertEqual(self.save(self.values).status_code, 200)

    def test_invalid_month_keeps_last_saved_data(self):
        self.values['month_area_5'] = '1000'
        saved = self.save(self.values)
        self.values['revision'] = saved.json['revision']
        self.values['month_area_6'] = ''
        self.assertEqual(self.save(self.values).status_code, 400)
        with sqlite3.connect(self.path) as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*),SUM(new_area) FROM rent_months').fetchone(), (48, 1000))

    def test_save_requires_admin_and_csrf(self):
        self.values['csrf'] = 'wrong'
        self.assertEqual(self.save(self.values).status_code, 400)
        self.values['csrf'] = 'test'
        with sqlite3.connect(self.path) as conn:
            conn.execute("UPDATE users SET role='employee' WHERE id=?", (self.uid,))
        self.assertEqual(self.save(self.values).status_code, 403)
