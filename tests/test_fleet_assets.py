import io
import re
import secrets
import unittest
from PIL import Image
import test_binflow as legacy


class FleetAssetTests(unittest.TestCase):
    setUp=legacy.BinFlowTests.setUp
    tearDown=legacy.BinFlowTests.tearDown
    post=legacy.BinFlowTests.post
    login=legacy.BinFlowTests.login

    def test_vehicle_record_service_documents_and_home_state(self):
        values=dict(name='NSW Rigid',state='NSW',make='Fuso',model='Fighter',rego='TEST123',
                    rego_expiry='2027-02-01',next_inspection='2027-01-05',last_service='2026-01-01')
        entity=self.post('/admin/prestart/assets',values)
        self.assertIn('NSW Rigid',entity.text)
        path=entity.request.path
        self.assertRegex(path,r'/admin/prestart/assets/\d+$')
        file=io.BytesIO()
        Image.new('RGB',(2,2)).save(file,'PNG')
        data=file.getvalue()
        records=dict(kind='Service',event_date='2026-09-06',summary='Brake service',key=secrets.token_urlsafe(24))
        with self.client.session_transaction() as sess: records['csrf']=sess['csrf']
        resp=self.client.post(path+'/history',data={**records,'files':[(io.BytesIO(data),'report.png'),(io.BytesIO(data),'parts.png')]},follow_redirects=True)
        self.assertEqual(resp.status_code,200)
        links=re.findall(r'href="('+path+r'/files/\d+)"',resp.text)
        self.assertEqual(len(links),2)
        self.assertEqual(self.client.get(links[0]).data,data)
        self.assertIn('Brake service',resp.text)
        self.assertEqual(self.client.get(links[0].replace(path,'/admin/prestart/assets/9999')).status_code,404)
        self.assertNotEqual(self.app.test_client().get(links[0]).status_code,200)
        self.post(path+'/history',records)
        with self.app.app_context():
            import sqlite3
            with sqlite3.connect(self.path) as conn:
                self.assertEqual(conn.execute('SELECT COUNT(*) FROM vehicle_history').fetchone()[0],1)
                from fleet_assets import VEHICLES
                self.assertEqual(conn.execute(VEHICLES).fetchone()[-3],'2026-09-06')
