import json
import sqlite3
import unittest
from werkzeug.security import check_password_hash, generate_password_hash
import test_binflow as legacy
from binflow_management import bootstrap
from pod_helpers import sign_pod

class ManagementTests(unittest.TestCase):
    setUp=legacy.BinFlowTests.setUp
    tearDown=legacy.BinFlowTests.tearDown
    rows=legacy.BinFlowTests.rows
    post=legacy.BinFlowTests.post
    login=legacy.BinFlowTests.login
    job=legacy.BinFlowTests.job
    exchange=legacy.BinFlowTests.exchange
    initial_delivery=legacy.BinFlowTests.initial_delivery

    def seed(self):
        with sqlite3.connect(self.path) as c:
            c.row_factory=sqlite3.Row
            c.execute("INSERT INTO bf_import_runs VALUES('blocktexx-test',1,1,'now','{}','now')")
            c.execute("INSERT INTO bf_location_sources VALUES('test','blocktexx-test',3,'collection','{}')")
            c.commit();bootstrap(c,'2026-10-24')
        return self.rows("SELECT * FROM bf_client_accounts WHERE username='blocktexx'")[0]

    def portal(self):
        client=self.app.test_client();client.get('/binflow/client/login')
        with client.session_transaction() as s:csrf=s['csrf']
        result=client.post('/binflow/client/login',data=dict(csrf=csrf,username='blocktexx',password='collections'))
        self.assertEqual(result.status_code,302)
        self.assertTrue(result.location.endswith('/binflow/client/calendar'))
        return client

    def test_bootstrap_login_scope_revocation_and_idempotence(self):
        with sqlite3.connect(self.path) as c:
            c.execute("INSERT INTO bf_client_accounts(username,name,password_hash,created_at,updated_at,updated_by) VALUES('other','Other',?,'now','now','Admin')",(generate_password_hash('old-password'),))
        guest=self.app.test_client()
        with guest.session_transaction() as s:s.update(bf_client_uid=1,bf_client_version=1)
        account=self.seed()
        self.assertEqual(guest.get('/binflow/client/pods').status_code,302)
        self.assertEqual(self.rows('SELECT site_id FROM bf_client_site_scope'),[{'site_id':3}])
        self.assertTrue(check_password_hash(account['password_hash'],'collections'))
        client=self.portal()
        for path in ('calendar','pods','requests'):
            page=client.get('/binflow/client/'+path)
            self.assertEqual(page.status_code,200)
            for label in ('Collection calendar','Daily PODs','Collection requests'):self.assertIn(label,page.text)
        self.assertEqual(client.get('/binflow/settings/client-access').status_code,302)
        self.assertEqual(client.post('/binflow/client/calendar').status_code,400)
        with sqlite3.connect(self.path) as c:
            c.row_factory=sqlite3.Row
            c.execute("UPDATE bf_client_accounts SET password_hash='changed' WHERE username='blocktexx'");c.commit()
            bootstrap(c,'later')
        self.assertEqual(self.rows("SELECT password_hash FROM bf_client_accounts WHERE username='blocktexx'")[0]['password_hash'],'changed')
        page=self.client.get('/binflow/settings/client-access')
        self.assertIn('Approved collection sites',page.text)
        self.assertNotIn('Create client login',page.text)

    def test_published_calendar_site_state_and_no_internal_notes(self):
        self.seed();self.job(deliver=(1,))
        plan={'days':[{'date':'2026-10-19','items':[
          {'id':'a','kind':'job','job_id':1,'site_id':3,'notes':'PRIVATE'},
          {'id':'b','kind':'visit','site_id':4,'title':'Beta'},
          {'id':'c','kind':'visit','site_id':5,'title':'Same company other site'},
          {'id':'d','kind':'note','title':'PRIVATE'}]}]}
        with sqlite3.connect(self.path) as c:
            c.execute("INSERT INTO bf_schedule_weeks(id,state,week_start,revision,status,plan_json,updated_at,updated_by) VALUES(1,'QLD','2026-10-19',1,'draft',?,'now',1)",(json.dumps(plan),))
            c.execute('UPDATE bf_jobs SET schedule_week_id=1 WHERE id=1')
        client=self.portal();path='/binflow/client/calendar?week=2026-10-19'
        self.assertNotIn('Alpha QLD',client.get(path).text)
        with sqlite3.connect(self.path) as c:c.execute("UPDATE bf_schedule_weeks SET status='published'")
        page=client.get(path).text
        self.assertIn('Alpha QLD',page)
        for value in ('Beta QLD','Alpha NSW','PRIVATE','Save changes','APPROVE WEEK'):self.assertNotIn(value,page)
        self.assertNotIn('Alpha QLD',client.get(path+'&state=NSW').text)
        self.assertEqual(client.get(path+'&state=INVALID').status_code,400)
        with sqlite3.connect(self.path) as c:c.execute("UPDATE bf_jobs SET status='cancelled'")
        self.assertIn('bf-client-status cancelled',client.get(path).text)

    def test_pods_and_qr_require_exact_site_and_final_signoff(self):
        self.seed();self.initial_delivery()
        token=self.rows('SELECT pod_token FROM bf_jobs WHERE id=1')[0]['pod_token']
        client=self.portal();path='/binflow/pod/'+token
        self.assertEqual(client.get(path).status_code,404)
        sign_pod(self,1)
        self.assertEqual(client.get(path).status_code,200)
        self.assertEqual(client.get('/binflow/client/pods/1.pdf').status_code,200)
        with sqlite3.connect(self.path) as c:c.execute('UPDATE bf_client_site_scope SET site_id=5')
        self.assertNotIn('BF-000001',client.get('/binflow/client/pods?date=2026-10-19').text)
        for suffix in ('','/qr.png','/signature.png'):
            self.assertEqual(client.get(path+suffix).status_code,404)
        self.assertEqual(client.get('/binflow/client/pods/1.pdf').status_code,404)

    def test_request_visibility_and_settings_revoke_sessions(self):
        self.seed()
        with sqlite3.connect(self.path) as c:
            for rid,sid,cid,binid in ((1,3,1,1),(2,4,2,2),(3,5,1,3)):
                c.execute("""INSERT INTO bf_collection_requests(id,request_key,bin_id,site_id,client_id,bin_code,requester,contact,notes,created_at,updated_at)
                 VALUES(?,?,?,?,?,?,'Person','private contact',?,'2026-10-24','now')""",(rid,str(rid),binid,sid,cid,'BF'+str(binid),'Site note '+str(rid)))
        client=self.portal();page=client.get('/binflow/client/requests').text
        self.assertIn('Site note 1',page)
        for secret in ('Site note 2','Site note 3','private contact'):self.assertNotIn(secret,page)
        self.post('/binflow/settings/client-access',dict(active='yes',site_id=[3,5]))
        self.assertEqual(client.get('/binflow/client/requests').status_code,302)
        client=self.portal()
        self.assertIn('Site note 3',client.get('/binflow/client/requests').text)
        self.assertNotIn('Site note 2',client.get('/binflow/client/requests').text)
