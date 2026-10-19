import json
import sqlite3
import unittest

import test_binflow as legacy
from binflow_helpers import prestart, depart
from binflow_requirements import converted_requirements, populate_requirements, site_plan, load_plan


class BinRequirementsTests(unittest.TestCase):
    setUp=legacy.BinFlowTests.setUp
    tearDown=legacy.BinFlowTests.tearDown
    post=legacy.BinFlowTests.post
    rows=legacy.BinFlowTests.rows
    login=legacy.BinFlowTests.login
    job=legacy.BinFlowTests.job
    initial_delivery=legacy.BinFlowTests.initial_delivery
    exchange=legacy.BinFlowTests.exchange

    def save_profile(self, site=3, small=3, large=0, version=None):
        if version is None:
            rows=self.rows('SELECT version FROM bf_site_profiles WHERE location_id=?',(site,))
            version=rows[0]['version'] if rows else 0
        return self.post(f'/binflow/locations/{site}/requirements',{'version':version,'quantity_1':small,'quantity_2':0,'quantity_3':large,'notes':'Service target'})

    def connection(self):
        conn=sqlite3.connect(self.path);conn.row_factory=sqlite3.Row
        return conn

    def test_edit_profile_audit_stale_forms_and_no_stock_mutations(self):
        self.initial_delivery()
        before={table:self.rows('SELECT * FROM '+table) for table in ('bf_bins','bf_jobs','bf_job_bins','bf_events')}
        self.assertIn(b'Client bin requirements saved.',self.save_profile(small=4,large=2).data)
        self.assertEqual(self.save_profile(small=2,version=0).status_code,400)
        self.assertEqual(self.save_profile(small=-1).status_code,400)
        self.assertEqual(self.save_profile(small='2.2').status_code,400)
        self.assertEqual(self.save_profile(small='1e3').status_code,400)
        self.assertEqual(self.save_profile(small=10001).status_code,400)
        self.assertEqual(len(self.rows('SELECT * FROM bf_profile_history')),1)
        self.save_profile(small=4,large=2)
        self.assertEqual(len(self.rows('SELECT * FROM bf_profile_history')),1)
        self.save_profile(small=5,large=2)
        self.assertEqual(len(self.rows('SELECT * FROM bf_profile_history')),2)
        self.assertEqual({t:self.rows('SELECT * FROM '+t) for t in before},before)
        for path in ('/binflow/locations/3/requirements','/binflow/allocations','/binflow/allocations?state=QLD','/binflow/pickups/new?site=3&warehouse=1'):
            self.assertEqual(self.client.get(path).status_code,200)
        self.assertEqual(self.client.get('/binflow/locations/1/requirements').status_code,404)
        self.assertEqual(self.client.get('/binflow/locations/999/requirements').status_code,404)
        self.assertEqual(self.client.post('/binflow/locations/3/requirements',data={'version':2}).status_code,400)

    def test_loading_suggestions_topup_exchange_and_other_bookings(self):
        self.save_profile(small=4)
        with self.connection() as conn:
            profile,plan=site_plan(conn,3,1)
        self.assertEqual((plan[0]['onsite'],plan[0]['collect'],plan[0]['recommended'],plan[0]['selected']),(0,0,4,4))
        self.initial_delivery()  # Two bins now at site.
        with self.connection() as conn:
            _,plan=site_plan(conn,3,1)
            self.assertEqual((plan[0]['onsite'],plan[0]['collect'],plan[0]['recommended']),(2,2,4))
            _,partial=site_plan(conn,3,1,{1:1})
            self.assertEqual(partial[0]['recommended'],3)
        self.job(collect=(1,),deliver=(3,4),legacy=False)
        with self.connection() as conn:
            _,plan=site_plan(conn,3,1,{1:1})
        self.assertEqual((plan[0]['incoming'],plan[0]['outgoing'],plan[0]['recommended']),(2,1,2))
        # Future reservations cannot falsely supply stock for an earlier run.
        with self.connection() as conn:
            conn.execute("UPDATE bf_jobs SET scheduled_date='2026-11-01' WHERE id=2")
            _,early=site_plan(conn,3,1,{1:1},'2026-10-19')
            _,later=site_plan(conn,3,1,{1:1},'2026-11-01')
        self.assertEqual(early[0]['recommended'],3)
        self.assertEqual(later[0]['recommended'],2)

    def test_conversion_preserves_capacity_and_flags_duplicate_source_lines(self):
        quantities,review,counts=converted_requirements({'equipment':'7 × PD 660L Perm Container (row 1)\n7 × PD 660L Perm Container (row 2)\n2 × PD Cage\n3 × PD 240L'})
        self.assertEqual(quantities,{240:3,1100:7})  # ceil(7*660/1100)=5, plus 2 cages.
        self.assertEqual(counts['bin660'],7)
        self.assertIn('Repeated service lines',review)
        quantities,review,_=converted_requirements({'containers':{'bin240':2,'bin660':2,'cage':3,'pallecon':4}})
        self.assertEqual(quantities,{240:2,1100:5})
        self.assertIn('4 pallecons',review)
        quantities,review,_=converted_requirements({'equipment':'Unconfirmed equipment'})
        self.assertEqual(quantities,{240:0,1100:0})
        self.assertIn('No confirmed',review)

    def test_population_once_preserves_manual_profiles_stock_and_history(self):
        self.save_profile(site=4,small=7)
        original=self.rows('SELECT * FROM bf_bins')
        with self.connection() as conn:
            conn.execute("INSERT INTO bf_import_runs VALUES('test',1,1,'now','{}','now')")
            for lid,equipment in ((3,'3 × Cage\n3 × Cage\n2 × 240L'),(4,'99 × Cage'),(5,'Unknown')):
                conn.execute('INSERT INTO bf_location_sources VALUES(?,?,?,?,?)',(str(lid),'test',lid,'collection',json.dumps({'source':{'equipment':equipment},'address_review':False})))
            conn.commit()
            summary,changed=populate_requirements(conn,'2026-10-19')
            self.assertTrue(changed)
            self.assertEqual((summary['profiles_added'],summary['profiles_preserved'],summary['review_sites']),(2,1,2))
            self.assertFalse(populate_requirements(conn,'later')[1])
        self.assertEqual(self.rows('SELECT * FROM bf_bins'),original)
        self.assertEqual(self.rows('SELECT quantity FROM bf_site_requirements WHERE location_id=4 AND size_id=1')[0]['quantity'],7)
        self.assertTrue(self.rows('SELECT review_note FROM bf_site_profiles WHERE location_id=3')[0]['review_note'])
        self.save_profile(site=3,small=2,large=3)
        self.assertFalse(self.rows('SELECT review_note FROM bf_site_profiles WHERE location_id=3')[0]['review_note'])
        with self.connection() as conn:
            self.assertFalse(populate_requirements(conn,'again')[1])
        self.assertEqual(self.rows('SELECT version FROM bf_site_profiles WHERE location_id=3')[0]['version'],2)

    def test_run_totals_partial_scans_shortfalls_and_date_isolation(self):
        self.save_profile(small=4)
        self.job(deliver=(1,2),legacy=False)
        self.save_profile(site=4,small=2)
        self.job(deliver=(3,4),site=4,legacy=False)
        self.post('/binflow/driver',{'name':'Loading truck','state':'QLD'})
        for jid in (1,2):self.post(f'/binflow/pickups/{jid}/truck',{'truck_id':1})
        with self.connection() as conn:
            plan=load_plan(conn,1,'2026-10-19')
        self.assertEqual(plan['total_to_load'],4)
        self.assertEqual(plan['stops'][0]['shortfalls'][0]['shortfall'],2)
        prestart(self)
        self.post('/binflow/driver/1/scan',{'job_id':1,'action':'load','code':'BF000001'})
        with self.connection() as conn:plan=load_plan(conn,1,'2026-10-19')
        self.assertEqual((plan['total_to_load'],plan['totals'][0]['loaded']),(3,1))
        for jid,bid in ((1,2),(2,3),(2,4)):
            self.post('/binflow/driver/1/scan',{'job_id':jid,'action':'load','code':f'BF{bid:06d}'})
        depart(self)
        self.post('/binflow/driver/1/scan',{'job_id':1,'action':'deliver','code':'BF000001'})
        with self.connection() as conn:
            plan=load_plan(conn,1,'2026-10-19')
            self.assertEqual((plan['total_to_load'],plan['totals'][0]['loaded'],plan['totals'][0]['delivered']),(0,3,1))
            self.assertFalse(load_plan(conn,1,'2026-10-20')['stops'])
        self.assertEqual(self.client.get('/binflow/driver/1/load-list?date=2026-10-19').status_code,200)
        self.assertEqual(self.client.get('/binflow/driver/1/load-list?date=bad').status_code,400)
        # Later profile changes cannot rewrite a saved run's load calculation.
        saved=self.rows('SELECT bin_plan FROM bf_jobs WHERE id=1')
        self.save_profile(small=8)
        self.assertEqual(self.rows('SELECT bin_plan FROM bf_jobs WHERE id=1'),saved)

    def test_state_allowances_permissions_and_counts(self):
        self.save_profile(small=3,large=2)
        self.assertIn(b'3',self.client.get('/binflow/allocations?state=QLD').data)
        self.post('/binflow/allocations?state=QLD',{'rotation_QLD_1':2,'rotation_QLD_3':1})
        self.assertEqual(self.rows('SELECT state,size_id,rotation FROM bf_state_bin_pools ORDER BY size_id'),[{'state':'QLD','size_id':1,'rotation':2},{'state':'QLD','size_id':3,'rotation':1}])
        self.assertEqual(self.post('/binflow/allocations',{'rotation_QLD_1':-1}).status_code,400)
        self.assertEqual(self.client.get('/binflow/allocations?state=BAD').status_code,400)
        self.post('/logout')
        self.assertEqual(self.client.get('/binflow/allocations').status_code,302)
        self.assertEqual(self.client.get('/binflow/locations/3/requirements').status_code,302)


if __name__=='__main__':unittest.main()
