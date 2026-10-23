import csv
import io
import sqlite3
import unittest
from datetime import timedelta

import test_binflow_journey as journey
from binflow_helpers import RUN_DATE
from binflow_reports import timing_values


class StepTimingTests(unittest.TestCase):
    setUp=journey.DriverJourneyTests.setUp
    tearDown=journey.DriverJourneyTests.tearDown
    post=journey.DriverJourneyTests.post
    rows=journey.DriverJourneyTests.rows
    login=journey.DriverJourneyTests.login
    exchange=journey.DriverJourneyTests.exchange
    job=journey.DriverJourneyTests.job
    initial_delivery=journey.DriverJourneyTests.initial_delivery
    stamp=journey.DriverJourneyTests.stamp
    start=journey.DriverJourneyTests.start
    accept_overview=journey.DriverJourneyTests.accept_overview
    choose=journey.DriverJourneyTests.choose
    simple_prestart=journey.DriverJourneyTests.simple_prestart
    scan=journey.DriverJourneyTests.scan
    action=journey.DriverJourneyTests.action

    def ready(self):
        self.start();self.accept_overview();self.choose();self.simple_prestart()

    def later(self, minutes):
        self.time+=timedelta(minutes=minutes)

    def values(self):
        return [timing_values(r,self.time.isoformat()) for r in self.rows('''SELECT t.*,d.break_started_at,
            d.break_finished_at,d.finished_at AS day_finished_at FROM bf_step_timings t
            JOIN bf_workdays d ON d.id=t.workday_id ORDER BY t.id''')]

    def report(self, extra=''):
        return self.client.get('/binflow/reports?start='+RUN_DATE+'&end='+RUN_DATE+extra)

    def test_four_stages_exact_durations_duplicates_filters_and_csv(self):
        self.ready();self.later(4);self.scan('load','BF000003')
        self.later(6);self.scan('load','BF000004')
        self.later(3);self.action('depart');self.later(25);self.action('client-arrival',job_id=2)
        self.later(2);self.scan('deliver','BF000003');self.later(3);self.scan('deliver','BF000004')
        self.later(4);self.scan('collect','BF000001');self.later(2);self.scan('collect','BF000002')
        before=self.rows('SELECT * FROM bf_step_timings')
        self.later(1);self.assertTrue(self.scan('collect','BF000002').json['duplicate'])
        self.assertEqual(before,self.rows('SELECT * FROM bf_step_timings'))
        self.later(20);self.action('depot-arrival',warehouse_id=1)
        self.later(1);self.scan('return','BF000001');self.later(3);self.scan('return','BF000002')
        values=self.values()
        self.assertEqual([(r['phase'],r['seconds'],r['bin_count'],r['eligible']) for r in values],
                         [('load',600,2,True),('deliver',300,2,True),('collect',360,2,True),('return',240,2,True)])
        self.post('/logout');self.login()
        page=self.report();self.assertEqual(page.status_code,200)
        self.assertIn(b'Driver step timings',page.data);self.assertIn(b'>Reports</a>',page.data)
        self.assertIn(b'10m 00s',page.data);self.assertIn(b'5m 00s / bin',page.data)
        for filter in ('&state=NSW','&driver=99999','&truck=2'):
            self.assertIn(b'No timings recorded',self.report(filter).data)
        exported=list(csv.DictReader(io.StringIO(self.report('&export=csv&driver='+str(self.uid)).data.decode('utf-8-sig'))))
        self.assertEqual(len(exported),4)
        self.assertEqual(sorted(int(r['Duration seconds (break excluded)']) for r in exported),[240,300,360,600])
        self.assertTrue(all(r['Included in averages']=='Yes' for r in exported))
        self.assertEqual(self.client.get('/binflow/reports?start=bad').status_code,400)
        self.assertEqual(self.client.get('/binflow/reports?start=2020-01-01&end=2026-01-01').status_code,400)
        with sqlite3.connect(self.path) as conn:
            conn.execute("UPDATE bf_step_timings SET location_name='=1+1'")
        csv_rows=list(csv.DictReader(io.StringIO(self.report('&export=csv').data.decode('utf-8-sig'))))
        self.assertTrue(all(r['Client / depot']=="'=1+1" for r in csv_rows))

    def test_recorded_break_is_excluded_and_in_progress_not_averaged(self):
        self.ready();self.later(2)
        wid=self.rows('SELECT id FROM bf_workdays')[0]['id']
        self.post(f'/binflow/driver/my-day/{wid}/break/start');self.later(30)
        current=self.values()[0]
        self.assertEqual(current['seconds'],120);self.assertFalse(current['eligible'])
        self.post(f'/binflow/driver/my-day/{wid}/break/finish');self.later(3)
        self.scan('load','BF000003');self.scan('load','BF000004')
        result=self.values()[0]
        self.assertEqual((result['seconds'],result['break_seconds'],result['eligible']),(300,1800,True))

    def test_inflight_stage_is_partial_and_rejected_scans_cannot_start_timing(self):
        self.ready()
        with sqlite3.connect(self.path) as conn:
            conn.execute('DELETE FROM bf_step_timings')  # Represents a run underway at rollout.
        self.assertEqual(self.scan('deliver','BF000003').status_code,409)
        self.assertEqual(self.scan('load','not-a-bin').status_code,409)
        self.assertFalse(self.rows('SELECT * FROM bf_step_timings'))
        self.later(10);self.scan('load','BF000003');self.later(5);self.scan('load','BF000004')
        row=self.values()[0]
        self.assertEqual((row['seconds'],row['status'],row['eligible']),(300,'Partial',False))
        self.post('/logout');self.login()
        self.assertIn(b'0 complete',self.report().data)

    def test_reports_and_exports_are_admin_only(self):
        for role in ('driver','contractor','manager','labour'):
            with sqlite3.connect(self.path) as conn:
                conn.execute('UPDATE users SET business_role=? WHERE id=?',(role,self.uid))
            self.assertEqual(self.report().status_code,403,role)
            self.assertEqual(self.report('&export=csv').status_code,403,role)
            if role=='manager':
                page=self.client.get('/admin/bin-management')
                self.assertEqual(page.status_code,200)
                self.assertNotIn(b'>Reports</a>',page.data)
        self.post('/logout')
        self.assertEqual(self.report().status_code,302)

    def test_pickup_only_single_bin_has_no_empty_loading_or_delivery_stage(self):
        with sqlite3.connect(self.path) as conn:
            conn.execute("DELETE FROM bf_job_bins WHERE job_id=2 AND (direction='deliver' OR bin_id=2)")
        self.ready()
        self.assertFalse(self.rows('SELECT * FROM bf_step_timings'))
        self.action('depart');self.later(15);self.action('client-arrival',job_id=2)
        self.later(7);self.scan('collect','BF000001')
        self.later(20);self.action('depot-arrival',warehouse_id=1)
        self.later(2);self.scan('return','BF000001')
        self.assertEqual([(r['phase'],r['seconds'],r['bin_count'],r['eligible']) for r in self.values()],
                         [('collect',420,1,True),('return',120,1,True)])

    def test_daylight_saving_uses_elapsed_time_and_driver_timezone(self):
        self.ready()
        row=self.rows("SELECT t.*,NULL AS day_finished_at,NULL AS break_started_at,NULL AS break_finished_at FROM bf_step_timings t")[0]
        row.update(started_at='2026-10-04T01:55:00+10:00',finished_at='2026-10-04T03:05:00+11:00',timezone='Australia/Sydney')
        result=timing_values(row,self.time.isoformat())
        self.assertEqual(result['seconds'],600)
        self.assertIn('01:55:00 AEST',result['start_local'])
        self.assertIn('03:05:00 AEDT',result['end_local'])
