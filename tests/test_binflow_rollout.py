import json
import secrets
import unittest
from datetime import date

import test_binflow_route_calendar as calendar_tests
from binflow_rollout import register_view, state_starts
from binflow_route_calendar import rebuild, week_plan
from binflow_schedule import get_plan


class ManualRolloutTests(unittest.TestCase):
    setUp = calendar_tests.CurrentCalendarTests.setUp
    tearDown = calendar_tests.CurrentCalendarTests.tearDown
    login = calendar_tests.CurrentCalendarTests.login
    post = calendar_tests.CurrentCalendarTests.post
    rows = calendar_tests.CurrentCalendarTests.rows
    prepare = calendar_tests.CurrentCalendarTests.prepare
    replace = calendar_tests.CurrentCalendarTests.replace
    payload = calendar_tests.CurrentCalendarTests.payload
    encoded = calendar_tests.CurrentCalendarTests.encoded
    connection = calendar_tests.CurrentCalendarTests.connection
    save_target = calendar_tests.CurrentCalendarTests.save_target
    week = date(2026, 11, 2)
    route = '/binflow/collection-calendar/rollout?state=QLD&date=2026-11-02'

    def start(self):
        self.prepare()
        with self.connection() as conn:
            rebuild(conn,'2026-10-24T12:00:00')
        response = self.post(self.route, {'action':'start'})
        self.assertIn('Manual planning for QLD starts', response.text)
        self.assertIn('Needs empty bins', response.text)

    def test_preloads_stop_from_selected_week_only_and_saved_weeks_survive(self):
        self.start()
        with self.connection() as conn:
            old = week_plan(conn, date(2026,10,26), 'QLD')[0]
            self.assertTrue(any(d['baling'] for d in old['days']))
            self.assertFalse(any(d['items'] for d in get_plan(conn,'QLD','2026-11-02')[1]['days']))
            self.assertTrue(any(d['items'] for d in get_plan(conn,'NSW','2026-11-02')[1]['days']))
            self.assertFalse(any(d['items'] for d in get_plan(conn,'QLD','2027-01-04')[1]['days']))
            self.assertEqual(len(register_view(conn,self.week,'',state_starts(conn))),3)
        response = self.client.get('/binflow/schedule/options?state=QLD&date=2026-11-02').json
        plan = response['plan']
        plan['days'][0]['items'].append(dict(kind='baling',id='manual-depot',time='',notes=''))
        saved = self.post('/binflow/schedule/save',dict(state='QLD',week='2026-11-02',action='save',revision=0,plan_token=response['plan_token'],plan=json.dumps(plan)))
        self.assertEqual(saved.status_code,200)
        self.post(self.route,{'action':'start'})
        self.assertIn('Review 1 existing saved weeks', self.client.get(self.route).text)
        delivery = dict(purpose='empty_delivery',request_key=secrets.token_urlsafe(24),site_id=3,warehouse_id=1,
                        scheduled_date='2026-11-03',deliver=['1'])
        self.post('/binflow/pickups/new',delivery)
        options = self.client.get('/binflow/schedule/options?state=QLD&date=2026-11-02').json
        self.assertEqual(options['plan']['days'][0]['items'][0]['id'],'manual-depot')
        self.assertIn('Empty-bin delivery',options['plan']['days'][1]['items'][0]['title'])

    def test_partial_delivery_and_cadence_coverage_follow_actual_bins_and_jobs(self):
        self.start()
        self.save_target(3,2,0,version=3)
        with self.connection() as conn:
            conn.execute("UPDATE bf_client_site_profiles SET schedule='Fortnightly Tuesday' WHERE location_id=3")
        delivery = dict(purpose='empty_delivery',site_id='3',warehouse_id='1',scheduled_date='2026-11-02',deliver=['1'])
        def create(**changes):
            return self.post('/binflow/pickups/new',{**delivery,'request_key':secrets.token_urlsafe(24),**changes})
        self.assertNotIn('2. Collect from client', self.client.get('/binflow/pickups/new?purpose=empty_delivery&site=3').text)
        self.assertIn('Select at least one bin to collect',create(purpose='scheduled_pickup').text)
        self.assertIn('deliveries only', create(collect=['2']).text)
        self.assertIn('awaiting approval',create().text)
        self.assertIn('More empties needed',self.client.get(self.route).text)
        self.assertIn('exceed the remaining site target',create(deliver=['2','3']).text)
        self.assertEqual(len(self.rows('SELECT * FROM bf_jobs')),1)
        self.assertIn('awaiting approval',create(deliver=['2']).text)
        self.assertIn('Awaiting empty-bin delivery',self.client.get(self.route).text)
        with self.connection() as conn:
            pid = conn.execute('SELECT id FROM bf_client_site_profiles WHERE location_id=3').fetchone()[0]
        anchor = dict(action='anchor',profile_id=pid,first_pickup='2026-11-10',schedule='Fortnightly Tuesday',previous='')
        self.assertIn('Confirm the state rollout',self.post(self.route,anchor).text)
        # Reproduce custody following the existing driver handover; do not hand-edit the rollout record.
        with self.connection() as conn:
            conn.execute("UPDATE bf_jobs SET approval_status='approved',status='complete'")
            conn.execute("UPDATE bf_job_bins SET active=0,delivered_at='2026-11-02T14:00'")
            conn.execute("UPDATE bf_bins SET status='at_client',location_id=3 WHERE id IN (1,2)")
        self.assertIn('matching the agreed service day',self.post(self.route,{**anchor,'first_pickup':'2026-11-09'}).text)
        self.assertIn('Pickup needs booking',self.post(self.route,anchor).text)
        with self.connection() as conn:
            def coverage(week):
                return next(r for r in register_view(conn,date.fromisoformat(week),'QLD',state_starts(conn)) if r['profile']['id']==pid)
            self.assertEqual([d['date'] for d in coverage('2026-11-09')['due']],['2026-11-10'])
            self.assertEqual(coverage('2026-11-16')['due'],[])
            self.assertEqual([d['date'] for d in coverage('2026-11-23')['due']],['2026-11-24'])
        create(purpose='',collect=['1'],deliver=[],scheduled_date='2026-11-10')
        with self.connection() as conn:
            self.assertEqual(coverage('2026-11-09')['service'],'Allocated this week')
            conn.execute("UPDATE bf_jobs SET status='cancelled' WHERE scheduled_date='2026-11-10'")
            self.assertEqual(coverage('2026-11-09')['service'],'Pickup needs booking')
            conn.execute("UPDATE bf_client_site_profiles SET schedule='On request' WHERE id=?",(pid,))
            self.assertEqual(coverage('2026-11-09')['service'],'On request')
