"""Driver and office closeout tests using normal HTTP actions and an isolated SQLite database."""
import re
import sqlite3
import unittest

import test_binflow_journey as journey
from binflow_helpers import RUN_DATE, SIGNATURE


class StopExceptionTests(unittest.TestCase):
    fixture=journey.DriverJourneyTests
    setUp=fixture.setUp
    tearDown=fixture.tearDown
    rows=fixture.rows
    post=fixture.post
    login=fixture.login
    job=fixture.job
    initial_delivery=fixture.initial_delivery
    exchange=fixture.exchange
    stamp=fixture.stamp
    choose=fixture.choose
    start=fixture.start
    stage=fixture.stage
    simple_prestart=fixture.simple_prestart
    accept_overview=fixture.accept_overview
    scan=fixture.scan
    action=fixture.action

    def depart(self,codes):
        self.start()
        self.accept_overview()
        self.choose()
        self.simple_prestart()
        for code in codes:
            self.assertTrue(self.scan('load',code).json['ok'])
        self.action('depart')
        self.assertEqual(self.stage(),'navigate')

    def test_all_stops_cancelled_still_requires_both_empty_receipts(self):
        self.depart(['BF000003','BF000004'])
        self.action('cancel-stop',job_id=2,reason='Closed all day')
        self.assertEqual(self.stage(),'navigate_depot')
        with sqlite3.connect(self.path) as conn:
            conn.execute("UPDATE bf_bins SET service_status='damaged' WHERE status='empty' AND truck_id IS NULL")
        driver=self.client
        self.client=self.app.test_client()
        self.login()
        self.post('/binflow/cancelled-jobs/2',dict(decision='rescheduled',date='2026-10-21'))
        repl=self.rows('SELECT replacement_id FROM bf_stop_exceptions WHERE job_id=2')[0]['replacement_id']
        self.assertTrue(repl)
        def reserved():
            return [i['bin_code'] for i in self.rows("SELECT bin_code FROM bf_job_bins WHERE job_id=? AND direction='deliver' AND active=1 ORDER BY bin_code",(repl,))]
        self.assertEqual(reserved(),[])
        self.client=driver
        self.action('depot-arrival',warehouse_id=1)
        self.assertEqual(self.stage(),'return_empty')
        self.assertTrue(self.scan('return_empty','BF000003').json['ok'])
        self.assertIsNone(self.rows('SELECT returned_at FROM bf_runs WHERE truck_id=1')[0]['returned_at'])
        self.assertEqual(reserved(),['BF000003'])
        self.assertTrue(self.scan('return_empty','BF000004').json['ok'])
        self.assertEqual(reserved(),['BF000003','BF000004'])
        self.assertEqual(self.stage(),'done')
        self.assertEqual(self.rows('SELECT id FROM bf_jobs WHERE truck_id=1 AND pod_signed_at IS NOT NULL'),[])

    def test_partial_stop_keeps_delivered_bin_and_returns_only_unused_empty(self):
        self.depart(['BF000003','BF000004'])
        self.action('client-arrival',job_id=2)
        self.assertTrue(self.scan('deliver','BF000003').json['ok'])
        self.action('cancel-stop',job_id=2,reason='Gate closed during exchange')
        self.assertEqual(self.stage(),'navigate_depot')
        self.assertEqual(self.rows('SELECT direction,bin_code FROM bf_job_bins WHERE job_id=2'),
                         [dict(direction='deliver',bin_code='BF000003')])
        self.assertEqual(len(self.rows('SELECT * FROM bf_unused_returns')),1)
        self.action('depot-arrival',warehouse_id=1)
        self.assertEqual(self.stage(),'return_empty')
        self.assertTrue(self.scan('return_empty','BF000004').json['ok'])
        self.assertEqual(self.stage(),'signoff')
        self.post('/logout');self.login()
        exc=self.rows('SELECT job_id FROM bf_stop_exceptions WHERE original_id=2')[0]['job_id']
        self.post(f'/binflow/cancelled-jobs/{exc}',dict(decision='rescheduled',date='2026-10-21'))
        repl=self.rows('SELECT replacement_id FROM bf_stop_exceptions WHERE job_id=?',(exc,))[0]['replacement_id']
        bins=self.rows('SELECT direction,bin_code FROM bf_job_bins WHERE job_id=?',(repl,))
        self.assertEqual(len(bins),3)
        self.assertNotIn('BF000003',[i['bin_code'] for i in bins])

    def test_four_stops_eight_empties_two_exchanges_two_missed(self):
        # Fixture already has the first two empties and two full bins allocated.
        self.post('/logout');self.login()
        empties=['BF000003','BF000004']
        stops=[(2,empties[:],['BF000001','BF000002'])]
        for seq in (2,3,4):
            pairs={'deliver':[],'collect':[]}
            with sqlite3.connect(self.path) as conn:
                for direction in pairs:
                    for n in (1,2):
                        code=f'RUN-{seq}-{direction}-{n}'
                        bid=conn.execute("""INSERT INTO bf_bins(code,token,size_id,state,location_id,status,tare_g,created_at)
                            VALUES(?,?,1,'QLD',?,?,5000,?)""",(code,'QR-'+code,1 if direction=='deliver' else 3,
                            'empty' if direction=='deliver' else 'full',RUN_DATE)).lastrowid
                        pairs[direction].append((bid,code))
            self.job(deliver=[b[0] for b in pairs['deliver']],collect=[b[0] for b in pairs['collect']],legacy=False)
            jid=self.rows('SELECT MAX(id) AS id FROM bf_jobs')[0]['id']
            self.post(f'/binflow/pickups/{jid}/truck',dict(truck_id=1))
            empty=[b[1] for b in pairs['deliver']]
            stops.append((jid,empty,[b[1] for b in pairs['collect']]))
            empties.extend(empty)
        self.post('/logout');self.login('employee','phonedriver')
        self.depart(empties)
        for jid,drops,pickups in stops[:2]:
            self.action('client-arrival',job_id=jid)
            for code in drops:self.assertTrue(self.scan('deliver',code).json['ok'])
            for code in pickups:self.assertTrue(self.scan('collect',code).json['ok'])
        for jid,_,_ in stops[2:]:
            self.action('cancel-stop',job_id=jid,reason=' ')
            self.assertEqual(self.stage(),'navigate')
            self.action('cancel-stop',job_id=jid,reason='Site closed')
        self.assertEqual(self.stage(),'navigate_depot')
        self.assertEqual(len(self.rows("SELECT id FROM bf_bins WHERE truck_load='empty'")),4)
        self.assertEqual(len(self.rows("SELECT id FROM bf_bins WHERE truck_load='full'")),4)
        self.action('depot-arrival',warehouse_id=1)
        for _,_,pickups in stops[:2]:
            for code in pickups:self.assertTrue(self.scan('return',code).json['ok'])
        self.assertEqual(self.stage(),'return_empty')
        unused=[code for _,drops,_ in stops[2:] for code in drops]
        for code in unused[:3]:
            self.assertTrue(self.scan('return_empty',code).json['ok'])
            self.assertTrue(self.scan('return_empty',code).json['duplicate'])
        self.assertIsNone(self.rows('SELECT returned_at FROM bf_runs WHERE truck_id=1')[0]['returned_at'])
        self.post('/logout');self.login()
        self.assertIn(unused[-1],self.client.get('/binflow/warehouse?state=QLD').text)
        self.post('/binflow/warehouse/unused-empties',dict(code=unused[-1]))
        self.assertEqual(self.rows('SELECT id FROM bf_bins WHERE truck_id=1'),[])
        self.assertEqual(len(self.rows("SELECT id FROM bf_bins WHERE status='received'")),4)
        self.assertEqual(len(self.rows('SELECT item_id FROM bf_unused_returns WHERE returned_at IS NOT NULL')),4)
        self.assertTrue(self.rows('SELECT returned_at FROM bf_runs WHERE truck_id=1')[0]['returned_at'])
        self.assertIn('QLD · Day locked',self.client.get('/binflow/collection-calendar?state=QLD&date='+RUN_DATE).text)
        before=len(self.rows('SELECT id FROM bf_jobs'))
        locked=self.post('/binflow/pickups/new',dict(request_key='locked-day-test-request-001',site_id=3,warehouse_id=1,
            scheduled_date=RUN_DATE,deliver=5))
        self.assertIn('completed and locked',locked.text)
        self.assertEqual(len(self.rows('SELECT id FROM bf_jobs')),before)
        cancelled,rebooked=[s[0] for s in stops[2:]]
        self.post(f'/binflow/cancelled-jobs/{cancelled}',dict(decision='approved'))
        self.post(f'/binflow/cancelled-jobs/{rebooked}',dict(decision='rescheduled',date='2026-10-21'))
        self.post(f'/binflow/cancelled-jobs/{rebooked}',dict(decision='rescheduled',date='2026-10-22'))
        self.assertEqual(len(self.rows('SELECT id FROM bf_jobs')),before+1)
        for i in self.rows("SELECT * FROM bf_job_bins WHERE direction='collect' AND active=1 AND job_id IN (?,?)",
                           tuple(s[0] for s in stops[:2])):
            self.post(f"/binflow/pickups/{i['job_id']}/weigh/{i['id']}",dict(gross=80,tare=5,confirm_tare='yes'))
        first=stops[0][0]
        pod=self.client.get(f'/binflow/pickups/{first}')
        field=lambda name:re.search('name="'+name+'" value="([^"]+)"',pod.text)[1]
        self.post(f'/binflow/pickups/{first}/pod/sign',dict(scope='run',version=field('version'),run_version=field('run_version'),confirm='yes',signature=SIGNATURE))
        self.assertEqual(len(self.rows('SELECT id FROM bf_jobs WHERE pod_signed_at IS NOT NULL AND truck_id=1')),2)
        page=self.client.get('/binflow/pods?state=QLD&view=signed&date='+RUN_DATE)
        self.assertIn('Completed PODs',page.text)
        for jid,_,_ in stops[2:]:self.assertNotIn(f'BF-{jid:06d}',page.text)
