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

    def simple_prestart(self,suffix=''):
        return self.post(f'/binflow/driver/{self.truck_id}/test-prestart'+suffix,dict(date=RUN_DATE))

    def scan(self, action, code, suffix=''):
        return self.post(f'/binflow/driver/{self.truck_id}/day/scan-bin'+suffix,dict(date=RUN_DATE,action=action,code=code),follow=False)

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
        self.assertFalse(self.rows('SELECT * FROM prestarts'))
        self.assertEqual(self.scan('deliver','BF000003').status_code,409)
        for code in ('BF000003','BF000004'):
            self.assertTrue(self.scan('load',code).json['ok'])
        self.assertEqual(self.stage(),'ready')
        self.assertIn(b'Confirm your truck manifest',self.client.get('/binflow/driver/1/day?date='+RUN_DATE).data)
        self.action('depart');self.assertEqual(self.stage(),'navigate')
        run=self.rows('SELECT * FROM bf_runs')[0]
        self.assertTrue(run['manifest_confirmed_at']);self.assertIn('BF000003',run['onboard_manifest'])
        self.assertEqual(self.scan('deliver','BF000003').status_code,409)
        self.action('client-arrival',job_id=2)
        self.assertEqual(self.stage(),'deliver')
        self.assertEqual(self.scan('collect','BF000001').status_code,409)
        self.scan('deliver','BF000003');self.scan('deliver','BF000004')
        self.assertEqual(self.stage(),'collect')
        self.scan('collect','BF000001');self.scan('collect','BF000002')
        self.assertEqual(self.stage(),'navigate_depot')
        job=self.rows('SELECT * FROM bf_jobs WHERE id=2')[0]
        self.assertEqual(job['confirmation'],'driver_scanned');self.assertIsNone(job['signature'])
        self.assertEqual(job['status'],'collected')
        self.assertIn(b'Driver QR scans completed',self.client.get('/binflow/pickups/2/pod').data)
        self.assertTrue(self.client.get('/binflow/pickups/2/pod.pdf').data.startswith(b'%PDF'))
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
        self.post('/logout');self.login()
        self.assertIn(b'TO BE WEIGHED',self.client.get('/binflow/warehouse').data)
        for item in self.rows("SELECT id FROM bf_job_bins WHERE job_id=2 AND direction='collect'"):
            self.post(f'/binflow/pickups/2/weigh/{item["id"]}',dict(gross='50',tare='5',confirm_tare='yes'))
        self.assertEqual(self.rows('SELECT status FROM bf_jobs WHERE id=2')[0]['status'],'complete')
        self.assertIn(b'SIGN-OFF PENDING',self.client.get('/binflow/pickups/2/pod').data)

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
        self.assertIn('Ready to load?', self.post(path,fields).text)
        self.assertIn('Ready to load?', self.post(path,fields).text)
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
            fit_for_duty='yes',signature=SIGNATURE,signed='yes',check_tyres='fail',note_tyres='Damaged tyre'))
        self.assertEqual(self.stage(),'hold')
        self.simple_prestart();self.assertEqual(self.stage(),'hold')
        self.assertIsNone(self.rows('SELECT test_prestart_at FROM bf_runs')[0]['test_prestart_at'])
        with sqlite3.connect(self.path) as conn:
            conn.execute("UPDATE bf_run_inspections SET resolved_at=?,resolution='Repaired'",(self.time.isoformat(),))
        self.simple_prestart();self.assertEqual(self.stage(),'prestart')
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
        self.post('/binflow/driver/1/test-prestart',dict(date='2026-10-20'))
        self.assertEqual(self.client.get('/binflow/driver/1/day/state?date=2026-10-20').json['stage'],'load')
        self.assertEqual(self.rows('SELECT test_prestart_date FROM bf_runs')[0]['test_prestart_date'],'2026-10-20')

    def test_admin_sample_never_links_to_real_mutations(self):
        self.post('/logout');self.login()
        response=self.post('/binflow/driver/demo',dict(state='NSW'),follow=False)
        token=parse_qs(urlsplit(response.location).query)['demo'][0]
        suffix='?demo='+token
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
            page = self.client.get(desk)
            actions = re.findall(r'<form method="post" action="([^"]+/weigh/\d+)"', page.text)
            gross = (165, 30) if jid == 1 else (115,)
            tares = (65, 5) if jid == 1 else (40,)
            self.assertEqual(len(actions),len(gross))
            for action, weight, tare in zip(actions,gross,tares):
                self.assertTrue(action.startswith(base + '/pickups/'))
                page = self.post(action, dict(gross=weight,tare=tare,confirm_tare='yes'))
                self.assertIn('Weight saved', page.text)
            materials[material] = re.search(r'<option value="(\d+)"[^>]*>'+material+'</option>',page.text)[1]
            fields = re.findall(r'<select name="(material_\d+)"',page.text)
            version = re.search(r'name="version" value="(\d+)"',page.text)[1]
            page = self.post(base+f'/pickups/{jid}/pod/materials',
                             dict(version=version,**{field:materials[material] for field in fields}))
            version = re.search(r'name="version" value="(\d+)"',page.text)[1]
            page = self.post(base+f'/pickups/{jid}/pod/sign',dict(version=version,confirm='yes',signature=SIGNATURE))
            self.assertIn('Signed by <strong>',page.text)
        created = []
        wrong = self.post(base+'/bales/source-scan',dict(warehouse_id=1,material_id=materials['COTTON'],code='DEMO-660-F06'))
        self.assertEqual(wrong.status_code,409)
        for material, codes, weight in [('COTTON',['DEMO-1100-F04','DEMO-240-F05'],125),('PC MIX',['DEMO-660-F06'],75)]:
            form = self.client.get(base+'/bales/new?warehouse=1')
            key = re.search(r'name="request_key" value="([^"]+)"',form.text)[1]
            scans = [self.post(base+'/bales/source-scan',dict(warehouse_id=1,material_id=materials[material],code=c)).json for c in codes]
            self.assertTrue(all(s['ok'] for s in scans))
            response = self.post(base+'/bales/new',dict(warehouse_id=1,material_id=materials[material],
                request_key=key,source_code=codes,source_item=[s['item_id'] for s in scans],weight=weight,confirm_sources='yes'))
            self.assertIn('Print bale label',response.text)
            created.append(re.search(r'BA\d{7}',response.text)[0])
            self.assertEqual(self.client.get(base+f'/bales/{len(created)}/qr.png').mimetype,'image/png')
        self.assertEqual(self.client.get(inbound+suffix).text.count('Emptied into'),3)
        form = self.client.get(base+'/bales/transfers/new')
        key = re.search(r'name="request_key" value="([^"]+)"',form.text)[1]
        page = self.post(base+'/bales/transfers/new',dict(origin_id=1,destination_id=4,carrier='Sample carrier',
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
        with sqlite3.connect(self.path) as conn:
            conn.execute("UPDATE users SET working_state='NSW' WHERE id=?", (self.uid,))
        before={table:self.rows('SELECT * FROM '+table) for table in ('bf_bins','bf_jobs','bf_job_bins','bf_workdays','bf_events','entries','sheets','bf_step_timings','bf_bale_storage','bf_bales','bf_bale_sources','bf_bale_transfers','bf_rental_invoices')}
        opened=self.post('/binflow/driver/demo',dict(state='NSW'),follow=False)
        self.assertEqual(opened.status_code,302)
        token=parse_qs(urlsplit(opened.location).query)['demo'][0]
        suffix='?demo='+token
        self.assertIn(b'SAMPLE RUN',self.client.get(opened.location).data)
        self.assertEqual(self.client.get('/binflow/warehouse'+suffix).status_code,403)
        self.assertEqual(self.client.get('/binflow/driver/my-day?demo=invalid').status_code,403)
        self.assertIn(b'DEMO-1100-E01',self.client.get('/binflow/driver/demo/labels'+suffix).data)
        self.assertEqual(self.client.get('/binflow/driver/demo/qr/1'+suffix).mimetype,'image/png')
        self.post('/binflow/driver/start-day'+suffix,dict(flow_version='2',**self.stamp()))
        picker = self.accept_overview(suffix, 'NSW')
        self.assertIn('SAMPLE · interstate spare · QLD', picker.text)
        self.truck_id = 2
        selected=self.post('/binflow/driver/my-day/1/vehicle'+suffix,dict(state='NSW',truck_id=self.truck_id))
        self.assertIn(b'OK',selected.data)
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
        self.assertIn(b'Driver QR scans completed',self.client.get('/binflow/pickups/1/pod'+suffix).data)
        self.action('client-arrival',suffix,job_id=2)
        self.scan('deliver','DEMO-660-E03',suffix);self.scan('collect','DEMO-660-F06',suffix)
        self.action('depot-arrival',suffix,warehouse_id=1)
        for code in ('DEMO-1100-F04','DEMO-240-F05','DEMO-660-F06'):
            self.assertTrue(self.scan('return',code,suffix).json['ok'])
        self.assertEqual(self.client.get('/binflow/driver/demo/inbound'+suffix).data.count(b'TO BE WEIGHED'),3)
        self.time+=timedelta(hours=8)
        finished=self.post('/binflow/driver/my-day/1/finish'+suffix,self.stamp())
        self.assertIn(b'Sample run complete',finished.data)
        self.complete_sample_warehouse(token)
        for table,data in before.items():self.assertEqual(self.rows('SELECT * FROM '+table),data,table)
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
