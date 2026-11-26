import json
import unittest
from datetime import date

import test_binflow_schedule as schedule
from binflow_calendar_reset import preview
from binflow_route_calendar import RESET_ID, view_data


class CalendarResetTests(unittest.TestCase):
    for name in ('setUp','tearDown','rows','post','login','job','connection','user','as_user','plan','save','visit_plan','prepare_job'):
        locals()[name]=getattr(schedule.WeeklyScheduleTests,name)
    route='/binflow/collection-calendar/clear-planning'

    def reset(self,token=None,confirm='clear-planning'):
        if token is None:
            with self.connection() as conn:token=preview(conn)['token']
        return self.post(self.route,dict(token=token,confirm=confirm),follow=False)

    def test_all_states_clear_saved_plans_stop_future_suggestions_and_allow_manual_work(self):
        for state in ('QLD','NSW','VIC','SA','WA','TAS','ACT','NT'):
            plan=self.plan(state)['plan']
            for day in plan['days']:day['items']=[]
            plan['days'][0]['items']=[dict(id='baling',kind='baling',time='06:30',notes='Prepare')]
            self.assertEqual(self.save(plan,state=state).status_code,200)
        before=self.rows('SELECT * FROM bf_client_site_profiles')
        weeks=self.rows('SELECT * FROM bf_schedule_weeks')
        page=self.client.get(self.route)
        self.assertEqual(page.status_code,200,page.text)
        self.assertIn('8 saved planning items',page.text)
        self.assertEqual(weeks,self.rows('SELECT * FROM bf_schedule_weeks'))
        self.assertEqual(self.reset().status_code,302)
        self.assertEqual(len(self.rows('SELECT * FROM bf_rollout_states')),8)
        self.assertEqual(before,self.rows('SELECT * FROM bf_client_site_profiles'))
        for row in self.rows('SELECT * FROM bf_schedule_weeks'):
            self.assertTrue(all(not d['items'] for d in json.loads(row['plan_json'])['days']))
            self.assertEqual(row['revision'],2)
        with self.connection() as conn:
            data=view_data(conn,date(2027,2,1),date(2027,2,7),'')
            self.assertEqual(set(data['calendar_events']),{'2027-02-03'})
            self.assertEqual(len(data['calendar_events']['2027-02-03']),8)
            self.assertTrue(all(e['day']['baling'] for e in data['calendar_events']['2027-02-03']))
        manual=self.visit_plan()
        self.assertEqual(self.save(manual,revision=2,plan_token=self.plan()['plan_token']).status_code,200)
        self.assertTrue(self.plan()['plan']['days'][0]['items'])
        audit=self.rows("SELECT * FROM bf_calendar_rebuilds WHERE id LIKE 'manual-planning-reset-%'")
        self.assertEqual(len(audit),1)
        self.assertEqual(len(json.loads(audit[0]['previous_plan'])['weeks']),8)

    def test_jobs_reservations_and_completed_day_plans_are_retained(self):
        plan=self.prepare_job()
        plan['days'][0]['items']=[dict(id='baling',kind='baling',time='06:30',notes='')]
        plan['days'][1]['items'].append(dict(id='protected-baling',kind='baling',time='06:30',notes=''))
        driver=self.user()
        self.assertEqual(self.save(plan,action='approve',driver=driver).status_code,200)
        with self.connection() as conn:
            conn.execute("UPDATE bf_jobs SET status='complete',pod_signature=x'89504e47',pod_signed_at='2026-10-27T09:00:00' WHERE id=1")
        jobs=self.rows('SELECT * FROM bf_jobs');bins=self.rows('SELECT * FROM bf_job_bins')
        self.assertEqual(self.reset().status_code,302)
        self.assertEqual(jobs,self.rows('SELECT * FROM bf_jobs'))
        self.assertEqual(bins,self.rows('SELECT * FROM bf_job_bins'))
        plan=self.plan()['plan']
        self.assertFalse(plan['days'][0]['items'])
        self.assertEqual([i['kind'] for i in plan['days'][1]['items']],['job','baling'])

    def test_stale_confirmation_permissions_and_csrf(self):
        self.save(self.visit_plan())
        with self.connection() as conn:token=preview(conn)['token']
        changed=self.plan()['plan'];changed['days'][4]['notes']='New instruction'
        self.save(changed,revision=1)
        before=self.rows('SELECT * FROM bf_schedule_weeks')
        self.assertEqual(self.reset(token).status_code,409)
        self.assertEqual(self.reset(confirm='').status_code,400)
        self.assertEqual(self.client.post(self.route,data={}).status_code,400)
        self.assertEqual(before,self.rows('SELECT * FROM bf_schedule_weeks'))
        self.assertFalse(self.rows('SELECT * FROM bf_rollout_states'))
        for role in ('manager','contractor','driver'):
            self.as_user(self.user(role,role=role))
            self.assertEqual(self.client.get(self.route).status_code,403)
            self.assertEqual(self.reset(token).status_code,403)

    def test_button_on_both_calendars_admin_only(self):
        page=self.client.get('/binflow/collection-calendar').text
        self.assertIn(self.route,page)
        with self.connection() as conn:
            conn.execute("INSERT OR IGNORE INTO bf_calendar_rebuilds VALUES(?,?,'[]','{}','now')",(RESET_ID,'2026-10-26'))
        self.assertIn(self.route,self.client.get('/binflow/collection-calendar').text)
        self.as_user(self.user('Manager',role='manager'))
        self.assertNotIn(self.route,self.client.get('/binflow/collection-calendar').text)


if __name__=='__main__':unittest.main()
