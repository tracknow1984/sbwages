import sqlite3
import unittest
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo
from unittest.mock import patch

from app import create_app
from work_weeks import WORK_WEEKS, profile_end, profile_start
import test_app


class WorkWeekTests(unittest.TestCase):
    setUp = test_app.AppTests.setUp
    tearDown = test_app.AppTests.tearDown
    query = test_app.AppTests.query
    post = test_app.AppTests.post
    login = test_app.AppTests.login
    add = test_app.AppTests.add
    day_form = test_app.AppTests.day_form

    def choose(self, person, values, profile):
        result = self.post(f'/admin/staff/{person["id"]}/edit', values | {'password': '', 'work_week': profile})
        self.assertIn(b'Staff details saved.', result.data)

    def employee(self):
        self.post('/logout')
        self.login('alex', 'test-staff-password')

    def admin(self):
        self.post('/logout')
        self.login('admin', 'test-admin-password', 'admin')

    def test_profile_save_validation_restart_and_existing_form_compatibility(self):
        person, values = self.add()
        self.assertEqual(person['work_week'], 'mon_sun')
        for profile in WORK_WEEKS:
            self.choose(person, values, profile)
            create_app(self.app.config)
            self.assertEqual(self.query('SELECT work_week FROM users WHERE id=?', (person['id'],))[0][0], profile)
            page = self.client.get(f'/admin/staff/{person["id"]}/edit').get_data(as_text=True)
            self.assertIn(f'value="{profile}" selected', page)
        result = self.post(f'/admin/staff/{person["id"]}/edit', values | {'password': '', 'work_week': 'invalid'})
        self.assertIn(b'Choose a valid timesheet work week', result.data)
        self.assertEqual(self.query('SELECT work_week FROM users WHERE id=?', (person['id'],))[0][0], 'fri_next_sat')
        self.post(f'/admin/staff/{person["id"]}/edit', values | {'password': ''})
        self.assertEqual(self.query('SELECT work_week FROM users WHERE id=?', (person['id'],))[0][0], 'fri_next_sat')
        self.employee()
        self.assertEqual(self.post(f'/admin/staff/{person["id"]}/edit', values | {'work_week': 'mon_sun'}).status_code, 403)
        self.post('/employee/details', values | {'work_week': 'mon_sun'})
        self.assertEqual(self.query('SELECT work_week FROM users WHERE id=?', (person['id'],))[0][0], 'fri_next_sat')

    def test_every_profile_dates_submission_payment_and_history(self):
        person, values = self.add()
        for profile, end, count in [('mon_sun', date(2025,12,28),7), ('mon_fri', date(2026,1,9),5), ('fri_next_sat', date(2026,1,24),9)]:
            self.choose(person, values, profile)
            self.employee()
            start = profile_start(end, profile)
            page = self.client.get('/employee/timesheet?week='+end.isoformat()).get_data(as_text=True)
            self.assertEqual(page.count('class="day-row '), count)
            self.assertIn(start.strftime('%d.%m.%Y'), page)
            for i in range(count):
                result = self.post('/employee/timesheet', self.day_form(end, day=start+timedelta(days=i)))
                self.assertIn(b'Day committed and locked', result.data)
            outside = self.post('/employee/timesheet', self.day_form(end, day=start-timedelta(days=1)))
            self.assertIn(b'Choose a day in this week', outside.data)
            self.post('/employee/timesheet', {'week':end.isoformat(), 'action':'submit'})
            sheet = self.query('SELECT * FROM sheets WHERE week_end=?',(end.isoformat(),))[0]
            self.assertEqual(sheet['total_units'], 800*count)
            self.assertEqual(sheet['total_cents'], 28400*count)
            self.assertEqual(sheet['week_start'], start.isoformat())
            self.assertEqual(sheet['work_week'], profile)
            self.admin()
            self.choose(person, values, 'mon_sun')
            create_app(self.app.config)
            page = self.client.get('/admin/timesheets/'+str(sheet['id'])).get_data(as_text=True)
            self.assertEqual(page.count('class="day-row '), count)
            result = self.post(f'/admin/timesheets/{sheet["id"]}/payment', {'cash_amount': '0', 'transfer_amount':str(284*count), 'expected_total':str(28400*count)})
            self.assertIn(b'Payment processed', result.data)
            payment = self.query('SELECT * FROM payments WHERE sheet_id=?',(sheet['id'],))[0]
            self.assertEqual(payment['week_start'], start.isoformat())
            self.assertEqual(payment['week_end'], end.isoformat())
            history = self.client.get('/admin/timesheets').get_data(as_text=True)
            self.assertIn(WORK_WEEKS[profile]['label'], history)
            self.assertIn(start.strftime('%d.%m.%Y'), history)

    def test_overlap_has_one_entry_and_one_payment_total_per_date(self):
        person, values = self.add()
        self.choose(person, values, 'fri_next_sat')
        self.employee()
        end = date(2026,1,17)
        self.post('/employee/timesheet', self.day_form(end, day=end-timedelta(days=1)))
        self.post('/employee/timesheet', self.day_form(end, day=end))
        next_end=end+timedelta(days=7)
        page = self.client.get('/employee/timesheet?week='+next_end.isoformat()).get_data(as_text=True)
        self.assertEqual(page.count('Recorded in another week'), 2)
        result = self.post('/employee/timesheet', self.day_form(next_end, day=end))
        self.assertIn(b'This date belongs to another timesheet', result.data)
        self.post('/employee/timesheet', self.day_form(next_end, day=end+timedelta(days=1)))
        for ending in (end,next_end):
            self.post('/employee/timesheet', {'week':ending.isoformat(),'action':'submit'})
        self.assertEqual(len(self.query('SELECT * FROM entries')), 3)
        self.assertEqual(self.query('SELECT SUM(total_units) FROM sheets')[0][0], 2400)
        self.assertEqual(self.query('SELECT SUM(total_cents) FROM sheets')[0][0], 85200)

    def test_profile_switch_preserves_draft_and_legacy_migration(self):
        person, values = self.add()
        self.employee()
        end = date(2026,1,11)
        self.post('/employee/timesheet', self.day_form(end, action='save_day', day=end-timedelta(days=5)))
        self.admin()
        with sqlite3.connect(self.path) as conn:
            conn.execute('ALTER TABLE users DROP COLUMN work_week')
            conn.execute('ALTER TABLE sheets DROP COLUMN work_week')
            conn.execute('ALTER TABLE sheets DROP COLUMN week_start')
        create_app(self.app.config)
        create_app(self.app.config)
        self.choose(person, values, 'mon_fri')
        old=self.query('SELECT * FROM sheets')[0]
        self.assertEqual(old['week_start'],'2026-01-05')
        self.assertIsNone(old['work_week'])
        self.assertEqual(len(self.query('SELECT * FROM entries')),1)
        self.employee()
        page=self.client.get('/employee/timesheet?week='+end.isoformat()).get_data(as_text=True)
        self.assertEqual(page.count('class="day-row '),7)
        result=self.post('/employee/timesheet', self.day_form(date(2026,1,9),day=date(2026,1,6)))
        self.assertIn(b'This date belongs to another timesheet',result.data)
        self.assertEqual(self.client.get('/employee/timesheet?week=2026-01-18').status_code,400)

    def test_current_week_boundaries_and_navigation(self):
        person, values=self.add()
        self.choose(person,values,'mon_fri')
        self.employee()
        for day, expected in [(date(2026,1,2),'2026-01-02'),(date(2026,1,3),'2026-01-02'),(date(2026,1,4),'2026-01-02'),(date(2026,1,5),'2026-01-09')]:
            with patch('app.datetime') as clock:
                clock.now.return_value = datetime(day.year, day.month, day.day, 12, tzinfo=ZoneInfo('Australia/Brisbane'))
                page=self.client.get('/employee/timesheet').get_data(as_text=True)
                self.assertIn(f'name="week" value="{expected}"',page)
                self.assertEqual(self.client.get('/employee/timesheet?week=2026-01-16').status_code,400)
        self.assertEqual(profile_start(date(2026,1,3),'fri_next_sat'),date(2025,12,26))
        self.assertEqual(profile_start(date(2024,3,1),'mon_fri'),date(2024,2,26))
        self.assertEqual(profile_end(date(2026,1,4),'fri_next_sat'),date(2026,1,10))
