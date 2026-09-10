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

    def week_form(self, end, hours='8', action='save'):
        return {'week': end.isoformat(), 'action': action,
                **{'hours_' + (end - timedelta(days=i)).isoformat(): hours for i in range(7)},
                **{'activity_' + (end - timedelta(days=i)).isoformat(): 'Yard maintenance' for i in range(7)}}

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
        response = self.post('/employee/timesheet', self.week_form(end, '0.01', 'submit'))
        self.assertIn(b'Timesheet submitted to admin.', response.data)
        sheet = self.query('SELECT * FROM sheets')[0]
        self.assertEqual(sheet['total_cents'], 249)
        rows = self.query('SELECT work_date FROM entries ORDER BY work_date')
        self.assertEqual(datetime.fromisoformat(rows[0]['work_date']).weekday(), 0)
        self.assertEqual(datetime.fromisoformat(rows[-1]['work_date']).weekday(), 6)

    def test_validation_csrf_and_tenant_isolation(self):
        self.add()
        self.add('jordan')
        self.post('/logout')
        self.login('alex', 'test-staff-password')
        end = self.last_week()
        for hours in ['25', '-1', 'NaN', 'Infinity', '1.001']:
            response = self.post('/employee/timesheet', self.week_form(end, hours))
            self.assertIn(b'Daily hours must', response.data)
        values = self.week_form(end)
        values['activity_' + end.isoformat()] = ''
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


if __name__ == '__main__':
    unittest.main()
