import unittest
import test_app
from app import create_app

class NoticeActionTests(unittest.TestCase):
    setUp = test_app.AppTests.setUp
    tearDown = test_app.AppTests.tearDown
    query = test_app.AppTests.query
    post = test_app.AppTests.post
    login = test_app.AppTests.login
    add = test_app.AppTests.add

    def test_archive_restore_delete_and_permissions(self):
        person, _ = self.add()
        self.post('/admin/hit-me', dict(user_id=person['id'], title='Old yard job', body='Collect pallets'))
        notice = self.query('SELECT * FROM staff_notices')[0]
        base = f"/admin/hit-me/{notice['id']}"
        self.assertEqual(self.client.post(base+'/archive').status_code, 400)
        self.post(base+'/archive')
        self.assertNotIn(b'Old yard job', self.client.get('/admin/hit-me').data)
        self.assertIn(b'Old yard job', self.client.get('/admin/hit-me?archived=1').data)
        self.post('/logout')
        self.login('alex', 'test-staff-password')
        for path in ('/employee/dashboard', '/employee/hit-me'):
            self.assertNotIn(b'Old yard job', self.client.get(path).data)
        self.assertEqual(self.client.get('/employee/hit-me/status').json['unread'], 0)
        self.assertEqual(self.post(base+'/archive').status_code, 403)
        self.assertEqual(self.client.get(base+'/delete').status_code, 403)
        self.assertEqual(self.post(base+'/delete', {'confirm_delete':'yes'}).status_code, 403)
        self.assertEqual(self.post(f"/employee/hit-me/{notice['id']}/read").status_code, 404)
        self.post('/logout')
        self.login('admin', 'test-admin-password', 'admin')
        self.post(base+'/archive', {'action':'restore'})
        self.assertIn(b'Old yard job', self.client.get('/admin/hit-me').data)
        self.post('/logout')
        self.login('alex', 'test-staff-password')
        self.assertIn(b'Old yard job', self.client.get('/employee/dashboard').data)
        self.assertEqual(self.client.get('/employee/hit-me/status').json['unread'], 1)
        self.post('/logout')
        self.login('admin', 'test-admin-password', 'admin')
        self.assertEqual(self.client.get(base+'/delete').status_code, 200)
        self.assertEqual(self.post(base+'/delete').status_code, 400)
        self.assertEqual(len(self.query('SELECT * FROM staff_notices')), 1)
        self.post(base+'/delete', {'confirm_delete':'yes'})
        self.assertEqual(len(self.query('SELECT * FROM staff_notices')), 0)
        self.assertEqual(self.client.get(base+'/delete').status_code, 404)
        self.assertEqual(self.post(base+'/archive').status_code, 404)

    def test_existing_database_migration(self):
        person, _ = self.add()
        self.post('/admin/hit-me', dict(user_id=person['id'], title='Keep history', body='Existing notice'))
        import sqlite3
        with sqlite3.connect(self.path) as conn:
            conn.execute('ALTER TABLE staff_notices DROP COLUMN archived_at')
        create_app(dict(self.app.config))
        self.assertIsNone(self.query('SELECT archived_at FROM staff_notices')[0]['archived_at'])
        self.assertEqual(self.query('SELECT title FROM staff_notices')[0]['title'], 'Keep history')
