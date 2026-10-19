import sqlite3
import time
import unittest
from unittest.mock import patch

import test_binflow as legacy
from binflow_helpers import prestart, depart, RUN_DATE
from binflow_gps import cipher, fetch_units, store_units, distance_m


class DriverDayTests(unittest.TestCase):
    setUp=legacy.BinFlowTests.setUp
    tearDown=legacy.BinFlowTests.tearDown
    post=legacy.BinFlowTests.post
    rows=legacy.BinFlowTests.rows
    login=legacy.BinFlowTests.login
    job=legacy.BinFlowTests.job
    initial_delivery=legacy.BinFlowTests.initial_delivery
    exchange=legacy.BinFlowTests.exchange

    def prepare(self, multiple=False):
        self.initial_delivery()
        self.post('/binflow/driver',{'name':'Day truck','state':'QLD'})
        self.job(collect=(1,),deliver=(3,),legacy=False)
        if multiple:
            self.job(deliver=(4,),site=4,legacy=False)
            self.job(collect=(2,),deliver=(5,),legacy=False)
            self.job(deliver=(6,),site=4,legacy=False)
        for j in self.rows("SELECT id FROM bf_jobs WHERE status='scheduled'"):
            self.post(f'/binflow/pickups/{j["id"]}/truck',{'truck_id':1})

    def scan(self,bid,status=200):
        with self.client.session_transaction() as session: csrf=session['csrf']
        response=self.client.post('/binflow/driver/1/day/scan-bin',data={'csrf':csrf,'date':RUN_DATE,'code':f'BF{bid:06d}'},headers={'Accept':'application/json'})
        self.assertEqual(response.status_code,status,response.data)
        return response.json

    def stage(self):
        return self.client.get('/binflow/driver/1/day/state?date='+RUN_DATE).json['stage']

    def account(self,client_id=1,username='client-alpha',password='client-test-password'):
        return self.post('/binflow/settings/client-access',dict(name='Client contact',username=username,password=password,active='yes',client_id=[client_id]))

    def test_four_stop_guidance_returns_weights_and_client_isolation(self):
        self.prepare(multiple=True)
        original=self.rows('SELECT id,token FROM bf_bins ORDER BY id')
        self.assertEqual(self.stage(),'acceptance')
        self.scan(3,409)
        prestart(self)
        self.assertEqual(self.stage(),'load')
        self.scan(3)
        depart(self)
        self.assertEqual(self.stage(),'load')
        for bid in (4,5,6): self.scan(bid)
        self.assertEqual(self.stage(),'ready')
        depart(self)
        self.assertEqual(self.stage(),'deliver')
        self.scan(4,409)  # Correct day's bin, wrong current client.
        self.scan(3)
        self.assertEqual(self.stage(),'collect')
        self.scan(1)
        self.assertEqual(self.stage(),'handover')
        self.exchange(2,())
        self.assertEqual(self.client.get('/binflow/driver/1/day/state?date='+RUN_DATE).json['current'],3)
        self.scan(1,409)  # Cannot return while later clients remain.
        self.scan(4);self.exchange(3,())
        self.scan(5);self.scan(2);self.exchange(4,())
        self.scan(6);self.exchange(5,())
        self.assertEqual(self.stage(),'return')
        self.assertIn(b'Client completed',self.client.get('/binflow/calendar?date='+RUN_DATE).data)
        self.account()
        self.account(client_id=2,username='client-beta')
        client=self.app.test_client()
        client.get('/binflow/client/login')
        with client.session_transaction() as s: csrf=s['csrf']
        login=client.post('/binflow/client/login',data={'csrf':csrf,'username':'client-alpha','password':'client-test-password'})
        self.assertEqual(login.status_code,302)
        self.assertEqual(client.get('/binflow/client/pods/2.pdf').status_code,404)  # Weight pending.
        self.assertEqual(client.get('/binflow/client/pods/3.pdf').status_code,404)  # Another client, even though complete.
        self.assertEqual(client.get('/binflow/pickups/1/pod.pdf').status_code,302)  # No staff access.
        self.scan(1)
        self.assertEqual(self.stage(),'return')
        i=self.rows('SELECT id FROM bf_job_bins WHERE job_id=2 AND direction="collect"')[0]['id']
        self.post(f'/binflow/pickups/2/weigh/{i}',dict(gross='25',tare='5',confirm_tare='yes'))
        self.assertEqual(self.rows('SELECT status FROM bf_jobs WHERE id=2')[0]['status'],'complete')
        self.assertTrue(client.get('/binflow/client/pods/2.pdf').data.startswith(b'%PDF-'))
        self.scan(2)
        self.assertEqual(self.stage(),'weighing')
        self.assertIn(b'Back at warehouse',self.client.get('/binflow/calendar?date='+RUN_DATE).data)
        i=self.rows('SELECT id FROM bf_job_bins WHERE job_id=4 AND direction="collect"')[0]['id']
        self.post(f'/binflow/pickups/4/weigh/{i}',dict(gross='35',tare='5',confirm_tare='yes'))
        self.assertEqual(self.stage(),'done')
        self.assertEqual(original,self.rows('SELECT id,token FROM bf_bins ORDER BY id'))
        self.assertFalse(self.rows('SELECT * FROM bf_bins WHERE truck_id=1'))
        # Location changes must never change ownership of historical client PODs.
        with sqlite3.connect(self.path) as conn: conn.execute('UPDATE bf_locations SET client_id=2 WHERE id=3')
        self.assertEqual(client.get('/binflow/client/pods/2.pdf').status_code,200)
        self.post('/binflow/settings/client-access',dict(account_id=1,name='Client contact',username='client-alpha',active='',client_id=[1]))
        self.assertEqual(client.get('/binflow/client/pods/2.pdf').status_code,302)

    def test_failed_prestart_hold_resolution_new_check_and_daily_expiry(self):
        self.prepare()
        prestart(self,fail=True)
        self.assertEqual(self.stage(),'hold')
        self.scan(3,409)
        self.assertIn(b'Damaged tyre',self.client.get('/binflow/calendar').data)
        before=self.rows('SELECT * FROM prestarts')
        self.post('/binflow/prestarts/1/resolve',{'resolution':''})
        self.assertEqual(self.stage(),'hold')
        self.post('/binflow/prestarts/1/resolve',{'resolution':'Tyre replaced and inspected by workshop.'})
        self.assertEqual(self.stage(),'prestart')
        self.scan(3,409)
        prestart(self)
        self.assertEqual(self.stage(),'load')
        self.assertEqual(before[0],self.rows('SELECT * FROM prestarts WHERE id=1')[0])
        self.assertIn(b'Fail',self.client.get('/admin/prestart/1').data)
        with sqlite3.connect(self.path) as conn: conn.execute("UPDATE bf_run_inspections SET inspection_date='2026-10-18' WHERE passed=1")
        self.assertEqual(self.stage(),'prestart')
        self.scan(3,409)

    def test_schedule_order_driver_ownership_and_freeze_after_loading(self):
        self.prepare(multiple=True)
        uid=self.rows("SELECT id FROM users WHERE username='admin'")[0]['id']
        values=dict(date=RUN_DATE,driver_id=uid,order_2=2,order_3=1,order_4=3,order_5=4,time_2='09:00',time_3='08:00')
        self.post('/binflow/driver/1/schedule',values)
        self.assertEqual(self.rows('SELECT id FROM bf_jobs WHERE truck_id=1 ORDER BY run_order,id')[0]['id'],3)
        prestart(self);self.scan(3)
        self.post('/binflow/driver/1/schedule',{**values,'order_2':1,'order_3':2})
        self.assertEqual(self.rows('SELECT id FROM bf_jobs WHERE truck_id=1 ORDER BY run_order,id')[0]['id'],3)
        from werkzeug.security import generate_password_hash
        with sqlite3.connect(self.path) as conn:
            driver=conn.execute("INSERT INTO users(role,first_name,last_name,email,username,password_hash) VALUES('employee','Other','Driver','other@example.com','other',?)",(generate_password_hash('test-admin-password'),)).lastrowid
            conn.execute('INSERT INTO bf_access(user_id) VALUES(?)',(driver,))
        self.post('/logout');self.login('employee','other')
        self.assertEqual(self.client.get('/binflow/driver/1/day?date='+RUN_DATE).status_code,403)
        self.scan(4,403)
        self.assertEqual(self.client.get('/binflow/settings/gps').status_code,403)
        self.assertEqual(self.client.get('/binflow/settings/client-access').status_code,403)

    def test_client_login_limits_csrf_and_scope_validation(self):
        self.account()
        self.assertEqual(self.client.post('/binflow/settings/client-access',data={}).status_code,400)
        client=self.app.test_client();client.get('/binflow/client/login')
        with client.session_transaction() as s: csrf=s['csrf']
        for _ in range(10):
            self.assertEqual(client.post('/binflow/client/login',data={'csrf':csrf,'username':'missing','password':'bad'}).status_code,401)
        self.assertEqual(client.post('/binflow/client/login',data={'csrf':csrf,'username':'missing','password':'bad'}).status_code,429)
        self.assertEqual(client.get('/binflow/client/pods/1.pdf').status_code,302)
        self.post('/binflow/settings/client-access',dict(name='Bad scope',username='bad-scope',password='client-test-password',active='yes',client_id=[999]))
        self.assertEqual(len(self.rows('SELECT * FROM bf_client_accounts')),1)

    def test_wialon_token_encryption_mapping_geofences_and_stale_positions(self):
        self.prepare()
        stock=self.rows('SELECT * FROM bf_bins')
        token='a'*72
        units=[dict(unit_id=42,name='GPS truck',latitude=-27.47,longitude=153.02,position_time=int(time.time()),speed=0)]
        with patch('binflow_gps.fetch_units',return_value=units) as api:
            self.post('/binflow/settings/gps',dict(action='connect',host='http://127.0.0.1',token=token))
            api.assert_not_called()
            self.post('/binflow/settings/gps',dict(action='connect',host='https://hst-api.wialon.com',token=token))
            api.assert_called_once()
        config=self.rows('SELECT * FROM bf_gps_config')[0]
        self.assertNotIn(token.encode(),config['token_cipher'])
        self.assertEqual(cipher(self.app.config['SECRET_KEY']).decrypt(config['token_cipher']).decode(),token)
        self.assertNotIn(token.encode(),self.client.get('/binflow/settings/gps').data)
        self.post('/binflow/settings/gps',dict(action='link',truck_id=1,unit_id=42))
        self.post('/binflow/locations/3/geofence',dict(latitude=-27.47,longitude=153.02,radius_m=150))
        with sqlite3.connect(self.path) as conn:
            conn.row_factory=sqlite3.Row;store_units(conn,units,'observed')
        status=self.client.get('/binflow/gps/status').json['html']
        self.assertIn('Alpha QLD',status)
        self.assertEqual(self.rows('SELECT * FROM bf_bins'),stock)
        self.assertEqual(self.rows('SELECT status FROM bf_jobs WHERE id=2')[0]['status'],'scheduled')
        with sqlite3.connect(self.path) as conn: conn.execute('UPDATE bf_gps_units SET position_time=?',(int(time.time())-600,))
        self.assertIn('stale',self.client.get('/binflow/gps/status').json['html'])
        self.post('/binflow/locations/3/geofence',dict(latitude='NaN',longitude=153,radius_m=150))
        self.assertEqual(self.rows('SELECT latitude FROM bf_geofences')[0]['latitude'],-27.47)
        with sqlite3.connect(self.path) as conn: conn.execute("UPDATE bf_gps_config SET token_cipher='invalid',last_poll=0")
        self.assertIn('Unable to refresh',self.client.get('/binflow/gps/status').json['html'])
        self.post('/binflow/settings/gps',dict(action='disconnect'))
        self.assertFalse(self.rows('SELECT * FROM bf_gps_config'))
        self.assertFalse(self.rows('SELECT * FROM bf_truck_gps'))

    def test_wialon_protocol_private_positions_and_logout_on_error(self):
        responses=[{'eid':'session'}, {'items':[{'id':1,'nm':'Public','pos':{'t':int(time.time()),'y':-27.,'x':153.,'s':0}},
                {'id':2,'nm':'Private','pos':{'t':int(time.time())}}]},{}]
        with patch('binflow_gps.api_call',side_effect=responses) as api:
            rows=fetch_units('https://hst-api.wialon.com','a'*72)
            self.assertEqual(api.call_args_list[1].args[2]['flags'],1025)
            self.assertEqual(api.call_args_list[-1].args[1],'core/logout')
        self.assertIsNone(rows[1]['latitude'])
        self.assertLess(distance_m(-27,153,-27,153),1)
        with patch('binflow_gps.api_call',side_effect=[{'eid':'session'},ValueError('No access'),{}]) as api:
            with self.assertRaises(ValueError): fetch_units('https://hst-api.wialon.com','a'*72)
            self.assertEqual(api.call_args_list[-1].args[1],'core/logout')


if __name__=='__main__': unittest.main()
