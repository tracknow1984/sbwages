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

    def add(self, username='alex', ending='4', password='test-staff-password'):
        values = dict(first_name='Alex', last_name='Example', email=username+'@example.com', mobile='0400000000',
                      emergency_name='Casey Example', emergency_email='casey@example.com',
                      hourly_rate='35.50', username=username, password=password, status='active', week_ending=ending)
        response = self.post('/admin/staff/new', values)
        self.assertIn(b'Staff details saved.', response.data)
        return self.query('SELECT * FROM users WHERE username=?', (username,))[0], values

    def last_week(self, ending=4):
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

    def test_friday_submission_snapshot_and_lock(self):
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
        self.assertIn(b'No submitted weeks yet', self.client.get('/employee/history').data)
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
        self.assertIn(b'My timesheet', self.login('alex', 'new-safe-password').data)

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
        self.assertIn(b'Submit existing draft', response.data)
        self.assertEqual(self.query('SELECT week_ending FROM users WHERE id=?', (person['id'],))[0]['week_ending'], 4)

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

