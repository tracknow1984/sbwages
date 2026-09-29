import json
import os
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
from app import create_app
from blocktexx import empty_model
from blocktexx_pricing import validate_policy

class PricingTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.path=self.tmp.name+'/app.db'
        self.policy={'administration_pct':7,'profit_pct':23,'minimum_pct':30,'gst_pct':10}
        self.env=patch.dict(os.environ,{'BLOCKTEXX_PRIVATE_PRICING_JSON':json.dumps(self.policy)});self.env.start()
        self.app=create_app({'TESTING':True,'DATABASE':self.path,'SECRET_KEY':'test-'*10,'ADMIN_EMAIL':'test@example.com','ADMIN_PASSWORD':'test-password','SESSION_COOKIE_SECURE':False})
        self.client=self.app.test_client()
        with sqlite3.connect(self.path) as db:self.uid,version=db.execute("SELECT id,auth_version FROM users WHERE role='admin'").fetchone()
        with self.client.session_transaction() as session:session.update(uid=self.uid,version=version,csrf='test')
    def tearDown(self):self.env.stop();self.tmp.cleanup()
    def save(self,admin=7,profit=23,revision=0,**extra):
        return self.client.post('/admin/blocktexx/private-pricing',data={'csrf':'test','revision':revision,'administration_pct':admin,'profit_pct':profit,**extra})
    def test_floor_and_hostile_values(self):
        self.assertEqual(self.save(1,1,minimum_pct=0).status_code,400)
        for bad in ('NaN','Infinity',-1,1001,'bad'):
            with self.subTest(bad=bad):self.assertEqual(self.save(bad).status_code,400)
        with self.assertRaises(ValueError):validate_policy({**self.policy,'profit_pct':True})
        self.assertEqual(self.save().status_code,200)
    def test_revision_persistence_and_exports(self):
        saved=self.save(8,24);self.assertEqual(saved.status_code,200)
        self.assertEqual(saved.json['pricing']['revision'],1)
        self.assertEqual(self.save().status_code,409)
        model=empty_model()
        self.assertEqual(self.client.post('/admin/blocktexx',data={'csrf':'test','revision':0,'model':json.dumps(model)}).status_code,200)
        exported=self.client.get('/admin/blocktexx/export').get_data(as_text=True)
        for field in ('administration_pct','profit_pct','minimum_pct','private_pricing'):self.assertNotIn(field,exported)
        self.assertNotIn('administration_pct',self.client.get('/admin/blocktexx/export?format=csv').get_data(as_text=True))
        page=self.client.get('/admin/blocktexx').get_data(as_text=True)
        self.assertIn('"administration_pct": 8.0',page)
        self.assertIn('"profit_pct": 24.0',page)
        with sqlite3.connect(self.path) as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM blocktexx_private_pricing_history').fetchone()[0],1)
        # Operational imports/saves cannot change the private policy.
        model['administration_pct']=99
        self.assertEqual(self.client.post('/admin/blocktexx',data={'csrf':'test','revision':1,'model':json.dumps(model)}).status_code,200)
        self.assertIn('"administration_pct": 8.0',self.client.get('/admin/blocktexx').get_data(as_text=True))
    def test_admin_csrf_and_unconfigured(self):
        self.assertEqual(self.save(csrf='wrong').status_code,400)
        self.assertIn(self.app.test_client().post('/admin/blocktexx/private-pricing').status_code,(302,400))
        with sqlite3.connect(self.path) as db:db.execute("UPDATE users SET role='employee' WHERE id=?",(self.uid,))
        self.assertEqual(self.save().status_code,403)
    def test_no_defaults_in_public_model(self):
        with patch.dict(os.environ,{'BLOCKTEXX_PRIVATE_PRICING_JSON':'{}'}):
            self.assertEqual(self.save().status_code,400)
