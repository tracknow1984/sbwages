import json
import secrets
import sqlite3
import unittest
from werkzeug.security import generate_password_hash
import test_binflow as legacy
from pod_helpers import SIGNATURE
from binflow_bales import stock, readiness, migrate

class BaleTests(unittest.TestCase):
    setUp=legacy.BinFlowTests.setUp
    tearDown=legacy.BinFlowTests.tearDown
    post=legacy.BinFlowTests.post
    rows=legacy.BinFlowTests.rows
    login=legacy.BinFlowTests.login

    def prepare(self):
        self.post('/binflow/setup/materials',dict(name='Cotton textiles',active='yes'))
        self.settings(1,receiving_hub='yes',max_bales='100')
        self.settings(2,max_bales='10',target_kg='800')

    def settings(self,wid,**data):
        row=self.rows('SELECT * FROM bf_bale_settings WHERE warehouse_id=?',(wid,))
        return self.post('/binflow/bales/settings',dict(warehouse_id=wid,version=row[0]['version'] if row else 0,warning_percent=90,**data))

    def create(self,grams='400',wid=2,**data):
        key=data.pop('request_key',secrets.token_urlsafe(24))
        response=self.post('/binflow/bales/new',dict(warehouse_id=wid,material_id=1,weight=grams,request_key=key,**data))
        return response

    def transfer(self,payload='1000'):
        return self.post('/binflow/bales/transfers/new',dict(origin_id=2,destination_id=1,carrier='Carrier',vehicle='B DOUBLE',payload=payload,request_key=secrets.token_urlsafe(24)))

    def scan(self,code,action='load',tid=1,expected=200):
        response=self.post(f'/binflow/bales/transfers/{tid}/scan',dict(action=action,code=code),follow=False)
        self.assertEqual(response.status_code,expected,response.text)
        return response

    def action(self,action,tid=1,**data):
        t=self.rows('SELECT * FROM bf_bale_transfers WHERE id=?',(tid,))[0]
        return self.post(f'/binflow/bales/transfers/{tid}/action',dict(version=t['version'],action=action,**data))

    def inventory(self,wid):
        with sqlite3.connect(self.path) as c:
            c.row_factory=sqlite3.Row;return stock(c,wid)

    def test_complete_scanned_custody_signed_storage_note_and_export(self):
        self.prepare();self.create();self.create('450.125')
        self.assertEqual(self.inventory(2)['grams'],850125)
        self.assertEqual(len(self.rows("SELECT * FROM bf_bale_pickups WHERE status='ready'")),1)
        self.transfer();self.scan('BA0000001');self.scan('BA0000002')
        self.assertEqual(self.inventory(2)['count'],2);self.assertEqual(self.inventory(2)['available'],0)
        t=self.rows('SELECT * FROM bf_bale_transfers')[0]
        self.post('/binflow/bales/transfers/1/action',dict(action='depart',version=1,confirm='yes'))
        self.assertEqual(self.rows('SELECT status FROM bf_bale_transfers')[0]['status'],'loading')
        self.action('depart',confirm='yes')
        self.assertEqual(self.inventory(2)['count'],0);self.assertEqual(self.inventory(1)['count'],0)
        self.scan('BA0000001','receive');self.assertEqual(self.inventory(1)['grams'],400000)
        self.assertIn('Scan every',self.action('handover',recipient='Receiver',confirm='yes',signature=SIGNATURE).text)
        self.scan('BA0000002','receive');self.assertEqual(self.inventory(1)['grams'],850125)
        self.assertIn('Draw your signature',self.action('handover',recipient='Receiver',confirm='yes',signature='[]').text)
        self.action('handover',recipient='Receiver',confirm='yes',signature=SIGNATURE)
        self.assertEqual(self.inventory(1)['count'],0)
        self.assertEqual(len(self.rows('SELECT * FROM bf_bale_storage')),2)
        page=self.client.get('/binflow/bales/transfers/1/note')
        self.assertEqual(page.status_code,200);self.assertIn('850.125',page.text);self.assertIn('Receiver',page.text)
        self.assertTrue(self.client.get('/binflow/bales/transfers/1/signature.png').data.startswith(b'\x89PNG'))
        self.assertIn('BA0000002',self.client.get('/binflow/bales/storage.csv').text)
        before=self.rows('SELECT * FROM bf_bale_storage')
        self.action('handover',recipient='Other',confirm='yes',signature=SIGNATURE)
        self.assertEqual(self.rows('SELECT * FROM bf_bale_storage'),before)
        with sqlite3.connect(self.path) as c:c.execute("UPDATE bf_material_classes SET name='Renamed'")
        self.assertIn('Cotton textiles',self.client.get('/binflow/bales/transfers/1/note').text)

    def test_creation_validation_retry_and_label_privacy(self):
        self.prepare()
        key=secrets.token_urlsafe(24);self.create(request_key=key);self.create(request_key=key)
        self.assertEqual(len(self.rows('SELECT * FROM bf_bales')),1)
        for bad in ('0','-1','NaN','Infinity','1.0001','abc'):
            self.create(bad)
        self.assertEqual(len(self.rows('SELECT * FROM bf_bales')),1)
        self.assertEqual(self.client.get('/binflow/bales/1/label').status_code,200)
        self.assertTrue(self.client.get('/binflow/bales/1/qr.png').data.startswith(b'\x89PNG'))
        guest=self.app.test_client()
        for path in ('/binflow/bales','/binflow/bales/1','/binflow/bales/1/label','/binflow/bales/1/qr.png','/binflow/bales/storage.csv'):
            self.assertEqual(guest.get(path).status_code,302)
        self.assertEqual(self.client.post('/binflow/bales/new',data={}).status_code,400)

    def test_duplicate_scans_and_payload_limit(self):
        self.prepare();self.create();self.create();self.transfer('500')
        self.scan('BA0000001');before=self.rows('SELECT * FROM bf_bale_transfer_items')
        result=self.scan('BA0000001');self.assertTrue(result.json['duplicate']);self.assertEqual(before,self.rows('SELECT * FROM bf_bale_transfer_items'))
        self.assertIn('payload',self.scan('BA0000002',expected=409).json['message'])
        self.scan('BF000001',expected=409)
        self.assertEqual(self.inventory(2)['available'],1)
        self.action('depart',confirm='yes');self.scan('BA0000001',expected=409)
        self.scan('BA0000002','receive',expected=409)
        self.scan('BA0000001','receive');before=self.rows('SELECT * FROM bf_bale_transfer_items')
        self.assertTrue(self.scan('BA0000001','receive').json['duplicate'])
        self.assertEqual(before,self.rows('SELECT * FROM bf_bale_transfer_items'))

    def test_corrections_cancel_and_readiness_deduplication(self):
        self.prepare()
        self.settings(2,max_bales=2,target_kg=5000)
        self.create();self.create()
        with sqlite3.connect(self.path) as c:
            c.row_factory=sqlite3.Row
            readiness(c,2,'later');readiness(c,2,'later')
        self.assertEqual(len(self.rows("SELECT * FROM bf_bale_pickups WHERE status='ready'")),1)
        self.post('/binflow/bales/1/correct',dict(version=1,action='correct',weight='410',material_id=1,reason='Scale correction'))
        self.assertEqual(self.inventory(2)['grams'],810000)
        self.transfer();self.scan('BA0000001')
        self.post('/binflow/bales/1/correct',dict(version=3,action='correct',weight='420',material_id=1,reason='Attempt'))
        self.assertEqual(self.rows('SELECT net_g FROM bf_bales WHERE id=1')[0]['net_g'],410000)
        self.action('cancel');self.assertEqual(self.inventory(2)['available'],2)
        self.assertEqual(len(self.rows("SELECT * FROM bf_bale_pickups WHERE status='ready'")),1)
        version=self.rows('SELECT version FROM bf_bales WHERE id=1')[0]['version']
        self.post('/binflow/bales/1/correct',dict(version=version,action='void',reason='Duplicate physical entry'))
        self.assertEqual(self.inventory(2)['count'],1)
        self.assertEqual(self.client.get('/binflow/bales/1/label').status_code,410)
        self.assertEqual(len(self.rows("SELECT * FROM bf_bale_pickups WHERE status='ready'")),0)

    def manager_user(self,state,username):
        with sqlite3.connect(self.path) as c:
            return c.execute("INSERT INTO users(role,first_name,last_name,email,username,password_hash,business_role,working_state) VALUES('employee',?,'Manager',?,?,?,'manager',?)",(state,username+'@example.com',username,generate_password_hash('test-admin-password'),state)).lastrowid

    def test_manager_state_warehouse_and_transfer_side_permissions(self):
        self.prepare();self.create();self.transfer();self.scan('BA0000001');self.action('depart',confirm='yes')
        nsw=self.manager_user('NSW','nsw');qld=self.manager_user('QLD','qld')
        self.post('/binflow/bales/managers',dict(user_id=nsw,warehouse_id=2))
        self.post('/logout');self.login('employee','nsw')
        self.assertEqual(self.client.get('/admin/bin-management').status_code,302)
        self.assertEqual(self.client.get('/binflow/bales?warehouse=1').status_code,403)
        self.assertEqual(self.client.get('/binflow/bales/settings').status_code,403)
        self.scan('BA0000001','receive',expected=403)
        self.assertEqual(self.client.get('/binflow/bales/transfers/1').status_code,200)
        self.post('/logout');self.login('employee','qld');self.scan('BA0000001','receive')
        self.action('handover',recipient='QLD receiver',confirm='yes',signature=SIGNATURE)
        self.assertEqual(self.client.get('/binflow/bales/transfers/1/note').status_code,200)
        self.assertEqual(self.client.get('/binflow/bales?warehouse=2').status_code,403)

    def test_bale_cannot_load_on_two_manifests_or_wrong_warehouse(self):
        self.prepare();self.create();self.create(wid=1);self.transfer();self.transfer()
        token=self.rows('SELECT token FROM bf_bales WHERE id=1')[0]['token']
        self.scan('https://example.com/binflow/bales/qr/'+token)
        self.scan('BA0000001',tid=2,expected=409)
        self.scan('BA0000002',expected=409)
        self.action('remove',bale_id=1);self.scan('BA0000001',tid=2)
        self.assertEqual(self.rows('SELECT COUNT(*) AS n FROM bf_bale_transfer_items')[0]['n'],1)

    def test_repeated_migration_preserves_stock_and_qr(self):
        self.prepare();self.create()
        before=self.rows('SELECT * FROM bf_bales')
        with sqlite3.connect(self.path) as c:
            c.row_factory=sqlite3.Row;migrate(c);migrate(c)
        self.assertEqual(before,self.rows('SELECT * FROM bf_bales'))

    def test_weight_trigger_assignment_and_client_driver_denial(self):
        self.prepare();self.settings(2,max_kg='500',target_kg='5000')
        self.create('450')
        ready=self.rows("SELECT * FROM bf_bale_pickups WHERE status='ready'")
        self.assertEqual(len(ready),1);self.assertIn('weight threshold',ready[0]['reason'])
        self.post('/binflow/setup',dict(action='warehouse',name='Other NSW warehouse',state='NSW',address='Other depot'))
        uid=self.manager_user('NSW','bound-manager')
        self.post('/binflow/bales/managers',dict(user_id=uid,warehouse_id=2))
        other=self.rows("SELECT id FROM bf_locations WHERE name='Other NSW warehouse'")[0]['id']
        self.post('/logout');self.login('employee','bound-manager')
        self.assertEqual(self.client.get('/binflow/bales?warehouse='+str(other)).status_code,403)
        self.assertNotIn('Other NSW warehouse',self.client.get('/binflow/bales/new').text)
        with sqlite3.connect(self.path) as c:c.execute("UPDATE users SET business_role='driver' WHERE id=?",(uid,))
        self.assertEqual(self.client.get('/binflow/bales').status_code,403)
        guest=self.app.test_client()
        with guest.session_transaction() as s:s.update(bf_client_uid=1,bf_client_version=1)
        self.assertEqual(guest.get('/binflow/bales').status_code,302)
