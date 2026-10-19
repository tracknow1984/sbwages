import re
import sqlite3
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch
from zoneinfo import ZoneInfo
from werkzeug.security import generate_password_hash

import test_binflow as legacy
from binflow_helpers import prestart, accept, depart, RUN_DATE
from binflow_workday import clock_stamp, migrate_workdays


class DriverWorkdayTests(unittest.TestCase):
    post=legacy.BinFlowTests.post
    rows=legacy.BinFlowTests.rows
    login=legacy.BinFlowTests.login
    job=legacy.BinFlowTests.job
    initial_delivery=legacy.BinFlowTests.initial_delivery
    exchange=legacy.BinFlowTests.exchange

    def setUp(self):
        legacy.BinFlowTests.setUp(self)
        self.time=datetime(2026,10,19,7,5,17,tzinfo=ZoneInfo('Australia/Brisbane'))
        self.clock=patch('binflow.datetime',wraps=datetime)
        self.mock_clock=self.clock.start();self.mock_clock.now.side_effect=lambda zone: self.time.astimezone(zone)
        self.initial_delivery()
        self.job(collect=(1,2),deliver=(3,4),legacy=False)
        self.post('/binflow/driver',dict(name='QLD day truck',state='QLD'))
        self.post('/binflow/driver',dict(name='NSW day truck',state='NSW'))
        self.post('/binflow/pickups/2/truck',dict(truck_id=1))
        with sqlite3.connect(self.path) as conn:
            self.uid=conn.execute("INSERT INTO users(role,first_name,last_name,email,username,password_hash) VALUES('employee','Phone','Driver','phone@example.com','phonedriver',?)",(generate_password_hash('test-admin-password'),)).lastrowid
            conn.execute('INSERT INTO bf_access(user_id) VALUES(?)',(self.uid,))
        self.post('/logout');self.login('employee','phonedriver')

    def tearDown(self):
        self.clock.stop();legacy.BinFlowTests.tearDown(self)

    def stamp(self,zone='Australia/Brisbane'):
        return dict(device_at=self.time.astimezone(timezone.utc).isoformat(),device_zone=zone)

    def start(self,zone='Australia/Brisbane'):
        return self.post('/binflow/driver/start-day',self.stamp(zone))

    def choose(self,truck=1,state='QLD'):
        day=self.rows('SELECT id FROM bf_workdays WHERE user_id=?',(self.uid,))[0]
        return self.post(f'/binflow/driver/my-day/{day["id"]}/vehicle',dict(state=state,truck_id=truck))

    def finish(self,**extra):
        day=self.rows('SELECT id FROM bf_workdays WHERE user_id=?',(self.uid,))[0]
        return self.post(f'/binflow/driver/my-day/{day["id"]}/finish',{**self.stamp(),**extra})

    def stage(self):
        return self.client.get('/binflow/driver/1/day/state?date='+RUN_DATE).json['stage']

    def scan(self,action,bid):
        return self.post('/binflow/driver/1/scan',dict(job_id=2,action=action,code=f'BF{bid:06d}'))

    def test_complete_phone_day_updates_timesheet_and_all_bins_without_waiting_for_weight(self):
        self.assertEqual(self.stage(),'startday')
        self.assertIn(b'Start day',self.client.get('/binflow/driver/my-day').data)
        self.assertIn(b'Start your driver day',self.scan('load',3).data)
        self.start();self.choose()
        self.assertEqual(self.stage(),'prestart')
        entry=self.rows('SELECT * FROM entries WHERE user_id=?',(self.uid,))[0]
        self.assertEqual(entry['start_time'],'07:05');self.assertIsNone(entry['finish_time']);self.assertIsNone(entry['committed_at'])
        self.assertIn(b'Driver day in progress',self.client.get('/employee/timesheet').data)
        self.post('/employee/timesheet',dict(work_date=RUN_DATE,action='save_day',start_time='06:00',finish_time='18:00'))
        self.assertEqual(self.rows('SELECT start_time FROM entries WHERE user_id=?',(self.uid,))[0]['start_time'],'07:05')
        prestart(self);self.assertEqual(self.stage(),'acceptance')
        self.assertIn(b'Accept schedule',self.client.get('/binflow/driver/1/day?date='+RUN_DATE).data)
        accept(self);self.assertEqual(self.stage(),'load')
        self.scan('load',3);self.scan('load',4);depart(self)
        self.assertEqual(self.stage(),'deliver')
        self.scan('collect',1)  # Must deliver both empty bins first.
        self.assertIsNone(self.rows('SELECT picked_up_at FROM bf_job_bins WHERE job_id=2 AND bin_id=1')[0]['picked_up_at'])
        self.scan('deliver',3);self.scan('deliver',4);self.scan('collect',1);self.scan('collect',2)
        self.exchange(2,(1,2,3,4));self.assertEqual(self.stage(),'return')
        self.time+=timedelta(hours=8,minutes=30)
        self.assertIn(b'Scan every bin off the truck',self.finish().data)
        self.assertIsNone(self.rows('SELECT finished_at FROM bf_workdays')[0]['finished_at'])
        self.scan('return',1);self.scan('return',2);self.assertEqual(self.stage(),'weighing')
        result=self.finish(notes='All client stops and returns completed')
        self.assertIn(b'Your work is saved',result.data)
        entry=self.rows('SELECT * FROM entries WHERE user_id=?',(self.uid,))[0]
        self.assertEqual((entry['start_time'],entry['finish_time'],entry['units'],entry['lunch_units']),('07:05','15:35',800,50))
        self.assertTrue(entry['committed_at']);self.assertIn('1 client stops',entry['activity'])
        self.assertEqual(self.rows('SELECT status FROM bf_jobs WHERE id=2')[0]['status'],'received')
        self.assertEqual(self.stage(),'finished')
        self.assertFalse(self.rows('SELECT * FROM bf_bins WHERE truck_id=1'))
        self.assertEqual(self.rows('SELECT status FROM sheets WHERE id=?',(entry['sheet_id'],))[0]['status'],'draft')
        self.assertIn(b'WEIGHT PENDING',self.client.get('/binflow/pickups/2/pod').data)

    def test_duplicate_taps_and_old_finish_form_do_not_change_recorded_times(self):
        self.start();first=self.rows('SELECT * FROM bf_workdays')[0]
        self.time+=timedelta(minutes=8);self.start()
        self.assertEqual(self.rows('SELECT * FROM bf_workdays'),[first])
        self.assertEqual(len(self.rows('SELECT * FROM entries WHERE user_id=?',(self.uid,))),1)
        self.time+=timedelta(hours=2)
        self.finish(notes='Warehouse work only')
        finished=self.rows('SELECT * FROM bf_workdays');entries=self.rows('SELECT * FROM entries WHERE user_id=?',(self.uid,));history=self.rows('SELECT * FROM bf_workday_events')
        self.time+=timedelta(minutes=10);self.finish(notes='Double tap');self.start()
        self.assertEqual(self.rows('SELECT * FROM bf_workdays'),finished)
        self.assertEqual(self.rows('SELECT * FROM entries WHERE user_id=?',(self.uid,)),entries)
        self.assertEqual(self.rows('SELECT * FROM bf_workday_events'),history)
        self.assertIn(b'already finalised',self.choose().data)

    def test_vehicle_state_prestart_and_acceptance_cannot_be_bypassed(self):
        self.start()
        self.choose(truck=2,state='QLD')
        self.assertIsNone(self.rows('SELECT truck_id FROM bf_workdays')[0]['truck_id'])
        self.choose()
        self.post('/binflow/driver/1/accept',dict(date=RUN_DATE,manifest_version='stale'))
        self.assertFalse(self.rows('SELECT * FROM bf_run_acceptance_history WHERE truck_id=1'))
        self.assertIn(b'Continue to prestart',self.client.get('/binflow/driver/my-day?summary=1').data)
        prestart(self,fail=True);self.assertEqual(self.stage(),'hold')
        self.post('/binflow/driver/1/accept',dict(date=RUN_DATE,manifest_version='stale'))
        self.scan('load',3)
        self.assertIsNone(self.rows('SELECT truck_id FROM bf_bins WHERE id=3')[0]['truck_id'])
        self.assertFalse(self.rows('SELECT * FROM bf_run_acceptance_history WHERE truck_id=1'))
        self.assertIn(b'Complete every client stop',self.finish().data)

    def test_clock_skew_falls_back_and_timezone_handles_sydney_midnight(self):
        self.time=datetime(2026,10,19,23,30,tzinfo=ZoneInfo('Australia/Brisbane'))
        self.start('Australia/Sydney')
        day=self.rows('SELECT * FROM bf_workdays')[0]
        self.assertEqual(day['work_date'],'2026-10-20')
        self.assertEqual(self.rows('SELECT start_time FROM entries WHERE user_id=?',(self.uid,))[0]['start_time'],'00:30')
        stamp,raw,zone,source=clock_stamp(dict(device_at='2000-01-01T00:00:00Z',device_zone='Australia/Perth'),self.time.isoformat())
        self.assertEqual(stamp.hour,21);self.assertIn('server timestamp used',source)
        stamp,_,zone,_=clock_stamp(dict(device_at='invalid',device_zone='Not/AZone'),self.time.isoformat())
        self.assertEqual(zone,'Australia/Brisbane')

    def test_overnight_work_splits_dates_and_commits_each_saved_week(self):
        self.time=datetime(2026,10,25,22,0,tzinfo=ZoneInfo('Australia/Brisbane'))  # Sunday to Monday.
        self.start();self.time+=timedelta(hours=4)
        result=self.finish(notes='Overnight warehouse return')
        self.assertIn(b'Your work is saved',result.data)
        entries=self.rows('SELECT e.*,s.week_end FROM entries e JOIN sheets s ON s.id=e.sheet_id WHERE e.user_id=? ORDER BY work_date',(self.uid,))
        self.assertEqual([(e['work_date'],e['start_time'],e['finish_time'],e['units']) for e in entries],
                         [('2026-10-25','22:00','24:00',150),('2026-10-26','00:00','02:00',150)])
        self.assertEqual([e['week_end'] for e in entries],['2026-10-25','2026-11-01'])
        self.assertTrue(all(e['committed_at'] for e in entries))

    def test_existing_hours_and_locked_timesheets_are_preserved(self):
        self.post('/employee/timesheet',dict(work_date=RUN_DATE,action='commit_day',start_time='06:00',finish_time='08:00',activity='Existing work'))
        before=self.rows('SELECT * FROM entries WHERE user_id=?',(self.uid,))
        self.assertIn(b'already has timesheet hours',self.start().data)
        self.assertFalse(self.rows('SELECT * FROM bf_workdays'))
        self.assertEqual(self.rows('SELECT * FROM entries WHERE user_id=?',(self.uid,)),before)
        with sqlite3.connect(self.path) as conn:conn.execute("UPDATE sheets SET status='submitted' WHERE user_id=?",(self.uid,))
        self.assertIn(b'timesheet is locked',self.start().data)
        self.assertFalse(self.rows('SELECT * FROM bf_workdays'))

    def test_login_csrf_ownership_and_transport_access(self):
        self.assertEqual(self.client.post('/binflow/driver/start-day',data=self.stamp()).status_code,400)
        self.start()
        day_id=self.rows('SELECT id FROM bf_workdays')[0]['id']
        self.post('/logout')
        self.assertEqual(self.post('/binflow/driver/start-day',self.stamp(),follow=False).status_code,302)
        self.login()  # Even admins cannot finish another person's workday.
        self.assertEqual(self.post(f'/binflow/driver/my-day/{day_id}/finish',self.stamp()).status_code,404)
        self.assertEqual(self.client.get('/binflow/driver/my-day?day='+str(day_id)).status_code,404)
        self.post('/logout');self.login('employee','phonedriver')
        with sqlite3.connect(self.path) as conn:conn.execute('DELETE FROM bf_access WHERE user_id=?',(self.uid,))
        self.assertEqual(self.post(f'/binflow/driver/my-day/{day_id}/finish',self.stamp()).status_code,403)
        self.assertIsNone(self.rows('SELECT finished_at FROM bf_workdays')[0]['finished_at'])

    def test_office_can_reconcile_a_forgotten_finish_without_overwriting_active_hours(self):
        self.start();entry=self.rows('SELECT * FROM entries WHERE user_id=?',(self.uid,))[0]
        day=self.rows('SELECT id FROM bf_workdays')[0]['id']
        route=f'/binflow/driver/my-day/{day}/reconcile'
        self.assertEqual(self.post(route,dict(finished_at='2026-10-19T15:05',reason='Forgot to finish')).status_code,403)
        self.time+=timedelta(days=3)
        self.post('/logout');self.login()
        self.assertIn(b'Active BINFLOW driver day',self.client.get(f'/admin/timesheets/{entry["sheet_id"]}/days/{RUN_DATE}').data)
        self.post(f'/admin/timesheets/{entry["sheet_id"]}/delete',dict(confirm_delete='yes',payment_id=''))
        self.assertEqual(self.rows('SELECT * FROM entries WHERE user_id=?',(self.uid,)),[entry])
        self.post(route,dict(finished_at='2026-10-19T15:05'))
        self.assertIsNone(self.rows('SELECT finished_at FROM bf_workdays')[0]['finished_at'])
        self.post(route,dict(finished_at='2026-10-19T15:05',reason='Supervisor confirmed actual end of shift'))
        saved=self.rows('SELECT * FROM entries WHERE user_id=?',(self.uid,))[0]
        self.assertEqual((saved['finish_time'],saved['units']),('15:05',750))
        self.assertTrue(saved['committed_at'])
        event=self.rows("SELECT * FROM bf_workday_events WHERE action='Finish time reconciled by office'")[0]
        self.assertNotEqual(event['actor_id'],self.uid)
        self.assertIn('Supervisor confirmed',event['notes'])
        self.post(f'/admin/timesheets/{entry["sheet_id"]}/delete',dict(confirm_delete='yes',payment_id=''))
        self.assertFalse(self.rows('SELECT * FROM entries WHERE user_id=?',(self.uid,)))
        self.assertTrue(self.rows('SELECT finished_at FROM bf_workdays')[0]['finished_at'])
        self.assertIsNone(self.rows('SELECT sheet_id FROM bf_workday_entries')[0]['sheet_id'])

    def test_overnight_overlap_rolls_back_all_timesheet_changes(self):
        self.time=datetime(2026,10,19,22,0,tzinfo=ZoneInfo('Australia/Brisbane'))
        self.start()
        entry=self.rows('SELECT * FROM entries WHERE user_id=?',(self.uid,))[0]
        with sqlite3.connect(self.path) as conn:
            conn.execute("INSERT INTO entries(user_id,work_date,sheet_id,units,activity,start_time,finish_time,lunch_units) VALUES(?,'2026-10-20',?,100,'Existing work','01:00','02:00',0)",(self.uid,entry['sheet_id']))
        before=self.rows('SELECT * FROM entries WHERE user_id=? ORDER BY work_date',(self.uid,))
        self.time+=timedelta(hours=4)
        self.assertIn(b'overnight shift overlaps existing hours',self.finish().data)
        self.assertEqual(self.rows('SELECT * FROM entries WHERE user_id=? ORDER BY work_date',(self.uid,)),before)
        self.assertIsNone(self.rows('SELECT finished_at FROM bf_workdays')[0]['finished_at'])

    def test_restart_keeps_phone_records_and_never_creates_old_payroll_entries(self):
        self.start();original=self.rows('SELECT * FROM bf_workdays');entries=self.rows('SELECT * FROM entries')
        with sqlite3.connect(self.path) as conn:
            conn.row_factory=sqlite3.Row;migrate_workdays(conn);migrate_workdays(conn)
        self.assertEqual(self.rows('SELECT * FROM bf_workdays'),original)
        self.assertEqual(self.rows('SELECT * FROM entries'),entries)


if __name__=='__main__':unittest.main()
