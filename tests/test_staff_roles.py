import re
import sqlite3
import unittest

from werkzeug.security import generate_password_hash
import test_binflow as legacy
from app import create_app
from binflow_permissions import WORKING_STATES


class StaffRoleTests(unittest.TestCase):
    tearDown = legacy.BinFlowTests.tearDown
    post = legacy.BinFlowTests.post
    rows = legacy.BinFlowTests.rows
    login = legacy.BinFlowTests.login
    job = legacy.BinFlowTests.job

    def setUp(self):
        legacy.BinFlowTests.setUp(self)
        self.ids = {}
        hashed = generate_password_hash('test-admin-password')
        with sqlite3.connect(self.path) as conn:
            for name, role, state in (('qlddriver', 'driver', 'QLD'), ('nswcontractor', 'contractor', 'NSW'),
                                      ('manager', 'manager', 'VIC'), ('labour', 'labour', 'QLD'),
                                      ('unprofiled', '', '')):
                self.ids[name] = conn.execute('''INSERT INTO users(role,first_name,last_name,email,username,password_hash,business_role,working_state)
                    VALUES('employee',?,'Test',?,?,?,?,?)''',
                    (name, name+'@example.com', name, hashed, role, state)).lastrowid

    def profile(self, name='qlddriver', **changes):
        person=self.rows('SELECT * FROM users WHERE id=?',(self.ids[name],))[0]
        return dict(first_name=person['first_name'],last_name='Test',email=person['email'],username=person['username'],
                    mobile='0400000000',emergency_name='Emergency Test',emergency_email='emergency@example.com',
                    hourly_rate='30',status='active',work_week='mon_sun',
                    **({'business_role':person['business_role'],'working_state':person['working_state']} | changes))

    def sign_in(self, name):
        self.post('/logout')
        return self.login('employee',name)

    def prepare_runs(self):
        self.post('/binflow/driver',{'name':'QLD only truck','state':'QLD'})
        self.post('/binflow/driver',{'name':'NSW only truck','state':'NSW'})
        self.post('/binflow/bins',{'warehouse_id':2,'size_id':1,'quantity':2,'tare':'13'})
        self.job(deliver=(1,),legacy=False)
        self.job(deliver=(7,),site=5,warehouse=2,legacy=False)
        self.post('/binflow/pickups/1/truck',{'truck_id':1})
        self.post('/binflow/pickups/2/truck',{'truck_id':2})

    def test_admin_profile_form_saves_roles_states_and_employee_cannot_change_them(self):
        route=f'/admin/staff/{self.ids["qlddriver"]}/edit'
        page=self.client.get(route).text
        roles=re.search(r'<select name="business_role".*?</select>',page,re.S).group()
        states=re.search(r'<select name="working_state".*?</select>',page,re.S).group()
        for label in ('DRIVER','LABOUR','MANAGER','CONTRACTOR'): self.assertIn(label,roles)
        for state in WORKING_STATES: self.assertIn(f'value="{state}"',states)
        for state in ('NT','TAS'): self.assertNotIn(f'value="{state}"',states)
        result=self.post(route,self.profile(business_role='contractor',working_state='SA'))
        self.assertIn(b'Staff details saved',result.data)
        self.assertIn(b'CONTRACTOR',result.data)
        row=self.rows('SELECT * FROM users WHERE id=?',(self.ids['qlddriver'],))[0]
        self.assertEqual((row['role'],row['business_role'],row['working_state']),('employee','contractor','SA'))
        self.sign_in('qlddriver')
        result=self.post('/employee/details',self.profile(business_role='manager',working_state='WA'))
        self.assertEqual(result.status_code,200)
        row=self.rows('SELECT * FROM users WHERE id=?',(self.ids['qlddriver'],))[0]
        self.assertEqual((row['business_role'],row['working_state']),('contractor','SA'))
        self.assertIn(b'CONTRACTOR',result.data);self.assertIn(b'<dd>SA</dd>',result.data)
        self.assertEqual(self.post(route,self.profile()).status_code,403)

    def test_assignment_save_does_not_require_or_change_contact_pay_or_password(self):
        uid=self.ids['unprofiled'];route=f'/admin/staff/{uid}/edit'
        before=self.rows('SELECT * FROM users WHERE id=?',(uid,))[0]
        self.assertEqual(before['emergency_email'],'')
        result=self.post(route,dict(action='save_assignment',business_role='driver',working_state='QLD',
            hourly_rate='999',email='changed@example.com',password='ignored-password-value'))
        self.assertIn(b'Role and working state saved.',result.data)
        self.assertIn(b'value="QLD" selected',self.client.get(route).data)
        after=self.rows('SELECT * FROM users WHERE id=?',(uid,))[0]
        self.assertEqual((after['business_role'],after['working_state']),('driver','QLD'))
        self.assertEqual(after['auth_version'],before['auth_version']+1)
        for key in before:
            if key not in ('business_role','working_state','auth_version'):self.assertEqual(after[key],before[key],key)
        options=self.client.get('/binflow/schedule/options?state=QLD&date=2026-10-26').json
        self.assertIn(uid,[d['id'] for d in options['drivers']])
        self.assertEqual(self.client.post(route,data=dict(action='save_assignment',business_role='driver',working_state='NSW')).status_code,400)
        self.sign_in('unprofiled')
        self.assertEqual(self.post(route,dict(action='save_assignment',business_role='manager',working_state='NSW')).status_code,403)

    def test_assignment_validation_preserves_existing_staff_fields(self):
        uid=self.ids['qlddriver'];route=f'/admin/staff/{uid}/edit'
        before=self.rows('SELECT * FROM users WHERE id=?',(uid,))[0]
        result=self.post(route,dict(action='save_assignment',business_role='driver',working_state='BAD'))
        self.assertIn(b'Choose a business role and working state',result.data)
        self.assertIn(before['email'].encode(),result.data)
        self.assertEqual(before,self.rows('SELECT * FROM users WHERE id=?',(uid,))[0])

    def test_contact_form_error_keeps_saved_role_and_state_visible(self):
        route=f'/admin/staff/{self.ids["qlddriver"]}/edit'
        values=self.profile();values.pop('business_role');values.pop('working_state');values['email']='invalid'
        result=self.post(route,values)
        self.assertIn(b'Enter valid contact',result.data)
        self.assertIn(b'value="driver" selected',result.data)
        self.assertIn(b'value="QLD" selected',result.data)

    def test_initial_state_can_match_legacy_active_day_without_changing_clock_or_vehicle(self):
        uid=self.ids['qlddriver'];route=f'/admin/staff/{uid}/edit'
        with sqlite3.connect(self.path) as conn:
            conn.execute("UPDATE users SET working_state='' WHERE id=?",(uid,))
            truck=conn.execute("INSERT INTO bf_trucks(name,state,created_at) VALUES('Legacy truck','QLD','2026-10-19')").lastrowid
            conn.execute("""INSERT INTO bf_workdays(user_id,work_date,timezone,state,truck_id,started_at,start_received_at)
                VALUES(?,'2026-10-19','Australia/Brisbane','QLD',?,'2026-10-19T08:00:00+10:00','2026-10-19T08:00:00+10:00')""",(uid,truck))
        before=self.rows('SELECT * FROM bf_workdays');entries=self.rows('SELECT * FROM entries')
        rejected=self.post(route,dict(action='save_assignment',business_role='driver',working_state='NSW'))
        self.assertIn(b'active driver day',rejected.data)
        self.assertEqual(self.rows('SELECT working_state FROM users WHERE id=?',(uid,))[0]['working_state'],'')
        result=self.post(route,dict(action='save_assignment',business_role='driver',working_state='QLD'))
        self.assertIn(b'Role and working state saved.',result.data)
        self.assertEqual(before,self.rows('SELECT * FROM bf_workdays'));self.assertEqual(entries,self.rows('SELECT * FROM entries'))
        self.assertEqual(self.rows('SELECT working_state FROM users WHERE id=?',(uid,))[0]['working_state'],'QLD')
        self.assertEqual(self.rows('SELECT action FROM bf_workday_events')[-1]['action'],'Working state allocated')
        self.sign_in('qlddriver');self.assertEqual(self.client.get('/binflow/driver/my-day?summary=1').status_code,200)

    def test_initial_state_can_complete_unassigned_legacy_day_but_cannot_switch_roles(self):
        uid=self.ids['qlddriver'];route=f'/admin/staff/{uid}/edit'
        with sqlite3.connect(self.path) as conn:
            conn.execute("UPDATE users SET working_state='' WHERE id=?",(uid,))
            conn.execute("""INSERT INTO bf_workdays(user_id,work_date,timezone,started_at,start_received_at)
                VALUES(?,'2026-10-19','Australia/Brisbane','2026-10-19T08:00:00+10:00','2026-10-19T08:00:00+10:00')""",(uid,))
        result=self.post(route,dict(action='save_assignment',business_role='manager',working_state='QLD'))
        self.assertIn(b'active driver day',result.data)
        result=self.post(route,dict(action='save_assignment',business_role='driver',working_state='QLD'))
        self.assertIn(b'Role and working state saved.',result.data)
        day=self.rows('SELECT * FROM bf_workdays')[0]
        self.assertEqual(day['state'],'QLD');self.assertIsNone(day['finished_at']);self.assertIsNone(day['truck_id'])

    def test_driver_and_contractor_cannot_read_or_operate_other_state(self):
        self.prepare_runs()
        for name, own, other, other_job in (('qlddriver','QLD','NSW',2),('nswcontractor','NSW','QLD',1)):
            with self.subTest(role=name):
                self.sign_in(name)
                for route in ('/binflow/driver/my-day?summary=1&date=2026-10-19','/binflow/driver/my-day/plan-state?date=2026-10-19'):
                    response=self.client.get(route)
                    html=response.json['html'] if response.is_json else response.text
                    self.assertIn(own+' only truck',html)
                    self.assertNotIn(other+' only truck',html)
                pods=self.client.get('/binflow/pods?date=2026-10-19').text
                self.assertNotIn(f'BF-{other_job:06d}',pods)
                for path in (f'/binflow/driver/my-day?summary=1&state={other}', f'/binflow/driver/my-day/plan-state?state={other}',
                             f'/binflow/driver/my-day?state={other}', f'/binflow/driver/{other_job}',
                             f'/binflow/driver/{other_job}/day?date=2026-10-19',
                             f'/binflow/driver/{other_job}/day/state?date=2026-10-19',
                             f'/binflow/driver/{other_job}/load-list?date=2026-10-19',
                             f'/binflow/pickups/{other_job}',f'/binflow/pickups/{other_job}/pod',
                             f'/binflow/pickups/{other_job}/pod.pdf', f'/binflow/pickups/{other_job}/evidence/photo'):
                    self.assertEqual(self.client.get(path).status_code,403,path)
                token=self.rows('SELECT token FROM bf_bins WHERE id=?',(7 if other_job==2 else 1,))[0]['token']
                self.assertEqual(self.client.get('/binflow/qr/'+token+'/driver/load').status_code,403)
                self.post('/binflow/driver/start-day')
                day=self.rows('SELECT * FROM bf_workdays WHERE user_id=?',(self.ids[name],))[0]
                self.assertEqual(day['state'],own)
                page=self.client.get('/binflow/driver/my-day').text
                self.assertIn(own+' only truck',page);self.assertNotIn(other+' only truck',page)
                for path,values in (
                    (f'/binflow/driver/my-day/{day["id"]}/vehicle',{'state':other,'truck_id':other_job}),
                    (f'/binflow/driver/{other_job}/accept',{'date':'2026-10-19'}),
                    (f'/binflow/driver/{other_job}/prestart',{'date':'2026-10-19'}),
                    (f'/binflow/driver/{other_job}/day/depart',{'date':'2026-10-19'}),
                    (f'/binflow/driver/{other_job}/day/scan-bin',{'date':'2026-10-19','code':'BF000007'}),
                    (f'/binflow/driver/{other_job}/scan',{'job_id':other_job,'action':'load','code':'BF000007'}),
                    ('/binflow/qr/'+token+'/driver/load',{'job_id':other_job})):
                    self.assertEqual(self.post(path,values).status_code,403,path)
                self.assertIsNone(self.rows('SELECT truck_id FROM bf_workdays WHERE id=?',(day['id'],))[0]['truck_id'])
        self.assertFalse(self.rows('SELECT * FROM bf_events WHERE event=\'Empty bin scanned onto truck\''))

    def test_all_six_state_profiles_force_state_filter(self):
        self.sign_in('qlddriver')
        for state in WORKING_STATES:
            with sqlite3.connect(self.path) as conn:
                conn.execute('UPDATE users SET working_state=? WHERE id=?',(state,self.ids['qlddriver']))
            response=self.client.get('/binflow/driver/my-day?summary=1')
            self.assertEqual(response.status_code,200)
            picker=re.search(r'<select name="state".*?</select>',response.text,re.S).group()
            self.assertNotIn('All states',picker)
            for other in WORKING_STATES:
                if other==state: self.assertIn(other,picker)
                else:
                    self.assertNotIn(other,picker)
                    self.assertEqual(self.client.get('/binflow/driver/my-day/plan-state?state='+other).status_code,403)

    def test_labour_and_unprofiled_accounts_have_no_binflow_even_with_old_access_checkbox(self):
        with sqlite3.connect(self.path) as conn:
            for name in ('labour','unprofiled'):
                conn.execute('INSERT INTO bf_access VALUES(?)',(self.ids[name],))
        for name in ('labour','unprofiled'):
            page=self.sign_in(name)
            self.assertEqual(page.request.path,'/employee/dashboard')
            self.assertNotIn('>BINFLOW</a>',page.text)
            self.assertNotIn('aria-label="Driver menu"',page.text)
            for path in ('/admin/bin-management','/binflow/driver/my-day','/binflow/driver/my-day?summary=1','/binflow/pods','/binflow/gps/map'):
                self.assertEqual(self.client.get(path).status_code,403,path)
            for path in ('/employee/timesheet','/employee/prestart','/employee/details','/employee/documents','/employee/leave'):
                self.assertEqual(self.client.get(path).status_code,200,path)

    def test_manager_has_national_binflow_without_payroll_or_account_administration(self):
        self.prepare_runs()
        page=self.sign_in('manager')
        self.assertEqual(page.request.path,'/admin/bin-management')
        self.assertIn('National overview',page.text)
        calendar=self.client.get('/binflow/driver/my-day?summary=1&date=2026-10-19').text
        for value in ('QLD only truck','NSW only truck'): self.assertIn(value,calendar)
        for path in ('/binflow/bins','/binflow/pickups/1','/binflow/pickups/2','/binflow/warehouse',
                     '/binflow/collection-calendar','/binflow/allocations','/binflow/gps/map'):
            self.assertEqual(self.client.get(path).status_code,200,path)
        for path in ('/admin/staff','/admin/timesheets','/admin/settings','/binflow/settings/gps','/binflow/settings/client-access'):
            self.assertEqual(self.client.get(path).status_code,403,path)

    def test_office_cannot_assign_driver_to_wrong_state_and_profile_changes_revoke_session(self):
        self.prepare_runs()
        response=self.post('/binflow/driver/2/schedule',{'date':'2026-10-19','driver_id':self.ids['qlddriver'],'order_2':1})
        self.assertIn(b'allocated to this state',response.data)
        self.assertIsNone(self.rows('SELECT driver_id FROM bf_jobs WHERE id=2')[0]['driver_id'])
        page=self.client.get('/binflow/pickups/new?site=5&warehouse=2').text
        drivers=re.search(r'<select name="driver_id".*?</select>',page,re.S).group()
        self.assertNotIn(f'value="{self.ids["qlddriver"]}"',drivers)
        self.assertIn(f'value="{self.ids["nswcontractor"]}"',drivers)
        self.sign_in('qlddriver');old_client=self.client
        self.client=self.app.test_client();self.login()
        response=self.post(f'/admin/staff/{self.ids["qlddriver"]}/edit',self.profile(working_state='NSW'))
        self.assertIn(b'Staff details saved',response.data)
        self.assertEqual(old_client.get('/binflow/driver/my-day?summary=1').status_code,302)
        self.sign_in('qlddriver')
        page=self.client.get('/binflow/driver/my-day?summary=1&date=2026-10-19').text
        self.assertIn('NSW only truck',page);self.assertNotIn('QLD only truck',page)

    def test_invalid_profiles_and_active_day_changes_are_rejected(self):
        route=f'/admin/staff/{self.ids["qlddriver"]}/edit'
        for changes in ({'business_role':'admin'},{'working_state':'TAS'},{'working_state':''}):
            response=self.post(route,self.profile(**changes))
            self.assertIn(b'Choose a business role and working state',response.data)
        self.sign_in('qlddriver');self.post('/binflow/driver/start-day')
        self.post('/logout');self.login()
        response=self.post(route,self.profile(business_role='labour',working_state='NSW'))
        self.assertIn(b'active driver day',response.data)
        row=self.rows('SELECT business_role,working_state FROM users WHERE id=?',(self.ids['qlddriver'],))[0]
        self.assertEqual(row,{'business_role':'driver','working_state':'QLD'})

    def test_legacy_migration_preserves_records_without_guessing_states(self):
        stock=self.rows('SELECT * FROM bf_bins')
        with sqlite3.connect(self.path) as conn:
            conn.execute('INSERT INTO bf_access VALUES(?)',(self.ids['qlddriver'],))
            conn.execute('ALTER TABLE users DROP COLUMN business_role')
            conn.execute('ALTER TABLE users DROP COLUMN working_state')
        config=dict(self.app.config)
        create_app(config)
        row=self.rows('SELECT business_role,working_state FROM users WHERE id=?',(self.ids['qlddriver'],))[0]
        self.assertEqual(row,{'business_role':'driver','working_state':''})
        row=self.rows('SELECT business_role,working_state FROM users WHERE id=?',(self.ids['labour'],))[0]
        self.assertEqual(row,{'business_role':'','working_state':''})
        self.assertEqual(self.rows('SELECT * FROM bf_bins'),stock)
        create_app(config)
        self.assertEqual(self.rows('SELECT business_role,working_state FROM users WHERE id=?',(self.ids['qlddriver'],))[0],
                         {'business_role':'driver','working_state':''})


if __name__=='__main__': unittest.main()
