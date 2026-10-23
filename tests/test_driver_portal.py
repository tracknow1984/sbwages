import re
import sqlite3
import unittest

from werkzeug.security import generate_password_hash
import test_binflow as legacy


class DriverPortalTests(unittest.TestCase):
    tearDown = legacy.BinFlowTests.tearDown
    post = legacy.BinFlowTests.post
    rows = legacy.BinFlowTests.rows
    login = legacy.BinFlowTests.login
    job = legacy.BinFlowTests.job

    def setUp(self):
        legacy.BinFlowTests.setUp(self)
        with sqlite3.connect(self.path) as conn:
            self.uid = conn.execute("""INSERT INTO users(role,first_name,email,username,password_hash)
                VALUES('employee','Driver','driver@example.com','driver',?)""",
                (generate_password_hash('test-admin-password'),)).lastrowid
            self.other = conn.execute("""INSERT INTO users(role,first_name,email,username,password_hash)
                VALUES('employee','Other','other@example.com','other',?)""",
                (generate_password_hash('test-admin-password'),)).lastrowid
            conn.execute("UPDATE users SET business_role='driver',working_state='QLD' WHERE id=?", (self.uid,))
            conn.execute("UPDATE users SET business_role='driver',working_state='QLD' WHERE id=?", (self.other,))

    def driver(self):
        self.post('/logout')
        return self.login('employee', 'driver')

    def test_exact_menu_and_all_requested_pages_with_driver_landing(self):
        response = self.driver()
        self.assertEqual(response.request.path, '/binflow/driver/my-day')
        expected = ['/binflow/driver/my-day', '/binflow/pods',
                    '/employee/hit-me', '/employee/timesheet', '/employee/prestart',
                    '/employee/details', '/employee/payslips', '/employee/history',
                    '/employee/documents', '/employee/leave']
        for path in expected:
            page = self.client.get(path)
            self.assertEqual(page.status_code, 200, path)
            menu = re.search(r'<nav aria-label="Driver menu">(.*?)</nav>', page.text, re.S).group(1)
            self.assertEqual(re.findall(r'href="([^"]+)"', menu), expected, path)
            for forbidden in ('National overview', 'New allocation', 'Collection calendar',
                              'Collection requests', 'State allocations', 'GPS & geofences', 'Truck locations',
                              'Daily schedule', 'Allocation approvals'):
                self.assertNotIn(forbidden, page.text, path)
        for path in ('/', '/employee/dashboard', '/admin/bin-management', '/binflow/sign-in', '/binflow/driver'):
            self.assertEqual(self.client.get(path).location, '/binflow/driver/my-day')

    def test_office_pages_and_mutations_are_blocked_with_valid_csrf(self):
        self.driver()
        for path in ('/binflow/bins', '/binflow/bins/1', '/binflow/scan', '/binflow/labels',
                     '/binflow/export.csv', '/binflow/pickups', '/binflow/pickups/new',
                     '/binflow/warehouse', '/binflow/allocations', '/binflow/collection-calendar',
                     '/binflow/collection-requests', '/binflow/allocation-approvals',
                     '/binflow/settings/gps', '/binflow/gps/map', '/binflow/gps/status',
                     '/binflow/setup', '/binflow/settings/client-access', '/admin/dashboard',
                     '/admin/staff', '/admin/timesheets', '/admin/settings'):
            self.assertEqual(self.client.get(path).status_code, 403, path)
        for path in ('/binflow/bins', '/binflow/bins/1', '/binflow/pickups/new',
                     '/binflow/pickups/1/weigh/1', '/binflow/allocations', '/binflow/driver'):
            self.assertEqual(self.post(path, {}).status_code, 403, path)
        self.assertEqual(self.rows('SELECT COUNT(*) AS n FROM bf_bins')[0]['n'], 6)
        self.assertEqual(self.rows('SELECT COUNT(*) AS n FROM bf_jobs')[0]['n'], 0)

    def test_other_driver_dockets_loading_lists_and_live_calendar_are_private(self):
        for name in ('My truck', 'Other truck', 'Available truck'):
            self.post('/binflow/driver', {'name': name, 'state': 'QLD'})
        for number in (1, 2, 3):
            self.job(deliver=(number,), legacy=False)
            self.post(f'/binflow/pickups/{number}/truck', {'truck_id': number})
        with sqlite3.connect(self.path) as conn:
            for jid, uid in ((1, self.uid), (2, self.other)):
                conn.execute('UPDATE bf_jobs SET driver_id=?,status=\'complete\',collected_at=created_at,completed_at=created_at WHERE id=?', (uid, jid))
                conn.execute('INSERT INTO bf_runs(truck_id,run_date,driver_id) VALUES(?,\'2026-10-19\',?)', (jid, uid))
        self.driver()
        for path in ('/binflow/driver/my-day?summary=1&date=2026-10-19', '/binflow/driver/my-day/plan-state?date=2026-10-19'):
            page = self.client.get(path)
            content = page.json['html'] if page.is_json else page.text
            self.assertIn('My truck', content)
            self.assertIn('Available truck', content)
            self.assertNotIn('Other truck', content)
        pods = self.client.get('/binflow/pods?date=2026-10-19').text
        self.assertIn('BF-000001', pods)
        self.assertNotIn('BF-000002', pods)
        self.assertEqual(self.client.get('/binflow/pickups/1/pod.pdf').status_code, 200)
        docket = self.client.get('/binflow/pickups/1').text
        self.assertIn('Bins on this docket', docket)
        self.assertNotIn('Warehouse weigh-in', docket)
        self.assertNotIn('Cancel this scheduled job', docket)
        for path in ('/binflow/pickups/2', '/binflow/pickups/2/pod', '/binflow/pickups/2/pod.pdf',
                     '/binflow/pickups/2/evidence/photo', '/binflow/driver/2/load-list?date=2026-10-19',
                     '/binflow/driver/2/day?date=2026-10-19'):
            self.assertEqual(self.client.get(path).status_code, 403, path)
        for action in ('cancel', 'receive'):
            self.assertEqual(self.post('/binflow/pickups/1/' + action, {}).status_code, 403)

    def test_admin_and_non_transport_staff_keep_existing_navigation(self):
        admin = self.client.get('/admin/bin-management').text
        self.assertIn('National overview', admin)
        self.assertNotIn('>Driver day</a>', admin)
        self.assertIn('New allocation', admin)
        self.assertNotIn('aria-label="Driver menu"', admin)
        with sqlite3.connect(self.path) as conn:
            conn.execute("UPDATE users SET business_role='labour' WHERE id=?", (self.other,))
        self.post('/logout')
        page = self.login('employee', 'other')
        self.assertEqual(page.request.path, '/employee/dashboard')
        self.assertIn('>HOME</a>', page.text)
        self.assertNotIn('aria-label="Driver menu"', page.text)
        self.assertEqual(self.client.get('/binflow/driver/my-day').status_code, 403)


if __name__ == '__main__':
    unittest.main()
