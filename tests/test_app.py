import io
import re
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta
from unittest.mock import patch
from zoneinfo import ZoneInfo

from app import create_app


class AppTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = self.temp.name + '/test.db'
        self.app = create_app({'TESTING': True, 'DATABASE': self.path, 'SECRET_KEY': 'test-only-secret-key-' * 3,
                               'SESSION_COOKIE_SECURE': False, 'ADMIN_EMAIL': 'admin@example.com',
                               'ADMIN_PASSWORD': 'test-admin-password', 'APP_URL': 'https://example.com',
                               'SMTP_HOST': 'smtp.example.com', 'SMTP_FROM': 'staff@example.com'})
        self.client = self.app.test_client()
        self.login('admin', 'test-admin-password', 'admin')

    def tearDown(self):
        self.temp.cleanup()

    def query(self, sql, args=()):
        with sqlite3.connect(self.path) as conn:
            conn.row_factory = sqlite3.Row
            return conn.execute(sql, args).fetchall()

    def post(self, path, data=None):
        with self.client.session_transaction() as sess:
            sess.setdefault('csrf', 'test-csrf-token')
            token = sess['csrf']
        return self.client.post(path, data={'csrf': token, **(data or {})}, follow_redirects=True)

    def login(self, username, password, audience='employee'):
        self.client.get('/' + audience + '/login')
        response = self.post('/' + audience + '/login', {'username': username, 'password': password})
        self.assertEqual(response.status_code, 200)
        return response

    def add(self, username='alex', ending='6', password='test-staff-password'):
        values = dict(first_name='Alex', last_name='Example', email=username+'@example.com', mobile='0400000000',
                      emergency_name='Casey Example', emergency_email='casey@example.com',
                      hourly_rate='35.50', username=username, password=password, status='active', week_ending=ending)
        response = self.post('/admin/staff/new', values)
        self.assertIn(b'Staff details saved.', response.data)
        return self.query('SELECT * FROM users WHERE username=?', (username,))[0], values

    def last_week(self, ending=6):
        today = datetime.now(ZoneInfo('Australia/Brisbane')).date()
        return today - timedelta(days=((today.weekday() - ending) % 7) + 7)

    def day_form(self, end, start='08:00', finish='16:00', action='commit_day', day=None):
        return dict(week=end.isoformat(), work_date=(day or end).isoformat(), action=action,
                    start_time=start, finish_time=finish, activity='Yard maintenance')

    def week_form(self, end, hours='8', action='save'):
        minutes = round(float(hours) * 60)
        finish = f'{minutes // 60:02d}:{minutes % 60:02d}'
        for i in range(7 if action == 'submit' else 6):
            self.post('/employee/timesheet', self.day_form(end, '00:00', finish,
                      'commit_day' if action == 'submit' else 'save_day', end - timedelta(days=i)))
        if action == 'submit':
            return {'week': end.isoformat(), 'action': 'submit'}
        return self.day_form(end, '00:00', finish, 'save_day', end - timedelta(days=6))

    def test_hit_me_delivery_read_status_and_permissions(self):
        person, _ = self.add()
        self.add('jordan')
        message={'user_id':str(person['id']), 'title':'Pickup at North Maclean', 'body':'Collect 4 pallets.\nContact the yard manager. <script>bad()</script>', 'priority':'urgent'}
        self.assertIn(b'Hit Me notice sent', self.post('/admin/hit-me',message).data)
        notice=self.query('SELECT * FROM staff_notices')[0]
        self.assertIsNone(notice['read_at'])
        self.post('/logout')
        page=self.login('alex','test-staff-password')
        self.assertEqual(page.request.path,'/employee/dashboard')
        self.assertIn(b'Pickup at North Maclean',page.data)
        self.assertIn(b'&lt;script&gt;',page.data)
        self.assertNotIn(b'<script>bad()',page.data)
        self.assertIsNone(self.query('SELECT read_at FROM staff_notices')[0][0])
        self.assertEqual(self.client.get('/employee/hit-me/status').json['unread'],1)
        self.assertEqual(self.post('/admin/hit-me',message).status_code,403)
        route=f'/employee/hit-me/{notice["id"]}/read'
        self.assertEqual(self.client.post(route).status_code,400)
        self.post('/logout')
        self.login('jordan','test-staff-password')
        self.assertNotIn(b'Pickup at North Maclean',self.client.get('/employee/dashboard').data)
        self.assertEqual(self.client.get('/employee/hit-me/status').json['total'],0)
        self.assertEqual(self.post(route).status_code,404)
        self.post('/logout')
        self.login('alex','test-staff-password')
        self.assertIn(b'Notice marked as read',self.post(route,{'return_to':'dashboard'}).data)
        self.assertEqual(self.client.get('/employee/hit-me/status').json['unread'],0)
        read_at=self.query('SELECT read_at FROM staff_notices')[0][0]
        self.post(route)
        self.assertEqual(self.query('SELECT read_at FROM staff_notices')[0][0],read_at)
        self.post('/logout')
        self.login('admin','test-admin-password','admin')
        self.assertIn(b'Marked as read',self.client.get('/admin/hit-me').data)
        self.assertEqual(self.client.get('/employee/dashboard').status_code,403)

    def test_hit_me_validation_and_inactive_recipient(self):
        person, values=self.add()
        message={'user_id':str(person['id']), 'body':'Job details'}
        for invalid in ({'user_id':'bad'}, {'user_id':'99999'}, {'body':''}, {'body':'x'*10001}, {'priority':'invalid'}):
            self.post('/admin/hit-me',message | invalid)
        self.assertEqual(len(self.query('SELECT * FROM staff_notices')),0)
        self.post(f'/admin/staff/{person["id"]}/edit',values | {'status':'terminated','password':''})
        self.post('/admin/hit-me',message)
        self.assertEqual(len(self.query('SELECT * FROM staff_notices')),0)

    def test_dashboard_timesheet_and_expiry_reminders(self):
        person, _ = self.add()
        self.post('/logout')
        self.login('alex','test-staff-password')
        page=self.client.get('/employee/dashboard')
        self.assertIn(b'Remember your hours',page.data)
        self.assertIn(b'Add your expiry date',page.data)
        today=datetime.now(ZoneInfo('Australia/Brisbane')).date()
        for days,expected in [(-1,b'Licence expired'),(0,b'Licence expires today'),(1,b'Expires in 1 day'),(30,b'Expires in 30 days'),(31,b'Licence up to date')]:
            with sqlite3.connect(self.path) as db:
                db.execute('UPDATE users SET licence_expiry=? WHERE id=?',((today+timedelta(days=days)).isoformat(),person['id']))
            self.assertIn(expected,self.client.get('/employee/dashboard').data)
        end=today+timedelta(days=6-today.weekday())
        values=self.day_form(end,day=today,action='save_day')
        self.post('/employee/timesheet',values)
        self.assertIn(b'Finish your daily entry',self.client.get('/employee/dashboard').data)
        self.post('/employee/timesheet',values | {'action':'commit_day'})
        self.assertIn(b'Today is locked in',self.client.get('/employee/dashboard').data)
        self.post('/employee/timesheet',{'week':end.isoformat(),'action':'submit'})
        self.assertIn(b'Today is locked in',self.client.get('/employee/dashboard').data)

    def test_dashboard_submitted_week_without_today_entry(self):
        person, _ = self.add()
        today=datetime.now(ZoneInfo('Australia/Brisbane')).date()
        end=today+timedelta(days=6-today.weekday())
        with sqlite3.connect(self.path) as db:
            db.execute("INSERT INTO sheets(user_id,week_end,status,rate_cents,total_units,total_cents,submitted_at) VALUES(?,?,'submitted',3550,800,28400,?)",(person['id'],end.isoformat(),today.isoformat()))
        self.post('/logout')
        self.login('alex','test-staff-password')
        self.assertIn(b'This week is submitted',self.client.get('/employee/dashboard').data)

    def test_staff_form_and_employee_permissions(self):
        person, values = self.add()
        self.assertNotIn('test-staff-password', person['password_hash'])
        self.assertIn(b'Alex', self.client.get('/admin/staff').data)
        self.assertEqual(self.client.get(f'/admin/staff/{person["id"]}/edit').status_code, 200)
        self.post('/logout')
        self.login('alex', 'test-staff-password')
        self.assertEqual(self.client.get('/admin/staff').status_code, 403)
        response = self.post('/employee/details', values | {'hourly_rate': '999', 'status': 'terminated', 'first_name': 'Updated'})
        self.assertIn(b'Your details have been updated.', response.data)
        updated = self.query('SELECT * FROM users WHERE id=?', (person['id'],))[0]
        self.assertEqual(updated['rate_cents'], 3550)
        self.assertEqual(updated['status'], 'active')
        self.assertEqual(updated['first_name'], 'Updated')

    def test_licence_details_employee_admin_and_validation(self):
        person, values = self.add()
        licence = dict(licence_number='00123456', licence_state='QLD', licence_expiry='2028-05-15')
        self.post('/logout')
        self.login('alex', 'test-staff-password')
        self.assertIn(b'Licence details', self.client.get('/employee/details').data)
        self.assertIn(b'Your details have been updated.', self.post('/employee/details', values | licence).data)
        row = self.query('SELECT * FROM users WHERE id=?', (person['id'],))[0]
        for key, value in licence.items():
            self.assertEqual(row[key], value)
        for invalid in ({'licence_state':'INVALID'}, {'licence_expiry':'2028-02-30'}, {'licence_number':'x'*81}):
            self.assertNotIn(b'Your details have been updated.', self.post('/employee/details', values | licence | invalid).data)
        self.assertEqual(self.query('SELECT licence_expiry FROM users WHERE id=?', (person['id'],))[0][0], '2028-05-15')
        self.post('/logout')
        self.login('admin', 'test-admin-password', 'admin')
        route = f'/admin/staff/{person["id"]}/edit'
        self.assertIn(b'00123456', self.client.get(route).data)
        self.assertIn(b'Staff details saved.', self.post(route, values | licence | {'licence_state':'NSW', 'password':''}).data)
        self.assertEqual(self.query('SELECT licence_state FROM users WHERE id=?', (person['id'],))[0][0], 'NSW')
        self.post(route, values | {key:'' for key in licence} | {'password':''})
        self.assertEqual(self.query('SELECT licence_number FROM users WHERE id=?', (person['id'],))[0][0], '')

    def test_licence_migration_preserves_staff(self):
        person, _ = self.add()
        with sqlite3.connect(self.path) as db:
            for col in ('licence_number', 'licence_state', 'licence_expiry'):
                db.execute(f'ALTER TABLE users DROP COLUMN {col}')
        create_app(self.app.config)
        create_app(self.app.config)
        row = self.query('SELECT * FROM users WHERE id=?', (person['id'],))[0]
        self.assertEqual(row['password_hash'], person['password_hash'])
        self.assertEqual(row['first_name'], person['first_name'])
        self.assertEqual(row['licence_number'], '')
        self.assertEqual(row['licence_state'], '')
        self.assertEqual(row['licence_expiry'], '')

    def upload_document(self, filename='licence.pdf', content=b'%PDF-1.4\n%%EOF', title='Driver licence'):
        return self.post('/employee/documents', {'title':title, 'document':(io.BytesIO(content), filename)})

    def test_document_upload_dashboard_archive_and_isolation(self):
        person, _ = self.add()
        self.add('jordan')
        self.post('/logout')
        self.login('alex', 'test-staff-password')
        self.assertIn(b'Document uploaded', self.upload_document().data)
        doc = self.query('SELECT * FROM documents')[0]
        self.assertEqual(doc['data'], b'%PDF-1.4\n%%EOF')
        self.assertEqual(self.client.get(f'/documents/{doc["id"]}/view').status_code, 200)
        file = self.client.get(f'/documents/{doc["id"]}/file')
        self.assertEqual(file.mimetype, 'application/pdf')
        self.assertEqual(file.data, doc['data'])
        self.assertIn('sandbox', file.headers['Content-Security-Policy'])
        self.assertEqual(file.headers['Cache-Control'], 'no-store')
        self.assertEqual(self.post(f'/admin/documents/{doc["id"]}/archive').status_code, 403)
        self.assertEqual(self.client.get('/admin/dashboard').status_code, 403)
        self.post('/logout')
        self.login('jordan', 'test-staff-password')
        for action in ('view', 'file'):
            self.assertEqual(self.client.get(f'/documents/{doc["id"]}/{action}').status_code, 404)
        self.assertNotIn(b'<strong>Driver licence</strong>', self.client.get('/employee/documents').data)
        self.post('/logout')
        self.assertEqual(self.client.get(f'/documents/{doc["id"]}/file').status_code, 403)
        self.login('admin', 'test-admin-password', 'admin')
        self.assertIn(b'Driver licence', self.client.get('/admin/dashboard').data)
        self.client.get(f'/documents/{doc["id"]}/view')
        self.assertTrue(self.query('SELECT viewed_at FROM documents')[0][0])
        self.assertEqual(self.client.post(f'/admin/documents/{doc["id"]}/archive').status_code,400)
        self.assertIn(b'Document archived', self.post(f'/admin/documents/{doc["id"]}/archive').data)
        self.assertNotIn(b'Driver licence', self.client.get('/admin/dashboard').data)
        self.assertIn(b'Driver licence', self.client.get('/admin/documents?archived=1').data)
        self.assertEqual(self.client.get(f'/documents/{doc["id"]}/file').data, doc['data'])

    def test_document_validation_and_size_limits(self):
        self.add()
        self.post('/logout')
        self.login('alex', 'test-staff-password')
        for name, data in [('bad.html', b'<script>alert(1)</script>'), ('fake.pdf', b'not a PDF'), ('empty.pdf', b'')]:
            self.upload_document(name, data)
        self.assertEqual(len(self.query('SELECT * FROM documents')), 0)
        self.upload_document(content=b'%PDF-' + b'x' * (5 * 1024 * 1024))
        self.assertEqual(len(self.query('SELECT * FROM documents')), 0)
        result = self.upload_document(content=b'%PDF-' + b'x' * (6 * 1024 * 1024))
        self.assertEqual(result.status_code, 413)
        self.assertIn(b'Document uploaded', self.upload_document('../../licence.pdf').data)
        self.assertEqual(self.query('SELECT filename FROM documents')[0][0], 'licence.pdf')

    def test_document_email_attachment_and_failure(self):
        self.add()
        self.post('/logout')
        self.login('alex', 'test-staff-password')
        self.upload_document()
        doc = self.query('SELECT * FROM documents')[0]
        route = f'/admin/documents/{doc["id"]}/email'
        self.assertEqual(self.post(route, {'recipient':'recipient@example.com'}).status_code,403)
        self.post('/logout')
        self.login('admin', 'test-admin-password', 'admin')
        self.assertEqual(self.client.get(route).status_code,200)
        with patch('app.smtplib.SMTP') as smtp:
            result = self.post(route, {'recipient':'recipient@example.com', 'message':'For review'})
            self.assertIn(b'Document emailed', result.data)
            message = smtp.return_value.__enter__.return_value.send_message.call_args.args[0]
            self.assertEqual(message['To'], 'recipient@example.com')
            attachment = next(message.iter_attachments())
            self.assertEqual(attachment.get_payload(decode=True), doc['data'])
            self.assertEqual(attachment.get_filename(), 'licence.pdf')
        self.assertEqual(len(self.query('SELECT * FROM document_emails')),1)
        with patch('app.smtplib.SMTP', side_effect=OSError('offline')):
            self.assertIn(b'could not be emailed', self.post(route, {'recipient':'recipient@example.com'}).data)
        self.assertEqual(len(self.query('SELECT * FROM document_emails')),1)
        with patch('app.smtplib.SMTP') as smtp:
            self.post(route, {'recipient':'one@example.com,two@example.com'})
            smtp.assert_not_called()
        self.app.config['SMTP_HOST']=''
        self.assertIn(b'Email is not connected', self.post(route, {'recipient':'recipient@example.com'}).data)

    def test_annual_leave_submission_decisions_and_isolation(self):
        self.add()
        self.add('jordan')
        self.post('/logout')
        self.login('alex', 'test-staff-password')
        start = datetime.now(ZoneInfo('Australia/Brisbane')).date()+timedelta(days=10)
        values = {'start_date':start.isoformat(), 'end_date':(start+timedelta(days=2)).isoformat(), 'notes':'Family holiday'}
        self.assertIn(b'sent to admin', self.post('/employee/leave',values).data)
        row=self.query('SELECT * FROM leave_requests')[0]
        self.assertEqual(row['status'],'pending')
        self.assertIn(b'overlap',self.post('/employee/leave',values).data)
        route=f'/admin/leave/{row["id"]}/decision'
        self.assertEqual(self.post(route,{'decision':'approved'}).status_code,403)
        self.post('/logout')
        self.login('jordan','test-staff-password')
        self.assertNotIn(b'Family holiday',self.client.get('/employee/leave').data)
        self.post('/logout')
        self.login('admin','test-admin-password','admin')
        self.assertIn(b'Family holiday',self.client.get('/admin/dashboard').data)
        self.assertEqual(self.client.post(route,data={'decision':'approved'}).status_code,400)
        self.assertIn(b'Annual leave approved.',self.post(route,{'decision':'approved','admin_notes':'Enjoy your break'}).data)
        self.assertIn(b'already been reviewed',self.post(route,{'decision':'rejected'}).data)
        updated=self.query('SELECT * FROM leave_requests')[0]
        self.assertEqual(updated['status'],'approved')
        self.assertTrue(updated['decided_at'])
        self.assertTrue(updated['decided_by'])
        self.assertNotIn(b'Family holiday',self.client.get('/admin/dashboard').data)
        self.post('/logout')
        self.login('alex','test-staff-password')
        page=self.client.get('/employee/leave')
        self.assertIn(b'Approved',page.data)
        self.assertIn(b'Enjoy your break',page.data)
        self.assertIn(b'overlap',self.post('/employee/leave',values).data)
        values['start_date']=(start+timedelta(days=5)).isoformat()
        values['end_date']=values['start_date']
        self.post('/employee/leave',values)
        self.post('/logout')
        self.login('admin','test-admin-password','admin')
        self.post('/admin/leave/2/decision',{'decision':'rejected','admin_notes':'Please choose another day'})
        self.post('/logout')
        self.login('alex','test-staff-password')
        self.assertIn(b'Disapproved',self.client.get('/employee/leave').data)
        self.assertIn(b'sent to admin',self.post('/employee/leave',values).data)

    def test_leave_date_validation(self):
        self.add()
        self.post('/logout')
        self.login('alex','test-staff-password')
        for start,end in [('bad','bad'),('2027-02-30','2027-03-01'),('2028-06-04','2028-06-03'),('2020-01-01','2020-01-02')]:
            self.post('/employee/leave',{'start_date':start,'end_date':end})
        self.assertEqual(len(self.query('SELECT * FROM leave_requests')),0)

    def test_sunday_submission_snapshot_and_lock(self):
        person, values = self.add()
        self.post('/logout')
        self.login('alex', 'test-staff-password')
        end = self.last_week()
        response = self.post('/employee/timesheet', self.week_form(end, '7.5', 'submit'))
        self.assertIn(b'Timesheet submitted to admin.', response.data)
        sheet = self.query('SELECT * FROM sheets')[0]
        self.assertEqual(sheet['total_units'], 5250)
        self.assertEqual(sheet['total_cents'], 186375)
        self.assertEqual(len(self.query('SELECT * FROM entries')), 7)
        self.assertIn(b'locked', self.post('/employee/timesheet', self.week_form(end, '1')).data)
        self.post('/logout')
        self.login('admin', 'test-admin-password', 'admin')
        self.post(f'/admin/staff/{person["id"]}/edit', values | {'hourly_rate': '50', 'password': ''})
        response = self.client.get(f'/admin/timesheets/{sheet["id"]}')
        self.assertIn(b'$1,863.75', response.data)
        self.assertIn(b'$35.50', response.data)
        self.assertIn(b'Alex', self.client.get('/admin/timesheets').data)

    def test_sunday_seven_days_and_rounding(self):
        self.add(ending='6')
        self.post('/logout')
        self.login('alex', 'test-staff-password')
        end = self.last_week(6)
        response = self.post('/employee/timesheet', self.week_form(end, str(1/60), 'submit'))
        self.assertIn(b'Timesheet submitted to admin.', response.data)
        sheet = self.query('SELECT * FROM sheets')[0]
        self.assertEqual(sheet['total_cents'], 497)
        rows = self.query('SELECT work_date FROM entries ORDER BY work_date')
        self.assertEqual(datetime.fromisoformat(rows[0]['work_date']).weekday(), 0)
        self.assertEqual(datetime.fromisoformat(rows[-1]['work_date']).weekday(), 6)

    def test_validation_csrf_and_tenant_isolation(self):
        self.add()
        self.add('jordan')
        self.post('/logout')
        self.login('alex', 'test-staff-password')
        end = self.last_week()
        for start, finish in [('25:00','26:00'), ('08:00','07:00'), ('08:00','08:00'), ('','17:00'), ('08:00','NaN')]:
            response = self.post('/employee/timesheet', self.day_form(end, start, finish))
            self.assertNotIn(b'Day committed and locked.', response.data)
            self.assertEqual(len(self.query('SELECT * FROM entries')), 0)
        values = self.day_form(end)
        values['activity'] = ''
        self.assertIn(b'Add activity notes', self.post('/employee/timesheet', values).data)
        self.assertEqual(self.client.post('/employee/details', data={}).status_code, 400)
        self.post('/employee/timesheet', self.week_form(end, '8', 'submit'))
        self.post('/logout')
        self.login('jordan', 'test-staff-password')
        self.assertIn(b'No timesheets yet', self.client.get('/employee/history').data)
        self.assertEqual(self.client.get('/admin/timesheets/1').status_code, 403)
        self.assertNotIn(b'Yard maintenance', self.client.get('/employee/timesheet?week=' + end.isoformat()).data)

    def test_termination_revokes_live_session(self):
        person, values = self.add()
        employee_client = self.app.test_client()
        admin_client = self.client
        self.client = employee_client
        self.login('alex', 'test-staff-password')
        self.client = admin_client
        self.post(f'/admin/staff/{person["id"]}/edit', values | {'status': 'terminated', 'password': ''})
        response = employee_client.get('/employee/details')
        self.assertEqual(response.status_code, 302)
        self.assertIn('/employee/login', response.location)

    def test_email_invite_single_use(self):
        self.add(password='')
        person = self.query("SELECT * FROM users WHERE username='alex'")[0]
        with patch('app.smtplib.SMTP') as smtp:
            result = self.post(f'/admin/staff/{person["id"]}/invite')
            self.assertIn(b'Invitation emailed', result.data)
            message = smtp.return_value.__enter__.return_value.send_message.call_args.args[0]
            token = re.search(r'/activate/([A-Za-z0-9_-]+)', message.get_content()).group(1)
            self.assertIn('Username: alex', message.get_content())
        self.post('/logout')
        self.assertEqual(self.client.get('/activate/' + token).status_code, 200)
        result = self.post('/activate/' + token, {'password': 'new-safe-password', 'confirm_password': 'new-safe-password'})
        self.assertIn(b'Your password is set', result.data)
        self.assertEqual(self.client.get('/activate/' + token).status_code, 400)
        self.assertIn(b'Hit Me notice board', self.login('alex', 'new-safe-password').data)

    def test_email_failure_and_rate_limit(self):
        person, _ = self.add()
        with patch('app.smtplib.SMTP', side_effect=OSError('offline')):
            self.assertIn(b'could not be sent', self.post(f'/admin/staff/{person["id"]}/invite').data)
        self.assertEqual(len(self.query('SELECT * FROM invitations')), 0)
        self.post('/logout')
        for _ in range(10):
            self.post('/employee/login', {'username': 'unknown', 'password': 'bad'})
        response = self.post('/employee/login', {'username': 'unknown', 'password': 'bad'})
        self.assertEqual(response.status_code, 429)

    def test_draft_week_ending_change_and_duplicate_username(self):
        person, values = self.add()
        duplicate = self.post('/admin/staff/new', values | {'email': 'other@example.com'})
        self.assertIn(b'already in use', duplicate.data)
        self.post('/logout')
        self.login('alex', 'test-staff-password')
        self.post('/employee/timesheet', self.week_form(self.last_week()))
        self.post('/logout')
        self.login('admin', 'test-admin-password', 'admin')
        response = self.post(f'/admin/staff/{person["id"]}/edit', values | {'week_ending': '6', 'password': ''})
        self.assertIn(b'Staff details saved.', response.data)
        self.assertEqual(self.query('SELECT week_ending FROM users WHERE id=?', (person['id'],))[0]['week_ending'], 6)

    def test_sunday_only_and_legacy_records_do_not_duplicate_hours(self):
        person, values = self.add(ending='4')
        self.assertEqual(person['week_ending'], 6)
        sunday = self.last_week()
        friday = sunday - timedelta(days=2)
        with sqlite3.connect(self.path) as db:
            db.execute('UPDATE users SET week_ending=4 WHERE id=?', (person['id'],))
            sid = db.execute("INSERT INTO sheets(user_id,week_end,status,rate_cents,total_units,total_cents,submitted_at) VALUES(?,?,'submitted',3550,800,28400,'2026-01-01')", (person['id'], friday.isoformat())).lastrowid
            db.execute("INSERT INTO entries(user_id,work_date,sheet_id,units,activity,start_time,finish_time,committed_at) VALUES(?,?,?,800,'Original work','08:00','16:00','2026-01-01')", (person['id'], friday.isoformat(), sid))
        create_app(self.app.config)
        create_app(self.app.config)
        self.assertEqual(self.query('SELECT week_ending FROM users WHERE id=?', (person['id'],))[0][0], 6)
        self.assertEqual(self.query('SELECT total_cents FROM sheets WHERE id=?', (sid,))[0][0], 28400)
        self.post('/logout')
        self.login('alex', 'test-staff-password')
        page = self.client.get('/employee/timesheet?week=' + sunday.isoformat())
        self.assertIn(b'Recorded in another week', page.data)
        self.assertIn(b'Excluded from this week', page.data)
        self.assertEqual(self.client.get('/employee/timesheet?week=' + (friday-timedelta(days=7)).isoformat()).status_code, 400)
        self.assertIn(b'locked', self.client.get('/employee/timesheet?week=' + friday.isoformat()).data)
        self.assertIn(b'belongs to another timesheet', self.post('/employee/timesheet', self.day_form(sunday, day=friday)).data)
        self.assertIn(b'Day committed', self.post('/employee/timesheet', self.day_form(sunday)).data)
        self.assertEqual(self.client.get('/employee/history').status_code, 200)
        self.assertIn(b'Timesheet submitted', self.post('/employee/timesheet', {'week':sunday.isoformat(), 'action':'submit'}).data)
        sheet = self.query('SELECT * FROM sheets WHERE week_end=?', (sunday.isoformat(),))[0]
        self.assertEqual(sheet['total_units'], 800)
        self.assertEqual(sheet['total_cents'], 28400)
        self.assertEqual(len(self.query('SELECT * FROM entries')), 2)

    def test_commit_cannot_be_changed_by_employee_and_ignores_other_days(self):
        self.add()
        self.post('/logout')
        self.login('alex', 'test-staff-password')
        end = self.last_week()
        values = self.day_form(end, '07:30', '16:00')
        self.assertIn(b'Day committed and locked.', self.post('/employee/timesheet', values).data)
        entry = self.query('SELECT * FROM entries')[0]
        self.assertEqual(entry['units'], 850)
        self.assertTrue(entry['committed_at'])
        for action in ['commit_day', 'save_day']:
            result = self.post('/employee/timesheet', values | {'action': action, 'finish_time': '18:00'})
            self.assertIn(b'Only an administrator', result.data)
        self.assertEqual(self.query('SELECT units FROM entries')[0]['units'], 850)
        other = self.day_form(end, day=end-timedelta(days=1), action='save_day')
        self.assertIn(b'Day saved.', self.post('/employee/timesheet', other).data)
        self.assertEqual(self.query('SELECT units FROM entries WHERE work_date=?', (end.isoformat(),))[0]['units'], 850)
        self.assertIn(b'Commit each entered day', self.post('/employee/timesheet', {'week':end.isoformat(), 'action':'submit'}).data)

    def test_partial_week_submission_before_sunday(self):
        # A fixed Wednesday ensures this exercises early submission for the Monday-Sunday schedule.
        fixed_now = datetime(2026, 9, 9, 12, tzinfo=ZoneInfo('Australia/Brisbane'))
        for ending_day in (6,):
            with self.subTest(ending=ending_day), patch('app.datetime') as clock:
                clock.now.return_value = fixed_now
                username = f'partial{ending_day}'
                self.add(username=username, ending=str(ending_day))
                self.post('/logout')
                self.login(username, 'test-staff-password')
                day = fixed_now.date()
                end = day + timedelta(days=ending_day - day.weekday())
                page = self.client.get('/employee/timesheet?week=' + end.isoformat())
                button = re.search(rb'<button[^>]*name="action"[^>]*value="submit"[^>]*>', page.data)
                self.assertIsNotNone(button)
                self.assertNotIn(b'disabled', button.group())
                values = self.day_form(end, action='save_day', day=day)
                self.post('/employee/timesheet', values)
                submit = {'week': end.isoformat(), 'action': 'submit'}
                self.assertIn(b'Commit each entered day', self.post('/employee/timesheet', submit).data)
                self.post('/employee/timesheet', values | {'action': 'commit_day'})
                self.assertIn(b'Timesheet submitted to admin.', self.post('/employee/timesheet', submit).data)
                sheet = self.query('SELECT s.* FROM sheets s JOIN users u ON u.id=s.user_id WHERE u.username=?', (username,))[0]
                self.assertEqual(sheet['status'], 'submitted')
                self.assertEqual(sheet['total_units'], 800)
                self.assertEqual(sheet['total_cents'], 28400)
                self.assertEqual(len(self.query('SELECT * FROM entries WHERE sheet_id=?', (sheet['id'],))), 1)
                self.assertIn(b'locked', self.post('/employee/timesheet', values | {'finish_time': '18:00'}).data)
                self.assertIn(b'locked', self.post('/employee/timesheet', self.day_form(end, day=day-timedelta(days=1))).data)
                self.assertEqual(self.query('SELECT total_units FROM sheets WHERE id=?', (sheet['id'],))[0]['total_units'], 800)
                self.post('/logout')
                self.login('admin', 'test-admin-password', 'admin')
                self.assertIn(b'$284.00', self.client.get(f'/admin/timesheets/{sheet["id"]}').data)

    def test_admin_correction_and_unlock_of_submitted_week(self):
        person, values = self.add()
        self.post('/logout')
        self.login('alex', 'test-staff-password')
        end = self.last_week()
        self.post('/employee/timesheet', self.week_form(end, '8', 'submit'))
        sheet = self.query('SELECT * FROM sheets')[0]
        route = f'/admin/timesheets/{sheet["id"]}/days/{end.isoformat()}'
        self.assertEqual(self.post(route, {'action':'unlock', 'reason':'test'}).status_code, 403)
        self.post('/logout')
        self.login('admin', 'test-admin-password', 'admin')
        self.post(f'/admin/staff/{person["id"]}/edit', values | {'hourly_rate':'50','password':''})
        self.assertEqual(self.client.get(route).status_code, 200)
        correction = dict(action='correct', start_time='08:00', finish_time='17:00',activity='Corrected work',reason='Checked clock times')
        self.assertIn(b'Correction saved.', self.post(route, correction).data)
        updated = self.query('SELECT * FROM sheets')[0]
        self.assertEqual(updated['total_units'], 5700)
        self.assertEqual(updated['total_cents'], 202350)
        self.assertEqual(updated['rate_cents'], 3550)
        self.assertEqual(updated['status'], 'submitted')
        self.assertIn(b'Enter a reason', self.post(route, {'action':'unlock'}).data)
        self.assertIn(b'Day unlocked.', self.post(route, {'action':'unlock','reason':'Employee needs to correct notes'}).data)
        self.assertEqual(self.query('SELECT status FROM sheets')[0]['status'], 'draft')
        self.assertEqual(len(self.query('SELECT * FROM entries WHERE committed_at IS NOT NULL')), 6)
        self.assertEqual(len(self.query('SELECT * FROM entry_audit')), 2)
        self.post('/logout')
        self.login('alex', 'test-staff-password')
        self.assertIn(b'Day committed', self.post('/employee/timesheet', self.day_form(end)).data)
        self.assertIn(b'Timesheet submitted', self.post('/employee/timesheet', {'week':end.isoformat(),'action':'submit'}).data)

    def test_draft_visible_to_admin_and_csrf_future_validation(self):
        self.add()
        self.post('/logout')
        self.login('alex','test-staff-password')
        end = self.last_week()
        values = self.day_form(end)
        self.assertEqual(self.client.post('/employee/timesheet',data=values).status_code,400)
        wrong = values | {'work_date': (end+timedelta(days=1)).isoformat()}
        self.assertIn(b'Choose a day in this week',self.post('/employee/timesheet',wrong).data)
        self.post('/employee/timesheet',values)
        self.post('/logout')
        self.login('admin','test-admin-password','admin')
        page = self.client.get('/admin/timesheets')
        self.assertIn(b'Draft',page.data)
        self.assertIn(b'$284.00',page.data)
        self.assertIn(b'Edit / unlock',self.client.get('/admin/timesheets/1').data)

    def test_no_work_day_and_independent_save(self):
        self.add()
        self.post('/logout')
        self.login('alex','test-staff-password')
        end=self.last_week()
        data=self.day_form(end, '', '') | {'activity':''}
        self.assertIn(b'Day committed',self.post('/employee/timesheet',data).data)
        self.assertEqual(self.query('SELECT units FROM entries')[0]['units'],0)
        self.assertIn(b'Enter some worked hours',self.post('/employee/timesheet',{'week':end.isoformat(),'action':'submit'}).data)

    def test_migration_preserves_existing_records_and_passwords(self):
        person, _ = self.add()
        before = self.query('SELECT password_hash FROM users WHERE id=?',(person['id'],))[0]['password_hash']
        with sqlite3.connect(self.path) as db:
            db.execute("INSERT INTO sheets(user_id,week_end,status,rate_cents,total_units,total_cents,submitted_at) VALUES(?, '2026-01-02','submitted',3550,800,28400,'2026-01-02T17:00:00')",(person['id'],))
            db.execute("INSERT INTO entries(user_id,work_date,sheet_id,units,activity) VALUES(?,'2026-01-02',1,800,'Historical work')",(person['id'],))
            for col in ['start_time','finish_time','committed_at']:
                db.execute(f'ALTER TABLE entries DROP COLUMN {col}')
        create_app(self.app.config)
        create_app(self.app.config)
        entry=self.query('SELECT * FROM entries')[0]
        self.assertEqual(entry['units'],800)
        self.assertEqual(entry['activity'],'Historical work')
        self.assertIsNone(entry['start_time'])
        self.assertTrue(entry['committed_at'])
        self.assertEqual(before,self.query('SELECT password_hash FROM users WHERE id=?',(person['id'],))[0]['password_hash'])


if __name__ == '__main__':
    unittest.main()

