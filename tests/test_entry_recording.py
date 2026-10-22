import sqlite3
import unittest
from unittest.mock import patch

import test_app as core
from entry_recording import migrate_recording, recording_datetime


class EntryRecordingTests(unittest.TestCase):
    setUp = core.AppTests.setUp
    tearDown = core.AppTests.tearDown
    query = core.AppTests.query
    post = core.AppTests.post
    login = core.AppTests.login
    add = core.AppTests.add
    last_week = core.AppTests.last_week
    day_form = core.AppTests.day_form

    def staff_session(self):
        person, _ = self.add()
        self.post('/logout')
        self.login('alex', 'test-staff-password')
        return person

    def save_json(self, form, stamp):
        with self.client.session_transaction() as sess:
            token = sess['csrf']
        with patch('entry_recording.recording_now', return_value=stamp):
            return self.client.post('/employee/timesheet', data={'csrf': token, **form},
                                    headers={'Accept': 'application/json'})

    def test_first_save_is_server_owned_and_visible_to_staff_and_admin(self):
        person = self.staff_session()
        end = self.last_week()
        stamp = '2026-10-22T14:35:27+00:00'
        response = self.save_json(self.day_form(end, action='save_day') | {
            'first_logged_at': '2000-01-01T00:00:00Z', 'first_logged_by': '999',
            'last_updated_at': '2000-01-01T00:00:00Z'}, stamp)
        self.assertEqual(response.status_code, 200)
        saved = self.query('SELECT * FROM entries')[0]
        self.assertEqual((saved['first_logged_at'], saved['first_logged_by']), (stamp, person['id']))
        self.assertIsNone(saved['last_updated_at'])
        self.assertEqual(response.json['recording'][0]['text'], '23/10/2026 · 00:35:27 AEST')
        self.assertEqual(response.json['recording'][0]['by'], 'Alex Example · Staff')
        for path in ('/employee/timesheet?week=' + end.isoformat(), '/employee/history'):
            self.assertIn(b'23/10/2026', self.client.get(path).data)
        self.post('/logout'); self.login('admin', 'test-admin-password', 'admin')
        for path in ('/admin/timesheets', f'/admin/timesheets/{saved["sheet_id"]}',
                     f'/admin/timesheets/{saved["sheet_id"]}/days/{end}'):
            self.assertIn(b'23/10/2026', self.client.get(path).data)

    def test_edits_preserve_first_log_and_noop_or_failed_saves_do_not_change_it(self):
        person = self.staff_session()
        form = self.day_form(self.last_week(), action='save_day')
        first, edited = '2026-10-22T01:00:00+00:00', '2026-10-22T02:00:00+00:00'
        self.save_json(form, first)
        self.save_json(form, edited)
        self.assertIsNone(self.query('SELECT last_updated_at FROM entries')[0][0])
        form['activity'] = 'Corrected activities'
        self.save_json(form, edited)
        self.save_json(form | {'action': 'commit_day'}, '2026-10-22T03:00:00+00:00')
        saved = self.query('SELECT * FROM entries')[0]
        self.assertEqual((saved['first_logged_at'], saved['last_updated_at'], saved['last_updated_by']),
                         (first, edited, person['id']))
        self.assertTrue(saved['committed_at'])
        self.assertEqual(saved['units'], 800)
        self.assertFalse(self.save_json(form | {'finish_time': '19:00'}, edited).json['ok'])
        self.assertEqual(dict(self.query('SELECT * FROM entries')[0]), dict(saved))

    def test_admin_correction_and_unlock_keep_staff_log_and_attribute_changes(self):
        person = self.staff_session()
        end = self.last_week()
        first, edited = '2026-10-22T01:00:00+00:00', '2026-10-22T02:00:00+00:00'
        self.save_json(self.day_form(end), first)
        saved = self.query('SELECT * FROM entries')[0]
        route = f'/admin/timesheets/{saved["sheet_id"]}/days/{end}'
        self.post('/logout'); self.login('admin', 'test-admin-password', 'admin')
        admin_id = self.query("SELECT id FROM users WHERE username='admin'")[0][0]
        with patch('entry_recording.recording_now', return_value=edited):
            self.post(route, {'action': 'correct', 'start_time': '08:00', 'finish_time': '17:00',
                              'activity': 'Office correction', 'reason': 'Checked actual shift'})
        corrected = self.query('SELECT * FROM entries')[0]
        self.assertEqual((corrected['first_logged_at'], corrected['first_logged_by']), (first, person['id']))
        self.assertEqual((corrected['last_updated_at'], corrected['last_updated_by']), (edited, admin_id))
        self.assertIn(b'Last updated', self.client.get(route).data)
        self.post(route, {'action': 'unlock', 'reason': 'Allow staff to add detail'})
        self.assertEqual(self.query('SELECT last_updated_at FROM entries')[0][0], edited)
        self.post('/logout'); self.login('alex', 'test-staff-password')
        later = '2026-10-22T03:00:00+00:00'
        self.save_json(self.day_form(end, finish='17:00', action='save_day'), later)
        row = self.query('SELECT * FROM entries')[0]
        self.assertEqual((row['first_logged_at'], row['last_updated_at'], row['last_updated_by']),
                         (first, later, person['id']))

    def test_legacy_entries_remain_unknown_after_migration_and_later_edit(self):
        self.staff_session()
        end = self.last_week()
        form = self.day_form(end, action='save_day')
        self.save_json(form, '2026-10-22T01:00:00+00:00')
        with sqlite3.connect(self.path) as conn:
            conn.row_factory = sqlite3.Row
            for column in ('first_logged_at', 'first_logged_by', 'last_updated_at', 'last_updated_by'):
                conn.execute(f'ALTER TABLE entries DROP COLUMN {column}')
            before = dict(conn.execute('SELECT * FROM entries').fetchone())
            migrate_recording(conn); migrate_recording(conn)
            after = dict(conn.execute('SELECT * FROM entries').fetchone())
            self.assertEqual({key: after[key] for key in before}, before)
            self.assertIsNone(after['first_logged_at'])
        later = '2026-10-22T03:00:00+00:00'
        result = self.save_json(form | {'finish_time': '17:00'}, later)
        self.assertEqual(result.json['recording'][0]['text'], 'Not recorded for this older entry')
        row = self.query('SELECT * FROM entries')[0]
        self.assertIsNone(row['first_logged_at'])
        self.assertEqual(row['last_updated_at'], later)
        self.assertEqual(recording_datetime('2026-10-22'), 'Not recorded')


if __name__ == '__main__':
    unittest.main()
