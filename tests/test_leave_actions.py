import unittest
from datetime import datetime,timedelta
from zoneinfo import ZoneInfo
import test_app

class LeaveActionTests(unittest.TestCase):
    setUp=test_app.AppTests.setUp
    tearDown=test_app.AppTests.tearDown
    query=test_app.AppTests.query
    post=test_app.AppTests.post
    login=test_app.AppTests.login
    add=test_app.AppTests.add

    def test_archive_restore_delete_and_permissions(self):
        self.add()
        self.post('/logout');self.login('alex','test-staff-password')
        day=(datetime.now(ZoneInfo('Australia/Brisbane'))+timedelta(days=7)).date().isoformat()
        self.post('/employee/leave',{'start_date':day,'end_date':day,'notes':'Holiday'})
        item=self.query('SELECT * FROM leave_requests')[0]
        base=f'/admin/leave/{item["id"]}'
        self.assertEqual(self.post(base+'/archive').status_code,403)
        self.assertEqual(self.client.get(base+'/delete').status_code,403)
        self.assertEqual(self.post(base+'/delete',{'confirm_delete':'yes','expected_status':'pending'}).status_code,403)
        self.post('/logout');self.login('admin','test-admin-password','admin')
        self.assertEqual(self.client.post(base+'/archive').status_code,400)
        self.post(base+'/archive')
        self.assertNotIn(b'Holiday',self.client.get('/admin/leave').data)
        self.assertNotIn(b'Holiday',self.client.get('/admin/dashboard').data)
        self.assertIn(b'Holiday',self.client.get('/admin/leave?archived=1').data)
        self.post('/logout');self.login('alex','test-staff-password')
        self.assertIn(b'Holiday',self.client.get('/employee/leave').data)
        self.post('/logout');self.login('admin','test-admin-password','admin')
        self.post(base+'/archive',{'action':'restore'})
        self.assertIn(b'Holiday',self.client.get('/admin/leave').data)
        self.assertEqual(self.client.get(base+'/delete').status_code,200)
        self.assertEqual(self.post(base+'/delete').status_code,400)
        self.post(base+'/decision',{'decision':'approved'})
        self.assertIn(b'decision changed',self.post(base+'/delete',{'confirm_delete':'yes','expected_status':'pending'}).data)
        self.assertEqual(len(self.query('SELECT * FROM leave_requests')),1)
        self.post(base+'/delete',{'confirm_delete':'yes','expected_status':'approved'})
        self.assertEqual(len(self.query('SELECT * FROM leave_requests')),0)
        self.assertEqual(len(self.query("SELECT * FROM users WHERE role='employee'")),1)
        self.assertEqual(self.client.get(base+'/delete').status_code,404)
