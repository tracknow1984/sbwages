"""Calendar plan security, publication, custody and concurrent-edit regression tests."""
import copy
import json
import re
import sqlite3
import unittest
from datetime import date

import test_binflow as legacy
from binflow_acceptance import manifest
from binflow_runs import run_jobs
from binflow_route_calendar import RESET_ID, view_data


class WeeklyScheduleTests(unittest.TestCase):
    setUp=legacy.BinFlowTests.setUp
    tearDown=legacy.BinFlowTests.tearDown
    rows=legacy.BinFlowTests.rows
    post=legacy.BinFlowTests.post
    login=legacy.BinFlowTests.login
    job=legacy.BinFlowTests.job

    def connection(self):
        conn=sqlite3.connect(self.path);conn.row_factory=sqlite3.Row;conn.execute('PRAGMA foreign_keys=ON');return conn

    def user(self,name='Alice',state='QLD',role='driver'):
        with self.connection() as conn:
            return conn.execute('''INSERT INTO users(role,first_name,last_name,email,username,password_hash,business_role,working_state)
                VALUES('employee',?,'Example',?,?,'unused',?,?)''',(name,name+'@example.com',name,role,state)).lastrowid

    def as_user(self,uid):
        with self.client.session_transaction() as session:
            session.clear();session['uid']=uid;session['version']=self.rows('SELECT auth_version FROM users WHERE id=?',(uid,))[0]['auth_version']

    def plan(self,state='QLD',week='2026-10-26'):
        result=self.client.get(f'/binflow/schedule/options?date={week}&state={state}')
        self.assertEqual(result.status_code,200)
        return result.json

    def save(self,plan,revision=0,action='save',driver=None,state='QLD',week='2026-10-26',**extra):
        return self.post('/binflow/schedule/save',dict(plan=json.dumps(plan),revision=str(revision),state=state,week=week,
            action=action,driver_id=driver or '',reviewed='yes',**extra),follow=False)

    def visit_plan(self):
        plan=self.plan()['plan']
        for d in plan['days']:d['items']=[]
        plan['days'][0]['items']=[dict(id='new-visit',kind='visit',profile_id=1,time='07:00',notes='Front gate')]
        return plan

    def test_admin_only_mutation_and_drafts_hidden_from_drivers(self):
        driver=self.user();manager=self.user('Manager',role='manager');plan=self.visit_plan()
        self.assertFalse(self.rows('SELECT * FROM bf_schedule_weeks'))
        saved=self.save(plan);self.assertEqual(saved.status_code,200,saved.json)
        self.assertEqual(self.rows('SELECT status,revision FROM bf_schedule_weeks'),[dict(status='draft',revision=1)])
        self.as_user(driver)
        page=self.client.get('/binflow/calendar?date=2026-10-26').data
        self.assertNotIn(b'Alpha QLD',page);self.assertNotIn(b'week-editor-data',page)
        self.assertIn(b'No jobs scheduled',page)
        self.assertEqual(self.client.get('/binflow/schedule/options?date=2026-10-26&state=QLD').status_code,403)
        self.assertEqual(self.client.get('/binflow/schedule/version?date=2026-10-26').json,{'revision':0})
        self.assertEqual(self.client.get('/binflow/schedule?state=NSW').status_code,403)
        self.assertEqual(self.save(plan,1,action='approve',driver=driver).status_code,403)
        self.assertEqual(self.post('/binflow/driver/1/schedule',{},follow=False).status_code,403)
        self.as_user(manager)
        page=self.client.get('/binflow/collection-calendar?date=2026-10-26&state=QLD').data
        self.assertIn(b'Alpha QLD',page);self.assertNotIn(b'data-calendar-editor',page)
        self.assertEqual(self.save(plan,1).status_code,403)
        self.assertEqual(self.post('/binflow/pickups/1/cancel',{'reason':'test'},follow=False).status_code,403)
        self.as_user(1)
        self.assertEqual(self.client.post('/binflow/schedule/save',data={'state':'QLD'}).status_code,400)

    def test_publish_assigned_driver_readonly_republish_and_reassign(self):
        driver=self.user();other=self.user('Bob');plan=self.visit_plan()
        before=self.rows('SELECT * FROM bf_bins')
        response=self.save(plan,action='approve',driver=driver)
        self.assertEqual(response.status_code,200,response.json)
        row=self.rows('SELECT * FROM bf_schedule_weeks')[0]
        self.assertTrue(row['approved_at']);self.assertEqual(row['approved_by'],1)
        self.assertEqual(before,self.rows('SELECT * FROM bf_bins'))
        self.as_user(driver)
        page=self.client.get('/binflow/calendar?date=2026-10-26').data
        self.assertIn(b'Alpha QLD',page);self.assertIn(b'Front gate',page)
        self.assertNotIn(b'data-week-save',page);self.assertNotIn(b'draggable=',page)
        self.assertIn('Front gate',self.client.get('/binflow/calendar/state?date=2026-10-26').json['html'])
        self.as_user(other);self.assertNotIn(b'Alpha QLD',self.client.get('/binflow/calendar?date=2026-10-26').data)
        self.as_user(1);plan=self.plan()['plan']
        plan['days'][3]['items']=plan['days'][0]['items'];plan['days'][0]['items']=[]
        plan['days'][3]['notes']='Updated by admin'
        self.assertEqual(self.save(plan,1).status_code,200)
        self.as_user(driver)
        self.assertEqual(self.client.get('/binflow/schedule/version?date=2026-10-26').json,dict(revision=2))
        page=self.client.get('/binflow/calendar?date=2026-10-29').data
        self.assertRegex(page,rb'data-date="2026-10-29".*?Alpha QLD');self.assertIn(b'Updated by admin',page)
        self.as_user(1);self.assertEqual(self.save(plan,2,'approve',other).status_code,200)
        self.as_user(driver);self.assertEqual(self.client.get('/binflow/schedule/version?date=2026-10-26').json,dict(revision=0))
        self.as_user(other);self.assertIn(b'Alpha QLD',self.client.get('/binflow/calendar?date=2026-10-29').data)
        history=self.rows('SELECT revision,actor_id,action FROM bf_schedule_history ORDER BY revision')
        self.assertEqual([h['revision'] for h in history],[1,2,3]);self.assertEqual({h['actor_id'] for h in history},{1})

    def test_eligible_driver_state_and_duplicate_or_foreign_jobs_rejected(self):
        foreign=self.user('NSW','NSW');manager=self.user('Office',role='manager');plan=self.visit_plan()
        for uid in (foreign,manager,1,999):
            result=self.save(plan,action='approve',driver=uid)
            self.assertEqual(result.status_code,400,result.json)
        self.assertFalse(self.rows('SELECT * FROM bf_schedule_weeks'))
        bad=copy.deepcopy(plan);bad['days'][0]['items'][0]['profile_id']=3
        self.assertEqual(self.save(bad).status_code,400)
        bad=copy.deepcopy(plan);bad['days'][1]['items']=bad['days'][0]['items']
        self.assertEqual(self.save(bad).status_code,400)
        bad=copy.deepcopy(plan);bad['days'][0]['date']='2026-11-02'
        self.assertEqual(self.save(bad).status_code,400)
        bad=copy.deepcopy(plan);bad['days'][0]['items'][0]['time']='19:00'
        self.assertEqual(self.save(bad).status_code,400)
        bad=copy.deepcopy(plan);bad['days'][0]['items'][0]['title']='<script>unsafe</script>'
        self.assertEqual(self.save(bad).status_code,200)
        self.assertNotIn('<script>',self.rows('SELECT plan_json FROM bf_schedule_weeks')[0]['plan_json'])

    def test_concurrent_stale_save_and_approval_do_not_overwrite(self):
        driver=self.user();plan=self.visit_plan()
        self.assertEqual(self.save(plan).status_code,200)
        for action in ('save','approve'):
            result=self.save(plan,0,action,driver);self.assertEqual(result.status_code,409)
        self.assertEqual(self.rows('SELECT revision,status FROM bf_schedule_weeks'),[dict(revision=1,status='draft')])
        self.assertEqual(len(self.rows('SELECT * FROM bf_schedule_history')),1)

    def prepare_job(self):
        self.job(deliver=[1],legacy=False)
        self.post('/binflow/driver',dict(name='Weekly truck',state='QLD'))
        self.post('/binflow/pickups/1/truck',dict(truck_id=1))
        plan=self.visit_plan();plan['days'][0]['items']=[]
        plan['days'][1]['items']=[dict(id='job-1',kind='job',job_id=1,time='08:00',notes='')]
        return plan

    def test_draft_hides_operational_manifest_publish_updates_job_and_move_invalidates_acceptance(self):
        driver=self.user();plan=self.prepare_job()
        self.assertEqual(self.save(plan).status_code,200)
        with self.connection() as conn:
            self.assertFalse(run_jobs(conn,1,'2026-10-19'));self.assertFalse(manifest(conn,1,'2026-10-19')[0])
        self.as_user(driver);self.assertEqual(self.client.get('/binflow/pickups/1').status_code,403)
        self.as_user(1);response=self.save(plan,1,'approve',driver);self.assertEqual(response.status_code,200,response.json)
        job=self.rows('SELECT * FROM bf_jobs')[0]
        self.assertEqual((job['scheduled_date'],job['scheduled_time'],job['driver_id'],job['schedule_live']),('2026-10-27','08:00',driver,1))
        with self.connection() as conn:
            conn.execute("UPDATE bf_runs SET accepted_at='2026-10-23',accepted_by=?,accepted_name='Alice',accepted_manifest='old' WHERE run_date='2026-10-27'",(driver,))
        plan['days'][4]['items']=plan['days'][1]['items'];plan['days'][1]['items']=[]
        response=self.save(plan,2);self.assertEqual(response.status_code,200,response.json)
        self.assertEqual(self.rows('SELECT scheduled_date FROM bf_jobs')[0]['scheduled_date'],'2026-10-30')
        self.assertIsNone(self.rows("SELECT accepted_at FROM bf_runs WHERE run_date='2026-10-27'")[0]['accepted_at'])
        self.assertEqual(self.rows("SELECT driver_id FROM bf_runs WHERE run_date='2026-10-30'")[0]['driver_id'],driver)
        # Legacy admin scheduling must not silently diverge from the weekly publication.
        response=self.post('/binflow/driver/1/schedule',dict(date='2026-10-30',driver_id=driver,order_1=1,time_1='08:00'))
        self.assertIn(b'Collection calendar',response.data)
        plan['days'][4]['items']=[];self.assertEqual(self.save(plan,3).status_code,200)
        job=self.rows('SELECT * FROM bf_jobs')[0]
        self.assertEqual(job['schedule_live'],0);self.assertIsNone(job['truck_id']);self.assertIsNone(job['driver_id'])
        self.as_user(driver);self.assertEqual(self.client.get('/binflow/pickups/1').status_code,403)

    def test_started_job_changes_blocked_but_other_day_can_be_saved(self):
        driver=self.user();plan=self.prepare_job();self.assertEqual(self.save(plan,action='approve',driver=driver).status_code,200)
        with self.connection() as conn:
            conn.execute("UPDATE bf_job_bins SET loaded_at='2026-10-27T07:00:00' WHERE job_id=1")
        moved=copy.deepcopy(plan);moved['days'][2]['items']=moved['days'][1]['items'];moved['days'][1]['items']=[]
        response=self.save(moved,1);self.assertEqual(response.status_code,400,response.json)
        self.assertEqual(self.rows('SELECT revision FROM bf_schedule_weeks')[0]['revision'],1)
        self.assertEqual(self.rows('SELECT scheduled_date FROM bf_jobs')[0]['scheduled_date'],'2026-10-27')
        plan['days'][4]['notes']='Friday instructions'
        response=self.save(plan,1);self.assertEqual(response.status_code,200,response.json)

    def test_pending_allocation_stock_failure_rolls_back_week_and_approval(self):
        driver=self.user();plan=self.prepare_job()
        with self.connection() as conn:
            conn.execute("UPDATE bf_jobs SET approval_status='pending' WHERE id=1")
            conn.execute("UPDATE bf_bins SET status='at_client',location_id=3 WHERE id=1")
        response=self.save(plan,action='approve',driver=driver);self.assertEqual(response.status_code,400,response.json)
        self.assertFalse(self.rows('SELECT * FROM bf_schedule_weeks'))
        self.assertEqual(self.rows('SELECT approval_status FROM bf_jobs')[0]['approval_status'],'pending')
        with self.connection() as conn: conn.execute("UPDATE bf_bins SET status='empty',location_id=1 WHERE id=1")
        response=self.save(plan,action='approve',driver=driver);self.assertEqual(response.status_code,200,response.json)
        self.assertEqual(self.rows('SELECT approval_status FROM bf_jobs')[0]['approval_status'],'approved')

    def test_month_calendar_uses_saved_days_and_new_profiles_do_not_rewrite_them(self):
        plan=self.visit_plan();plan['days'][4]['items']=plan['days'][0]['items'];plan['days'][0]['items']=[]
        self.assertEqual(self.save(plan).status_code,200)
        with self.connection() as conn:
            conn.execute("INSERT INTO bf_calendar_rebuilds VALUES(?,?,'[]','{}','2026-10-23')",(RESET_ID,'2026-10-26'))
            conn.execute("UPDATE bf_client_site_profiles SET schedule='Mon - Weekly',suburb='Eagle Farm' WHERE id=1")
            data=view_data(conn,date(2026,10,26),date(2026,11,1),'QLD')
            self.assertFalse(data['calendar_events']['2026-10-26'])
            self.assertEqual(data['calendar_events']['2026-10-30'][0]['day']['saved_items'][0]['title'],'Alpha QLD')
        page=self.client.get('/binflow/collection-calendar?date=2026-10-26&state=QLD').data
        self.assertIn(b'Calendar options',page);self.assertNotIn(b'Manage / approve week',page)

    def test_second_week_cannot_take_a_linked_allocation(self):
        plan=self.prepare_job();self.assertEqual(self.save(plan).status_code,200)
        other=self.plan(week='2026-11-02')['plan']
        other['days'][0]['items']=[dict(id='job-1',kind='job',job_id=1)]
        result=self.save(other,week='2026-11-02')
        self.assertEqual(result.status_code,400,result.json)
        self.assertEqual(len(self.rows('SELECT * FROM bf_schedule_weeks')),1)

    def template(self,plan,name='Standard week A',revision=0,state='QLD'):
        return self.post('/binflow/schedule/templates',dict(plan=json.dumps(plan),name=name,revision=revision,
            state=state,week='2026-10-26'),follow=False)

    def test_named_state_schedules_versioned_loadable_and_do_not_publish_or_copy_jobs(self):
        driver=self.user();plan=self.prepare_job()
        self.assertEqual(self.save(plan,action='approve',driver=driver).status_code,200)
        before={t:self.rows('SELECT * FROM '+t) for t in ('bf_schedule_weeks','bf_jobs','bf_bins','bf_job_bins')}
        saved=self.template(plan,revision=1);self.assertEqual(saved.status_code,200,saved.json)
        self.assertEqual(saved.json['version'],1)
        saved2=self.template(plan,name='standard week a',revision=1);self.assertEqual(saved2.json['version'],2)
        templates=self.rows('SELECT * FROM bf_schedule_templates ORDER BY version')
        item=json.loads(templates[0]['plan_json'])['days'][1]['items'][0]
        self.assertEqual(item['kind'],'visit');self.assertNotIn('job_id',item)
        loaded=self.client.get('/binflow/schedule/load?state=QLD&week=2026-11-02&template='+str(saved.json['id']))
        self.assertEqual(loaded.status_code,200,loaded.json)
        future=loaded.json['plan'];self.assertEqual(future['days'][1]['date'],'2026-11-03')
        self.assertEqual(future['days'][1]['items'][0]['profile_id'],1)
        self.assertEqual(future['days'][1]['items'][0]['time'],'08:00')
        self.assertFalse(future['days'][1]['estimate_valid'])
        self.assertEqual(before,{t:self.rows('SELECT * FROM '+t) for t in before})
        self.assertEqual(self.save(future,week='2026-11-02').status_code,200)
        self.assertEqual(len(self.rows('SELECT * FROM bf_jobs')),1)
        self.assertEqual(self.rows("SELECT status FROM bf_schedule_weeks WHERE week_start='2026-11-02'")[0]['status'],'draft')
        self.assertEqual(self.client.get('/binflow/schedule/load?state=NSW&week=2026-11-02&template='+str(saved.json['id'])).status_code,404)
        self.assertEqual(self.template(plan,revision=0).status_code,409)
        self.as_user(driver)
        self.assertEqual(self.template(plan,revision=1).status_code,403)
        self.assertEqual(self.client.get('/binflow/schedule/load?state=QLD&week=2026-11-02&template=1').status_code,403)
        self.assertNotIn(b'Standard week A',self.client.get('/binflow/calendar?date=2026-11-02').data)

    def test_saved_week_restore_is_preview_until_explicit_save_and_retains_history(self):
        driver=self.user();plan=self.visit_plan()
        self.assertEqual(self.save(plan,action='approve',driver=driver).status_code,200)
        plan['days'][4]['items']=plan['days'][0]['items'];plan['days'][0]['items']=[]
        self.assertEqual(self.save(plan,1).status_code,200)
        before=self.rows('SELECT * FROM bf_schedule_weeks')
        result=self.client.get('/binflow/schedule/load?state=QLD&week=2026-10-26&revision=1')
        self.assertEqual(result.status_code,200,result.json)
        self.assertEqual(before,self.rows('SELECT * FROM bf_schedule_weeks'))
        self.assertTrue(result.json['plan']['days'][0]['items']);self.assertFalse(result.json['plan']['days'][4]['items'])
        self.assertEqual(self.save(result.json['plan'],2).status_code,200)
        self.assertEqual([h['revision'] for h in self.rows('SELECT revision FROM bf_schedule_history ORDER BY revision')],[1,2,3])
        current=self.rows('SELECT status,revision,driver_id FROM bf_schedule_weeks')[0]
        self.assertEqual(current,dict(status='published',revision=3,driver_id=driver))
        self.assertEqual(self.client.get('/binflow/schedule/load?state=NSW&week=2026-10-26&revision=1').status_code,404)

    def test_loading_template_over_published_week_leaves_live_work_unchanged_until_save(self):
        driver=self.user();plan=self.prepare_job()
        self.assertEqual(self.save(plan,action='approve',driver=driver).status_code,200)
        result=self.template(plan,revision=1);self.assertEqual(result.status_code,200)
        loaded=self.client.get('/binflow/schedule/load?state=QLD&week=2026-10-26&template='+str(result.json['id']))
        self.assertEqual(loaded.status_code,200,loaded.json)
        self.assertEqual(self.rows('SELECT schedule_live FROM bf_jobs')[0]['schedule_live'],1)
        self.assertEqual(self.rows('SELECT revision FROM bf_schedule_weeks')[0]['revision'],1)
        self.assertEqual(self.save(loaded.json['plan'],1).status_code,200)
        self.assertEqual(self.rows('SELECT schedule_live FROM bf_jobs')[0]['schedule_live'],0)
        self.assertEqual(self.rows('SELECT revision FROM bf_schedule_weeks')[0]['revision'],2)

    def preview_clear(self,period='week',chosen='2026-10-26',state='QLD'):
        return self.client.get(f'/binflow/schedule/clear-preview?state={state}&period={period}&date={chosen}')

    def clear(self,period='week',chosen='2026-10-26',state='QLD',token=None):
        if token is None: token=self.preview_clear(period,chosen,state).json['token']
        return self.post('/binflow/schedule/clear',dict(state=state,period=period,date=chosen,token=token),follow=False)

    def test_clear_published_week_updates_driver_retains_allocations_and_is_restorable(self):
        driver=self.user();plan=self.prepare_job();self.assertEqual(self.save(plan,action='approve',driver=driver).status_code,200)
        bins=self.rows('SELECT * FROM bf_bins');reserved=self.rows('SELECT * FROM bf_job_bins')
        preview=self.preview_clear().json;self.assertEqual((preview['jobs'],preview['published'],preview['blocked']),(1,1,0))
        result=self.clear(token=preview['token']);self.assertEqual(result.status_code,200,result.json)
        row=self.rows('SELECT * FROM bf_schedule_weeks')[0]
        self.assertEqual((row['status'],row['revision']),('published',2))
        self.assertTrue(all(not d['items'] for d in json.loads(row['plan_json'])['days']))
        job=self.rows('SELECT * FROM bf_jobs')[0]
        self.assertEqual(job['schedule_live'],0);self.assertIsNone(job['truck_id']);self.assertIsNone(job['driver_id'])
        self.assertEqual(bins,self.rows('SELECT * FROM bf_bins'));self.assertEqual(reserved,self.rows('SELECT * FROM bf_job_bins'))
        self.as_user(driver)
        page=self.client.get('/binflow/calendar?date=2026-10-26').data
        self.assertNotIn(b'Alpha QLD',page);self.assertIn(b'No jobs scheduled',page)
        self.assertEqual(self.clear(token=preview['token']).status_code,403)
        self.assertEqual(self.preview_clear().status_code,403)
        self.as_user(1)
        restored=self.client.get('/binflow/schedule/load?state=QLD&week=2026-10-26&revision=1')
        self.assertEqual(restored.status_code,200,restored.json)
        self.assertEqual(self.save(restored.json['plan'],2).status_code,200)
        self.assertEqual(self.rows('SELECT schedule_live FROM bf_jobs')[0]['schedule_live'],1)

    def test_clear_unsaved_week_with_actual_jobs_keeps_empty_override_and_original_version(self):
        self.prepare_job()
        with self.connection() as conn:
            conn.execute("UPDATE bf_jobs SET scheduled_date='2026-10-26' WHERE id=1")
            conn.execute("UPDATE bf_client_site_profiles SET schedule='Weekly',suburb='Eagle Farm' WHERE id=1")
        result=self.clear();self.assertEqual(result.status_code,200,result.json)
        self.assertEqual(self.rows('SELECT schedule_live FROM bf_jobs')[0]['schedule_live'],0)
        data=self.plan()
        self.assertTrue(all(not d['items'] for d in data['plan']['days']))
        self.assertEqual(data['revision'],2)
        before=self.client.get('/binflow/schedule/load?state=QLD&week=2026-10-26&revision=1')
        self.assertTrue(before.json['plan']['days'][0]['items'])
        with self.connection() as conn:
            data=view_data(conn,date(2026,10,26),date(2026,11,1),'QLD')
            self.assertFalse(any(data['calendar_events'].values()))

    def test_clear_month_preserves_boundary_days_other_states_and_unmanaged_outside_jobs(self):
        driver=self.user();plan=self.visit_plan()
        plan['days'][6]['items']=[dict(id='sunday',kind='visit',profile_id=2,time='',notes='')]
        self.assertEqual(self.save(plan,action='approve',driver=driver).status_code,200)
        nsw=self.plan('NSW')['plan']
        self.assertEqual(self.save(nsw,state='NSW').status_code,200)
        other_state=self.rows("SELECT * FROM bf_schedule_weeks WHERE state='NSW'")
        self.job(deliver=[1],legacy=False)
        with self.connection() as conn: conn.execute("UPDATE bf_jobs SET scheduled_date='2026-12-01',driver_id=? WHERE id=1",(driver,))
        job_before=self.rows('SELECT * FROM bf_jobs')[0]
        preview=self.preview_clear('month','2026-11-15').json
        self.assertEqual((preview['first'],preview['last']),('2026-11-01','2026-11-30'))
        result=self.clear('month','2026-11-15',token=preview['token']);self.assertEqual(result.status_code,200,result.json)
        october=self.plan()['plan']
        self.assertTrue(october['days'][0]['items']);self.assertFalse(october['days'][6]['items'])
        self.assertEqual(other_state,self.rows("SELECT * FROM bf_schedule_weeks WHERE state='NSW'"))
        self.assertEqual(job_before,self.rows('SELECT * FROM bf_jobs')[0])
        december=self.plan(week='2026-11-30')['plan']
        self.assertFalse(december['days'][0]['items']);self.assertTrue(december['days'][1]['items'])
        for row in self.rows("SELECT plan_json FROM bf_schedule_weeks WHERE state='QLD'"):
            for day in json.loads(row['plan_json'])['days']:
                if '2026-11-01'<=day['date']<='2026-11-30':self.assertFalse(day['items'])

    def test_clear_stale_preview_and_started_job_are_atomic(self):
        driver=self.user();plan=self.prepare_job();self.assertEqual(self.save(plan,action='approve',driver=driver).status_code,200)
        preview=self.preview_clear().json
        plan['days'][4]['notes']='Changed after preview';self.assertEqual(self.save(plan,1).status_code,200)
        self.assertEqual(self.clear(token=preview['token']).status_code,409)
        before=self.rows('SELECT * FROM bf_schedule_weeks')
        with self.connection() as conn:conn.execute("UPDATE bf_job_bins SET loaded_at='2026-10-27T06:45:00' WHERE job_id=1")
        preview=self.preview_clear('month','2026-10-26').json;self.assertEqual(preview['blocked'],1)
        result=self.clear('month','2026-10-26',token=preview['token']);self.assertEqual(result.status_code,400,result.json)
        self.assertEqual(before,self.rows('SELECT * FROM bf_schedule_weeks'))
        self.assertEqual(self.rows('SELECT schedule_live FROM bf_jobs')[0]['schedule_live'],1)
