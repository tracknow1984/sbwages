from binflow_helpers import pickup_material
import re
import sqlite3
import unittest
from datetime import timedelta
from urllib.parse import urlsplit, parse_qs

import test_binflow_workday as workday_tests
from binflow_helpers import RUN_DATE, SIGNATURE, prestart


class DriverJourneyTests(unittest.TestCase):
    post=workday_tests.DriverWorkdayTests.post
    rows=workday_tests.DriverWorkdayTests.rows
    login=workday_tests.DriverWorkdayTests.login
    job=workday_tests.DriverWorkdayTests.job
    initial_delivery=workday_tests.DriverWorkdayTests.initial_delivery
    exchange=workday_tests.DriverWorkdayTests.exchange
    setUp=workday_tests.DriverWorkdayTests.setUp
    tearDown=workday_tests.DriverWorkdayTests.tearDown
    stamp=workday_tests.DriverWorkdayTests.stamp
    choose=workday_tests.DriverWorkdayTests.choose
    finish=workday_tests.DriverWorkdayTests.finish
    stage=workday_tests.DriverWorkdayTests.stage

    def start(self):
        return self.post('/binflow/driver/start-day',dict(flow_version='2',**self.stamp()))

    def accept_overview(self, suffix='', state='QLD'):
        page=self.client.get('/binflow/driver/my-day'+suffix)
        version=re.search(rb'name="version" value="([^"]+)"',page.data)
        self.assertIsNotNone(version,page.data[-5000:])
        wid=re.search(rb'/my-day/(\d+)/overview',page.data)[1].decode()
        return self.post(f'/binflow/driver/my-day/{wid}/overview'+suffix,dict(state=state,version=version[1].decode()))

    def simple_prestart(self,suffix='',run_date=RUN_DATE):
        import secrets
        from binflow_runs import CHECKS
        values = {'check_'+key:'pass' for key,_,_ in CHECKS}
        return self.post(f'/binflow/driver/{self.truck_id}/prestart'+suffix,
                         dict(values,date=run_date,reading='12000',submission_token=secrets.token_urlsafe(24),
                              fit_for_duty='yes',signature=SIGNATURE,signed='yes'))

    def scan(self, action, code, suffix=''):
        material = (2 if code=='DEMO-660-F06' else 1) if suffix else pickup_material(self)
        return self.post(f'/binflow/driver/{self.truck_id}/day/scan-bin'+suffix,dict(date=RUN_DATE,action=action,code=code,material_id=material),follow=False)

    def action(self, action, suffix='', **values):
        return self.post(f'/binflow/driver/{self.truck_id}/day/'+action+suffix,dict(date=RUN_DATE,**values))

    def test_complete_live_day_records_manifest_automatic_pod_and_inbound(self):
        self.start()
        self.assertIn(b'Your daily overview',self.client.get('/binflow/driver/my-day').data)
        self.choose()
        self.assertIsNone(self.rows('SELECT truck_id FROM bf_workdays')[0]['truck_id'])
        self.accept_overview();self.choose()
        self.assertEqual(self.stage(),'prestart')
        self.simple_prestart();self.assertEqual(self.stage(),'load')
        self.assertEqual(len(self.rows('SELECT * FROM prestarts')),1)
        self.assertEqual(self.scan('deliver','BF000003').status_code,409)
        for code in ('BF000003','BF000004'):
            self.assertTrue(self.scan('load',code).json['ok'])
        self.assertEqual(self.stage(),'ready')
        self.assertIn(b'Confirm manifest',self.client.get('/binflow/driver/1/day?date='+RUN_DATE).data)
        self.action('depart');self.assertEqual(self.stage(),'navigate')
        run=self.rows('SELECT * FROM bf_runs')[0]
        self.assertTrue(run['manifest_confirmed_at']);self.assertIn('BF000003',run['onboard_manifest'])
        self.assertEqual(self.scan('deliver','BF000003').status_code,409)
        self.action('client-arrival',job_id=2)
        self.assertEqual(self.stage(),'deliver')
        self.assertEqual(self.scan('collect','BF000001').status_code,409)
        self.scan('deliver','BF000003');self.scan('deliver','BF000004')
        self.assertEqual(self.stage(),'collect')
        rejected = self.post('/binflow/driver/1/day/scan-bin',dict(date=RUN_DATE,action='collect',code='BF000001'),follow=False)
        self.assertEqual(rejected.status_code,409)
        self.assertIn('Choose the material',rejected.json['message'])
        self.assertIsNone(self.rows('SELECT picked_up_at FROM bf_job_bins WHERE job_id=2 AND bin_id=1')[0]['picked_up_at'])
        invalid = self.post('/binflow/driver/1/day/scan-bin',dict(date=RUN_DATE,action='collect',code='BF000001',material_id=99999),follow=False)
        self.assertEqual(invalid.status_code,409)
        self.scan('collect','BF000001');self.scan('collect','BF000002')
        self.assertEqual(self.stage(),'navigate_depot')
        job=self.rows('SELECT * FROM bf_jobs WHERE id=2')[0]
        self.assertEqual(job['confirmation'],'driver_scanned');self.assertIsNone(job['signature'])
        self.assertEqual(job['status'],'collected')
        self.assertEqual(self.client.get('/binflow/pickups/2/pod').status_code,409)
        self.assertEqual(self.client.get('/binflow/pickups/2/pod.pdf').status_code,409)
        count=len(self.rows('SELECT * FROM bf_events'))
        retry=self.scan('collect','BF000002')
        self.assertTrue(retry.json['duplicate']);self.assertEqual(len(self.rows('SELECT * FROM bf_events')),count)
        self.assertEqual(self.scan('return','BF000001').status_code,409)
        self.action('depot-arrival',warehouse_id=1);self.assertEqual(self.stage(),'return')
        self.scan('return','BF000001');self.scan('return','BF000002')
        self.assertEqual(self.stage(),'weighing')
        self.assertEqual(self.rows('SELECT status,location_id,truck_id FROM bf_bins WHERE id=1')[0],dict(status='received',location_id=1,truck_id=None))
        self.time+=timedelta(hours=8)
        self.assertIn(b'Your work is saved',self.finish().data)
        self.assertTrue(self.rows('SELECT committed_at FROM entries')[0]['committed_at'])
        self.assertIsNone(self.rows('SELECT pod_signed_at FROM bf_jobs WHERE id=2')[0]['pod_signed_at'])
        with sqlite3.connect(self.path) as conn:
            warehouse_user = conn.execute("""INSERT INTO users(role,first_name,last_name,email,username,password_hash,
                business_role,extra_business_roles,working_state) VALUES('employee','Warehouse','Operator','warehouse@example.com','warehouseperson',?,
                '','warehouse','QLD')""", (workday_tests.generate_password_hash('test-admin-password'),)).lastrowid
            conn.execute('INSERT INTO bf_bale_managers(user_id,warehouse_id) VALUES(?,1)',(warehouse_user,))
        self.post('/logout');self.login('employee','warehouseperson')
        self.assertIn(b'TO BE WEIGHED',self.client.get('/binflow/warehouse').data)
        page = self.client.get('/binflow/pickups/2')
        self.assertIn('Pickup material',page.text)
        self.assertNotIn('name="material_',page.text)
        version = self.rows('SELECT pod_version FROM bf_jobs WHERE id=2')[0]['pod_version']
        self.assertIn('all weights',self.post('/binflow/pickups/2/pod/sign',dict(version=version,confirm='yes',signature=SIGNATURE)).text)
        before = self.rows('SELECT material_id,material_name FROM bf_job_bins WHERE job_id=2 ORDER BY id')
        for item in self.rows("SELECT * FROM bf_job_bins WHERE job_id=2 AND direction='collect'"):
            scanned = self.post('/binflow/warehouse/scan',dict(code=item['bin_code']),follow=False)
            self.assertTrue(scanned.json['ok'])
            self.assertIn('#weigh-'+str(item['id']),scanned.json['redirect'])
            self.assertEqual(self.client.get(scanned.json['redirect']).status_code,200)
            self.post(f'/binflow/pickups/2/weigh/{item["id"]}',dict(gross='50',tare='5',confirm_tare='yes'))
        self.assertEqual(self.rows('SELECT status FROM bf_jobs WHERE id=2')[0]['status'],'complete')
        self.assertEqual(self.rows('SELECT material_id,material_name FROM bf_job_bins WHERE job_id=2 ORDER BY id'),before)
        version = self.rows('SELECT pod_version FROM bf_jobs WHERE id=2')[0]['pod_version']
        self.assertIn('POD signed off.',self.post('/binflow/pickups/2/pod/sign',dict(version=version,confirm='yes',signature=SIGNATURE)).text)
        with sqlite3.connect(self.path) as conn:
            conn.execute("UPDATE users SET working_state='NSW' WHERE id=?",(warehouse_user,))
        self.assertEqual(self.client.get('/binflow/pickups/2').status_code,403)
        self.assertEqual(self.post('/binflow/warehouse/scan',dict(code='BF000001'),follow=False).status_code,409)
        self.post('/logout');self.login('employee','phonedriver')
        self.assertIn(b'Your work is saved',self.finish().data)
        self.assertTrue(self.rows('SELECT committed_at FROM entries')[0]['committed_at'])

    def test_stale_overview_and_other_state_cannot_select_vehicle(self):
        self.start();self.accept_overview()
        with sqlite3.connect(self.path) as conn:
            conn.execute("UPDATE bf_jobs SET notes='Changed stop instructions' WHERE id=2")
        self.assertIn(b'Review and accept the current daily overview',self.choose().data)
        self.assertIsNone(self.rows('SELECT truck_id FROM bf_workdays')[0]['truck_id'])
        self.accept_overview();self.assertEqual(self.choose(truck=2,state='NSW').status_code,403)
        self.assertIsNone(self.rows('SELECT truck_id FROM bf_workdays')[0]['truck_id'])

    def test_reserved_spare_cannot_take_accepted_stops(self):
        self.start()
        picker = self.accept_overview()
        self.assertIn('NSW day truck · NSW', picker.text)
        with sqlite3.connect(self.path) as conn:
            other = conn.execute("SELECT id FROM users WHERE role='admin' LIMIT 1").fetchone()[0]
            conn.execute("INSERT INTO bf_runs(truck_id,run_date,driver_id) VALUES(2,?,?)", (RUN_DATE,other))
        self.assertIn('This vehicle is unavailable', self.choose(truck=2).text)
        self.assertNotIn('NSW day truck · NSW', self.client.get('/binflow/driver/my-day').text)
        self.assertEqual(self.rows('SELECT truck_id FROM bf_jobs WHERE id=2')[0]['truck_id'], 1)
        self.assertIsNone(self.rows('SELECT truck_id FROM bf_workdays')[0]['truck_id'])

    def test_spare_takes_only_the_chosen_manifest_when_two_runs_are_available(self):
        self.post('/logout')
        self.login()
        self.post('/binflow/driver', dict(name='Second QLD truck',state='QLD'))
        self.job(deliver=(5,),site=4,legacy=False)
        self.post('/binflow/pickups/3/truck', dict(truck_id=3))
        self.post('/logout')
        self.login('employee','phonedriver')
        self.start()
        picker = self.accept_overview()
        self.assertIn('name="scheduled_truck_id"', picker.text)
        self.assertIn('Select the scheduled run', self.choose(truck=2).text)
        day = self.rows('SELECT * FROM bf_workdays')[0]
        path = f'/binflow/driver/my-day/{day["id"]}/vehicle'
        fields = dict(truck_id=2, state='QLD', scheduled_truck_id=3)
        self.assertIn('Vehicle prestart', self.post(path,fields).text)
        self.assertIn('Vehicle prestart', self.post(path,fields).text)
        self.assertEqual(self.rows('SELECT truck_id FROM bf_jobs WHERE id IN (2,3) ORDER BY id'),
                         [dict(truck_id=1),dict(truck_id=2)])
        self.assertEqual(len(self.rows('SELECT * FROM bf_run_acceptance_history')), 1)

    def test_break_persists_and_requires_thirty_minutes(self):
        self.start();self.accept_overview();self.choose();self.simple_prestart()
        self.time+=timedelta(hours=4)
        wid=self.rows('SELECT id FROM bf_workdays')[0]['id']
        self.post(f'/binflow/driver/my-day/{wid}/break/start')
        self.assertEqual(self.stage(),'break')
        start=self.rows('SELECT break_started_at FROM bf_workdays')[0]['break_started_at']
        self.time+=timedelta(minutes=5)
        self.post(f'/binflow/driver/my-day/{wid}/break/start')
        self.assertEqual(self.rows('SELECT break_started_at FROM bf_workdays')[0]['break_started_at'],start)
        self.assertIn(b'break is still running',self.post(f'/binflow/driver/my-day/{wid}/break/finish').data)
        self.assertEqual(self.scan('load','BF000003').status_code,409)
        self.time+=timedelta(minutes=25)
        self.post(f'/binflow/driver/my-day/{wid}/break/finish')
        self.assertEqual(self.stage(),'load')
        self.assertTrue(self.client.get('/binflow/driver/my-day/clock-state').json['day']['break_finished_at'])

    def test_testing_prestart_cannot_clear_existing_defect(self):
        self.start();self.accept_overview();self.choose()
        import secrets
        from binflow_helpers import SIGNATURE
        from binflow_runs import CHECKS
        fields={f'check_{key}':'pass' for key,_,_ in CHECKS}
        self.post('/binflow/driver/1/prestart',dict(fields,date=RUN_DATE,submission_token=secrets.token_urlsafe(24),reading='100',
            fit_for_duty='yes',signature=SIGNATURE,signed='yes',check_brakes='fail',note_brakes='Brake issue'))
        self.assertEqual(self.stage(),'hold')
        self.post('/binflow/driver/1/test-prestart',dict(date=RUN_DATE));self.assertEqual(self.stage(),'hold')
        self.assertIsNone(self.rows('SELECT test_prestart_at FROM bf_runs')[0]['test_prestart_at'])
        with sqlite3.connect(self.path) as conn:
            conn.execute("UPDATE bf_run_inspections SET resolved_at=?,resolution='Repaired'",(self.time.isoformat(),))
        self.post('/binflow/driver/1/test-prestart',dict(date=RUN_DATE));self.assertEqual(self.stage(),'prestart')
        self.assertIn(b'Odometer',self.client.get('/binflow/driver/1/day?date='+RUN_DATE).data)
        prestart(self);self.assertEqual(self.stage(),'load')

    def test_testing_prestart_uses_driver_local_date_across_midnight(self):
        from datetime import datetime
        from zoneinfo import ZoneInfo
        self.time=datetime(2026,10,19,23,30,tzinfo=ZoneInfo('Australia/Brisbane'))
        with sqlite3.connect(self.path) as conn:
            conn.execute("UPDATE bf_jobs SET scheduled_date='2026-10-20' WHERE id=2")
        self.post('/binflow/driver/start-day',dict(flow_version='2',**self.stamp('Australia/Sydney')))
        self.accept_overview();self.choose()
        self.simple_prestart(run_date='2026-10-20')
        self.assertEqual(self.client.get('/binflow/driver/1/day/state?date=2026-10-20').json['stage'],'load')
        self.assertEqual(self.rows('SELECT inspection_date FROM bf_run_inspections')[0]['inspection_date'],'2026-10-20')

    def test_admin_sample_never_links_to_real_mutations(self):
        self.post('/logout');self.login()
        response=self.post('/binflow/driver/demo',dict(state='NSW'),follow=False)
        token=parse_qs(urlsplit(response.location).query)['demo'][0]
        suffix='?demo='+token
        self.post('/binflow/driver/start-day'+suffix,dict(flow_version='2',**self.stamp()))
        self.assertIn('Smithfield',self.client.get('/binflow/driver/my-day'+suffix).text)
        self.assertIn('Choose your truck',self.accept_overview(suffix,'NSW').text)
        job=self.client.get('/binflow/pickups/1'+suffix)
        self.assertNotIn(b'Warehouse weigh-in',job.data)
        self.assertNotIn(b'Cancel this scheduled job',job.data)
        pods=self.client.get('/binflow/pods'+suffix)
        self.assertIn(('name="demo" value="'+token+'"').encode(),pods.data)
        for action in (b'Export list CSV',b'Manage client access',b'>Email</a>',b'>Delete</button>'):
            self.assertNotIn(action,pods.data)
        self.assertEqual(self.post('/binflow/pickups/1/weigh/1'+suffix,dict(gross='50')).status_code,403)
        self.assertEqual(self.client.get('/binflow/pods/1/email'+suffix).status_code,403)

    def complete_sample_warehouse(self, token):
        suffix = '?demo=' + token
        base = '/training/' + token + '/binflow'
        inbound = '/binflow/driver/demo/inbound'
        hub = self.client.get(inbound + suffix)
        self.assertIn(base + '/bales/new', hub.text)
        materials = {}
        for jid, material in ((1, 'COTTON'), (2, 'PC MIX')):
            desk = inbound + '/' + str(jid) + suffix
            for code in (('DEMO-1100-F04','DEMO-240-F05') if jid==1 else ('DEMO-660-F06',)):
                scanned = self.post(base+'/warehouse/scan',dict(code=code),follow=False)
                self.assertTrue(scanned.json['ok'])
                self.assertIn('/demo/inbound/'+str(jid),scanned.json['redirect'])
            page = self.client.get(desk)
            actions = re.findall(r'<form method="post" action="([^"]+/weigh/\d+)"', page.text)
            gross = (165, 30) if jid == 1 else (115,)
            tares = (65, 5) if jid == 1 else (40,)
            self.assertEqual(len(actions),len(gross))
            for action, weight, tare in zip(actions,gross,tares):
                self.assertTrue(action.startswith(base + '/pickups/'))
                page = self.post(action, dict(gross=weight,tare=tare,confirm_tare='yes'))
                self.assertIn('Weight saved', page.text)
            self.assertNotIn('name="material_',page.text)
            self.assertIn(material,page.text)
            # Read the classification from the sample, whose IDs are isolated from live stock.
            form = self.client.get(base+'/bales/new?warehouse=1')
            mid = re.search(r'<option value="(\d+)"[^>]*>COTTON</option>',form.text)[1]
            key = re.search(r'name="request_key" value="([^"]+)"',form.text)[1]
            blocked_scan = self.post(base+'/bales/source-scan',dict(warehouse_id=1,material_id=mid,
                request_key=key,code='DEMO-1100-F04'))
            self.assertEqual(blocked_scan.status_code,409)
            self.assertIn('sign off',blocked_scan.json['message'])
            self.assertIn('Awaiting POD sign-off',self.client.get(inbound+suffix).text)
            if jid==1:
                driver_page = self.client.get('/binflow/driver/my-day'+suffix)
                driver_page = self.client.get(driver_page.location)
                self.assertIn('2 of 3 bins weighed',driver_page.text)
                self.assertNotIn('WAREHOUSE HANDOVER COMPLETE',driver_page.text)
                self.assertIn('>Finish day<',driver_page.text)
                for document in ('pod','pod.pdf'):
                    self.assertEqual(self.client.get('/binflow/pickups/1/'+document+suffix).status_code,409)
                self.assertNotIn('name="run_version"',page.text)
                version = re.search(r'name="version" value="(\d+)"',page.text)
                blocked = self.post(base+'/pickups/1/pod/sign',dict(scope='run',version=version[1] if version else 0,
                                                                     confirm='yes',signature=SIGNATURE))
                self.assertNotIn('Signed by <strong>',blocked.text)
        version = re.search(r'name="version" value="(\d+)"',page.text)[1]
        run_version = re.search(r'name="run_version" value="([^"]+)"',page.text)[1]
        signed = self.post(base+'/pickups/2/pod/sign',dict(version=version,run_version=run_version,
                                                           scope='run',confirm='yes',signature=SIGNATURE))
        self.assertIn('Signed by <strong>',signed.text)
        driver_page = self.client.get('/binflow/driver/my-day'+suffix, follow_redirects=True)
        self.assertIn('WAREHOUSE HANDOVER COMPLETE',driver_page.text)
        self.assertIn('>Finish day<',driver_page.text)
        for jid in (1,2):
            self.assertIn('Signed by <strong>',self.client.get(inbound+'/'+str(jid)+suffix).text)
        form = self.client.get(base+'/bales/new?warehouse=1')
        for material in ('COTTON','PC MIX'):
            materials[material] = re.search(r'<option value="(\d+)"[^>]*>'+material+'</option>',form.text)[1]
        created = []
        wrong = self.post(base+'/bales/source-scan',dict(warehouse_id=1,material_id=materials['COTTON'],
            request_key=key,code='DEMO-660-F06'))
        self.assertEqual(wrong.status_code,409)
        for material, codes, weight in [('COTTON',['DEMO-1100-F04','DEMO-240-F05'],125),('PC MIX',['DEMO-660-F06'],75)]:
            form = self.client.get(base+'/bales/new?warehouse=1')
            key = re.search(r'name="request_key" value="([^"]+)"',form.text)[1]
            scans = [self.post(base+'/bales/source-scan',dict(warehouse_id=1,material_id=materials[material],request_key=key,code=c)).json for c in codes]
            self.assertTrue(all(s['ok'] for s in scans))
            self.assertIn('Bin released',self.client.get(inbound+suffix).text)
            response = self.post(base+'/bales/new',dict(warehouse_id=1,material_id=materials[material],
                request_key=key,source_code=codes,source_item=[s['item_id'] for s in scans],weight=weight,confirm_sources='yes'))
            self.assertIn('Print bale label',response.text)
            created.append(re.search(r'BA\d{7}',response.text)[0])
            self.assertEqual(self.client.get(base+f'/bales/{len(created)}/qr.png').mimetype,'image/png')
        self.assertEqual(self.client.get(inbound+suffix).text.count('Emptied into'),3)
        form = self.client.get(base+'/bales/transfers/new')
        key = re.search(r'name="request_key" value="([^"]+)"',form.text)[1]
        page = self.post(base+'/bales/transfers/new',dict(origin_id=1,destination_id=4,carrier_supplier_id=1,vehicle_type='SINGLE',
            vehicle='TEST-01',payload=1000,request_key=key))
        transfer = base+'/bales/transfers/1'
        self.assertEqual(page.request.path,transfer)
        for action in ('load','receive'):
            for code in created:
                resp = self.post(transfer+'/scan',dict(action=action,code=code))
                self.assertEqual(resp.status_code,200,resp.text)
            version = resp.json['version']
            if action=='load':
                self.post(transfer+'/action',dict(action='depart',confirm='yes',version=version))
        self.post(transfer+'/action',dict(action='handover',version=version,recipient='Sample receiver',confirm='yes',signature=SIGNATURE))
        note = self.client.get(transfer+'/note')
        self.assertEqual(note.status_code,200)
        for text in ['Sample receiver','200','Wetherill Park','Brisbane',*created]:
            self.assertIn(text,note.text)
        self.assertIn('View signed storage note',self.client.get(inbound+suffix).text)
        self.assertEqual(self.client.get('/binflow/bales/new').status_code,403)
        other=self.app.test_client()
        with other.session_transaction() as session: session.update(uid=self.uid,version=0,csrf='other')
        self.assertEqual(other.get(transfer+'/note').status_code,403)
        self.assertEqual(self.client.get('/training/invalid/binflow/bales/new').status_code,403)

    def test_today_sample_uses_same_flow_without_changing_real_stock_or_payroll(self):
        self.assertEqual(self.rows('SELECT working_state FROM users WHERE id=?', (self.uid,))[0]['working_state'],'QLD')
        page = self.client.get('/binflow/driver/test-cycle')
        self.assertIn('Start the NSW sample cycle',page.text)
        self.assertIn('name="state" value="NSW"',page.text)
        self.assertIn('name="state" value="NSW"',self.client.get('/binflow/driver/my-day').text)
        before={table:self.rows('SELECT * FROM '+table) for table in ('users','bf_bins','bf_jobs','bf_job_bins','bf_workdays','bf_events','entries','sheets','bf_step_timings','bf_bale_storage','bf_bales','bf_bale_sources','bf_bale_transfers','bf_rental_invoices')}
        opened=self.post('/binflow/driver/demo',follow=False)
        self.assertEqual(opened.status_code,302)
        token=parse_qs(urlsplit(opened.location).query)['demo'][0]
        suffix='?demo='+token
        self.assertIn(b'SAMPLE RUN',self.client.get(opened.location).data)
        self.assertEqual(self.client.get('/binflow/warehouse'+suffix).status_code,403)
        self.assertEqual(self.client.get('/binflow/driver/my-day?demo=invalid').status_code,403)
        self.assertIn(b'DEMO-1100-E01',self.client.get('/binflow/driver/demo/labels'+suffix).data)
        self.assertEqual(self.client.get('/binflow/driver/demo/qr/1'+suffix).mimetype,'image/png')
        self.post('/binflow/driver/start-day'+suffix,dict(flow_version='2',**self.stamp()))
        overview = self.client.get('/binflow/driver/my-day'+suffix)
        for place in ('Smithfield','Fairfield'):
            self.assertIn(place,overview.text)
        picker = self.accept_overview(suffix, 'NSW')
        self.assertIn('SAMPLE · interstate spare · QLD', picker.text)
        self.truck_id = 2
        selected=self.post('/binflow/driver/my-day/1/vehicle'+suffix,dict(state='NSW',truck_id=self.truck_id))
        self.assertIn(b'Vehicle prestart',selected.data)
        loading = self.client.get(f'/binflow/driver/{self.truck_id}/load-list'+suffix+'&date='+RUN_DATE)
        self.assertIn('Wetherill Park',loading.text)
        self.simple_prestart(suffix)
        self.assertEqual(self.scan('load','DEMO-660-E03',suffix).status_code,409)
        self.scan('load','DEMO-1100-E01',suffix)
        next_job=self.scan('load','DEMO-240-E02',suffix).json
        self.assertEqual(next_job['step_key'],'load:2')
        self.scan('load','DEMO-660-E03',suffix)
        self.action('depart',suffix)
        self.action('client-arrival',suffix,job_id=1)
        self.scan('deliver','DEMO-1100-E01',suffix);self.scan('deliver','DEMO-240-E02',suffix)
        self.scan('collect','DEMO-1100-F04',suffix);self.scan('collect','DEMO-240-F05',suffix)
        self.assertEqual(self.client.get('/binflow/pickups/1/pod'+suffix).status_code,409)
        self.action('client-arrival',suffix,job_id=2)
        self.scan('deliver','DEMO-660-E03',suffix);self.scan('collect','DEMO-660-F06',suffix)
        self.action('depot-arrival',suffix,warehouse_id=1)
        for code in ('DEMO-1100-F04','DEMO-240-F05','DEMO-660-F06'):
            self.assertTrue(self.scan('return',code,suffix).json['ok'])
        self.assertEqual(self.client.get('/binflow/driver/demo/inbound'+suffix).data.count(b'TO BE WEIGHED'),3)
        self.time+=timedelta(hours=8)
        self.assertIn(b'Finish day',self.client.get('/binflow/driver/my-day'+suffix,follow_redirects=True).data)
        self.complete_sample_warehouse(token)
        finished=self.post('/binflow/driver/my-day/1/finish'+suffix,self.stamp())
        self.assertIn(b'Sample run complete',finished.data)
        for table,data in before.items():self.assertEqual(self.rows('SELECT * FROM '+table),data,table)
        self.assertEqual(self.client.get('/binflow/driver/my-day?state=NSW').status_code,403)
        self.assertIn('<p>QLD</p>',self.client.get('/binflow/driver/my-day').text)
        # A second authenticated session cannot use the owner's sample token.
        second=self.app.test_client()
        with second.session_transaction() as session: session.update(uid=self.uid,version=0,csrf='x')
        self.assertEqual(second.get('/binflow/driver/my-day'+suffix).status_code,403)
        resumed=self.post('/binflow/driver/demo',dict(state='NSW'),follow=False)
        self.assertIn(token,resumed.location)
        reset=self.post('/binflow/driver/demo',dict(state='NSW',reset='yes'),follow=False)
        self.assertNotIn(token,reset.location)
        self.assertEqual(self.client.get('/binflow/driver/my-day'+suffix).status_code,403)


if __name__=='__main__':unittest.main()
