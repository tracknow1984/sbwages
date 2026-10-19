import json
import secrets
import sqlite3
import unittest
from datetime import date, timedelta

import test_binflow as legacy
from binflow_collections import parse_frequency, source_plan, occurs, populate_collection_profiles, calendar_data
from binflow_import import import_blocktexx


class CollectionRecurrenceTests(unittest.TestCase):
    def test_source_frequencies_keep_four_and_eight_week_intervals(self):
        for text,cadence,days,visits in [
            ('Weekly','weekly',[],4),('Fortnightly','fortnightly',[],2),
            ('Every 4 Weeks','four_weekly',[],1),('Every 8 Weeks','eight_weekly',[],.5),
            ('Tue Fri - Weekly','weekly',[1,4],8),('Mon Wed Fri - Weekly','weekly',[0,2,4],12),
            ('Mon - Weekly','weekly',[0],4),('Monthly','monthly',[],None),
            ('Adhoc','adhoc',[],None),('Unconfirmed','unconfirmed',[],None)]:
            self.assertEqual(parse_frequency(dict(frequency=text)),(cadence,days,visits))
        self.assertEqual(parse_frequency(dict(frequency='Weekly',visits_4w=2))[0],'fortnightly')
        self.assertEqual(parse_frequency(dict(frequency='Custom: 3 visits / 4 weeks',visits_4w=3))[0],'unconfirmed')
        self.assertEqual(parse_frequency(dict(frequency='Paused',visits_4w=0))[0],'paused')

    def test_recurring_dates_cross_month_year_and_month_end(self):
        p=dict(cadence='fortnightly',days=[0,2],start_date='2026-12-21',end_date='2027-01-20')
        start=date(2026,12,21)
        dates=[(start+timedelta(days=n)).isoformat() for n in range(50) if occurs(p,start+timedelta(days=n))]
        self.assertEqual(dates,['2026-12-21','2026-12-23','2027-01-04','2027-01-06','2027-01-18','2027-01-20'])
        p.update(cadence='four_weekly',days=[0],end_date='')
        self.assertTrue(occurs(p,date(2027,1,18)));self.assertFalse(occurs(p,date(2027,1,21)))
        p.update(cadence='eight_weekly')
        self.assertFalse(occurs(p,date(2027,1,18)));self.assertTrue(occurs(p,date(2027,2,15)))
        p.update(cadence='monthly',start_date='2028-01-31')
        self.assertTrue(occurs(p,date(2028,2,29)));self.assertTrue(occurs(p,date(2028,3,31)))
        self.assertFalse(occurs(p,date(2028,3,29)))
        for cadence in ('adhoc','unconfirmed','paused'):
            p['cadence']=cadence;self.assertFalse(occurs(p,date(2028,2,29)))

    def test_inference_preserves_source_days_and_route_cycle_phase(self):
        source=dict(id='alpha',frequency='Fortnightly',state='QLD')
        run=dict(name='Local run',activity_type='collection',site_ids=['alpha'],runs_4w=2,
            planner_slots=[dict(week=2,day=3),dict(week=4,day=3)])
        model=dict(states=dict(QLD=dict(runs=[run])))
        p=source_plan(source,model,date(2026,10,19),'collection')
        self.assertEqual((p['service_days'],p['start_date']),('[3]','2026-10-26'))
        self.assertTrue(occurs(p,date(2026,10,29)));self.assertFalse(occurs(p,date(2026,11,5)))
        run['planner_slots']=[dict(week=w,day=3) for w in range(1,5)]
        p=source_plan(source,model,date(2026,10,19),'collection')
        self.assertEqual(p['service_days'],'[]');self.assertIn('do not match',p['review_note'])
        source.update(frequency='Tue Fri - Weekly',service_days=[1,4],visits_4w=8)
        p=source_plan(source,model,date(2026,10,19),'collection')
        self.assertEqual(p['service_days'],'[1, 4]')


class CollectionCalendarTests(unittest.TestCase):
    setUp=legacy.BinFlowTests.setUp
    tearDown=legacy.BinFlowTests.tearDown
    post=legacy.BinFlowTests.post
    rows=legacy.BinFlowTests.rows
    login=legacy.BinFlowTests.login
    job=legacy.BinFlowTests.job
    exchange=legacy.BinFlowTests.exchange
    initial_delivery=legacy.BinFlowTests.initial_delivery

    def profile(self,lid=3,**changes):
        saved=self.rows('SELECT version FROM bf_collection_profiles WHERE location_id=?',(lid,))
        values=dict(version=saved[0]['version'] if saved else 0,cadence='weekly',days=[0],
            start_date='2026-10-19',warehouse_id=1,confirmed='yes',notes='Collection instructions')
        values.update(changes)
        return self.post(f'/binflow/locations/{lid}/collection-profile',values)

    def data(self,state='QLD',start='2026-10-19',end='2026-10-25'):
        with sqlite3.connect(self.path) as conn:
            conn.row_factory=sqlite3.Row
            return calendar_data(conn,date.fromisoformat(start),date.fromisoformat(end),state)

    def test_profile_edit_version_warehouse_validation_and_job_preservation(self):
        before=self.rows('SELECT * FROM bf_bins')
        result=self.profile();self.assertIn(b'Collection profile saved.',result.data)
        original=self.rows('SELECT * FROM bf_collection_profiles')[0]
        self.assertEqual(original['version'],1);self.assertEqual(original['confirmed'],1)
        self.assertEqual(self.profile(version=0).status_code,400)
        self.assertEqual(self.profile(warehouse_id=2).status_code,400)
        self.assertEqual(self.profile(start_date='',days=[]).status_code,400)
        self.assertEqual(self.profile(end_date='2026-10-18').status_code,400)
        self.assertEqual(self.profile(cadence='unconfirmed').status_code,400)
        self.assertEqual(self.rows('SELECT * FROM bf_collection_profiles')[0],original)
        self.assertEqual(self.rows('SELECT * FROM bf_bins'),before)
        self.assertEqual(self.rows('SELECT * FROM bf_jobs'),[])
        self.job(deliver=[1]);job_before=self.rows('SELECT * FROM bf_jobs')
        self.profile(cadence='fortnightly',days=[2],start_date='2026-11-02')
        self.assertEqual(self.rows('SELECT * FROM bf_jobs'),job_before)
        self.assertEqual(len(self.rows('SELECT * FROM bf_collection_profile_history')),2)
        self.assertEqual(self.data()['counts']['proposed'],0)

    def test_state_week_and_month_views_render_with_dated_allocation_link(self):
        self.profile();self.profile(lid=5,warehouse_id=2,days=[1])
        for view in ('week','month'):
            response=self.client.get('/binflow/collection-calendar?state=QLD&date=2026-10-19&view='+view)
            self.assertEqual(response.status_code,200)
            self.assertIn(b'State collection calendar',response.data)
            self.assertIn(b'site=3&amp;warehouse=1&amp;date=2026-10-19',response.data)
            self.assertNotIn(b'Alpha NSW',response.data)
        self.assertEqual(self.data()['counts']['proposed'],1)
        self.assertEqual(self.data('NSW')['counts']['proposed'],1)
        for query in ('state=BOGUS','view=year','date=not-a-date','date=2200-01-01'):
            self.assertEqual(self.client.get('/binflow/collection-calendar?'+query).status_code,400)
        self.assertEqual(self.client.get('/binflow/collection-calendar?state=WA').status_code,200)
        self.assertIn(b'No client sites in this state',self.client.get('/binflow/collection-calendar?state=WA').data)

    def test_jobs_replace_matching_collection_visit_but_delivery_does_not(self):
        self.profile();self.initial_delivery()
        data=self.data()
        self.assertEqual(len(data['events']['2026-10-19']),2)  # Initial delivery does not satisfy a collection.
        key=secrets.token_urlsafe(24)
        self.post('/binflow/pickups/new',dict(request_key=key,site_id=3,warehouse_id=1,scheduled_date='2026-10-19',collect=[1]))
        data=self.data();self.assertEqual(data['counts']['proposed'],0);self.assertEqual(data['counts']['pending'],1)
        self.assertNotIn(b'BF-000002',self.client.get('/binflow/calendar?date=2026-10-19').data)
        self.post('/binflow/pickups/2/review',dict(decision='reject',notes='Reschedule'))
        data=self.data();self.assertEqual(data['counts']['proposed'],1);self.assertEqual(data['counts']['pending'],0)
        self.assertFalse(any(e['job'] and e['job']['id']==2 for e in data['events']['2026-10-19']))

    def test_ad_hoc_and_unknown_stay_undated_without_creating_stock(self):
        self.profile(cadence='adhoc');self.profile(lid=4,cadence='unconfirmed',confirmed='')
        data=self.data()
        self.assertEqual(len(data['undated']),2);self.assertFalse(data['events'])
        self.assertEqual(len(self.rows('SELECT * FROM bf_bins')),6)
        self.assertEqual(self.rows('SELECT * FROM bf_jobs'),[])

    def test_permissions_csrf_and_live_day_state_filter(self):
        from werkzeug.security import generate_password_hash
        self.profile();self.job(deliver=[1])
        self.assertIn(b'Alpha QLD',self.client.get('/binflow/calendar?state=QLD&date=2026-10-19').data)
        self.assertNotIn(b'Alpha QLD',self.client.get('/binflow/calendar?state=NSW&date=2026-10-19').data)
        self.assertNotIn('Alpha QLD',self.client.get('/binflow/calendar/state?state=NSW&date=2026-10-19').json['html'])
        self.assertEqual(self.client.get('/binflow/calendar/state?state=BOGUS').status_code,400)
        self.assertEqual(self.client.post('/binflow/locations/3/collection-profile',data={}).status_code,400)
        with sqlite3.connect(self.path) as conn:
            uid=conn.execute("INSERT INTO users(role,first_name,last_name,email,username,password_hash) VALUES('employee','Transport','Worker','worker@example.com','planner',?)",(generate_password_hash('test-admin-password'),)).lastrowid
            conn.execute('INSERT INTO bf_access VALUES(?)',(uid,))
        self.post('/logout');self.login('employee','planner')
        self.assertEqual(self.client.get('/binflow/collection-calendar').status_code,200)
        self.assertEqual(self.client.get('/binflow/locations/3/collection-profile').status_code,403)
        self.assertEqual(self.profile().status_code,403)
        self.post('/logout')
        self.assertEqual(self.client.get('/binflow/collection-calendar').status_code,302)

    def test_import_uses_current_saved_model_preserves_user_edits_and_audits(self):
        with sqlite3.connect(self.path) as conn:
            conn.row_factory=sqlite3.Row
            source=dict(id='alpha',name='Alpha QLD',state='QLD',address='3 Client Road',frequency='Weekly')
            model=dict(sites=[source],partners=[])
            conn.execute('INSERT INTO blocktexx_model VALUES(1,1,?,1,?)',(json.dumps(model),'2026-10-19'));conn.commit()
            import_blocktexx(conn,['Client Alpha'],'2026-10-19')
            # Match the imported identity to our operational test site, as existing source mapping does.
            conn.execute('UPDATE bf_location_sources SET location_id=3')
            source['frequency']='Every 8 Weeks';source['service_days']=[4]
            model['sites']=[source]
            conn.execute('UPDATE blocktexx_model SET revision=2,data=?',(json.dumps(model),));conn.commit()
            before=[dict(r) for r in conn.execute('SELECT * FROM bf_bins')]
            summary=populate_collection_profiles(conn,'2026-10-19T15:00:00+10:00')
            self.assertEqual(summary,{'QLD':1})
            saved=dict(conn.execute('SELECT * FROM bf_collection_profiles').fetchone())
            self.assertEqual((saved['cadence'],saved['source_revision'],saved['confirmed']),('eight_weekly',2,0))
            self.assertEqual(saved['service_days'],'[4]')
            self.assertEqual([dict(r) for r in conn.execute('SELECT * FROM bf_bins')],before)
            self.assertFalse(conn.execute('SELECT * FROM bf_jobs').fetchall())
        self.profile(cadence='fortnightly')
        before=self.rows('SELECT * FROM bf_collection_profiles')
        with sqlite3.connect(self.path) as conn:
            conn.row_factory=sqlite3.Row
            self.assertEqual(populate_collection_profiles(conn,'2026-11-02T15:00:00+10:00'),{})
        self.assertEqual(self.rows('SELECT * FROM bf_collection_profiles'),before)
        self.assertEqual(len(self.rows('SELECT * FROM bf_collection_profile_history')),2)


if __name__=='__main__': unittest.main()
