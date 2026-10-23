"""Calendar moves preserve publication, custody, concurrency and state boundaries."""
import json
import uuid
import unittest
import test_binflow_schedule as schedule
from binflow_route_calendar import RESET_ID


class CalendarEditTests(unittest.TestCase):
    for _name in ('setUp','tearDown','rows','post','login','job','connection','user','as_user','plan','save','visit_plan','prepare_job'):
        locals()[_name] = getattr(schedule.WeeklyScheduleTests, _name)

    def data(self,state='QLD',first='2026-10-26',last='2026-11-08'):
        return self.client.get(f'/binflow/schedule/calendar-data?state={state}&first={first}&last={last}')

    def move(self,source='2026-10-26',target='2026-10-30',kind='day',state='QLD',**extra):
        return dict(uid=uuid.uuid4().hex,state=state,source=source,target=target,kind=kind,**extra)

    def calendar_save(self,moves,tokens=None):
        if tokens is None:tokens={w['key']:w['token'] for w in self.data().json['weeks']}
        return self.post('/binflow/schedule/calendar-save',dict(moves=json.dumps(moves),tokens=json.dumps(tokens)),follow=False)

    def test_day_swap_moves_notes_and_keeps_destination_work(self):
        p=self.visit_plan();p['days'][0]['notes']='Monday instruction'
        p['days'][4]['items']=[dict(id='baling-friday',kind='baling',time='06:30',notes='Depot')]
        p['days'][4]['notes']='Friday instruction'
        self.assertEqual(self.save(p).status_code,200)
        result=self.calendar_save([self.move()]);self.assertEqual(result.status_code,200,result.json)
        p=self.plan()['plan']
        self.assertEqual(p['days'][0]['items'][0]['kind'],'baling')
        self.assertEqual(p['days'][4]['items'][0]['profile_id'],1)
        self.assertEqual(p['days'][4]['notes'],'Monday instruction')
        self.assertEqual(p['days'][0]['notes'],'Friday instruction')
        self.assertFalse(p['days'][4]['estimate_valid'])
        self.assertEqual(self.rows('SELECT status,revision FROM bf_schedule_weeks'),[dict(status='draft',revision=2)])

    def test_move_stop_across_weeks_then_reorder_and_undo_by_not_saving(self):
        p=self.visit_plan();p['days'][0]['items'].append(dict(id='second',kind='visit',profile_id=2,time='',notes=''))
        self.save(p)
        other=self.plan(week='2026-11-02')['plan']
        for d in other['days']:d['items']=[]
        other['days'][1]['items']=[dict(id='target-baling',kind='baling',time='06:30',notes='')]
        self.save(other,week='2026-11-02')
        before=self.rows('SELECT * FROM bf_schedule_weeks');self.data()
        self.assertEqual(before,self.rows('SELECT * FROM bf_schedule_weeks'))
        op=self.move(target='2026-11-03',kind='item',item_id='new-visit')
        reorder=self.move(source='2026-11-03',target='2026-11-03',kind='item',item_id=f'cal-{op["uid"]}-item',before='target-baling')
        result=self.calendar_save([op,reorder]);self.assertEqual(result.status_code,200,result.json)
        self.assertEqual(self.plan()['plan']['days'][0]['items'][0]['profile_id'],2)
        items=self.plan(week='2026-11-02')['plan']['days'][1]['items']
        self.assertEqual([i['kind'] for i in items],['visit','baling'])
        self.assertEqual([r['revision'] for r in self.rows('SELECT revision FROM bf_schedule_weeks')],[2,2])

    def test_published_cross_week_docket_uses_destination_driver_and_keeps_stock_and_truck(self):
        alice=self.user();bob=self.user('Bob');p=self.prepare_job()
        self.assertEqual(self.save(p,action='approve',driver=alice).status_code,200)
        target=self.plan(week='2026-11-02')['plan']
        for d in target['days']:d['items']=[]
        target['days'][4]['items']=[dict(id='depot',kind='baling',time='06:30',notes='')]
        self.assertEqual(self.save(target,action='approve',driver=bob,week='2026-11-02').status_code,200)
        before=self.rows('SELECT * FROM bf_job_bins');bins=self.rows('SELECT * FROM bf_bins')
        result=self.calendar_save([self.move(source='2026-10-27',target='2026-11-03')])
        self.assertEqual(result.status_code,200,result.json)
        job=self.rows('SELECT * FROM bf_jobs')[0]
        self.assertEqual((job['scheduled_date'],job['driver_id'],job['truck_id'],job['schedule_live']),('2026-11-03',bob,1,1))
        self.assertEqual(before,self.rows('SELECT * FROM bf_job_bins'));self.assertEqual(bins,self.rows('SELECT * FROM bf_bins'))
        self.assertEqual([r['status'] for r in self.rows('SELECT status FROM bf_schedule_weeks')],['published','published'])
        self.as_user(alice);self.assertNotIn(b'Job / allocation',self.client.get('/binflow/calendar?date=2026-10-27').data)
        self.as_user(bob);self.assertIn(b'Job / allocation',self.client.get('/binflow/calendar?date=2026-11-03').data)

    def test_transfer_into_draft_needs_approval_and_preserves_assigned_truck(self):
        driver=self.user();p=self.prepare_job();self.save(p,action='approve',driver=driver)
        result=self.calendar_save([self.move(source='2026-10-27',target='2026-11-03')])
        self.assertEqual(result.status_code,200,result.json)
        self.assertEqual(self.rows('SELECT truck_id,schedule_live FROM bf_jobs'),[dict(truck_id=1,schedule_live=0)])
        self.assertEqual(self.rows("SELECT status,driver_id FROM bf_schedule_weeks WHERE week_start='2026-11-02'"),[dict(status='draft',driver_id=None)])
        self.as_user(driver);self.assertNotIn(b'Job / allocation',self.client.get('/binflow/calendar?date=2026-11-03').data)

    def test_started_job_and_stale_tokens_roll_back_both_weeks(self):
        driver=self.user();p=self.prepare_job();self.save(p,action='approve',driver=driver)
        tokens={w['key']:w['token'] for w in self.data().json['weeks']}
        p['days'][4]['notes']='Another admin edit';self.save(p,1)
        move=self.move(source='2026-10-27',target='2026-11-03')
        result=self.calendar_save([move],tokens);self.assertEqual(result.status_code,409,result.json)
        with self.connection() as conn:conn.execute("UPDATE bf_job_bins SET loaded_at='2026-10-27T07:00:00' WHERE job_id=1")
        before=self.rows('SELECT * FROM bf_schedule_weeks');jobs=self.rows('SELECT * FROM bf_jobs')
        result=self.calendar_save([move]);self.assertEqual(result.status_code,400,result.json)
        self.assertEqual(before,self.rows('SELECT * FROM bf_schedule_weeks'));self.assertEqual(jobs,self.rows('SELECT * FROM bf_jobs'))

    def test_duplicate_stops_and_baling_are_rejected_without_partial_save(self):
        p=self.visit_plan();p['days'][1]['items']=[dict(id='same-client',kind='visit',profile_id=1,time='',notes='')]
        self.save(p);before=self.rows('SELECT * FROM bf_schedule_weeks')
        result=self.calendar_save([self.move(kind='item',item_id='new-visit',target='2026-10-27')])
        self.assertEqual(result.status_code,400,result.json);self.assertEqual(before,self.rows('SELECT * FROM bf_schedule_weeks'))

    def test_admin_only_data_and_mutation_and_csrf(self):
        p=self.visit_plan();self.save(p)
        for role in ('driver','contractor','manager','labour'):
            self.as_user(self.user(role,role=role))
            self.assertEqual(self.data().status_code,403)
            self.assertEqual(self.calendar_save([self.move()],{}).status_code,403)
        self.as_user(1)
        self.assertEqual(self.client.post('/binflow/schedule/calendar-save',data={}).status_code,400)
        self.assertEqual(self.data(state='BAD').status_code,400)
        self.assertEqual(self.data(last='2027-11-08').status_code,400)
        self.assertEqual(self.calendar_save([self.move(target='2026-10-01')]).status_code,400)

    def test_removed_screens_redirect_and_are_absent_from_navigation(self):
        with self.connection() as conn:conn.execute("INSERT INTO bf_calendar_rebuilds VALUES(?,?,'[]','{}','2026-10-23')",(RESET_ID,'2026-10-26'))
        before=self.rows('SELECT * FROM bf_schedule_weeks')
        for path in ('/binflow/schedule?state=QLD&date=2026-10-26','/binflow/pickups?state=QLD'):
            result=self.client.get(path)
            self.assertEqual(result.status_code,302)
            self.assertIn('/binflow/collection-calendar',result.location)
        page=self.client.get('/binflow/collection-calendar?state=QLD&date=2026-10-26').data
        for text in (b'Weekly dispatch',b'Pickups &amp; deliveries',b'Pickups & deliveries',b'Manage / approve week',b'/binflow/schedule?',b'binflow-schedule.js'):
            self.assertNotIn(text,page)
        self.assertIn(b'Calendar options',page)
        self.assertEqual(before,self.rows('SELECT * FROM bf_schedule_weeks'))
        driver=self.user();self.as_user(driver)
        result=self.client.get('/binflow/schedule?date=2026-10-26')
        self.assertEqual(result.status_code,302);self.assertIn('/binflow/calendar?',result.location)
        page=self.client.get(result.location).data
        self.assertNotIn(b'My weekly schedule',page);self.assertNotIn(b'My week</a>',page)

    def test_main_calendar_exposes_direct_edit_controls_only_to_admin(self):
        with self.connection() as conn:conn.execute("INSERT INTO bf_calendar_rebuilds VALUES(?,?,'[]','{}','2026-10-23')",(RESET_ID,'2026-10-26'))
        self.save(self.visit_plan())
        page=self.client.get('/binflow/collection-calendar?date=2026-10-26&state=QLD').data
        self.assertIn(b'data-calendar-editor',page);self.assertIn(b'calendar-expanded-day',page)
        self.assertIn(b'data-drag-date="2026-10-26"',page);self.assertIn(b'Save changes',page)
        self.as_user(self.user('Manager',role='manager'))
        page=self.client.get('/binflow/collection-calendar?date=2026-10-26&state=QLD').data
        self.assertNotIn(b'data-calendar-editor',page);self.assertNotIn(b'calendar-expanded-day',page)


if __name__=='__main__':unittest.main()
