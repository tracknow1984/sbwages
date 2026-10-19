import sqlite3
import unittest
from concurrent.futures import ThreadPoolExecutor

from app import create_app
import test_binflow as legacy


class DriverScanTests(unittest.TestCase):
    setUp = legacy.BinFlowTests.setUp
    tearDown = legacy.BinFlowTests.tearDown
    post = legacy.BinFlowTests.post
    login = legacy.BinFlowTests.login
    rows = legacy.BinFlowTests.rows
    exchange = legacy.BinFlowTests.exchange
    initial_delivery = legacy.BinFlowTests.initial_delivery
    job = legacy.BinFlowTests.job

    def prepare(self, collect=(1, 2), deliver=(3, 4)):
        self.initial_delivery()  # Existing stock / historic POD before the truck workflow.
        self.post('/binflow/driver', {'name':'QLD Truck 01','state':'QLD'})
        self.job(collect=collect, deliver=deliver, legacy=False)
        self.post('/binflow/pickups/2/truck', {'truck_id':1})

    def scan(self, action, bid, jid=2, truck=1, expected=200):
        with self.client.session_transaction() as session:
            csrf = session['csrf']
        response = self.client.post(f'/binflow/driver/{truck}/scan',
            data={'csrf':csrf,'job_id':jid,'action':action,'code':f'BF{bid:06d}' if isinstance(bid,int) else bid},
            headers={'Accept':'application/json'})
        self.assertEqual(response.status_code, expected, response.json)
        return response.json

    def custody(self, bid):
        return self.rows('SELECT location_id,truck_id,truck_load,status FROM bf_bins WHERE id=?', (bid,))[0]

    def assert_custody(self, bid, location, truck, load, status):
        self.assertEqual(self.custody(bid), dict(location_id=location, truck_id=truck, truck_load=load, status=status))

    def weigh(self, bid, gross='25'):
        iid = self.rows('SELECT id FROM bf_job_bins WHERE job_id=2 AND bin_id=?', (bid,))[0]['id']
        return self.post(f'/binflow/pickups/2/weigh/{iid}', {'gross':gross,'tare':'5','confirm_tare':'yes'})

    def test_four_scans_partial_receipt_and_final_pod(self):
        self.prepare()
        tokens = self.rows('SELECT id,token FROM bf_bins ORDER BY id')
        old_pod = self.client.get('/binflow/pickups/1/pod.pdf').data
        self.scan('deliver',3,expected=409)
        self.scan('load',3)
        self.assert_custody(3,None,1,'empty','transit')
        self.assert_custody(4,1,None,None,'empty')
        self.assertIn(b'Empty on truck',self.client.get('/binflow/bins/3').data)
        self.scan('load','https://example.com/binflow/qr/'+tokens[3]['token'])
        self.scan('deliver',3)
        self.assert_custody(3,3,None,None,'at_client')
        self.scan('collect',1)
        self.assert_custody(1,None,1,'full','transit')
        self.scan('return',1,expected=409)
        self.exchange(2,(1,2,3,4))
        self.assertIsNone(self.rows('SELECT collected_at FROM bf_jobs WHERE id=2')[0]['collected_at'])
        self.scan('deliver',4)
        self.scan('collect',2)
        self.assertIn(b'Client scans complete',self.client.get('/binflow/driver/1?job=2').data)
        self.exchange(2,())
        self.assertEqual(self.rows('SELECT status FROM bf_jobs WHERE id=2')[0]['status'],'collected')
        self.assertIn(b'WEIGHT PENDING',self.client.get('/binflow/pickups/2/pod').data)
        self.post('/binflow/pickups/2/receive',{'confirmed':[1,2]})
        self.assert_custody(1,None,1,'full','transit')
        self.weigh(1)
        self.assertFalse(self.rows('SELECT weighed_at FROM bf_job_bins WHERE job_id=2 AND bin_id=1')[0]['weighed_at'])
        self.scan('return',1)
        self.assert_custody(1,1,None,None,'received')
        self.assert_custody(2,None,1,'full','transit')
        self.weigh(1)
        self.assert_custody(1,1,None,None,'weighed')
        self.assertEqual(self.rows('SELECT status FROM bf_jobs WHERE id=2')[0]['status'],'collected')
        self.scan('return',2)
        self.assertEqual(self.rows('SELECT status FROM bf_jobs WHERE id=2')[0]['status'],'received')
        self.weigh(2,'35')
        self.assertEqual(self.rows('SELECT status FROM bf_jobs WHERE id=2')[0]['status'],'complete')
        self.assertEqual(self.rows('SELECT SUM(net_g) AS n FROM bf_job_bins WHERE job_id=2')[0]['n'],50000)
        self.assertTrue(self.client.get('/binflow/pickups/2/pod.pdf').data.startswith(b'%PDF-'))
        self.assertFalse(self.rows('SELECT * FROM bf_bins WHERE truck_id=1'))
        self.assertEqual(self.rows('SELECT id,token FROM bf_bins ORDER BY id'),tokens)
        self.assertTrue(old_pod.startswith(b'%PDF-'))
        events = self.rows('SELECT from_location,to_location,from_truck,to_truck FROM bf_events WHERE job_id=2 AND (from_truck IS NOT NULL OR to_truck IS NOT NULL)')
        self.assertEqual(len(events),8)
        self.assertTrue(all(e['from_truck'] or e['to_truck'] for e in events))

    def test_repeat_and_concurrent_scans_never_move_bins_twice(self):
        self.prepare(collect=(),deliver=(3,))
        with self.client.session_transaction() as session:
            auth = dict(session)
        def simultaneous(_):
            client = self.app.test_client()
            with client.session_transaction() as session:
                session.update(auth)
            return client.post('/binflow/driver/1/scan',data={'csrf':auth['csrf'],'job_id':2,'action':'load','code':'BF000003'},headers={'Accept':'application/json'})
        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(simultaneous,range(2)))
        self.assertEqual([r.status_code for r in results],[200,200])
        self.assertEqual(sum(r.json['duplicate'] for r in results),1)
        self.scan('deliver',3)
        events = self.rows('SELECT * FROM bf_events')
        self.assertTrue(self.scan('load',3)['duplicate'])
        self.assertTrue(self.scan('deliver',3)['duplicate'])
        self.assertEqual(self.rows('SELECT * FROM bf_events'),events)
        self.assert_custody(3,3,None,None,'at_client')
        self.exchange(2,())
        self.assertEqual(self.rows('SELECT status FROM bf_jobs WHERE id=2')[0]['status'],'complete')
        self.assertTrue(self.scan('deliver',3)['duplicate'])

    def test_multiple_client_stops_share_truck_without_mixing_allocations(self):
        self.prepare(collect=(1,),deliver=(3,))
        self.job(deliver=(4,),site=4,legacy=False)
        self.post('/binflow/pickups/3/truck',{'truck_id':1})
        self.post('/binflow/driver',{'name':'QLD Truck 02','state':'QLD'})
        self.scan('load',3)
        self.scan('load',4,jid=3)
        state=self.client.get('/binflow/driver/1/state?job=2').json['html']
        self.assertIn('2 empty',state)
        self.scan('deliver',4,expected=409)
        self.scan('deliver',3,truck=2,expected=409)
        self.scan('collect',2,expected=409)
        self.scan('load','BF999999',expected=409)
        self.scan('collect',3,expected=409)
        self.assertEqual(len(self.rows('SELECT * FROM bf_bins WHERE truck_id=1')),2)
        self.scan('deliver',4,jid=3)
        self.exchange(3,())
        self.assert_custody(4,4,None,None,'at_client')
        self.assert_custody(3,None,1,'empty','transit')

    def test_unload_cancellation_and_reassignment_preserve_custody(self):
        self.prepare(collect=(),deliver=(3,))
        self.post('/binflow/driver',{'name':'QLD Truck 02','state':'QLD'})
        self.post('/binflow/driver',{'name':'NSW Truck 01','state':'NSW'})
        self.post('/binflow/pickups/2/truck',{'truck_id':3})
        self.assertEqual(self.rows('SELECT truck_id FROM bf_jobs WHERE id=2')[0]['truck_id'],1)
        self.scan('load',3)
        self.post('/binflow/pickups/2/cancel',{'reason':'Changed route'})
        self.post('/binflow/pickups/2/truck',{'truck_id':2})
        self.assertEqual(self.rows('SELECT status,truck_id FROM bf_jobs WHERE id=2')[0],{'status':'scheduled','truck_id':1})
        self.scan('unload',3)
        self.assertTrue(self.scan('unload',3)['duplicate'])
        self.assert_custody(3,1,None,None,'empty')
        self.post('/binflow/pickups/2/truck',{'truck_id':2})
        self.scan('load',3,truck=2)
        self.scan('unload',3,truck=2)
        self.post('/binflow/pickups/2/cancel',{'reason':'Changed route'})
        self.assertEqual(self.rows('SELECT status FROM bf_jobs WHERE id=2')[0]['status'],'cancelled')
        self.assertFalse(self.rows('SELECT * FROM bf_job_bins WHERE job_id=2 AND active=1'))
        self.scan('load',3,truck=2,expected=409)

    def test_new_jobs_require_scans_access_and_restart_preserve_movements(self):
        self.prepare(collect=(1,),deliver=(3,))
        self.assertEqual(self.rows('SELECT scan_mode FROM bf_jobs WHERE id=2')[0]['scan_mode'],1)
        for route in ('/binflow/driver','/binflow/driver/1','/binflow/driver/1/state?job=2'):
            self.assertEqual(self.client.get(route).status_code,200)
        self.assertEqual(self.client.get('/binflow/driver/1/scan').status_code,405)
        self.assertEqual(self.client.post('/binflow/driver/1/scan',data={'job_id':2,'action':'load','code':'BF000003'}).status_code,400)
        self.scan('load',3)
        before = self.rows('SELECT * FROM bf_bins')
        create_app(dict(self.app.config))
        self.assertEqual(self.rows('SELECT * FROM bf_bins'),before)
        self.assert_custody(3,None,1,'empty','transit')
        with sqlite3.connect(self.path) as conn:
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute('UPDATE bf_bins SET location_id=1 WHERE id=3')
        self.post('/logout')
        self.assertEqual(self.client.get('/binflow/driver/1').status_code,302)
        # Authenticated staff still require explicit transport access.
        with sqlite3.connect(self.path) as conn:
            from werkzeug.security import generate_password_hash
            conn.execute("INSERT INTO users(role,first_name,last_name,email,username,password_hash) VALUES('employee','Scan','Driver','scan@example.com','scan',?)",(generate_password_hash('test-admin-password'),))
        self.login('employee','scan')
        self.assertEqual(self.client.get('/binflow/driver/1').status_code,403)
        self.assertEqual(self.post('/binflow/driver/1/scan',{'job_id':2,'action':'collect','code':'BF000001'}).status_code,403)
        uid=self.rows("SELECT id FROM users WHERE username='scan'")[0]['id']
        with sqlite3.connect(self.path) as conn:
            conn.execute('INSERT INTO bf_access(user_id) VALUES(?)',(uid,))
        self.scan('deliver',3)
        self.scan('collect',1)
        self.assertEqual(self.post('/binflow/driver',{'name':'Blocked','state':'QLD'}).status_code,403)

    def test_old_schema_migration_preserves_stock_and_dockets(self):
        from binflow import SCHEMA
        from binflow_driver import migrate_driver
        self.initial_delivery()
        conn = sqlite3.connect(':memory:')
        conn.row_factory = sqlite3.Row
        conn.executescript(SCHEMA)
        originals = {}
        for table in ('bf_clients','bf_locations','bf_sizes','bf_bins','bf_jobs','bf_job_bins','bf_events'):
            columns = ','.join(row['name'] for row in conn.execute(f'PRAGMA table_info({table})'))
            originals[table] = (columns, self.rows(f'SELECT {columns} FROM {table} ORDER BY id'))
            for record in originals[table][1]:
                conn.execute(f'INSERT INTO {table}({columns}) VALUES({",".join("?" for _ in record)})',tuple(record.values()))
        migrate_driver(conn)
        migrate_driver(conn)
        for table, (columns, records) in originals.items():
            self.assertEqual([dict(r) for r in conn.execute(f'SELECT {columns} FROM {table} ORDER BY id')],records)
        self.assertEqual(conn.execute('SELECT scan_mode FROM bf_jobs').fetchone()[0],0)
        self.assertEqual(conn.execute('SELECT COUNT(*) FROM bf_bins WHERE truck_id IS NOT NULL').fetchone()[0],0)
        conn.close()


if __name__ == '__main__':
    unittest.main()
