import secrets
import sqlite3
import unittest
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import test_binflow as legacy
from binflow_approvals import migrate_approvals
from binflow_helpers import prestart


class AllocationApprovalTests(unittest.TestCase):
    setUp=legacy.BinFlowTests.setUp
    tearDown=legacy.BinFlowTests.tearDown
    post=legacy.BinFlowTests.post
    rows=legacy.BinFlowTests.rows
    login=legacy.BinFlowTests.login
    job=legacy.BinFlowTests.job
    exchange=legacy.BinFlowTests.exchange
    initial_delivery=legacy.BinFlowTests.initial_delivery

    def allocate(self, **changes):
        values=dict(request_key=secrets.token_urlsafe(24),site_id=3,warehouse_id=1,
            scheduled_date='2026-10-19',deliver=[1,2],notes='Approval test allocation')
        values.update(changes)
        return self.post('/binflow/pickups/new',values)

    def review(self,jid=1,decision='approve',notes=''):
        return self.post(f'/binflow/pickups/{jid}/review',dict(decision=decision,notes=notes))

    def test_pending_reserves_stock_but_cannot_enter_driver_flow(self):
        before=self.rows('SELECT * FROM bf_bins')
        key=secrets.token_urlsafe(24)
        page=self.allocate(request_key=key,approval_status='approved')
        self.assertIn(b'Awaiting approval',page.data)
        self.assertIn(b'Approve allocation',page.data)
        self.assertNotIn(b'Use truck &amp; scan',page.data)
        self.assertEqual(self.rows('SELECT approval_status FROM bf_jobs')[0]['approval_status'],'pending')
        self.assertEqual(self.rows('SELECT * FROM bf_bins'),before)
        self.assertEqual([r['active'] for r in self.rows('SELECT active FROM bf_job_bins')],[1,1])
        self.allocate(request_key=key)
        self.assertEqual(len(self.rows('SELECT * FROM bf_jobs')),1)
        self.assertEqual(len(self.rows('SELECT * FROM bf_allocation_history')),1)
        self.assertIn(b'already allocated',self.allocate(deliver=[1]).data)
        self.assertEqual(len(self.rows('SELECT * FROM bf_jobs')),1)
        queue=self.client.get('/binflow/allocation-approvals').data
        self.assertIn(b'BF-000001',queue);self.assertIn(b'2',queue)
        for route in ('/binflow/driver','/binflow/calendar?date=2026-10-19','/binflow/pods?date=2026-10-19'):
            self.assertNotIn(b'href="/binflow/pickups/1"',self.client.get(route).data)
        self.post('/binflow/driver',dict(name='Review truck',state='QLD'))
        self.assertIn(b'must be approved',self.post('/binflow/pickups/1/truck',dict(truck_id=1)).data)
        self.assertIsNone(self.rows('SELECT truck_id FROM bf_jobs')[0]['truck_id'])
        for action in ('exchange','receive','cancel'):
            result=self.post('/binflow/pickups/1/'+action,dict(confirmed=[1,2],reason='Bypass'))
            self.assertIn(b'must be approved',result.data)
        self.assertIn(b'must be approved',self.post('/binflow/driver/1/scan',dict(job_id=1,action='load',code='BF000001')).data)
        self.assertEqual(self.rows('SELECT * FROM bf_bins'),before)

    def test_approve_records_review_and_enables_existing_scan_checks(self):
        self.allocate()
        self.post('/binflow/driver',dict(name='Approved truck',state='QLD'))
        self.assertIn(b'Allocation approved.',self.review(notes='Ready for this run').data)
        first=self.rows('SELECT approval_status,reviewed_at,reviewed_by,review_note FROM bf_jobs')[0]
        self.assertEqual(first['approval_status'],'approved');self.assertTrue(first['reviewed_at']);self.assertTrue(first['reviewed_by'])
        self.assertEqual(first['review_note'],'Ready for this run')
        history=self.rows('SELECT * FROM bf_allocation_history');events=self.rows('SELECT * FROM bf_events')
        self.review(notes='Duplicate click')
        self.assertEqual(self.rows('SELECT * FROM bf_allocation_history'),history)
        self.assertEqual(self.rows('SELECT * FROM bf_events'),events)
        self.assertEqual(self.rows('SELECT approval_status,reviewed_at,reviewed_by,review_note FROM bf_jobs')[0],first)
        self.assertIn(b'already been reviewed',self.review(decision='reject',notes='Cannot undo approval').data)
        self.assertIn(b'href="/binflow/pickups/1"',self.client.get('/binflow/calendar?date=2026-10-19').data)
        self.post('/binflow/pickups/1/truck',dict(truck_id=1))
        self.assertEqual(self.rows('SELECT truck_id FROM bf_jobs')[0]['truck_id'],1)
        result=self.post('/binflow/driver/1/scan',dict(job_id=1,action='load',code='BF000001'))
        self.assertIn(b'prestart',result.data)
        prestart(self)
        self.post('/binflow/driver/1/scan',dict(job_id=1,action='load',code='BF000001'))
        self.assertEqual(self.rows('SELECT truck_id FROM bf_bins WHERE id=1')[0]['truck_id'],1)

    def test_reject_requires_reason_releases_stock_and_keeps_original_record(self):
        self.allocate();before=self.rows('SELECT * FROM bf_bins')
        self.assertIn(b'Enter a reason',self.review(decision='reject').data)
        self.assertEqual(self.rows('SELECT approval_status FROM bf_jobs')[0]['approval_status'],'pending')
        result=self.review(decision='reject',notes='Wrong quantities <script>test</script>')
        self.assertIn(b'Wrong quantities &lt;script&gt;test&lt;/script&gt;',result.data)
        original=self.rows('SELECT * FROM bf_jobs')[0]
        self.assertEqual((original['approval_status'],original['status']),('rejected','cancelled'))
        self.assertEqual({r['active'] for r in self.rows('SELECT active FROM bf_job_bins')},{0})
        self.assertEqual(self.rows('SELECT * FROM bf_bins'),before)
        history=self.rows('SELECT * FROM bf_allocation_history')
        self.review(decision='reject',notes='Repeated decision')
        self.review(decision='approve')
        self.assertEqual(self.rows('SELECT * FROM bf_jobs')[0],original)
        self.assertEqual(self.rows('SELECT * FROM bf_allocation_history'),history)
        self.assertNotIn(b'BF-000001',self.client.get('/binflow/allocation-approvals').data)
        self.assertIn(b'BF-000001',self.client.get('/binflow/allocation-approvals?status=rejected').data)
        self.allocate()
        self.assertEqual(len(self.rows('SELECT * FROM bf_jobs')),2)
        self.assertEqual(self.rows('SELECT approval_status FROM bf_jobs WHERE id=2')[0]['approval_status'],'pending')

    def test_only_office_can_submit_and_decide_allocations(self):
        from werkzeug.security import generate_password_hash
        with sqlite3.connect(self.path) as conn:
            uid=conn.execute("INSERT INTO users(role,first_name,last_name,email,username,password_hash) VALUES('employee','Allocation','Operator','allocation@example.com','allocator',?)",(generate_password_hash('test-admin-password'),)).lastrowid
            conn.execute("UPDATE users SET business_role='driver',working_state='QLD' WHERE id=?",(uid,))
        self.post('/logout');self.login('employee','allocator')
        result=self.allocate()
        self.assertEqual(result.status_code,403)
        self.assertEqual(self.review().status_code,403)
        self.assertEqual(self.review(decision='reject',notes='Not permitted').status_code,403)
        self.assertEqual(self.rows('SELECT * FROM bf_jobs'),[])
        self.post('/logout')
        self.assertEqual(self.client.get('/binflow/allocation-approvals').status_code,302)
        self.login()
        self.allocate()
        self.assertEqual(self.client.post('/binflow/pickups/1/review',data=dict(decision='approve')).status_code,400)
        self.review()
        self.assertEqual(self.rows('SELECT approval_status FROM bf_jobs')[0]['approval_status'],'approved')

    def test_rejected_collection_allocation_returns_public_request_to_queue(self):
        self.initial_delivery()
        token=self.rows('SELECT token FROM bf_bins WHERE id=1')[0]['token']
        public=self.app.test_client();route='/binflow/qr/'+token+'/collection'
        public.get(route)
        with public.session_transaction() as session:csrf=session['csrf']
        public.post(route,data=dict(csrf=csrf,request_key=secrets.token_urlsafe(24),requester='Client',contact='client@example.com'))
        self.allocate(collect=[1],deliver=[3],collection_request=1)
        page=self.client.get('/binflow/collection-requests?status=scheduled').data
        self.assertIn(b'Awaiting allocation approval',page)
        self.review(jid=2,decision='reject',notes='Use the next run')
        self.assertEqual(self.rows('SELECT status,job_id FROM bf_collection_requests')[0],dict(status='new',job_id=None))
        self.assertEqual(self.rows('SELECT location_id FROM bf_bins WHERE id=1')[0]['location_id'],3)
        self.allocate(collect=[1],deliver=[3],collection_request=1)
        self.review(jid=3)
        self.assertEqual(self.rows('SELECT job_id FROM bf_collection_requests')[0]['job_id'],3)

    def test_simultaneous_decisions_record_only_one_outcome(self):
        self.allocate()
        clients=[self.app.test_client(),self.app.test_client()]
        for client in clients:
            with self.client.session_transaction() as source: values=dict(source)
            with client.session_transaction() as session:session.update(values)
        gate=Barrier(2)
        def decide(index):
            gate.wait()
            return clients[index].post('/binflow/pickups/1/review',data=dict(csrf=values['csrf'],decision=('approve','reject')[index],notes='Concurrent review')).status_code
        with ThreadPoolExecutor(max_workers=2) as pool:results=list(pool.map(decide,(0,1)))
        self.assertEqual(results,[302,302])
        state=self.rows('SELECT approval_status,status FROM bf_jobs')[0]
        self.assertIn(state['approval_status'],('approved','rejected'))
        self.assertEqual(len(self.rows('SELECT * FROM bf_allocation_history')),2)
        expected=1 if state['approval_status']=='approved' else 0
        self.assertEqual({r['active'] for r in self.rows('SELECT active FROM bf_job_bins')},{expected})

    def test_existing_dockets_migrate_as_approved_without_rewriting_history(self):
        from binflow import SCHEMA
        self.initial_delivery()
        conn=sqlite3.connect(':memory:');conn.row_factory=sqlite3.Row;conn.executescript(SCHEMA)
        originals={}
        for table in ('bf_clients','bf_locations','bf_sizes','bf_bins','bf_jobs','bf_job_bins','bf_events'):
            columns=','.join(row['name'] for row in conn.execute(f'PRAGMA table_info({table})'))
            records=self.rows(f'SELECT {columns} FROM {table} ORDER BY id');originals[table]=(columns,records)
            for record in records:conn.execute(f'INSERT INTO {table}({columns}) VALUES({",".join("?" for _ in record)})',tuple(record.values()))
        migrate_approvals(conn);migrate_approvals(conn)
        for table,(columns,records) in originals.items():
            self.assertEqual([dict(r) for r in conn.execute(f'SELECT {columns} FROM {table} ORDER BY id')],records)
        self.assertEqual(conn.execute('SELECT approval_status FROM bf_jobs').fetchone()[0],'approved')
        self.assertEqual(conn.execute('SELECT COUNT(*) FROM bf_allocation_history').fetchone()[0],0)
        conn.close()
