import json
import sqlite3
import unittest
from datetime import date, timedelta

import test_binflow as legacy
from blocktexx import empty_model
from test_binflow_allocation_import import AllocationWorkbookTests
from binflow_allocation_import import replace_pallecons
from binflow_collections import occurs, parse_frequency, populate_collection_profiles
from binflow_route_calendar import (START, clients, due, geography, leg, pack, rebuild,
                                    resources, split_loads, week_plan, view_data)


class RecurrenceTests(unittest.TestCase):
    def test_last_weekday_is_calendar_monthly_not_four_weekly(self):
        for text, weekday in [('Last Monday of the Month',0),('Last Tuesday of the month',1)]:
            cadence, days, _ = parse_frequency({'frequency':text})
            p=dict(cadence=cadence,days=days,start_date='2026-10-26',end_date='',monthly_rule='last_weekday')
            self.assertEqual(cadence,'monthly');self.assertEqual(days,[weekday])
            actual=[]
            for i in range(100):
                day=START+timedelta(days=i)
                if occurs(p,day):actual.append(day.isoformat())
            self.assertEqual(actual, ['2026-10-26','2026-11-30','2026-12-28','2027-01-25'] if weekday==0 else
                             ['2026-10-27','2026-11-24','2026-12-29','2027-01-26'])

    def test_fortnight_eight_week_and_monthly_week_demand(self):
        p=dict(cadence='eight_weekly',days=[],monthly_rule='')
        self.assertEqual(due(p, START),[None])
        self.assertEqual(due(p,START+timedelta(weeks=4)),[])
        self.assertEqual(due(p,START+timedelta(weeks=8)),[None])
        p['cadence']='fortnightly'
        self.assertEqual(due(p,START+timedelta(weeks=1)),[])
        self.assertEqual(due(p,START+timedelta(weeks=2)),[None])
        p['cadence']='monthly'
        self.assertEqual(due(p,date(2026,11,23)),[None])
        self.assertEqual(due(p,date(2026,11,30)),[])
        p['cadence']='adhoc';self.assertEqual(due(p,START),[])
        p['cadence']='weekly';self.assertEqual(due(p,START-timedelta(weeks=1)),[])

    def test_capacity_splits_whole_containers_and_returns_each_load_to_depot(self):
        geo={'points':{'QLD|depot':{'lat':-27.5,'lon':153},'QLD|nearby':{'lat':-27.52,'lon':153.01}}}
        r=dict(capacity=14,small_space=.5,depot_key='QLD|depot')
        p=dict(id=1,large=23,small=3,geo_key='QLD|nearby')
        loads=split_loads(p,r)
        self.assertEqual([s['large'] for s in loads],[14,9])
        self.assertEqual(sum(s['small'] for s in loads),3)
        trips=pack(loads,'QLD',r,geo)
        self.assertEqual(len(trips),2)
        self.assertTrue(all(t['spaces']<=14 for t in trips))
        one_leg=leg(geo,'QLD','QLD|depot','QLD|nearby')[0]
        self.assertEqual([t['drive'] for t in trips],[one_leg*2]*2)
        self.assertIsNone(leg(geo,'QLD','missing','QLD|nearby'))


class CurrentCalendarTests(unittest.TestCase):
    setUp=legacy.BinFlowTests.setUp
    tearDown=legacy.BinFlowTests.tearDown
    rows=legacy.BinFlowTests.rows
    post=legacy.BinFlowTests.post
    login=legacy.BinFlowTests.login
    job=legacy.BinFlowTests.job
    connection=AllocationWorkbookTests.connection
    payload=AllocationWorkbookTests.payload
    encoded=AllocationWorkbookTests.encoded
    replace=AllocationWorkbookTests.replace
    save_target=AllocationWorkbookTests.save_target

    def prepare(self):
        # Retire synthetic old sites just as the existing import does in production.
        for lid in (3,4,5):self.save_target(lid,0,1)
        self.replace()
        with self.connection() as conn:
            conn.execute("UPDATE bf_client_site_profiles SET schedule='Weekly',suburb='Eagle Farm' WHERE location_id=3")
            saved=conn.execute('SELECT data FROM blocktexx_model WHERE id=1').fetchone()
            model=json.loads(saved['data']) if saved else empty_model()
            model['states']['QLD'].update(depot='North Maclean QLD',depot_status='confirmed',available_weekly_hours=40,
                truck=dict(pallet_positions=14,spaces={'bin240':.5},payload_kg=None))
            conn.execute("INSERT OR REPLACE INTO blocktexx_model(id,revision,data,updated_at,updated_by) VALUES(1,1,?,'2026-10-23',1)",(json.dumps(model),))

    def test_reset_current_cohort_audit_idempotence_live_profile_edits_and_jobs(self):
        self.job(deliver=[1])
        self.prepare()
        before={t:self.rows('SELECT * FROM '+t) for t in ('bf_jobs','bf_job_bins','bf_bins','bf_events')}
        with self.connection() as conn:
            summary,changed=rebuild(conn,'2026-10-23T12:00:00')
            self.assertTrue(changed);self.assertEqual(summary['current'],3)
            self.assertEqual(summary['missing_location'],1)
            self.assertEqual(len(clients(conn)),3)
            self.assertFalse(rebuild(conn,'2026-10-24T12:00:00')[1])
            self.assertEqual(populate_collection_profiles(conn,'2026-10-24T12:00:00'),{})
            first=week_plan(conn,START,'QLD')[0]
            self.assertEqual(sum(len(d['profiles']) for d in first['days']),1)
            self.assertFalse(first['days'][2]['trips']);self.assertTrue(first['days'][2]['baling'])
            self.assertTrue(all(d['minutes']<=540 for d in first['days']))
            conn.execute("UPDATE bf_client_site_profiles SET schedule='Tue Fri - Weekly' WHERE location_id=3")
            next_plan=week_plan(conn,START,'QLD')[0]
            self.assertEqual([i for i,d in enumerate(next_plan['days']) if d['trips']],[1,4])
        self.assertEqual({t:self.rows('SELECT * FROM '+t) for t in before},before)
        response=self.client.get('/binflow/collection-calendar?date=2026-10-26&view=week&state=QLD')
        self.assertEqual(response.status_code,200)
        self.assertIn(b'Start &amp; return: North Maclean',response.data)
        self.assertNotIn(b'Beta QLD',response.data)
        self.assertIn(b'1 client sites need location details',response.data)
        self.assertEqual(self.client.get('/binflow/collection-calendar?view=month&date=2027-01-26').status_code,200)

    def test_remote_unmatched_and_fixed_overflow_are_explicit_and_adhoc_unscheduled(self):
        self.prepare()
        with self.connection() as conn:
            conn.execute("UPDATE bf_client_site_profiles SET suburb='Paget',schedule='Weekly' WHERE location_id=3")
            plan=week_plan(conn,START,'QLD')[0]
            self.assertEqual(plan['exceptions'],1)
            self.assertIn('regional partner',plan['days'][0]['exceptions'][0]['reason'])
            conn.execute("UPDATE bf_client_site_profiles SET suburb='Unknown Example Place' WHERE location_id=3")
            self.assertIn('cannot be matched',week_plan(conn,START,'QLD')[0]['days'][0]['exceptions'][0]['reason'])
            conn.execute("UPDATE bf_client_site_profiles SET schedule='Adhoc' WHERE location_id=3")
            self.assertEqual(sum(len(d['trips']) for d in week_plan(conn,START,'QLD')[0]['days']),0)
            conn.execute("UPDATE bf_client_site_profiles SET suburb='Eagle Farm',schedule='Mon - Weekly' WHERE location_id=3")
            conn.execute('UPDATE bf_site_requirements SET quantity=80 WHERE location_id=3 AND size_id=3')
            plan=week_plan(conn,START,'QLD')[0]
            self.assertEqual(plan['exceptions'],1)
            self.assertFalse(plan['days'][0]['trips'])
            self.assertTrue(plan['days'][0]['exceptions'][0]['fixed'])

    def test_pallecon_conversion_adds_once_preserves_operations_and_clears_only_replaced_review(self):
        self.prepare()
        with self.connection() as conn:
            conn.execute("UPDATE bf_allocation_workbook_rows SET equipment=? WHERE profile_id=(SELECT id FROM bf_client_site_profiles WHERE location_id=3)",
                         (json.dumps(dict(pallecon=7,bulkbag=4,bin660=3,bin240=2,cage=1)),))
            conn.execute("UPDATE bf_site_profiles SET notes='Original targets. Pallecons and bulk bags require a separate decision.',review_note='7 pallecons listed; confirm whether these remain or need a bin replacement. Bulk bags require review.' WHERE location_id=3")
        before=self.rows('SELECT * FROM bf_bins')
        with self.connection() as conn:
            original=conn.execute('SELECT quantity FROM bf_site_requirements WHERE location_id=3 AND size_id=3').fetchone()[0]
            result,changed=replace_pallecons(conn,'2026-10-23T12:00:00')
            self.assertTrue(changed);self.assertEqual(result['replaced'],7)
            self.assertEqual(conn.execute('SELECT quantity FROM bf_site_requirements WHERE location_id=3 AND size_id=3').fetchone()[0],original+7)
            self.assertFalse(replace_pallecons(conn,'2026-10-24T12:00:00')[1])
            review=conn.execute('SELECT review_note FROM bf_site_profiles WHERE location_id=3').fetchone()[0]
            self.assertEqual(review,'Bulk bags require review.')
        self.assertEqual(self.rows('SELECT * FROM bf_bins'),before)

    def test_booked_jobs_visible_after_rebuild_and_staff_shortfall_flagged(self):
        self.job(deliver=[1]);self.prepare()
        with self.connection() as conn:
            conn.execute("UPDATE bf_jobs SET scheduled_date='2026-10-26'")
            model=json.loads(conn.execute('SELECT data FROM blocktexx_model WHERE id=1').fetchone()[0])
            model['states']['QLD']['available_weekly_hours']=0
            conn.execute('UPDATE blocktexx_model SET data=? WHERE id=1',(json.dumps(model),))
            plan=week_plan(conn,START,'QLD')[0]
            self.assertEqual(len(plan['days'][0]['jobs']),1)
            self.assertTrue(any('No staff hours' in s for s in plan['issues']))
            self.assertTrue(any('reconciliation' in s for s in plan['issues']))
            self.assertTrue(any('Weekly plan requires' in s for s in plan['issues']))

    def test_calendar_separates_unassigned_weekly_demand_from_fixed_dates(self):
        self.prepare()
        with self.connection() as conn:
            conn.execute("UPDATE bf_client_site_profiles SET suburb='Paget',schedule='Weekly' WHERE location_id=3")
            data=view_data(conn,START,START+timedelta(days=6),'QLD')
            self.assertEqual(len(data['calendar_weeks']),1)
            self.assertEqual(len(data['calendar_weeks'][0]),7)
            self.assertEqual(data['calendar_unassigned'],1)
            self.assertNotIn(START.isoformat(),data['calendar_events'])
            self.assertEqual(data['calendar_backlog'][0]['items'][0]['profile']['id'],3)
            conn.execute("UPDATE bf_client_site_profiles SET schedule='Mon - Weekly' WHERE location_id=3")
            fixed=view_data(conn,START,START+timedelta(days=6),'QLD')
            self.assertEqual(fixed['calendar_unassigned'],0)
            event=fixed['calendar_events'][START.isoformat()][0]
            self.assertEqual(event['day']['exceptions'][0]['profile']['id'],3)
            self.assertEqual(event['dialog_id'],'route-QLD-2026-10-26')
