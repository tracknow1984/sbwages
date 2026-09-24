import json
import sqlite3
import unittest
from lunch import deduct_lunch, migrate_lunch
from app import create_app
import test_app


class LunchTests(unittest.TestCase):
    def test_daily_boundaries(self):
        for gross, expected in [(900,(850,50)),(800,(750,50)),(50,(0,50)),(25,(0,25)),(0,(0,0)),(833,(783,50))]:
            self.assertEqual(deduct_lunch(gross), expected)

    def test_migration_all_employees_and_repeat_startup(self):
        fixture=test_app.AppTests(); fixture.setUp()
        try:
            first,_=fixture.add(); second,_=fixture.add('jordan')
            with sqlite3.connect(fixture.path) as db:
                db.execute('DROP TABLE lunch_migration_backup')
                db.execute('ALTER TABLE entries DROP COLUMN lunch_units')
                for sid,uid,status,archived in [(1,first['id'],'submitted',None),(2,second['id'],'submitted','2026-09-01'),(3,second['id'],'draft',None)]:
                    db.execute('INSERT INTO sheets(id,user_id,week_end,status,rate_cents,total_units,total_cents,archived_at) VALUES(?,?,?,?,?,?,?,?)',
                               (sid,uid,f'2026-08-{sid*7+2:02d}',status,3500,1700,59500,archived))
                for uid,day,sid,units in [(first['id'],'2026-08-03',1,900),(first['id'],'2026-08-04',1,800),(second['id'],'2026-08-10',2,900),(second['id'],'2026-08-11',2,800),(second['id'],'2026-08-17',3,25),(second['id'],'2026-08-18',3,0)]:
                    db.execute('INSERT INTO entries(user_id,work_date,sheet_id,units,activity) VALUES(?,?,?,?,?)',(uid,day,sid,units,'original'))
                db.execute("INSERT INTO payments(sheet_id,user_id,admin_id,employee_name,employee_username,processed_by,week_start,week_end,total_units,rate_cents,total_cents,cash_cents,transfer_cents,processed_at) VALUES(1,?,1,'Alex Example','alex','Admin','2026-08-03','2026-08-09',1700,3500,59500,10000,49500,'2026-08-10')",(first['id'],))
            before_payment=dict(fixture.query('SELECT * FROM payments')[0])
            create_app(fixture.app.config)
            entries=[dict(r) for r in fixture.query('SELECT * FROM entries ORDER BY sheet_id,work_date')]
            self.assertEqual([r['units'] for r in entries],[850,750,850,750,0,0])
            self.assertEqual([r['lunch_units'] for r in entries],[50,50,50,50,25,0])
            self.assertEqual([r['total_cents'] for r in fixture.query("SELECT * FROM sheets WHERE status='submitted'")],[56000,56000])
            self.assertEqual(dict(fixture.query('SELECT * FROM payments')[0]),before_payment)
            backup=fixture.query("SELECT before_json FROM lunch_migration_backup WHERE kind='entry'")[0][0]
            self.assertEqual(json.loads(backup)['units'],900)
            create_app(fixture.app.config)
            self.assertEqual([dict(r) for r in fixture.query('SELECT * FROM entries ORDER BY sheet_id,work_date')],entries)
            self.assertEqual(dict(fixture.query('SELECT * FROM payments')[0]),before_payment)
            page=fixture.client.get('/admin/timesheets/1').data
            self.assertIn(b'$560.00',page); self.assertIn(b'$595.00',page)
            self.assertIn(b'$35.00',page)
            self.assertIn(b'original processed payment record',fixture.client.get('/payments/1/slip').data)
        finally: fixture.tearDown()

    def test_resave_commit_correction_and_short_shift(self):
        f=test_app.AppTests();f.setUp()
        try:
            f.add();f.post('/logout');f.login('alex','test-staff-password');end=f.last_week()
            day=f.day_form(end,finish='17:00',action='save_day') | {'activity':''}
            for action in ['save_day','save_day','commit_day']:
                f.post('/employee/timesheet',day | {'action':action})
                entry=f.query('SELECT * FROM entries')[0]
                self.assertEqual((entry['units'],entry['lunch_units']),(850,50))
            f.post('/employee/timesheet',{'week':end.isoformat(),'action':'submit'})
            f.post('/logout');f.login('admin','test-admin-password','admin')
            f.post(f'/admin/timesheets/1/days/{end.isoformat()}',dict(action='correct',start_time='08:00',finish_time='16:00',activity='',reason='Test'))
            entry=f.query('SELECT * FROM entries')[0]
            self.assertEqual((entry['units'],entry['lunch_units']),(750,50))
            self.assertEqual(f.query('SELECT total_cents FROM sheets')[0][0],26625)
        finally:f.tearDown()
