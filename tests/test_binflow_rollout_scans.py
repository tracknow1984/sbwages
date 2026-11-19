import json
import secrets
import sqlite3
import unittest

from binflow_helpers import RUN_DATE
import test_binflow_journey as journey


class RolloutScanTests(unittest.TestCase):
    post, rows, login, job = journey.DriverJourneyTests.post, journey.DriverJourneyTests.rows, journey.DriverJourneyTests.login, journey.DriverJourneyTests.job
    initial_delivery, exchange = journey.DriverJourneyTests.initial_delivery, journey.DriverJourneyTests.exchange
    stamp, choose, stage = journey.DriverJourneyTests.stamp, journey.DriverJourneyTests.choose, journey.DriverJourneyTests.stage
    start, accept_overview, simple_prestart = journey.DriverJourneyTests.start, journey.DriverJourneyTests.accept_overview, journey.DriverJourneyTests.simple_prestart
    action, scan, tearDown = journey.DriverJourneyTests.action, journey.DriverJourneyTests.scan, journey.DriverJourneyTests.tearDown

    def setUp(self):
        journey.DriverJourneyTests.setUp(self)
        self.post('/logout')
        self.login()
        # The fixture has two bins at Alpha and one exact-ID swap. Remove the unstarted
        # swap via the office to leave normal warehouse stock for two rollout stops.
        self.post('/binflow/pickups/2/cancel',dict(reason='Replaced by empty rollout'))
        for site, small, large in [(3,3,1),(4,1,0)]:
            self.post(f'/binflow/locations/{site}/requirements',dict(version=0,quantity_1=small,quantity_2=0,quantity_3=large))
        self.post('/binflow/bins',dict(warehouse_id=1,size_id=3,quantity=1))
        self.post('/binflow/bins',dict(warehouse_id=1,size_id=2,quantity=1))
        self.post('/binflow/bins',dict(warehouse_id=2,size_id=1,quantity=1))
        self.post('/binflow/bins/3/condition',dict(service_version=1,service_status='damaged'))

    def book(self, site, **sizes):
        return self.post('/binflow/pickups/new',dict(purpose='empty_delivery',site_id=site,warehouse_id=1,
            scheduled_date=RUN_DATE,request_key=secrets.token_urlsafe(24),**sizes))

    def prepare_run(self):
        self.assertIn('Schedule empty-bin delivery',self.client.get('/binflow/pickups/new?purpose=empty_delivery&site=3').text)
        self.assertIn('exceed the remaining',self.book(3,rollout_qty_1=2).text)
        for site, sizes in [(3,dict(rollout_qty_1=1,rollout_qty_3=1)),(4,dict(rollout_qty_1=1))]:
            self.assertIn('awaiting approval',self.book(site,**sizes).text)
            jid=self.rows('SELECT MAX(id) AS id FROM bf_jobs')[0]['id']
            self.assertIn('Allocation approved.',self.post(f'/binflow/pickups/{jid}/review',dict(decision='approve')).text)
            self.post(f'/binflow/pickups/{jid}/truck',dict(truck_id=1))
        self.assertFalse(self.rows('SELECT * FROM bf_job_bins WHERE job_id IN (3,4)'))
        self.assertIn('exceed the remaining',self.book(4,rollout_qty_1=1).text)
        self.post('/logout');self.login('employee','phonedriver')
        self.start();self.accept_overview();self.choose();self.simple_prestart()
        self.assertEqual(self.stage(),'load')

    def test_any_matching_empty_assigned_only_at_site(self):
        self.prepare_run()
        for code in ['BF000008','BF000009','BF000003','BF000001']:
            self.assertEqual(self.scan('load',code).status_code,409,code)
        token=self.rows('SELECT token FROM bf_bins WHERE id=7')[0]['token']
        qr='/binflow/qr/'+token+'/driver/load'
        self.assertIn('Allocate this bin to truck',self.client.get(qr).text)
        self.assertIn('Truck loading recorded',self.post(qr,dict(job_id=3)).text)
        for code in ['BF000005','BF000006']:
            self.assertTrue(self.scan('load',code).json['ok'])
        self.assertTrue(self.scan('load','BF000006').json['duplicate'])
        self.assertEqual(self.stage(),'ready')
        self.assertFalse(self.rows('SELECT * FROM bf_job_bins WHERE job_id IN (3,4)'))
        self.assertEqual(len(self.rows("SELECT * FROM bf_events WHERE event='Empty bin scanned onto truck' AND job_id IS NULL")),3)
        self.assertEqual(self.scan('deliver','BF000006').status_code,409)
        self.action('depart')
        self.action('client-arrival',job_id=3)
        self.assertTrue(self.scan('deliver','BF000006').json['ok'])
        self.assertEqual(self.scan('deliver','BF000005').status_code,409)
        self.assertEqual(self.stage(),'deliver')
        self.assertTrue(self.scan('deliver','BF000006').json['duplicate'])
        self.assertIn('Record delivery now',self.client.get('/binflow/qr/'+token+'/driver/delivery').text)
        self.assertIn('Delivery recorded',self.post('/binflow/qr/'+token+'/driver/delivery',dict(job_id=3)).text)
        self.assertEqual(self.stage(),'navigate')
        self.action('client-arrival',job_id=4)
        self.assertTrue(self.scan('deliver','BF000005').json['ok'])
        self.assertEqual(self.rows('SELECT bin_id,job_id FROM bf_job_bins WHERE job_id IN (3,4) ORDER BY bin_id'),
                         [dict(bin_id=5,job_id=4),dict(bin_id=6,job_id=3),dict(bin_id=7,job_id=3)])
        self.assertEqual([r['location_id'] for r in self.rows('SELECT location_id FROM bf_bins WHERE id IN (5,6,7) ORDER BY id')],[4,3,3])
        self.assertEqual(self.stage(),'navigate_depot')
        self.assertTrue(all(r['client_name'] is None for r in json.loads(self.rows('SELECT onboard_manifest FROM bf_runs WHERE truck_id=1')[0]['onboard_manifest'])))

    def test_partial_delivery_then_cancel_leaves_unassigned_stock_for_next_site_or_depot(self):
        self.prepare_run()
        for code in ['BF000006','BF000007','BF000005']:
            self.assertTrue(self.scan('load',code).json['ok'])
        self.action('depart');self.action('client-arrival',job_id=3)
        self.scan('deliver','BF000007')
        self.action('cancel-stop',job_id=3,reason='Site only accepted the large bin')
        self.assertEqual(self.stage(),'navigate')
        self.action('client-arrival',job_id=4)
        self.assertTrue(self.scan('deliver','BF000006').json['ok'])
        self.action('depot-arrival',warehouse_id=1)
        self.assertEqual(self.stage(),'return_empty')
        self.assertTrue(self.scan('return_empty','BF000005').json['ok'])
        self.assertTrue(self.scan('return_empty','BF000005').json['duplicate'])
        self.assertEqual(self.rows('SELECT location_id,status FROM bf_bins WHERE id=5')[0],dict(location_id=1,status='empty'))
        self.assertFalse(self.rows('SELECT * FROM bf_job_bins WHERE bin_id=5'))
        exc=self.rows("SELECT * FROM bf_stop_exceptions WHERE original_id=3")[0]
        with sqlite3.connect(self.path) as conn:
            conn.row_factory=sqlite3.Row
            from binflow_stop_exceptions import rebook
            missed=conn.execute('SELECT * FROM bf_jobs WHERE id=?',(exc['job_id'],)).fetchone()
            replacement=rebook(conn,missed,'2026-10-20',self.uid,'Operations',RUN_DATE)
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM bf_rollout_slots WHERE job_id=? AND item_id IS NULL',(replacement,)).fetchone()[0],1)

    def test_schedule_before_stock_arrives_and_convert_an_existing_unstarted_delivery(self):
        with sqlite3.connect(self.path) as conn:
            conn.execute("UPDATE bf_bins SET service_status='damaged' WHERE location_id=1")
        self.assertIn('awaiting approval', self.book(3,rollout_qty_3=1).text)
        self.assertFalse(self.rows('SELECT * FROM bf_job_bins WHERE job_id=3'))
        with sqlite3.connect(self.path) as conn:
            conn.execute("UPDATE bf_bins SET service_status='in_service' WHERE location_id=1")
        self.job(site=4,deliver=(5,),legacy=False)
        self.assertIn('Switch to rollout by size',self.client.get('/binflow/pickups/4').text)
        self.assertIn('Rollout now uses the scheduled sizes',self.post('/binflow/pickups/4/rollout-by-size').text)
        self.assertFalse(self.rows('SELECT * FROM bf_job_bins WHERE job_id=4'))
        self.assertEqual(self.rows('SELECT size_id,item_id FROM bf_rollout_slots WHERE job_id=4'),[dict(size_id=1,item_id=None)])
        self.post('/binflow/pickups/4/rollout-by-size')
        self.assertEqual(len(self.rows('SELECT * FROM bf_rollout_slots WHERE job_id=4')),1)
        self.assertTrue(self.rows("SELECT * FROM bf_events WHERE job_id=4 AND bin_id=5 AND event LIKE 'Preselected empty%'"))
