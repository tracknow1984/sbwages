import json
import os
import unittest
from unittest.mock import patch
from blocktexx import empty_model, validate_model
from blocktexx_rfi import rfi_reference, ITEM_IDS

class RFITests(unittest.TestCase):
    def test_roundtrip_and_old_models(self):
        self.assertEqual(validate_model(empty_model())['rfi'], {'responses': {}, 'forecasts': {}, 'scenarios': {}})
        m=empty_model()
        m['rfi']={'responses': {'1.1': {'text': 'Agreed draft', 'status': 'proposed'}, '8.8': {'text': 'Approval required', 'status': 'clarify'}},
                  'forecasts': {'ACT': {'low': 0, 'high': 100}},
                  'scenarios': {'400': {'tonnes': 500, 'rate': 1.9, 'notes': 'Extra capacity'}}}
        self.assertEqual(validate_model(m)['rfi'],m['rfi'])
        self.assertEqual(len(ITEM_IDS),58)

    def test_invalid_inputs(self):
        for rfi in [[], {'responses':{'9.1':{}}}, {'responses':{'1.1':{'text':'x'*4001}}},
                    {'responses':{'1.1':{'status':'approved'}}}, {'forecasts':{'QLD':{'low':10,'high':1}}},
                    {'scenarios':{'400':{'tonnes':399}}}, {'scenarios':{'150':{'rate':-1}}},
                    {'scenarios':{'250':{'rate':float('nan')}}}]:
            with self.subTest(rfi=str(rfi)[:90]),self.assertRaises(ValueError):
                m=empty_model();m['rfi']=rfi;validate_model(m)

    def test_private_reference_and_safe_failure(self):
        reference={'source':'Synthetic reference','monthly_kg':100,'locations':2,'equipment':4,'states':{'QLD':{'kg':100,'locations':2,'equipment':4}}}
        with patch.dict(os.environ,{'BLOCKTEXX_RFI_REFERENCE_JSON':json.dumps(reference)}):
            self.assertEqual(rfi_reference(),reference)
            self.assertNotIn('rfi_reference',validate_model(empty_model()))
        with patch.dict(os.environ,{'BLOCKTEXX_RFI_REFERENCE_JSON':'bad'}):
            self.assertIn('error',rfi_reference())

    def test_admin_save_reload_and_private_reference(self):
        import sqlite3
        import tempfile
        from app import create_app
        with tempfile.TemporaryDirectory() as folder:
            path=folder+'/test.db'
            app=create_app({'TESTING':True,'DATABASE':path,'SECRET_KEY':'test-'*10,'ADMIN_EMAIL':'test@example.com','ADMIN_PASSWORD':'test-password','SESSION_COOKIE_SECURE':False})
            client=app.test_client()
            with sqlite3.connect(path) as db:
                uid,version=db.execute("SELECT id,auth_version FROM users WHERE role='admin'").fetchone()
            with client.session_transaction() as session:session.update(uid=uid,version=version,csrf='test')
            model=empty_model();model['rfi']={'responses':{'8.1':{'text':'Proposed term pending agreement','status':'proposed'}}}
            saved=client.post('/admin/blocktexx',data={'csrf':'test','revision':'0','model':json.dumps(model)})
            self.assertEqual(saved.status_code,200)
            self.assertEqual(client.get('/admin/blocktexx/export').json['rfi']['responses'],model['rfi']['responses'])
            page=client.get('/admin/blocktexx')
            self.assertEqual(page.status_code,200)
            self.assertIn(b'bx-rfi-tab',page.data)
            self.assertEqual(client.post('/admin/blocktexx',data={'csrf':'test','revision':'0','model':json.dumps(model)}).status_code,409)
            self.assertEqual(app.test_client().get('/admin/blocktexx').status_code,302)
            with sqlite3.connect(path) as db:db.execute("UPDATE users SET role='employee' WHERE id=?",(uid,))
            self.assertEqual(client.get('/admin/blocktexx').status_code,403)
