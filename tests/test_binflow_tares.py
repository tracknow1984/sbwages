import sqlite3
import unittest

import test_binflow as legacy
from binflow_tares import STANDARD_TARES, UPDATE_ID, apply_standard_tares


class StandardTareTests(unittest.TestCase):
    setUp = legacy.BinFlowTests.setUp
    tearDown = legacy.BinFlowTests.tearDown
    post = legacy.BinFlowTests.post
    rows = legacy.BinFlowTests.rows
    login = legacy.BinFlowTests.login
    job = legacy.BinFlowTests.job
    exchange = legacy.BinFlowTests.exchange
    initial_delivery = legacy.BinFlowTests.initial_delivery

    def test_default_registration_and_new_docket_weights(self):
        defaults = self.rows('SELECT litres,default_tare_g FROM bf_sizes')
        self.assertEqual({r['litres']:r['default_tare_g'] for r in defaults}, STANDARD_TARES)
        self.assertIn(b'value="13.0" required data-default-tare', self.client.get('/binflow/bins').data)
        for size, tare in ((1,13000),(2,42000),(3,42000)):
            values = dict(warehouse_id=1,size_id=size,quantity=1)
            if size==2: values['tare']=''
            self.assertIn(b'1 bins registered', self.post('/binflow/bins',values).data)
            self.assertEqual(self.rows('SELECT tare_g FROM bf_bins ORDER BY id DESC LIMIT 1')[0]['tare_g'],tare)
        self.job(deliver=(7,8,9))
        self.assertEqual([r['tare_g'] for r in self.rows('SELECT tare_g FROM bf_job_bins ORDER BY bin_id')],[13000,42000,42000])
        self.post('/binflow/bins',dict(warehouse_id=1,size_id=1,quantity=1,tare='13.5'))
        self.assertEqual(self.rows('SELECT tare_g FROM bf_bins ORDER BY id DESC LIMIT 1')[0]['tare_g'],13500)
        setup=self.client.get('/binflow/setup').data
        for label in ('240 L · 13 kg empty','660 L · 42 kg empty','1100 L · 42 kg empty'):
            self.assertIn(label.encode(),setup)

    def test_one_time_upgrade_preserves_pods_identity_and_later_measurements(self):
        self.initial_delivery()
        self.job(collect=(1,));self.exchange(2,(1,))
        self.post('/binflow/pickups/2/receive',{'confirmed':[1]})
        iid=self.rows('SELECT id FROM bf_job_bins WHERE job_id=2')[0]['id']
        self.post(f'/binflow/pickups/2/weigh/{iid}',dict(gross='20',tare='5',confirm_tare='yes'))
        self.job(collect=(2,));self.exchange(3,(2,))
        self.post('/binflow/pickups/3/receive',{'confirmed':[2]})
        self.job(deliver=(3,));self.post('/binflow/pickups/4/cancel',dict(reason='Test cancellation'))
        self.job(deliver=(4,))
        for size in (2,3):self.post('/binflow/bins',dict(warehouse_id=1,size_id=size,quantity=1,tare='0'))
        self.post('/binflow/setup',dict(action='size',name='Custom 120 L',litres=120,tare='8'))
        self.post('/binflow/bins',dict(warehouse_id=1,size_id=4,quantity=1,tare='8'))
        historical=self.rows('SELECT * FROM bf_job_bins WHERE job_id IN (1,2,4) ORDER BY id')
        identity=self.rows('SELECT id,code,token,location_id,truck_id,status FROM bf_bins ORDER BY id')
        with sqlite3.connect(self.path) as conn:
            conn.row_factory=sqlite3.Row
            conn.execute('DELETE FROM bf_tare_updates WHERE id=?',(UPDATE_ID,))
            conn.execute('UPDATE bf_sizes SET default_tare_g=0 WHERE litres IN (240,660,1100)')
            conn.commit()
            result=apply_standard_tares(conn,'2026-10-19T14:18:44+10:00')
        self.assertEqual(result['bins_updated'],8)
        self.assertEqual(result['pending_items_updated'],2)
        self.assertEqual(self.rows('SELECT * FROM bf_job_bins WHERE job_id IN (1,2,4) ORDER BY id'),historical)
        self.assertEqual(self.rows('SELECT id,code,token,location_id,truck_id,status FROM bf_bins ORDER BY id'),identity)
        self.assertEqual([r['tare_g'] for r in self.rows('SELECT tare_g FROM bf_bins ORDER BY id')],[13000]*6+[42000,42000,8000])
        self.assertEqual([r['tare_g'] for r in self.rows('SELECT tare_g FROM bf_job_bins WHERE job_id IN (3,5)')],[13000,13000])
        item=self.rows('SELECT id FROM bf_job_bins WHERE job_id=3')[0]['id']
        self.post(f'/binflow/pickups/3/weigh/{item}',dict(gross='30',tare='13',confirm_tare='yes'))
        self.assertEqual(self.rows('SELECT net_g FROM bf_job_bins WHERE job_id=3')[0]['net_g'],17000)
        self.assertEqual(self.rows('SELECT net_g FROM bf_job_bins WHERE job_id=2')[0]['net_g'],15000)
        self.post('/binflow/bins/6',dict(action='tare',tare='14'))
        events=self.rows('SELECT * FROM bf_events ORDER BY id')
        from app import create_app
        create_app(dict(self.app.config))
        self.assertEqual(self.rows('SELECT tare_g FROM bf_bins WHERE id=6')[0]['tare_g'],14000)
        self.assertEqual(self.rows('SELECT * FROM bf_events ORDER BY id'),events)
        self.assertEqual(len(self.rows('SELECT * FROM bf_tare_updates')),1)
