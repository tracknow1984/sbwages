"""Acceptance regression: claims, stale schedules, custody and deployed-run migration."""
import json
import re
import sqlite3
import unittest
from concurrent.futures import ThreadPoolExecutor

import test_binflow as legacy
from app import create_app
from binflow_acceptance import migrate_acceptance
from binflow_helpers import accept, prestart, depart, RUN_DATE, SIGNATURE
from binflow_runs import run_data, CHECKS
from werkzeug.security import generate_password_hash


class AcceptanceTests(unittest.TestCase):
    setUp=legacy.BinFlowTests.setUp
    tearDown=legacy.BinFlowTests.tearDown
    post=legacy.BinFlowTests.post
    rows=legacy.BinFlowTests.rows
    login=legacy.BinFlowTests.login
    job=legacy.BinFlowTests.job

    def prepare(self):
        self.post('/binflow/driver',{'name':'QLD acceptance truck','state':'QLD'})
        self.job(deliver=(1,),legacy=False)
        self.post('/binflow/pickups/1/truck',{'truck_id':1})

    def version(self,truck=1,day=RUN_DATE):
        page=self.client.get(f'/binflow/driver/{truck}/day?date={day}')
        return re.search(rb'name="manifest_version" value="([^"]+)"',page.data)[1].decode()

    def stage(self,truck=1,day=RUN_DATE):
        return self.client.get(f'/binflow/driver/{truck}/day/state?date={day}').json['stage']

    def scan(self,action='load',bin_id=1):
        with self.client.session_transaction() as s: csrf=s['csrf']
        return self.client.post('/binflow/driver/1/scan',data={'csrf':csrf,'job_id':1,'action':action,'code':f'BF{bin_id:06d}'},headers={'Accept':'application/json'})

    def driver(self,name):
        with sqlite3.connect(self.path) as conn:
            uid=conn.execute("INSERT INTO users(role,first_name,last_name,email,username,password_hash) VALUES('employee',?,'Driver',?,?,?)",(name,name+'@example.com',name,generate_password_hash('test-admin-password'))).lastrowid
            conn.execute('INSERT INTO bf_access(user_id) VALUES(?)',(uid,))
        return uid

    def test_state_day_cards_only_offer_approved_assigned_jobs(self):
        self.prepare()
        self.job(deliver=(2,),site=4,legacy=False)  # Approved, but no truck.
        page=self.client.get('/binflow/driver').data
        self.assertIn(b'Select a state',page);self.assertNotIn(b'Accept schedule</button>',page)
        page=self.client.get('/binflow/driver?state=QLD&date='+RUN_DATE).data
        self.assertIn(b'QLD acceptance truck',page)
        self.assertIn(b'Accept schedule</button>',page)
        self.assertIn(b'1 \xc3\x97 240 L',page)
        self.assertIn(b'Approved jobs needing a truck',page)
        self.assertNotIn(b'Accept schedule</button>',self.client.get('/binflow/driver?state=NSW&date='+RUN_DATE).data)
        self.assertNotIn(b'Accept schedule</button>',self.client.get('/binflow/driver?state=QLD&date=2026-10-20').data)
        with sqlite3.connect(self.path) as conn: conn.execute("UPDATE bf_jobs SET approval_status='pending' WHERE id=1")
        self.assertNotIn(b'Accept schedule</button>',self.client.get('/binflow/driver?state=QLD&date='+RUN_DATE).data)
        self.assertEqual(self.client.get('/binflow/driver?state=invalid').status_code,400)
        self.assertEqual(self.client.get('/binflow/driver?date=invalid').status_code,400)

    def test_acceptance_precedes_prestart_and_scans_without_moving_stock(self):
        self.prepare();stock=self.rows('SELECT * FROM bf_bins');events=self.rows('SELECT * FROM bf_events')
        self.assertEqual(self.stage(),'acceptance')
        self.assertEqual(self.scan().status_code,409)
        depart(self)
        self.post('/binflow/driver/1/prestart',{'date':RUN_DATE,'submission_token':'a'*32})
        self.assertFalse(self.rows('SELECT * FROM prestarts'))
        token=self.version();accept(self)
        self.assertEqual(self.stage(),'prestart')
        self.assertEqual(stock,self.rows('SELECT * FROM bf_bins'));self.assertEqual(events,self.rows('SELECT * FROM bf_events'))
        run=self.rows('SELECT * FROM bf_runs')[0]
        self.assertTrue(run['accepted_at']);self.assertTrue(run['accepted_name']);self.assertEqual(run['accepted_by'],run['driver_id'])
        self.assertEqual(len(json.loads(self.rows('SELECT manifest FROM bf_run_acceptance_history')[0]['manifest'])),1)
        self.post('/binflow/driver/1/accept',{'date':RUN_DATE,'manifest_version':token})
        self.assertEqual(self.rows('SELECT * FROM bf_runs')[0],run)
        self.assertEqual(len(self.rows('SELECT * FROM bf_run_acceptance_history')),1)
        self.assertEqual(self.scan().status_code,409)
        prestart(self);self.assertEqual(self.scan().status_code,200)
        self.assertEqual(self.rows('SELECT location_id,truck_id,truck_load FROM bf_bins WHERE id=1')[0],{'location_id':None,'truck_id':1,'truck_load':'empty'})
        self.assertEqual(self.stage(),'ready')

    def test_stale_form_and_office_changes_require_review_before_loading(self):
        self.prepare();token=self.version()
        self.job(deliver=(2,),site=4,legacy=False)
        self.post('/binflow/pickups/2/truck',{'truck_id':1})
        page=self.post('/binflow/driver/1/accept',{'date':RUN_DATE,'manifest_version':token})
        self.assertIn(b'The schedule changed',page.data);self.assertFalse(self.rows('SELECT * FROM bf_run_acceptance_history'))
        accept(self);prestart(self)
        uid=self.rows("SELECT id FROM users WHERE username='admin'")[0]['id']
        values=dict(date=RUN_DATE,driver_id=uid,order_1=2,order_2=1,time_2='08:30')
        self.post('/binflow/driver/1/schedule',values)
        self.assertEqual(self.stage(),'acceptance');self.assertEqual(self.scan().status_code,409)
        accept(self);self.assertEqual(self.stage(),'load')  # Passed daily prestart is still valid.
        self.post('/binflow/driver/1/schedule',values)
        self.assertEqual(self.stage(),'load')  # No-op save does not invalidate acceptance.
        self.assertEqual(self.scan().status_code,200)
        self.post('/binflow/driver/1/schedule',{**values,'order_1':1,'order_2':2})
        self.assertEqual(self.rows('SELECT run_order FROM bf_jobs WHERE id=1')[0]['run_order'],2)
        self.job(deliver=(3,),legacy=False)
        self.post('/binflow/pickups/3/truck',{'truck_id':1})
        self.assertIsNone(self.rows('SELECT truck_id FROM bf_jobs WHERE id=3')[0]['truck_id'])
        self.assertEqual(self.scan('unload').status_code,200)
        self.post('/binflow/pickups/2/cancel',{'reason':'Client postponed'})
        self.assertEqual(self.stage(),'acceptance')
        self.assertEqual(len(self.rows("SELECT * FROM bf_run_acceptance_history WHERE action='Schedule changed; accept again'")),2)

    def test_two_drivers_cannot_claim_same_run_and_admin_cannot_operate_it(self):
        self.prepare();token=self.version();driver_ids=[self.driver(name) for name in ('one','two')]
        clients=[]
        for name in ('one','two'):
            client=self.app.test_client()
            with client.session_transaction() as s: s['csrf']='claim-'+name
            client.post('/employee/login',data={'csrf':'claim-'+name,'username':name,'password':'test-admin-password'},follow_redirects=True)
            with client.session_transaction() as s: csrf=s['csrf']
            clients.append((client,csrf))
        def claim(pair):
            client,csrf=pair
            client.post('/binflow/driver/start-day',data={'csrf':csrf})
            with client.session_transaction() as session:uid=session['uid']
            day=self.rows('SELECT id FROM bf_workdays WHERE user_id=?',(uid,))[0]['id']
            client.post(f'/binflow/driver/my-day/{day}/vehicle',data={'csrf':csrf,'state':'QLD','truck_id':1})
            page=client.get('/binflow/driver/1/day?date='+RUN_DATE)
            prestart_token=re.search(rb'name="submission_token" value="([^"]+)"',page.data)
            if prestart_token:
                values={f'check_{key}':'pass' for key,_,_ in CHECKS}
                values.update(csrf=csrf,date=RUN_DATE,submission_token=prestart_token[1].decode(),reading='12345',fit_for_duty='yes',signature=SIGNATURE,signed='yes')
                client.post('/binflow/driver/1/prestart',data=values)
            return client.post('/binflow/driver/1/accept',data={'csrf':csrf,'date':RUN_DATE,'manifest_version':token})
        with ThreadPoolExecutor(max_workers=2) as pool: responses=list(pool.map(claim,clients))
        self.assertEqual([r.status_code for r in responses],[302,302])
        run=self.rows('SELECT * FROM bf_runs')[0];self.assertIn(run['accepted_by'],driver_ids)
        self.assertEqual(len(self.rows('SELECT * FROM bf_run_acceptance_history')),1)
        self.assertEqual(self.stage(),'assigned')  # Admin can view, but cannot scan for the driver.
        self.assertEqual(self.scan().status_code,409)
        self.assertIn(b'assigned to another driver',self.post('/binflow/driver/1/accept',{'date':RUN_DATE,'manifest_version':token}).data)
        losing=clients[1 if run['accepted_by']==driver_ids[0] else 0][0]
        self.assertEqual(losing.get('/binflow/driver/1/day?date='+RUN_DATE).status_code,403)
        self.assertNotIn(b'QLD acceptance truck',losing.get('/binflow/driver?state=QLD&date='+RUN_DATE).data)

    def test_future_acceptance_and_second_truck_do_not_bypass_daily_gates(self):
        self.prepare()
        with sqlite3.connect(self.path) as conn: conn.execute("UPDATE bf_jobs SET scheduled_date='2026-10-20' WHERE id=1")
        accept(self,run_date='2026-10-20');self.assertEqual(self.stage(day='2026-10-20'),'planned')
        self.post('/binflow/driver/1/prestart',{'date':'2026-10-20','submission_token':'b'*32})
        self.assertFalse(self.rows('SELECT * FROM prestarts'));self.assertEqual(self.scan().status_code,409)
        self.post('/binflow/driver',{'name':'Another truck','state':'QLD'})
        self.job(deliver=(2,),legacy=False)
        with sqlite3.connect(self.path) as conn: conn.execute("UPDATE bf_jobs SET scheduled_date='2026-10-20' WHERE id=2")
        self.post('/binflow/pickups/2/truck',{'truck_id':2})
        response=accept(self,truck=2,run_date='2026-10-20')
        self.assertIn(b'already accepted another truck',response.data)
        self.assertFalse(self.rows('SELECT * FROM bf_runs WHERE truck_id=2'))

    def test_manifest_survives_actual_weight_corrections_and_restart(self):
        self.prepare();accept(self);prestart(self)
        with sqlite3.connect(self.path) as conn:
            conn.execute('UPDATE bf_job_bins SET tare_g=14000 WHERE job_id=1')
        self.assertEqual(self.stage(),'load')
        before=self.rows('SELECT * FROM bf_run_acceptance_history');create_app(dict(self.app.config))
        self.assertEqual(self.rows('SELECT * FROM bf_run_acceptance_history'),before)
        self.assertEqual(self.stage(),'load')

    def test_first_migration_preserves_underway_runs_without_fake_acceptance(self):
        self.prepare();accept(self);prestart(self);self.scan()
        with sqlite3.connect(self.path) as conn:
            conn.row_factory=sqlite3.Row
            conn.execute('DROP TABLE bf_run_acceptance_history')
            for column in ('acceptance_required','accepted_at','accepted_by','accepted_name','accepted_manifest'):
                conn.execute('ALTER TABLE bf_runs DROP COLUMN '+column)
            migrate_acceptance(conn)
            run=dict(conn.execute('SELECT * FROM bf_runs').fetchone())
            self.assertEqual(run['acceptance_required'],0);self.assertIsNone(run['accepted_at'])
            self.assertFalse(conn.execute('SELECT * FROM bf_run_acceptance_history').fetchall())
            self.assertEqual(run_data(conn,1,RUN_DATE,run['driver_id'],RUN_DATE)['stage'],'ready')
            # A newly created run never gets grandfathered by a later restart.
            conn.execute("INSERT INTO bf_runs(truck_id,run_date) VALUES(1,'2026-10-20')")
            migrate_acceptance(conn)
            self.assertEqual(conn.execute("SELECT acceptance_required FROM bf_runs WHERE run_date='2026-10-20'").fetchone()[0],1)

    def test_acceptance_requires_csrf_login_and_transport_access(self):
        self.prepare();token=self.version()
        self.assertEqual(self.client.post('/binflow/driver/1/accept',data={'date':RUN_DATE,'manifest_version':token}).status_code,400)
        self.post('/logout')
        self.assertEqual(self.post('/binflow/driver/1/accept',{'date':RUN_DATE,'manifest_version':token},follow=False).status_code,302)
        uid=self.driver('unauthorised')
        with sqlite3.connect(self.path) as conn: conn.execute('DELETE FROM bf_access WHERE user_id=?',(uid,))
        self.login('employee','unauthorised')
        self.assertEqual(self.post('/binflow/driver/1/accept',{'date':RUN_DATE,'manifest_version':token}).status_code,403)
        self.assertFalse(self.rows('SELECT * FROM bf_run_acceptance_history'))


if __name__=='__main__': unittest.main()
