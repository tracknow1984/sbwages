import json
import re
import secrets
import unittest
import uuid

import test_binflow_schedule as schedule
from binflow_route_calendar import RESET_ID, view_data
from binflow_calendar_limits import summary
from datetime import date


class CalendarLimitsTests(unittest.TestCase):
    for name in ('setUp','tearDown','rows','post','login','connection','plan','save'):
        locals()[name] = getattr(schedule.WeeklyScheduleTests,name)

    def prepare(self, capacity=14):
        with self.connection() as conn:
            conn.execute("INSERT INTO bf_calendar_rebuilds VALUES(?,?,'[]','{}','now')",(RESET_ID,'2026-10-26'))
            for state in ('QLD','NSW','VIC','SA','WA','TAS','ACT','NT'):
                conn.execute("INSERT INTO bf_rollout_states VALUES(?,'2026-10-26',1)",(state,))
            conn.execute("INSERT OR REPLACE INTO blocktexx_model(id,revision,data,updated_by,updated_at) VALUES(1,1,?,1,'now')",
                         (json.dumps(dict(states=dict(QLD=dict(truck=dict(pallet_positions=capacity,spaces=dict(bin240=.5)))))),))
            self.large=conn.execute('SELECT id FROM bf_sizes WHERE litres=1100').fetchone()[0]
            self.small=conn.execute('SELECT id FROM bf_sizes WHERE litres=240').fetchone()[0]
            for site in (3,4):
                conn.execute('INSERT OR REPLACE INTO bf_site_requirements VALUES(?,?,?)',(site,self.large,10))

    def visit(self,pid):
        return dict(id='visit-'+str(pid),kind='visit',profile_id=pid,time='',notes='')

    def booking(self,site=3,bins=(1,),day='2026-11-30',**extra):
        return self.post('/binflow/pickups/new',dict(request_key=secrets.token_urlsafe(24),
            site_id=site,warehouse_id=1,scheduled_date=day,deliver=list(bins),calendar='1',state='QLD',**extra))

    def test_actual_booking_at_limit_then_overflow_rolls_back_job_and_reservations(self):
        self.prepare(capacity=1)
        self.booking(bins=(1,2))
        self.assertEqual(len(self.rows('SELECT * FROM bf_jobs')),1)
        jobs=self.rows('SELECT * FROM bf_jobs');reserved=self.rows('SELECT * FROM bf_job_bins')
        result=self.booking(site=4,bins=(3,))
        self.assertIn('Truck overload',result.text)
        self.assertEqual(self.rows('SELECT * FROM bf_jobs'),jobs)
        self.assertEqual(self.rows('SELECT * FROM bf_job_bins'),reserved)
        self.assertIn('0 positions remaining',result.text)
        self.booking(site=4,bins=(3,),day='2026-12-01')
        self.assertEqual(len(self.rows('SELECT * FROM bf_jobs')),2)

    def test_visits_mixed_sizes_and_drag_are_checked_atomically(self):
        self.prepare()
        with self.connection() as conn:
            conn.execute('UPDATE bf_site_requirements SET quantity=4 WHERE location_id=4')
            conn.execute('INSERT INTO bf_site_requirements VALUES(?,?,1)',(4,self.small))
        p=self.plan(week='2026-11-30')
        p['plan']['days'][0]['items']=[self.visit(1)]
        p['plan']['days'][1]['items']=[self.visit(2)]
        self.assertEqual(self.save(p['plan'],week='2026-11-30',plan_token=p['plan_token']).status_code,200)
        original=self.rows('SELECT * FROM bf_schedule_weeks')
        data=self.client.get('/binflow/schedule/calendar-data?state=QLD&first=2026-11-30&last=2026-11-30').json
        op=dict(uid=uuid.uuid4().hex,state='QLD',source='2026-12-01',target='2026-11-30',kind='item',item_id='visit-2')
        result=self.post('/binflow/schedule/calendar-save',dict(moves=json.dumps([op]),tokens=json.dumps({w['key']:w['token'] for w in data['weeks']})),follow=False)
        self.assertEqual(result.status_code,400,result.json)
        self.assertIn('14.5 of 14',result.json['error'])
        self.assertEqual(original,self.rows('SELECT * FROM bf_schedule_weeks'))

    def test_booking_cannot_displace_an_existing_recurring_visit(self):
        self.prepare(capacity=10)
        with self.connection() as conn:
            conn.execute("UPDATE bf_client_site_profiles SET schedule='Weekly' WHERE id=1")
        route='/binflow/collection-calendar/clients/1/add'
        page=self.client.get(route+'?date=2026-11-30')
        token=re.search(r'name="token" value="([^"]+)"',page.text)[1]
        result=self.post(route,dict(date='2026-11-30',token=token),follow=False)
        self.assertEqual(result.status_code,302,result.text)
        result=self.booking(site=4,bins=(1,))
        self.assertIn('Truck overload',result.text)
        self.assertFalse(self.rows('SELECT * FROM bf_jobs'))
        self.assertEqual(self.plan(week='2026-11-30')['plan']['days'][0]['items'][0]['profile_id'],1)

    def test_every_state_wednesday_reserved_survives_clear_and_rejects_booking(self):
        self.prepare()
        for state in ('QLD','NSW','VIC','SA','WA','TAS','ACT','NT'):
            p=self.plan(state=state,week='2026-11-30')['plan']
            self.assertEqual(p['days'][2]['items'][0]['title'],'Warehouse baling day · mandatory')
        result=self.booking(day='2026-12-02')
        self.assertIn('Wednesday is reserved',result.text)
        self.assertFalse(self.rows('SELECT * FROM bf_jobs'))
        p=self.plan(week='2026-11-30')
        p['plan']['days'][2]['items']=[]
        self.assertEqual(self.save(p['plan'],week='2026-11-30',plan_token=p['plan_token']).status_code,200)
        self.assertEqual(self.plan(week='2026-11-30')['plan']['days'][2]['items'][0]['kind'],'baling')
        preview=self.client.get('/binflow/schedule/clear-preview?state=QLD&period=week&date=2026-11-30').json
        self.assertEqual(self.post('/binflow/schedule/clear',dict(state='QLD',period='week',date='2026-11-30',token=preview['token']),follow=False).status_code,200)
        self.assertEqual(self.plan(week='2026-11-30')['plan']['days'][2]['items'][0]['kind'],'baling')
        html=self.client.get('/binflow/collection-calendar?state=QLD&date=2026-11-30').text
        self.assertNotIn('aria-label="Add allocation for 2026-12-02"',html)
        self.assertNotIn('aria-label="Move whole QLD day 2026-12-02"',html)

    def test_cannot_move_mandatory_baling_or_add_wednesday_visit(self):
        self.prepare()
        p=self.plan(week='2026-11-30')
        p['plan']['days'][2]['items'].append(self.visit(1))
        result=self.save(p['plan'],week='2026-11-30',plan_token=p['plan_token'])
        self.assertEqual(result.status_code,400,result.json)
        self.assertIn('Wednesday',result.json['error'])
        data=self.client.get('/binflow/schedule/calendar-data?state=QLD&first=2026-11-30&last=2026-11-30').json
        op=dict(uid=uuid.uuid4().hex,state='QLD',source='2026-12-02',target='2026-12-03',kind='day')
        result=self.post('/binflow/schedule/calendar-save',dict(moves=json.dumps([op]),tokens=json.dumps({w['key']:w['token'] for w in data['weeks']})),follow=False)
        self.assertEqual(result.status_code,400)
        self.assertFalse(self.rows('SELECT * FROM bf_schedule_weeks'))

    def test_future_recurring_conflict_rejects_entire_cycle(self):
        self.prepare()
        p=self.plan(week='2026-12-07')
        p['plan']['days'][0]['items']=[self.visit(2)]
        self.assertEqual(self.save(p['plan'],week='2026-12-07',plan_token=p['plan_token']).status_code,200)
        with self.connection() as conn:
            conn.execute("UPDATE bf_client_site_profiles SET schedule='Weekly' WHERE id=1")
        route='/binflow/collection-calendar/clients/1/add'
        page=self.client.get(route+'?date=2026-11-30')
        token=re.search(r'name="token" value="([^"]+)"',page.text)[1]
        result=self.post(route,dict(date='2026-11-30',token=token),follow=False)
        self.assertEqual(result.status_code,400)
        self.assertIn('2026-12-07',result.text)
        self.assertIn('Truck overload',result.text)
        self.assertFalse(self.rows('SELECT * FROM bf_calendar_cycles'))

    def test_existing_wednesday_job_retained_and_flagged(self):
        self.prepare()
        self.booking()
        with self.connection() as conn:
            conn.execute("UPDATE bf_jobs SET scheduled_date='2026-12-02'")
        jobs=self.rows('SELECT * FROM bf_jobs')
        day=self.plan(week='2026-11-30')['plan']['days'][2]
        self.assertTrue(day['booking_conflict'])
        self.assertEqual([i['kind'] for i in day['items']],['baling','job'])
        page=self.client.get('/binflow/collection-calendar?state=QLD&date=2026-11-30').text
        self.assertIn('Existing bookings need rescheduling',page)
        self.assertEqual(jobs,self.rows('SELECT * FROM bf_jobs'))

    def test_unknown_quantities_block_new_work_instead_of_counting_zero(self):
        self.prepare()
        with self.connection() as conn:
            conn.execute('DELETE FROM bf_site_requirements WHERE location_id=3')
        p=self.plan(week='2026-11-30')
        p['plan']['days'][0]['items']=[self.visit(1)]
        result=self.save(p['plan'],week='2026-11-30',plan_token=p['plan_token'])
        self.assertEqual(result.status_code,400)
        self.assertIn('Confirm bin quantities',result.json['error'])


if __name__=='__main__':unittest.main()
