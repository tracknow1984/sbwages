import unittest
import test_app


class SheetActionTests(unittest.TestCase):
    setUp=test_app.AppTests.setUp
    tearDown=test_app.AppTests.tearDown
    query=test_app.AppTests.query
    post=test_app.AppTests.post
    login=test_app.AppTests.login
    add=test_app.AppTests.add
    last_week=test_app.AppTests.last_week
    day_form=test_app.AppTests.day_form
    prepare_payment_sheet=test_app.AppTests.prepare_payment_sheet

    def test_archive_restore_and_staff_permissions(self):
        person,_,sheet,end=self.prepare_payment_sheet()
        base=f'/admin/timesheets/{sheet["id"]}'
        self.assertEqual(self.client.get(base+'/archive').status_code,405)
        self.assertEqual(self.client.post(base+'/archive').status_code,400)
        self.post(base+'/archive')
        self.assertNotIn(b'Alex Example',self.client.get('/admin/timesheets').data)
        self.assertIn(b'Alex Example',self.client.get('/admin/timesheets?archived=1').data)
        self.assertEqual(self.client.get(base).status_code,200)
        self.assertEqual(len(self.query('SELECT * FROM entries')),1)
        self.post('/logout');self.login('alex','test-staff-password')
        self.assertIn(b'View week',self.client.get('/employee/history').data)
        self.assertNotIn(base.encode()+b'/delete',self.client.get('/employee/history').data)
        self.assertEqual(self.post(base+'/archive').status_code,403)
        self.assertEqual(self.client.get(base+'/delete').status_code,403)
        self.assertEqual(self.post(base+'/delete',{'confirm_delete':'yes'}).status_code,403)
        self.post('/logout');self.login('admin','test-admin-password','admin')
        self.post(base+'/archive',{'action':'restore'})
        self.assertIn(b'Alex Example',self.client.get('/admin/timesheets').data)

    def test_delete_payment_confirmation_and_scoped_removal(self):
        person,_,sheet,end=self.prepare_payment_sheet()
        self.add('jordan')
        base=f'/admin/timesheets/{sheet["id"]}'
        self.assertEqual(self.client.get(base+'/delete').status_code,200)
        self.assertEqual(self.post(base+'/delete').status_code,400)
        self.post(base+'/payment',{'cash_amount':'100','transfer_amount':'184','expected_total':'28400'})
        self.assertIn(b'Payment details changed',self.post(base+'/delete',{'confirm_delete':'yes','payment_id':''}).data)
        self.assertEqual(len(self.query('SELECT * FROM sheets')),1)
        payment=self.query('SELECT * FROM payments')[0]
        self.assertIn(b'staff pay slip',self.client.get(base+'/delete').data)
        self.post(base+'/delete',{'confirm_delete':'yes','payment_id':str(payment['id'])})
        self.assertEqual(len(self.query('SELECT * FROM sheets')),0)
        self.assertEqual(len(self.query('SELECT * FROM entries')),0)
        self.assertEqual(len(self.query('SELECT * FROM payments')),0)
        self.assertEqual(len(self.query("SELECT * FROM users WHERE role='employee'")),2)
        self.assertEqual(self.client.get(base).status_code,404)
        self.assertEqual(self.post(base+'/delete',{'confirm_delete':'yes'}).status_code,404)
