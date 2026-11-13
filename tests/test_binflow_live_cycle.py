"""An employee and an independent administrator observe one production-style run."""
import re
import unittest

from werkzeug.security import generate_password_hash
from binflow_helpers import RUN_DATE, SIGNATURE
from binflow_runs import CHECKS
import test_binflow as legacy


class LiveCycleTests(unittest.TestCase):
    setUp=legacy.BinFlowTests.setUp
    tearDown=legacy.BinFlowTests.tearDown
    rows=legacy.BinFlowTests.rows
    login=legacy.BinFlowTests.login
    post=legacy.BinFlowTests.post

    def test_employee_actions_reach_the_normal_office_registers(self):
        import sqlite3
        with sqlite3.connect(self.path) as conn:
            conn.execute("""INSERT INTO users(role,first_name,email,username,password_hash)
                VALUES('employee','Existing','existing@example.invalid','testdriver',?)""",(generate_password_hash('existing-login'),))
        admin=self.client
        self.app.config['BINFLOW_TESTDRIVER_HASH']=generate_password_hash('field-test-password')
        self.client=self.app.test_client()
        credentials=dict(username='testdriver',password='field-test-password')
        page=self.post('/employee/login',credentials)
        self.assertEqual(page.request.path,'/employee/dashboard')
        self.assertIn('START BIN FLOW',page.text)
        run=self.rows('SELECT * FROM bf_live_cycle')[0]
        def field(html,name):return re.search('name="'+name+'" value="([^"]+)"',html)[1]
        def check(text,page):self.assertIn(text,page.text[-180000:])
        jobs=self.rows('SELECT * FROM bf_jobs WHERE pod_client_id=? ORDER BY run_order',(run['client_id'],))
        j1,j2=[j['id'] for j in jobs]
        check(jobs[0]['code'],admin.get('/binflow/pods?date='+RUN_DATE))
        self.assertEqual(len(jobs),2)
        check('LIVE-TEST-POD-02',admin.get('/admin/binflow/live-test'))
        self.assertEqual(self.client.get('/admin/binflow/live-test').status_code,403)
        truck=run['truck_id']
        self.assertEqual(self.client.get('/binflow/invoices').status_code,403)
        check('LT-1100-EMPTY',self.client.get('/binflow/driver/live-test/labels'))
        self.assertEqual(self.client.get('/binflow/driver/live-test/labels/1.png').status_code,404)
        self.post('/logout')
        self.post('/employee/login',credentials)
        self.assertEqual(len(self.rows('SELECT * FROM bf_live_cycle')),1)
        day=self.post('/binflow/driver/start-day',dict(flow_version='2'))
        wid=re.search(r'/my-day/(\d+)/overview',day.text)[1]
        day=self.post(f'/binflow/driver/my-day/{wid}/overview',dict(state='NSW',version=field(day.text,'version')))
        day=self.post(f'/binflow/driver/my-day/{wid}/vehicle',dict(state='NSW',truck_id=truck))
        date=field(day.text,'date')
        runpath=f'/binflow/driver/{truck}/day'
        self.post(f'/binflow/driver/{truck}/prestart',{**{'check_'+k:'pass' for k,_,_ in CHECKS},'date':date,
            'reading':'12000','submission_token':field(day.text,'submission_token'),'fit_for_duty':'yes','signature':SIGNATURE,'signed':'yes'})
        def scan(action,code,mid=None):
            p=self.post(runpath+'/scan-bin',dict(date=date,action=action,code=code,material_id=mid or ''))
            self.assertEqual(p.status_code,200,p.text[:300]);self.assertTrue(p.json['ok'])
        def action(a,**fields):return self.post(runpath+'/'+a,dict(date=date,**fields))
        mats={r['name']:r['id'] for r in self.rows('SELECT * FROM bf_material_classes')}
        stops=[(j1,[1100,240],mats['COTTON']),(j2,[660],mats['PC MIX'])]
        for _,sizes,_ in stops:
            for size in sizes:scan('load',f'LT-{size}-EMPTY')
        check('Loaded onto truck',admin.get('/admin/binflow/live-test'))
        action('depart')
        for jid,sizes,mid in stops:
            action('client-arrival',job_id=jid)
            for size in sizes:scan('deliver',f'LT-{size}-EMPTY')
            for size in sizes:scan('collect',f'LT-{size}-FULL',mid)
        action('depot-arrival',warehouse_id=run['origin_id'])
        for _,sizes,_ in stops:
            for size in sizes:scan('return',f'LT-{size}-FULL')
        for reg in ['/binflow/warehouse','/binflow/pods?date='+date]:
            check('LT-1100-FULL',admin.get(reg))
        self.post('/binflow/workspace',dict(workspace='warehouse'))
        for jid,sizes,mid in stops:
            for size in sizes:
                code=f'LT-{size}-FULL'
                found=self.post('/binflow/warehouse/scan',dict(code=code)).json
                self.assertTrue(found['ok'])
                row=self.rows('SELECT id,tare_g FROM bf_job_bins WHERE job_id=? AND bin_code=?',(jid,code))[0]
                tare=row['tare_g']/1000
                self.post(f'/binflow/pickups/{jid}/weigh/{row["id"]}',dict(tare=tare,gross=tare+{1100:100,240:25,660:75}[size],confirm_tare='yes'))
        pod=self.client.get(f'/binflow/pickups/{j2}')
        self.post(f'/binflow/pickups/{j2}/pod/sign',dict(scope='run',version=field(pod.text,'version'),run_version=field(pod.text,'run_version'),confirm='yes',signature=SIGNATURE))
        for jid,val in ((j1,'$220.00'),(j2,'$132.00')):
            check(val,admin.get(f'/binflow/pickups/{jid}/pod'))
            check('$352.00',self.client.get(f'/binflow/pickups/{jid}/pod'))
        draft=self.rows("SELECT * FROM bf_auto_drafts WHERE group_key LIKE 'pods:live-test:%'")[0]
        check('$352.00',admin.get('/binflow/invoices/review/'+draft['id']))
        self.assertTrue(draft['issue'])
        from binflow_management import permitted_pod
        from binflow_rental import report
        with sqlite3.connect(self.path) as conn:
            conn.row_factory=sqlite3.Row
            self.assertFalse(permitted_pod(conn,{'id':run['payer_id']},j1,{'pods.view','pods.all'}))
            self.assertNotIn('LIVE TEST',str(report(conn)['clients']))
        check('Bins ready for baling · 3',admin.get(f'/binflow/bales?warehouse={run["origin_id"]}'))
        codes=[]
        for jid,sizes,mid in stops:
            f=self.client.get('/binflow/bales/new?warehouse='+str(run['origin_id']))
            key=field(f.text,'request_key')
            bins=[f'LT-{size}-FULL' for size in sizes]
            fields=dict(warehouse_id=run['origin_id'],material_id=mid,request_key=key)
            items=[self.post('/binflow/bales/source-scan',dict(fields,code=b)).json['item_id'] for b in bins]
            f=self.post('/binflow/bales/new',dict(fields,source_code=bins,source_item=items,weight=125 if jid==j1 else 75,confirm_sources='yes'))
            bale=re.search(r'BA\d{7}',f.text)[0];codes.append(bale)
            check(bale,admin.get('/binflow/bales'))
            check('Print label',self.client.get(f.request.path+'/label'))
        f=self.client.get('/binflow/bales/transfers/new')
        fields=dict(origin_id=run['origin_id'],destination_id=run['destination_id'],carrier_supplier_id=run['carrier_id'],vehicle_type='SINGLE',request_key=field(f.text,'request_key'))
        self.assertEqual(self.post('/binflow/bales/transfers/new',dict(fields,destination_id=1)).status_code,403)
        f=self.post('/binflow/bales/transfers/new',fields);path=f.request.path
        for code in codes: resp=self.post(path+'/scan',dict(action='load',code=code)).json
        check('200 kg',admin.get('/admin/binflow/live-test'))
        manifest=admin.get(path)
        for value in ['200','Print manifest','LIVE TEST · Brisbane',*codes]:check(value,manifest)
        self.post(path+'/action',dict(action='depart',confirm='yes',version=resp['version']))
        for code in codes:self.assertTrue(self.post(path+'/scan',dict(action='receive',code=code)).json['ok'])
        for code in codes:check(code,admin.get(f'/binflow/bales?warehouse={run["destination_id"]}'))
        self.post('/binflow/workspace',dict(workspace='driver'))
        self.post(f'/binflow/driver/my-day/{wid}/finish')
        self.assertTrue(self.rows('SELECT finished_at FROM bf_workdays WHERE user_id=?',(run['driver_id'],))[0]['finished_at'])
        self.assertFalse(self.rows('SELECT * FROM sheets WHERE user_id=?',(run['driver_id'],)))
