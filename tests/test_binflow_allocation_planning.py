import re
import secrets
import unittest
from unittest.mock import patch
from datetime import date

import test_binflow_schedule as schedule
from binflow_allocation_planning import ordered_sites, day_estimate
from binflow_route_calendar import RESET_ID


class AllocationPlanningTests(unittest.TestCase):
    for name in ('setUp','tearDown','rows','post','login','job','connection','user','as_user','plan','save'):
        locals()[name]=getattr(schedule.WeeklyScheduleTests,name)
    route='/binflow/pickups/new'

    def prepare(self):
        with self.connection() as conn:
            conn.execute("INSERT INTO bf_calendar_rebuilds VALUES(?,?,'[]','{}','now')",(RESET_ID,'2026-10-26'))
            conn.execute("INSERT INTO bf_rollout_states VALUES('QLD','2026-10-26',1)")
            for lid,lat,lon in [(1,-27.46,153.02),(3,-28.0,153.4),(4,-27.56,151.95)]:
                conn.execute("INSERT INTO bf_geofences VALUES(?,?,?,100,'now','Admin')",(lid,lat,lon))
            for lid in (3,4):conn.execute('INSERT INTO bf_site_requirements VALUES(?,(SELECT id FROM bf_sizes WHERE litres=1100),10)',(lid,))

    def test_popup_links_selected_state_and_only_that_states_clients_and_depots(self):
        self.prepare()
        page=self.client.get('/binflow/collection-calendar?state=NSW&date=2026-11-30').text
        self.assertIn('state=NSW&amp;calendar=1',page)
        self.assertIn('data-allocation-popup',page)
        self.assertIn('data-allocation-dialog',page)
        qld=self.client.get(self.route+'?calendar=1&state=QLD&date=2026-11-30').text
        selector=re.search(r'<select name="site".*?</select>',qld,re.S)[0]
        self.assertIn('Alpha QLD',selector);self.assertIn('Beta QLD',selector);self.assertNotIn('Alpha NSW',selector)
        self.assertLess(selector.index('Beta QLD'),selector.index('Alpha QLD'))
        self.assertNotIn('NSW warehouse',qld)
        self.assertIn('name="date" value="2026-11-30"',qld)
        empty=self.client.get(self.route+'?calendar=1&date=2026-11-30').text
        selector=re.search(r'<select name="site".*?</select>',empty,re.S)[0]
        self.assertNotIn('Alpha QLD',selector);self.assertNotIn('Alpha NSW',selector)
        for query in ('state=QLD&site=5','state=QLD&warehouse=2','state=BAD'):
            self.assertEqual(self.client.get(self.route+'?calendar=1&'+query).status_code,400)

    def test_distance_unknown_last_and_day_estimate_includes_returns_and_multiple_loads(self):
        self.prepare()
        p=self.plan(week='2026-11-30');p['plan']['days'][0]['items']=[dict(id='alpha',kind='visit',profile_id=1,time='',notes='')]
        self.assertEqual(self.save(p['plan'],week='2026-11-30',plan_token=p['plan_token']).status_code,200)
        with self.app.test_request_context():
            from flask import g
            g.user=self.rows('SELECT * FROM users WHERE id=1')[0]
            with self.connection() as conn:
                warehouse=conn.execute('SELECT * FROM bf_locations WHERE id=1').fetchone()
                sites=conn.execute("SELECT l.*,c.name client_name FROM bf_locations l JOIN bf_clients c ON c.id=l.client_id WHERE l.state='QLD' AND l.kind='client'").fetchall()
                ordered,geo,depot=ordered_sites(conn,sites,warehouse)
                self.assertEqual([r['id'] for r in ordered],[4,3])
                estimate=day_estimate(conn,'QLD','2026-11-30',ordered,warehouse,ordered[0],geo,depot)
                self.assertEqual([s['id'] for s in estimate['stops']],[4,3])
                self.assertEqual(estimate['loads'],2)
                self.assertGreater(estimate['travel'],ordered[0]['drive_minutes'])
                self.assertEqual(estimate['minutes'],estimate['travel']+estimate['service']+estimate['handling']+75)
                self.assertFalse(estimate['missing']);self.assertTrue(estimate['finish'])
                conn.execute('DELETE FROM bf_geofences WHERE location_id=4')
                ordered,geo,depot=ordered_sites(conn,sites,warehouse)
                self.assertEqual(ordered[-1]['id'],4);self.assertIsNone(ordered[-1]['distance_km'])
                estimate=day_estimate(conn,'QLD','2026-11-30',ordered,warehouse,ordered[-1],geo,depot)
                self.assertIn('Beta QLD',estimate['missing']);self.assertIsNone(estimate['finish'])

    def test_submission_keeps_existing_allocation_flow_and_rejects_cross_state(self):
        self.prepare()
        response=self.client.get(self.route+'?calendar=1&state=QLD&date=2026-11-30&site=3')
        self.assertEqual(response.status_code,200,response.text)
        self.assertIn('Suggested farthest-first day',response.text)
        data=dict(calendar='1',state='QLD',request_key=secrets.token_urlsafe(24),site_id=3,warehouse_id=1,
                  scheduled_date='2026-11-30',deliver=[1])
        result=self.post(self.route,data,follow=False)
        self.assertEqual(result.status_code,302)
        self.assertTrue(result.location.endswith('/binflow/pickups/1'))
        self.assertEqual(self.rows('SELECT state,scheduled_date,approval_status FROM bf_jobs'),
                         [dict(state='QLD',scheduled_date='2026-11-30',approval_status='pending')])
        bad=dict(data,request_key=secrets.token_urlsafe(24),state='NSW',deliver=[2])
        result=self.post(self.route,bad,follow=False)
        self.assertIn('state=NSW',result.location)
        self.assertEqual(len(self.rows('SELECT * FROM bf_jobs')),1)
        self.assertEqual(self.client.post(self.route,data=data).status_code,400)
        self.as_user(self.user('Driver',role='driver'))
        self.assertEqual(self.client.get(self.route+'?calendar=1&state=QLD').status_code,403)


if __name__=='__main__':unittest.main()
