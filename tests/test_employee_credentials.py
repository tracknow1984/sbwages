import sqlite3
import unittest

from app import create_app
import test_app


class EmployeeCredentialsTests(unittest.TestCase):
    setUp=test_app.AppTests.setUp
    tearDown=test_app.AppTests.tearDown
    query=test_app.AppTests.query
    post=test_app.AppTests.post
    login=test_app.AppTests.login
    add=test_app.AppTests.add

    def test_public_scan_lifecycle_private_sources_and_admin_approval(self):
        alex,values=self.add()
        other,_=self.add('jordan')
        uid=alex['id']
        manage=f'/admin/staff/{uid}/credentials'
        self.post(f'/admin/staff/{uid}/edit',dict(values,licence_number='DL123456789',licence_state='QLD',
                licence_expiry='2099-12-20',licence_class='HR'))
        public=self.app.test_client()
        tokens=self.query('SELECT token FROM employee_badges ORDER BY user_id')
        self.assertNotEqual(tokens[0][0],tokens[1][0])
        url='/verify/employee/'+tokens[0][0]
        self.assertEqual(public.get(f'/staff/{uid}/qr.png').status_code,404)
        def save(kind,name,status,expires=''):
            self.assertEqual(self.post(manage,dict(kind=kind,name=name,status=status,expires=expires)).status_code,200)
        save('security','National police check','approved','2099-03-03')
        save('security','Airside','approved','2000-03-03')
        save('site','North Maclean','approved')
        save('site','Withdrawn depot','withdrawn')
        save('site','Expired depot','approved','2000-03-03')
        page=public.get(url)
        self.assertEqual(page.status_code,200)
        for value in ('HR','QLD','6789','National police check','Expired','North Maclean'):
            self.assertIn(value,page.text)
        for value in ('DL123456789','alex@example.com',values['mobile'],'Withdrawn depot','Expired depot','hourly_rate'):
            self.assertNotIn(value,page.text)
        self.assertIn('noindex',page.headers['X-Robots-Tag'])
        self.assertTrue(page.cache_control.no_store)
        self.assertEqual(self.client.get(f'/staff/{uid}/qr.png').mimetype,'image/png')
        self.post('/logout')
        self.login('alex','test-staff-password')
        self.assertIn('Download QR',self.client.get('/employee/qr-card').text)
        self.assertEqual(self.client.get(f"/staff/{other['id']}/qr.png").status_code,404)
        self.assertEqual(self.post(manage,dict(kind='site',name='Self-approved',status='approved')).status_code,403)
        self.post('/logout')
        self.login('admin','test-admin-password','admin')
        self.post(manage,dict(action='rotate',token=tokens[0][0]))
        self.assertEqual(public.get(url).status_code,404)
        current='/verify/employee/'+self.query('SELECT token FROM employee_badges WHERE user_id=?',(uid,))[0][0]
        self.assertEqual(public.get(current).status_code,200)
        self.post(f'/admin/staff/{uid}/edit',dict(values,status='terminated'))
        self.assertEqual(public.get(current).status_code,404)

    def test_existing_employee_backfill_preserves_prior_credentials(self):
        alex,_=self.add()
        with sqlite3.connect(self.path) as conn:
            conn.execute('DROP TRIGGER employee_badge_on_create')
            conn.execute('DROP TRIGGER employee_badge_on_termination')
            conn.execute('DROP TABLE employee_badges')
        create_app(dict(self.app.config))
        card=self.query('SELECT token FROM employee_badges WHERE user_id=?',(alex['id'],))[0][0]
        create_app(dict(self.app.config))
        self.assertEqual(card,self.query('SELECT token FROM employee_badges WHERE user_id=?',(alex['id'],))[0][0])
