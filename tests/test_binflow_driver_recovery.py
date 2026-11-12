import io
import json
import secrets
import sqlite3
from datetime import timedelta
from urllib.parse import parse_qs, urlsplit
from unittest import TestCase

from PIL import Image
from binflow_helpers import SIGNATURE, RUN_DATE
from binflow_lunch import send_due
from prestart_config import TRUCK_CHECKS
import test_binflow_journey as fixture


class DriverRecoveryTests(TestCase):
    setUp = fixture.DriverJourneyTests.setUp
    tearDown = fixture.DriverJourneyTests.tearDown
    post = fixture.DriverJourneyTests.post
    login = fixture.DriverJourneyTests.login
    rows = fixture.DriverJourneyTests.rows
    job = fixture.DriverJourneyTests.job
    exchange = fixture.DriverJourneyTests.exchange
    initial_delivery = fixture.DriverJourneyTests.initial_delivery
    stamp = fixture.DriverJourneyTests.stamp
    start = fixture.DriverJourneyTests.start
    accept_overview = fixture.DriverJourneyTests.accept_overview
    choose = fixture.DriverJourneyTests.choose
    simple_prestart = fixture.DriverJourneyTests.simple_prestart
    scan = fixture.DriverJourneyTests.scan

    def test_expired_scan_and_login_restore_same_sample_and_warehouse(self):
        token=parse_qs(urlsplit(self.post('/binflow/driver/demo',follow=False).location).query)['demo'][0]
        suffix='?demo='+token
        self.post('/binflow/driver/start-day'+suffix,dict(flow_version='2',**self.stamp()))
        self.accept_overview(suffix,'NSW')
        self.post('/binflow/driver/my-day/1/vehicle'+suffix,dict(state='NSW',truck_id=1))
        self.simple_prestart(suffix)
        self.assertTrue(self.scan('load','DEMO-1100-E01',suffix).json['ok'])
        self.client=self.app.test_client()
        expired=self.client.post('/binflow/driver/1/day/scan-bin'+suffix,headers={'Accept':'application/json'})
        self.assertEqual(expired.status_code,401)
        self.assertIn('resume',expired.json['login_url'])
        landing = self.post(expired.json['login_url'], {'username':'phonedriver', 'password':'test-admin-password'})
        self.assertEqual(landing.request.path, '/employee/dashboard')
        self.assertIn('href="/binflow/driver/resume"', landing.text)
        again = self.client.get('/binflow/driver/resume', follow_redirects=True)
        self.assertIn('data-demo-code="DEMO-240-E02"',again.text)
        self.assertNotIn('data-demo-code="DEMO-1100-E01"',again.text)
        docket='/binflow/driver/demo/inbound/1'
        self.assertEqual(self.client.get(docket+suffix).status_code,200)
        self.post('/logout')
        self.assertEqual(self.login('employee','phonedriver').request.path,'/employee/dashboard')
        self.assertEqual(self.client.get('/binflow/driver/resume',follow_redirects=True).request.path,docket)
        self.post('/logout'); self.login()
        self.assertEqual(self.client.get(docket+suffix).status_code,403)

    def test_vehicle_evidence_and_live_resume(self):
        self.start();self.accept_overview();self.choose()
        self.post('/logout')
        landing = self.login('employee','phonedriver')
        self.assertEqual(landing.request.path, '/employee/dashboard')
        page = self.client.get('/binflow/driver/resume', follow_redirects=True)
        self.assertIn('data-truck-inspection',page.text)
        values=dict(date=RUN_DATE,reading='45000',fit_for_duty='yes',signed='yes',signature=SIGNATURE,
                    submission_token=secrets.token_urlsafe(24))
        values.update({'check_'+k:'pass' for k in TRUCK_CHECKS})
        route='/binflow/driver/1/prestart'
        for key in ('body','tyres','windscreen'):
            resp=self.post(route,dict(values,**{'check_'+key:'fail'}))
            self.assertIn('Add a photo for the '+key,resp.text)
        self.assertIn('Describe the tailgate',self.post(route,dict(values,check_lift='fail')).text)
        self.assertFalse(self.rows('SELECT * FROM bf_run_inspections'))
        for key in ('body','tyres','windscreen'):
            data=io.BytesIO(); Image.new('RGB',(20,20)).save(data,'JPEG');data.seek(0)
            values['check_'+key]='fail';values['photo_'+key]=(data,key+'.jpg')
        values.update(check_lift='fail',note_lift='Platform will not rise.')
        self.post(route,values)
        record=self.rows('SELECT * FROM prestarts')[0]
        self.assertEqual(record['failure_count'],4)
        self.assertEqual({r['check_key'] for r in self.rows('SELECT check_key FROM prestart_photos')},{'body','tyres','windscreen'})
        self.assertEqual(json.loads(record['checks_json'])[0]['answer'],'Yes')
        self.assertEqual(self.rows('SELECT passed FROM bf_run_inspections')[0]['passed'],0)

    def test_phone_reminder_threshold_retry_and_lunch(self):
        self.start()
        api='/binflow/driver/lunch-alerts'
        self.assertTrue(self.client.get(api).json['key'])
        self.assertEqual(self.post(api,dict(endpoint='https://localhost/push')).status_code,400)
        self.assertTrue(self.post(api,dict(endpoint='https://fcm.googleapis.com/example')).json['ok'])
        sent=[]
        def send(endpoint,key,contact):
            self.assertIn('Authorization',key.sign({'aud':'https://fcm.googleapis.com','sub':contact}))
            sent.append(endpoint)
            return 503 if len(sent)==1 else 201
        due=int((self.time+timedelta(hours=4)).timestamp())
        with sqlite3.connect(self.path) as conn:
            for tick in (due-1,due,due+15,due+91,due+300):
                send_due(conn,'mailto:driver@example.invalid',tick,send)
                if tick==due-1:self.assertFalse(sent)
            self.assertEqual(len(sent),2)
            conn.execute('UPDATE bf_lunch_phones SET sent_day=NULL')
            conn.execute('UPDATE bf_workdays SET break_started_at=started_at')
            conn.commit()
            send_due(conn,'mailto:driver@example.invalid',due+600,send)
            self.assertEqual(len(sent),2)
