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

    def test_registration_uploads_and_older_edit_forms(self):
        from datetime import date, timedelta
        from pypdf import PdfWriter
        from fleet_assets import due_notices
        import sqlite3

        root = '/admin/prestart/assets'
        day = date(2028, 4, 10)
        pdf = io.BytesIO()
        writer = PdfWriter()
        writer.add_blank_page(width=300, height=400)
        writer.write(pdf)
        image = io.BytesIO()
        Image.new('RGB',(400,400)).save(image,'PNG',compress_level=0)
        self.assertGreater(len(image.getvalue()), 128 * 1024)
        fields = dict(name='Workshop rigid', state='QLD', rego='testxyz', vin='jh4ka8260mc000123',
                      description='Fleet test rigid', gvm_kg='18000', tare_kg='9800',
                      nhv_charging_code='2R3', purpose_of_use='COMMERCIAL', rego_status='CURRENT',
                      rego_expiry='2029-02-28', inspection_certificate_number='TEST COI 10',
                      inspection_certificate_due='2028-09-20', next_inspection='2028-09-10',
                      next_service_date=(day+timedelta(days=7)).isoformat(), service_notice_days='7',
                      coc_due_date=day.isoformat(), coc_notice_days='0')
        created = self.post(root, {**fields, 'registration_files': (io.BytesIO(image.getvalue()),'paper.png'),
                                  'inspection_files': (io.BytesIO(pdf.getvalue()),'certificate.pdf'),
                                  'coc_files': (io.BytesIO(pdf.getvalue()),'coc.pdf')})
        self.assertEqual(created.status_code, 200)
        path = created.request.path
        self.assertRegex(path, r'/assets/\d+$')
        for text in ('JH4KA8260MC000123','Fleet test rigid','18,000 kg','9,800 kg','TEST COI 10',
                     'data-preview="application/pdf"', 'value="0"', 'Service history'):
            self.assertIn(text, created.text)
        links = re.findall(r'href="('+path+r'/files/\d+)"',created.text)
        self.assertEqual(len(links), 3)
        response = self.client.get(links[0])
        self.assertEqual(response.headers['X-Frame-Options'],'SAMEORIGIN')
        self.assertIn("frame-ancestors 'self'",response.headers['Content-Security-Policy'])
        self.assertTrue(self.client.get(links[0]+'?download=1').headers['Content-Disposition'].startswith('attachment;'))
        self.assertEqual(self.client.get(links[0].replace(path,root+'/9999')).status_code,404)
        with sqlite3.connect(self.path) as conn:
            conn.row_factory = sqlite3.Row
            def notices(offset):
                return [(r['kind'], r['days_left']) for r in due_notices(conn,(day+timedelta(days=offset)).isoformat())]
            self.assertEqual(notices(-1), [])
            self.assertEqual(notices(0), [('COC',0),('Service',7)])
            self.assertEqual(notices(1), [('COC',-1),('Service',6)])
        saved = self.post(path+'/edit', dict(name='Renamed truck',state='QLD',version='1'))
        self.assertIn('JH4KA8260MC000123', saved.text)
        self.assertIn('name="coc_notice_days" value="0"', saved.text)
        duplicate = self.post(root,{**fields,'name':'Another truck','state':'NSW','rego':'OTHER'})
        self.assertEqual(duplicate.status_code,422)
        self.assertIn('VIN is already recorded', duplicate.text)
        failed = self.post(path+'/edit',dict(version='2',name='Rejected truck',state='QLD',tare_kg='20000'))
        self.assertEqual(failed.status_code,422)
        self.assertIn('Tare cannot exceed',failed.text)
        failed = self.post(path+'/edit', dict(version='2',name='Still unchanged',state='QLD',
                    registration_files=(io.BytesIO(image.getvalue()),'another.png'),
                    inspection_files=(io.BytesIO(b'invalid'),'broken.pdf')))
        self.assertEqual(failed.status_code,422)
        after = self.client.get(path)
        self.assertIn('Renamed truck', after.text)
        self.assertNotIn('another.png', after.text)
        self.assertNotIn('Still unchanged', after.text)
        self.assertIn('TESTXYZ', self.client.get(root).text)

    def test_service_table_preview_schedule_renewal_and_admin_notices(self):
        from datetime import datetime, timedelta
        from zoneinfo import ZoneInfo
        import sqlite3
        today = datetime.now(ZoneInfo('Australia/Brisbane')).date()
        root = '/admin/prestart/assets'
        created = self.post(root, dict(name='Workshop unit', state='VIC', next_service_date=today.isoformat(),
                                       coc_due_date=(today-timedelta(days=1)).isoformat(), coc_notice_days='0'))
        path = created.request.path
        self.assertIn('Due today', self.client.get(root).text)
        self.assertIn('1 day overdue', self.client.get(root).text)
        self.assertIn('Fleet service and COC notices', self.client.get('/admin/dashboard').text)
        record = dict(kind='Service',event_date=today.isoformat(),summary='Replaced brake pads',
                      version='1',key=secrets.token_urlsafe(24),
                      following_service=(today+timedelta(days=90)).isoformat(),following_notice='14')
        scan = io.BytesIO()
        Image.new('RGB',(400,400)).save(scan,'PNG',compress_level=0)
        def upload(values):
            return self.post(path+'/history',{**values,'files':(io.BytesIO(scan.getvalue()),'workshop.png')})
        result = upload(record)
        self.assertEqual(result.status_code,200)
        self.assertIn('Replaced brake pads',result.text)
        self.assertIn('Quick view of workshop.png',result.text)
        self.assertIn('14 days’ notice',result.text)
        self.assertNotIn('Due today',self.client.get(root).text)
        self.assertEqual(upload(record).status_code,200)
        self.assertEqual(upload({**record,'key':secrets.token_urlsafe(24)}).status_code,422)
        with sqlite3.connect(self.path) as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM vehicle_history").fetchone()[0],1)
            self.assertEqual(conn.execute("SELECT count(*) FROM vehicle_files").fetchone()[0],1)
        renewed = self.post(path+'/edit',dict(name='Workshop unit',state='VIC',version='2',
                                             coc_due_date=(today+timedelta(days=180)).isoformat()))
        self.assertEqual(renewed.status_code,200)
        self.assertIn('No service or COC reminders are due.', self.client.get(root).text)
        self.assertNotIn('Fleet service and COC notices', self.client.get('/admin/dashboard').text)

    def test_migration_preserves_existing_profiles_and_history(self):
        import sqlite3
        from fleet_assets import migrate
        with sqlite3.connect(':memory:') as conn:
            conn.executescript("""
                CREATE TABLE bf_trucks(id INTEGER PRIMARY KEY);
                CREATE TABLE bf_run_inspections(truck_id INTEGER,vehicle_state TEXT);
                CREATE TABLE vehicle_profiles(truck_id INTEGER PRIMARY KEY, make TEXT, model TEXT, rego TEXT,
                    rego_expiry TEXT, next_inspection TEXT, last_service TEXT, version INTEGER);
                INSERT INTO vehicle_profiles VALUES(42,'Test','Rigid','TEST123','2028-12-31',NULL,NULL,5);
                ALTER TABLE bf_trucks ADD COLUMN state TEXT;
            """)
            migrate(conn)
            conn.commit()
            migrate(conn)
            self.assertEqual(conn.execute("""SELECT rego,version,vin,service_notice_days,coc_notice_days,
                                             next_service_date,coc_due_date FROM vehicle_profiles""").fetchone(),
                             ('TEST123',5,'',30,30,None,None))
