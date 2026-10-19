import io
import sqlite3
import tempfile
import unittest
from pathlib import Path
from urllib.parse import urlparse
from werkzeug.datastructures import MultiDict
from werkzeug.security import generate_password_hash
from PIL import Image
from app import create_app


class BinFlowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = str(Path(self.temp.name) / 'test.db')
        self.app = create_app({'TESTING': True, 'DATABASE': self.path, 'SECRET_KEY': 'test-binflow-secret-'*4,
                               'SESSION_COOKIE_SECURE': False, 'ADMIN_EMAIL': 'admin@example.com',
                               'ADMIN_PASSWORD': 'test-admin-password', 'APP_URL': 'https://example.com'})
        self.client = self.app.test_client()
        self.login()
        self.post('/binflow/setup', {'action':'warehouse','name':'QLD warehouse','state':'QLD','address':'1 Depot Road'})
        self.post('/binflow/setup', {'action':'warehouse','name':'NSW warehouse','state':'NSW','address':'2 Depot Road'})
        for name in ('Client Alpha','Client Beta'):
            self.post('/binflow/setup', {'action':'client','name':name,'email':'ops@example.com'})
        for client,name,state in ((1,'Alpha QLD','QLD'),(2,'Beta QLD','QLD'),(1,'Alpha NSW','NSW')):
            self.post('/binflow/setup', {'action':'site','name':name,'state':state,'client_id':client,'address':'3 Client Road'})
        self.post('/binflow/bins', {'warehouse_id':1,'size_id':1,'quantity':6,'tare':'5'})

    def tearDown(self):
        self.temp.cleanup()

    def rows(self, query, args=()):
        with sqlite3.connect(self.path) as conn:
            conn.row_factory = sqlite3.Row
            return [dict(r) for r in conn.execute(query,args)]

    def post(self, path, data=None, follow=True):
        if path == '/binflow/bins':
            import secrets
            data = {'request_key': secrets.token_urlsafe(24), **(data or {})}
        with self.client.session_transaction() as s:
            s.setdefault('csrf','test-binflow-csrf')
            token=s['csrf']
        values=MultiDict()
        values.add('csrf',token)
        for key,value in (data or {}).items():
            for item in (value if isinstance(value,list) else [value]):
                values.add(key,item)
        return self.client.post(path,data=values,follow_redirects=follow)

    def login(self, audience='admin', username='admin'):
        return self.post('/'+audience+'/login',{'username':username,'password':'test-admin-password'})

    def job(self, collect=(), deliver=(), site=3, warehouse=1, key=None, legacy=True):
        import secrets
        key = key or secrets.token_urlsafe(24)
        result=self.post('/binflow/pickups/new',{'request_key':key,'site_id':site,'warehouse_id':warehouse,'scheduled_date':'2026-10-19','collect':list(collect),'deliver':list(deliver)})
        saved=self.rows('SELECT id,approval_status FROM bf_jobs WHERE request_key=?',(key,))
        if saved and saved[0]['approval_status']=='pending':
            reviewed=self.post(f'/binflow/pickups/{saved[0]["id"]}/review',{'decision':'approve'})
            self.assertIn(b'Allocation approved.',reviewed.data)
        if legacy:
            # Regression fixture for dockets created before individual truck scans were introduced.
            with sqlite3.connect(self.path) as conn:
                conn.execute('UPDATE bf_jobs SET scan_mode=0 WHERE request_key=?', (key,))
        return result

    def exchange(self, jid, ids):
        return self.post(f'/binflow/pickups/{jid}/exchange',{'confirmed':list(ids),'confirmation':'not_present','confirmation_note':'Driver completed the agreed unattended exchange.'})

    def initial_delivery(self):
        response=self.job(deliver=(1,2))
        self.assertIn(b'Job saved',response.data)
        response=self.exchange(1,(1,2))
        self.assertIn(b'Exchange recorded',response.data)

    def move_bin(self, bid, destination, **values):
        version = self.rows('SELECT COALESCE(MAX(id),0) AS n FROM bf_events WHERE bin_id=?', (bid,))[0]['n']
        return self.post(f'/binflow/bins/{bid}/edit', {'location_id':destination,'version':str(version),**values})

    def test_register_location_edits_preserve_qr_and_record_stock_movements(self):
        original = self.rows('SELECT * FROM bf_bins WHERE id=1')[0]
        self.assertIn(b'Edit location for BF000001', self.client.get('/binflow/bins').data)
        result = self.move_bin(1,3,notes='Initial site allocation')
        self.assertIn(b'Bin location updated.',result.data)
        b = self.rows('SELECT * FROM bf_bins WHERE id=1')[0]
        self.assertEqual((b['location_id'],b['status'],b['token'],b['code']), (3,'at_client',original['token'],original['code']))
        event = self.rows('SELECT * FROM bf_events WHERE bin_id=1 ORDER BY id DESC')[0]
        self.assertEqual((event['from_location'],event['to_location']), (1,3))
        self.assertIn('Initial site allocation',event['notes']);self.assertTrue(event['actor'])
        count = len(self.rows('SELECT * FROM bf_events WHERE bin_id=1'))
        self.move_bin(1,3,version='0')
        self.assertEqual(len(self.rows('SELECT * FROM bf_events WHERE bin_id=1')),count)
        self.assertEqual(self.move_bin(1,1).status_code,400)
        self.assertEqual(self.rows('SELECT location_id FROM bf_bins WHERE id=1')[0]['location_id'],3)
        self.move_bin(1,1,confirm_empty='yes')
        self.assertEqual(self.rows('SELECT status FROM bf_bins WHERE id=1')[0]['status'],'empty')
        self.assertEqual(self.move_bin(1,2).status_code,400)
        self.assertEqual(self.move_bin(1,999).status_code,400)
        self.assertEqual(self.client.post('/binflow/bins/1/edit',data={'location_id':3}).status_code,400)
        self.assertEqual(self.client.get('/binflow/bins/999/edit').status_code,404)
        self.assertFalse(self.rows('SELECT * FROM bf_jobs'))

    def test_location_edits_guard_active_dockets_full_stock_and_stale_forms(self):
        self.initial_delivery()
        original_jobs = self.rows('SELECT * FROM bf_jobs')
        version = self.rows('SELECT MAX(id) AS n FROM bf_events WHERE bin_id=1')[0]['n']
        self.move_bin(1,4)
        self.assertEqual(self.move_bin(1,1,version=str(version),confirm_empty='yes').status_code,400)
        self.assertEqual(self.rows('SELECT * FROM bf_jobs'),original_jobs)
        self.post('/binflow/bins/1',{'action':'full'})
        self.assertEqual(self.move_bin(1,1,confirm_empty='yes').status_code,400)
        self.move_bin(1,3)
        self.assertEqual(self.rows('SELECT status FROM bf_bins WHERE id=1')[0]['status'],'full')
        self.job(collect=(1,))
        self.assertEqual(self.move_bin(1,4).status_code,400)
        self.assertIn(b'Open docket',self.client.get('/binflow/bins/1/edit').data)
        self.exchange(2,(1,))
        self.assertEqual(self.move_bin(1,3).status_code,400)
        self.post('/binflow/pickups/2/receive',{'confirmed':[1]})
        self.assertEqual(self.move_bin(1,4).status_code,400)
        iid=self.rows("SELECT id FROM bf_job_bins WHERE job_id=2 AND direction='collect'")[0]['id']
        self.post(f'/binflow/pickups/2/weigh/{iid}',{'gross':'20','tare':'5','confirm_tare':'yes'})
        self.post('/binflow/setup',{'action':'warehouse','name':'Second QLD warehouse','state':'QLD','address':'5 Depot Road'})
        weights=self.rows('SELECT * FROM bf_job_bins')
        self.move_bin(1,6,notes='Warehouse stock transfer')
        self.assertEqual(self.rows('SELECT location_id,status FROM bf_bins WHERE id=1')[0], {'location_id':6,'status':'weighed'})
        self.assertEqual(self.rows('SELECT * FROM bf_job_bins'),weights)
        self.assertEqual(self.move_bin(1,3).status_code,400)
        self.post('/binflow/bins/1',{'action':'empty','notes':'Contents processed'})
        self.assertIn(b'Bin location updated.',self.move_bin(1,4).data)

    def test_all_pages_and_empty_setup_render(self):
        for route in ('/admin/bin-management','/binflow/setup','/binflow/bins','/binflow/pickups','/binflow/pickups/new','/binflow/pickups/new?site=3&warehouse=1','/binflow/warehouse','/binflow/scan','/binflow/bins/1','/binflow/labels?bin=1&bin=2'):
            with self.subTest(route=route):
                result=self.client.get(route)
                self.assertEqual(result.status_code,200,result.data[:300])
        self.assertIn(b'>BINFLOW</a>',self.client.get('/admin/dashboard').data)
        self.assertEqual(self.client.get('/health').status_code,200)

    def test_complete_rotation_preserves_identity_and_weight_history(self):
        original=self.rows('SELECT token FROM bf_bins WHERE id=1')[0]['token']
        self.initial_delivery()
        self.assertEqual(self.rows('SELECT status,location_id FROM bf_bins WHERE id=1')[0],{'status':'at_client','location_id':3})
        self.post('/binflow/bins/1',{'action':'full'})
        self.assertEqual(self.rows('SELECT status FROM bf_bins WHERE id=1')[0]['status'],'full')
        self.job(collect=(1,2),deliver=(3,4))
        self.exchange(2,(1,2,3,4))
        self.assertEqual(self.rows('SELECT status,location_id FROM bf_bins WHERE id=1')[0],{'status':'transit','location_id':None})
        self.assertEqual(self.rows('SELECT status,location_id FROM bf_bins WHERE id=3')[0],{'status':'at_client','location_id':3})
        self.assertIn(b'WEIGHT PENDING',self.client.get('/binflow/pickups/2/pod').data)
        self.post('/binflow/pickups/2/receive',{'confirmed':[1,2]})
        self.assertEqual(self.rows('SELECT status,location_id FROM bf_bins WHERE id=1')[0],{'status':'received','location_id':1})
        weights=self.rows("SELECT id FROM bf_job_bins WHERE job_id=2 AND direction='collect' ORDER BY id")
        for item,gross in zip(weights,('80.5','15.009')):
            response=self.post(f'/binflow/pickups/2/weigh/{item["id"]}',{'gross':gross,'tare':'5','confirm_tare':'yes'})
            self.assertIn(b'Weight saved',response.data)
        self.assertEqual(self.rows('SELECT status FROM bf_jobs WHERE id=2')[0]['status'],'complete')
        self.assertEqual(self.rows('SELECT SUM(net_g) AS n FROM bf_job_bins WHERE job_id=2')[0]['n'],85509)
        self.assertIn(b'FINAL',self.client.get('/binflow/pickups/2/pod').data)
        self.assertIn(b'85.509',self.client.get('/binflow/pickups/2/pod').data)
        self.assertNotIn(b'Client Beta',self.client.get('/binflow/pickups/2/pod').data)
        pdf=self.client.get('/binflow/pickups/2/pod.pdf')
        self.assertEqual(pdf.status_code,200)
        self.assertTrue(pdf.data.startswith(b'%PDF-'))
        self.post('/binflow/bins/1',{'action':'empty','notes':'Contents processed batch REF-123'})
        self.assertEqual(self.rows('SELECT token,status FROM bf_bins WHERE id=1')[0],{'token':original,'status':'empty'})
        self.assertEqual(self.rows('SELECT SUM(net_g) AS n FROM bf_job_bins WHERE job_id=2')[0]['n'],85509)
        self.assertGreaterEqual(len(self.rows('SELECT * FROM bf_events WHERE bin_id=1')),8)
        self.assertIn(b'BF000001',self.client.get('/binflow/export.csv').data)
        self.assertIn(b'Client Alpha',self.client.get('/binflow/export.csv').data)
        again=self.job(deliver=(1,),site=4)
        self.assertIn(b'Job saved',again.data)

    def test_reservations_and_duplicate_submissions(self):
        batch = {'request_key':'batch-key-12345678901234567890','warehouse_id':1,'size_id':1,'quantity':2,'tare':'5'}
        self.post('/binflow/bins',batch)
        self.post('/binflow/bins',batch)
        self.assertEqual(len(self.rows('SELECT * FROM bf_bins')),8)
        key='a'*32
        self.job(deliver=(1,),key=key)
        self.job(deliver=(1,),key=key)
        self.assertEqual(len(self.rows('SELECT * FROM bf_jobs')),1)
        response=self.job(deliver=(1,),site=4)
        self.assertIn(b'already allocated',response.data)
        self.assertEqual(len(self.rows('SELECT * FROM bf_jobs')),1)
        self.post('/binflow/pickups/1/cancel',{'reason':'Client rescheduled'})
        self.assertEqual(self.rows('SELECT active FROM bf_job_bins')[0]['active'],0)
        self.job(deliver=(1,),site=4)
        self.assertEqual(len(self.rows('SELECT * FROM bf_jobs')),2)
        self.exchange(2,(1,)); self.exchange(2,(1,))
        self.assertEqual(len(self.rows("SELECT * FROM bf_events WHERE event='Empty bin delivered to client'")),1)

    def test_cross_state_and_cross_client_rejected_atomically(self):
        result=self.job(deliver=(1,),site=5)
        self.assertIn(b'same state',result.data)
        self.assertFalse(self.rows('SELECT * FROM bf_jobs'))
        self.initial_delivery()
        result=self.job(collect=(1,),site=4)
        self.assertIn(b'not available at this client',result.data)
        self.assertEqual(len(self.rows('SELECT * FROM bf_jobs')),1)
        result=self.job(deliver=(3,3))
        self.assertIn(b'only once',result.data)
        self.assertEqual(len(self.rows('SELECT * FROM bf_jobs')),1)

    def test_location_edits_preserve_stock_and_job_snapshots(self):
        warehouse_path = '/binflow/locations/1/edit'
        self.assertIn(b'Edit QLD warehouse', self.client.get('/binflow/setup').data)
        self.assertIn(b'value="1 Depot Road"', self.client.get(warehouse_path).data)
        invalid = self.post(warehouse_path, {'name':'Changed', 'state':'NSW', 'address':'New address'})
        self.assertEqual(invalid.status_code, 400)
        self.assertIn(b'state cannot be changed', invalid.data)
        self.assertEqual(self.rows('SELECT name,state FROM bf_locations WHERE id=1')[0], {'name':'QLD warehouse','state':'QLD'})
        self.initial_delivery()
        stock, jobs = self.rows('SELECT * FROM bf_bins'), self.rows('SELECT * FROM bf_jobs')
        result = self.post(warehouse_path, {'name':'North Maclean depot', 'state':'QLD', 'address':'99 Depot Road'})
        self.assertIn(b'Warehouse updated.', result.data)
        self.assertEqual(self.rows('SELECT name,address FROM bf_locations WHERE id=1')[0], {'name':'North Maclean depot','address':'99 Depot Road'})
        result = self.post('/binflow/locations/3/edit', {'name':'Alpha distribution centre', 'state':'QLD', 'address':'88 Client Road'})
        self.assertIn(b'Client site updated.', result.data)
        self.assertEqual(self.rows('SELECT * FROM bf_bins'), stock)
        self.assertEqual(self.rows('SELECT * FROM bf_jobs'), jobs)
        pod = self.client.get('/binflow/pickups/1/pod').data
        self.assertIn(b'Alpha QLD', pod)
        self.assertNotIn(b'Alpha distribution centre', pod)
        self.job(deliver=(3,))
        self.assertEqual(self.rows('SELECT warehouse_name,site_name,site_address FROM bf_jobs WHERE id=2')[0],
                         {'warehouse_name':'North Maclean depot','site_name':'Alpha distribution centre','site_address':'88 Client Road'})
        unused_path = '/binflow/locations/2/edit'
        result = self.post(unused_path, {'name':'North Maclean depot', 'state':'QLD', 'address':'Duplicate'})
        self.assertEqual(result.status_code, 400)
        self.assertEqual(self.rows('SELECT state FROM bf_locations WHERE id=2')[0]['state'], 'NSW')
        result = self.post(unused_path, {'name':'Melbourne depot', 'state':'VIC', 'address':'4 Depot Road'})
        self.assertIn(b'Warehouse updated.', result.data)
        self.assertEqual(self.rows('SELECT state FROM bf_locations WHERE id=2')[0]['state'], 'VIC')
        self.assertEqual(self.client.get('/binflow/locations/999/edit').status_code, 404)
        self.assertEqual(self.client.post(warehouse_path, data={'name':'No CSRF'}).status_code, 400)

    def test_partial_confirmation_does_not_move_any_bin(self):
        self.job(deliver=(1,2))
        result=self.exchange(1,(1,))
        self.assertIn(b'every physical bin',result.data)
        self.assertEqual({r['status'] for r in self.rows('SELECT status FROM bf_bins')},{'empty'})
        self.assertEqual(self.rows('SELECT status FROM bf_jobs')[0]['status'],'scheduled')
        self.assertEqual(self.client.get('/binflow/pickups/1/pod.pdf').status_code,400)

    def test_weight_requires_receipt_valid_tare_and_cannot_be_overwritten(self):
        self.initial_delivery(); self.job(collect=(1,)); self.exchange(2,(1,))
        iid=self.rows("SELECT id FROM bf_job_bins WHERE job_id=2 AND direction='collect'")[0]['id']
        path=f'/binflow/pickups/2/weigh/{iid}'
        result=self.post(path,{'gross':'20','tare':'5','confirm_tare':'yes'})
        self.assertIn(b'Receive the bins',result.data)
        self.post('/binflow/pickups/2/receive',{'confirmed':[1]})
        for gross,tare,confirm in [('4','5','yes'),('NaN','5','yes'),('Infinity','5','yes'),('10.0001','5','yes'),('10','5','no')]:
            self.post(path,{'gross':gross,'tare':tare,'confirm_tare':confirm})
            self.assertIsNone(self.rows('SELECT weighed_at FROM bf_job_bins WHERE id=?',(iid,))[0]['weighed_at'])
        self.post(path,{'gross':'20','tare':'5','confirm_tare':'yes'})
        self.post(path,{'gross':'20','tare':'5','confirm_tare':'yes'})
        result=self.post(path,{'gross':'30','tare':'5','confirm_tare':'yes'})
        self.assertIn(b'cannot be overwritten',result.data)
        self.assertEqual(self.rows('SELECT net_g FROM bf_job_bins WHERE id=?',(iid,))[0]['net_g'],15000)

    def test_access_csrf_and_scan_after_login(self):
        with sqlite3.connect(self.path) as conn:
            conn.execute("INSERT INTO users(role,first_name,last_name,email,username,password_hash) VALUES('employee','Test','Driver','driver@example.com','driver',?)",(generate_password_hash('test-admin-password'),))
        self.post('/logout')
        token=self.rows('SELECT token FROM bf_bins WHERE id=1')[0]['token']
        result=self.client.get('/binflow/qr/'+token)
        self.assertEqual(result.status_code,200)
        self.client.get('/binflow/qr/'+token+'/driver/pickup')
        result=self.login('employee','driver')
        self.assertEqual(result.status_code,403)
        for path in ('/binflow/bins','/binflow/pickups/1','/binflow/export.csv','/binflow/bins/1/qr.png'):
            self.assertEqual(self.client.get(path).status_code,403)
        self.post('/logout'); self.login()
        uid=self.rows("SELECT id FROM users WHERE username='driver'")[0]['id']
        self.post('/binflow/setup',{'action':'access','users':[uid]})
        self.post('/logout')
        self.client.get('/binflow/qr/'+token+'/driver/pickup')
        result=self.login('employee','driver')
        self.assertEqual(result.status_code,200)
        self.assertEqual(result.request.path,'/binflow/qr/'+token+'/driver/pickup')
        self.assertEqual(self.client.get('/binflow/setup').status_code,403)
        self.assertEqual(self.client.get('/binflow/locations/1/edit').status_code,403)
        self.assertEqual(self.client.get('/binflow/bins/1/edit').status_code,403)
        self.assertEqual(self.move_bin(1,3).status_code,403)
        self.assertEqual(self.post('/binflow/locations/1/edit', {'name':'Blocked','state':'QLD','address':'Blocked'}).status_code,403)
        self.assertEqual(self.client.get('/admin/staff').status_code,403)
        self.assertEqual(self.client.post('/binflow/bins/1',data={'action':'full'}).status_code,400)
        self.assertEqual(self.post('/binflow/bins',{'warehouse_id':1,'size_id':1,'quantity':1,'tare':0}).status_code,403)
        self.assertIn(b'>BINFLOW</a>',self.client.get('/employee/dashboard').data)

    def test_unique_qr_labels_and_no_public_customer_data(self):
        tokens=[r['token'] for r in self.rows('SELECT token FROM bf_bins ORDER BY id')]
        self.assertEqual(len(set(tokens)),6)
        image=self.client.get('/binflow/bins/1/qr.png')
        self.assertEqual(image.status_code,200)
        self.assertTrue(image.data.startswith(b'\x89PNG'))
        self.assertEqual(self.client.get('/binflow/scan?code=BF000001').location,'/binflow/qr/'+tokens[0])
        self.assertEqual(self.client.get('/binflow/scan?code=https://example.com/binflow/qr/'+tokens[0]).location,'/binflow/qr/'+tokens[0])
        self.assertEqual(self.client.get('/binflow/labels?bin=nope').status_code,400)
        self.post('/logout')
        for path in ('/binflow/bins/1','/binflow/bins/1/qr.png','/binflow/qr/'+tokens[0]):
            response=self.client.get(path,follow_redirects=True)
            self.assertNotIn(b'Client Alpha',response.data)
            self.assertNotIn(b'QLD warehouse',response.data)

    def test_signed_handover_and_photo_are_saved_and_protected(self):
        self.job(deliver=(1,))
        result=self.post('/binflow/pickups/1/exchange',{'confirmed':[1],'confirmation':'signed','receiver_name':'Client Receiver'})
        self.assertIn(b'capture their signature',result.data)
        output=io.BytesIO();Image.new('RGB',(50,50),'blue').save(output,format='PNG');output.seek(0)
        result=self.post('/binflow/pickups/1/exchange',{'confirmed':[1],'confirmation':'signed','receiver_name':'Client Receiver','signature':'[[[10,10],[120,50],[180,20],[250,70]]]','photo':(output,'evidence.png')})
        self.assertIn(b'Exchange recorded',result.data)
        self.assertEqual(self.client.get('/binflow/pickups/1/evidence/signature').mimetype,'image/png')
        self.assertEqual(self.client.get('/binflow/pickups/1/evidence/photo').mimetype,'image/jpeg')
        self.assertTrue(self.client.get('/binflow/pickups/1/pod.pdf').data.startswith(b'%PDF-'))
        self.assertEqual(self.client.get('/binflow/pickups/1/evidence/other').status_code,404)

    def test_startup_preserves_records_and_invalid_changes_are_rejected(self):
        self.initial_delivery()
        before=self.rows('SELECT * FROM bf_bins')
        create_app(dict(self.app.config))
        self.assertEqual(self.rows('SELECT * FROM bf_bins'),before)
        self.post('/binflow/bins/1',{'action':'empty','notes':'not weighed'})
        self.assertEqual(self.rows('SELECT status FROM bf_bins WHERE id=1')[0]['status'],'at_client')
        self.post('/binflow/pickups/1/cancel',{'reason':'too late'})
        self.assertEqual(self.rows('SELECT status FROM bf_jobs WHERE id=1')[0]['status'],'complete')
        self.assertEqual(self.rows('SELECT COUNT(*) AS n FROM bf_sizes')[0]['n'],3)

if __name__=='__main__':
    unittest.main()
