import json
import sqlite3
import unittest
from unittest.mock import patch
from datetime import datetime
from zoneinfo import ZoneInfo
import test_binflow as legacy
from pod_helpers import sign_pod,SIGNATURE
from binflow_pod_records import migrate,journey,elapsed

class PODRecordTests(unittest.TestCase):
    setUp=legacy.BinFlowTests.setUp
    tearDown=legacy.BinFlowTests.tearDown
    rows=legacy.BinFlowTests.rows
    post=legacy.BinFlowTests.post
    login=legacy.BinFlowTests.login
    job=legacy.BinFlowTests.job
    exchange=legacy.BinFlowTests.exchange
    initial_delivery=legacy.BinFlowTests.initial_delivery

    def prepare(self):
        self.initial_delivery()
        self.job(collect=(1,2))
        self.exchange(2,(1,2))
        self.post('/binflow/pickups/2/receive',dict(confirmed=[1,2]))
        return self.rows("SELECT * FROM bf_job_bins WHERE job_id=2 ORDER BY id")

    def weigh(self,items):
        for i in items:self.post(f'/binflow/pickups/2/weigh/{i["id"]}',dict(gross='25',tare='5',confirm_tare='yes'))

    def sign(self,**values):
        version=self.rows('SELECT pod_version FROM bf_jobs WHERE id=2')[0]['pod_version']
        return self.post('/binflow/pickups/2/pod/sign',dict({'version':version,'signature':SIGNATURE,'confirm':'yes'},**values))

    def test_signoff_requires_all_weights_materials_signature_and_is_immutable(self):
        rows=self.prepare()
        self.assertIn(b'all weights',self.sign().data)
        self.weigh(rows[:1])
        self.assertIn(b'all weights',self.sign().data)
        self.weigh(rows[1:])
        self.assertIn(b'Classify the material',self.sign().data)
        self.post('/binflow/setup/materials',dict(name='Cotton',active='yes'))
        version=self.rows('SELECT pod_version FROM bf_jobs WHERE id=2')[0]['pod_version']
        self.post('/binflow/pickups/2/pod/materials',dict(version=version,**{'material_'+str(i['id']):1 for i in rows}))
        self.assertIn(b'Draw your signature',self.sign(signature='[]').data)
        self.assertIn(b'POD signed off',self.sign().data)
        j=self.rows('SELECT * FROM bf_jobs WHERE id=2')[0]
        self.assertTrue(j['pod_signed_at']);self.assertEqual(j['pod_signed_name'],'SB Empire')
        self.assertTrue(j['pod_signature'].startswith(b'\x89PNG'))
        self.assertIn(b'already been signed',self.sign().data)
        self.post('/binflow/pickups/2/pod/materials',dict(version=j['pod_version'],**{'material_'+str(i['id']):999 for i in rows}))
        self.assertEqual(self.rows('SELECT pod_snapshot FROM bf_jobs WHERE id=2')[0]['pod_snapshot'],j['pod_snapshot'])
        self.post('/binflow/setup/materials',dict(material_id=1,version=1,name='Renamed',active='yes'))
        self.assertEqual({r['material_name'] for r in self.rows("SELECT material_name FROM bf_job_bins WHERE job_id=2")},{'Cotton'})

    def test_unique_qr_stable_migration_and_scoped_client_journey(self):
        rows=self.prepare();self.weigh(rows);sign_pod(self,2)
        jobs=self.rows('SELECT id,pod_token FROM bf_jobs ORDER BY id')
        self.assertEqual(len(set(j['pod_token'] for j in jobs)),2)
        token=jobs[1]['pod_token'];url='/binflow/pod/'+token
        self.assertEqual(self.client.get(url).status_code,302)
        self.assertTrue(self.client.get(url+'/qr.png').data.startswith(b'\x89PNG'))
        with sqlite3.connect(self.path) as c:
            c.row_factory=sqlite3.Row;migrate(c)
            self.assertEqual(c.execute('SELECT pod_token FROM bf_jobs WHERE id=2').fetchone()[0],token)
            c.execute("INSERT INTO bf_client_accounts(id,username,name,password_hash,created_at,updated_at,updated_by) VALUES(1,'client','Client','unused','now','now','Admin')")
            c.execute("INSERT INTO bf_client_account_scope VALUES(1,1)")
        guest=self.app.test_client()
        self.assertIn(b'Sign in',guest.get(url).data)
        self.assertNotIn(b'Alpha QLD',guest.get(url).data)
        self.assertEqual(guest.get(url+'/qr.png').status_code,403)
        with guest.session_transaction() as s:s['bf_client_uid']=1;s['bf_client_version']=1
        self.assertIn(b'FINAL',guest.get(url).data)
        self.assertIn(b'Bin journey',guest.get(url+'/journey/'+str(rows[0]['id'])).data)
        self.assertEqual(guest.get(url+'/journey/999').status_code,404)
        with sqlite3.connect(self.path) as c:c.execute('UPDATE bf_client_account_scope SET client_id=2 WHERE account_id=1')
        self.assertEqual(guest.get(url).status_code,404)
        self.assertEqual(guest.get(url+'/signature.png').status_code,404)

    def test_journey_is_frozen_and_measures_custody_not_nonmovement_events(self):
        rows=self.prepare();self.weigh(rows)
        # Real chronology fixture: delivery then a later non-movement request then collection.
        with sqlite3.connect(self.path) as c:
            c.execute("UPDATE bf_events SET created_at='2026-10-01T08:00:00+10:00' WHERE bin_id=1 AND to_location=3")
            c.execute("UPDATE bf_events SET created_at='2026-10-04T10:30:00+10:00' WHERE bin_id=1 AND job_id=2")
        sign_pod(self,2)
        j=self.rows('SELECT * FROM bf_jobs WHERE id=2')[0]
        snap=json.loads(j['pod_snapshot'])
        stays=snap['journeys'][str(rows[0]['id'])]['stays']
        self.assertIn('3d 2h 30m',[s['duration'] for s in stays])
        self.assertEqual(len([s for s in stays if s['location']=='Alpha QLD']),1)
        with sqlite3.connect(self.path) as c:
            c.execute("INSERT INTO bf_events(bin_id,event,actor,created_at) VALUES(1,'LATER ROTATION','Admin','2026-11-01T08:00:00+10:00')")
            c.execute("UPDATE bf_locations SET name='Changed site name' WHERE id=3")
        page=self.client.get('/binflow/pod/'+j['pod_token']+'/journey/'+str(rows[0]['id']))
        self.assertNotIn(b'LATER ROTATION',page.data)
        self.assertNotIn(b'Changed site name',page.data)
        self.assertIn(b'3d 2h 30m',page.data)

    def test_materials_admin_csrf_and_inactive_validation(self):
        rows=self.prepare()
        self.assertEqual(self.client.post('/binflow/setup/materials',data=dict(name='Unsafe')).status_code,400)
        self.post('/binflow/setup/materials',dict(name='Textiles',active='yes'))
        self.post('/binflow/setup/materials',dict(name='textiles',active='yes'))
        self.assertEqual(len(self.rows('SELECT * FROM bf_material_classes')),1)
        self.post('/binflow/setup/materials',dict(material_id=1,version=1,name='Textiles',active=''))
        self.post('/binflow/pickups/2/pod/materials',dict(version=0,**{'material_'+str(i['id']):1 for i in rows}))
        self.assertTrue(all(i['material_id'] is None for i in self.rows('SELECT * FROM bf_job_bins WHERE job_id=2')))
        self.assertIsNone(self.rows('SELECT pod_signed_at FROM bf_jobs WHERE id=2')[0]['pod_signed_at'])

    def test_legacy_unsigned_deleted_pod_can_be_restored_for_review(self):
        self.initial_delivery()
        with sqlite3.connect(self.path) as c:c.execute("UPDATE bf_jobs SET pod_deleted_at='old' WHERE id=1")
        response=self.post('/binflow/pods/1/file',dict(action='restore',version=0,view='deleted'))
        self.assertIn(b'POD restored',response.data)
        self.assertIsNone(self.rows('SELECT pod_deleted_at FROM bf_jobs WHERE id=1')[0]['pod_deleted_at'])
        self.assertIsNone(self.rows('SELECT pod_signed_at FROM bf_jobs WHERE id=1')[0]['pod_signed_at'])

    def test_driver_cannot_sign_classify_or_edit_material_catalogue(self):
        self.prepare()
        with sqlite3.connect(self.path) as c:
            uid=c.execute("INSERT INTO users(role,first_name,email,username,password_hash,business_role,working_state) VALUES('employee','Driver','driver@example.com','driver','unused','driver','QLD')").lastrowid
            version=c.execute('SELECT auth_version FROM users WHERE id=?',(uid,)).fetchone()[0]
        with self.client.session_transaction() as session:
            session.clear();session['uid']=uid;session['version']=version
        for path in ['/binflow/setup/materials','/binflow/pickups/2/pod/sign','/binflow/pickups/2/pod/materials']:
            self.assertEqual(self.post(path,dict(name='Not allowed',active='yes',signature=SIGNATURE,confirm='yes',version=0)).status_code,403)
