import json
import re
import unittest
from datetime import date

import test_binflow_schedule as schedule
from binflow_calendar_cycles import pattern, due, next_dates
from binflow_calendar_reset import preview
from binflow_route_calendar import RESET_ID, view_data


class CyclePatternTests(unittest.TestCase):
    def test_supported_frequencies_and_month_boundaries(self):
        for text,gap in [('Weekly',7),('Fortnightly',14),('Every 4 weeks',28),('Every 8 weeks',56)]:
            rule=pattern(dict(schedule=text),date(2026,11,30))
            dates=next_dates(rule)
            self.assertEqual((date.fromisoformat(dates[1])-date.fromisoformat(dates[0])).days,gap)
        self.assertEqual(next_dates(pattern(dict(schedule='Monthly'),date(2027,1,31)))[:3],['2027-01-31','2027-02-28','2027-03-31'])
        rule=pattern(dict(schedule='Last Monday of the month'),date(2026,11,30))
        self.assertEqual(next_dates(rule)[:3],['2026-11-30','2026-12-28','2027-01-25'])
        self.assertEqual(len(next_dates(pattern(dict(schedule='Ad hoc'),date(2026,11,30)))),1)
        self.assertTrue(due(pattern(dict(schedule='Every day'),date(2026,11,30)),date(2026,12,6)))
        self.assertFalse(due(pattern(dict(schedule='Weekdays'),date(2026,11,30)),date(2026,12,6)))
        for text in ('Twice weekly','Paused','Unconfirmed'):
            with self.assertRaises(ValueError):pattern(dict(schedule=text),date(2026,11,30))
        rule=pattern(dict(schedule='Twice weekly'),date(2026,11,30),[0,3])
        self.assertEqual(next_dates(rule)[:3],['2026-11-30','2026-12-03','2026-12-07'])
        with self.assertRaises(ValueError):pattern(dict(schedule='Weekly — Tuesday'),date(2026,11,30))


class CalendarCycleTests(unittest.TestCase):
    for name in ('setUp','tearDown','rows','post','login','job','connection','user','as_user','plan','save','visit_plan','prepare_job'):
        locals()[name]=getattr(schedule.WeeklyScheduleTests,name)
    route='/binflow/collection-calendar/clients/1/add'

    def prepare(self):
        with self.connection() as conn:
            conn.execute("INSERT INTO bf_calendar_rebuilds VALUES(?,?,'[]','{}','now')",(RESET_ID,'2026-10-26'))
            conn.execute("UPDATE bf_client_site_profiles SET schedule='Fortnightly' WHERE id=1")
            for state in ('QLD','NSW','VIC','SA','WA','TAS','ACT','NT'):
                conn.execute("INSERT INTO bf_rollout_states VALUES(?,'2026-10-26',1)",(state,))

    def add(self,first='2026-11-30',token=None):
        page=self.client.get(self.route+'?date='+first)
        self.assertEqual(page.status_code,200,page.text)
        token=token or re.search(r'name="token" value="([^"]+)"',page.text)[1]
        self.last_token=token
        return self.post(self.route,dict(date=first,token=token),follow=False)

    def test_repeats_across_future_unsaved_and_saved_weeks_and_no_duplicate_jobs(self):
        self.prepare()
        p=self.plan(week='2026-11-30');self.save(p['plan'],week='2026-11-30',plan_token=p['plan_token'])
        jobs=self.rows('SELECT * FROM bf_jobs');bins=self.rows('SELECT * FROM bf_bins')
        result=self.add();self.assertEqual(result.status_code,302,result.text)
        self.assertIn('view=week',result.location)
        self.assertEqual(jobs,self.rows('SELECT * FROM bf_jobs'));self.assertEqual(bins,self.rows('SELECT * FROM bf_bins'))
        self.assertEqual(len(self.plan(week='2026-11-30')['plan']['days'][0]['items']),1)
        self.assertFalse(self.plan(week='2026-12-07')['plan']['days'][0]['items'])
        self.assertEqual(len(self.plan(week='2026-12-14')['plan']['days'][0]['items']),1)
        self.assertEqual(len(self.plan(week='2027-11-29')['plan']['days'][0]['items']),1)
        self.assertEqual(self.post(self.route,dict(date='2026-11-30',token=self.last_token),follow=False).status_code,409)
        self.assertEqual(len(self.rows('SELECT * FROM bf_calendar_cycles')),1)
        self.job(deliver=[1],legacy=False)
        with self.connection() as conn:
            conn.execute("UPDATE bf_jobs SET scheduled_date='2026-11-30' WHERE id=1")
        items=self.plan(week='2026-11-30')['plan']['days'][0]['items']
        self.assertEqual([i['kind'] for i in items],['job'])

    def test_existing_published_week_gets_cycle_with_revision_and_driver_visibility(self):
        self.prepare();driver=self.user()
        p=self.plan(week='2026-11-30')
        p['plan']['days'][2]['items']=[dict(id='depot',kind='baling',time='06:30',notes='Keep depot work')]
        result=self.save(p['plan'],week='2026-11-30',action='approve',driver=driver,plan_token=p['plan_token'])
        self.assertEqual(result.status_code,200,result.json)
        self.assertEqual(self.add().status_code,302)
        row=self.rows("SELECT * FROM bf_schedule_weeks WHERE week_start='2026-11-30'")[0]
        self.assertEqual((row['status'],row['revision']),('published',2))
        plan=json.loads(row['plan_json'])
        self.assertEqual(plan['days'][0]['items'][0]['profile_id'],1)
        self.assertEqual(plan['days'][2]['items'][0]['kind'],'baling')
        self.as_user(driver)
        self.assertIn('Alpha QLD',self.client.get('/binflow/driver/my-day?summary=1&date=2026-11-30').text)

    def test_manual_removal_does_not_regenerate_and_reset_disables_all_cycles(self):
        self.prepare();self.add()
        p=self.plan(week='2026-11-30');p['plan']['days'][0]['items']=[]
        self.assertEqual(self.save(p['plan'],week='2026-11-30',revision=p['revision'],plan_token=p['plan_token']).status_code,200)
        self.assertFalse(self.plan(week='2026-11-30')['plan']['days'][0]['items'])
        self.assertTrue(self.plan(week='2026-12-14')['plan']['days'][0]['items'])
        with self.connection() as conn:token=preview(conn)['token']
        self.post('/binflow/collection-calendar/clear-planning',dict(token=token,confirm='clear-planning'))
        self.assertEqual(self.rows('SELECT active FROM bf_calendar_cycles'),[dict(active=0)])
        self.assertFalse(self.plan(week='2026-12-14')['plan']['days'][0]['items'])

    def test_list_order_columns_icon_and_stale_profile(self):
        self.prepare()
        page=self.client.get('/binflow/collection-calendar?date=2026-11-30').text
        self.assertLess(page.index('Current client schedules'),page.index('Ad hoc / schedules to confirm'))
        for label in ('First cycle week','1100L / 240L','State depots, staffing &amp; weekly checks'):
            self.assertNotIn(label,page)
        for label in ('<th>240L bins</th>','<th>1100L bins</th>',self.route):self.assertIn(label,page)
        page=self.client.get(self.route+'?date=2026-11-30').text
        token=re.search(r'name="token" value="([^"]+)"',page)[1]
        with self.connection() as conn:conn.execute("UPDATE bf_client_site_profiles SET schedule='Weekly' WHERE id=1")
        self.assertEqual(self.add(token=token).status_code,409)
        self.assertFalse(self.rows('SELECT * FROM bf_calendar_cycles'))
        self.assertEqual(self.client.post(self.route,data={}).status_code,400)
        self.as_user(self.user('Manager',role='manager'))
        self.assertEqual(self.client.get(self.route).status_code,403)
        self.assertEqual(self.post(self.route,dict(date='2026-11-30',token=token),follow=False).status_code,403)


if __name__=='__main__':unittest.main()
