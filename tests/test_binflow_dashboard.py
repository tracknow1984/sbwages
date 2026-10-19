import re
import secrets
import sqlite3
import unittest
from html import unescape

import test_binflow_qr as qr
from binflow_helpers import RUN_DATE
from binflow_qr import migrate_qr


class DashboardReviewTests(unittest.TestCase):
    setUp=qr.PublicQrTests.setUp
    tearDown=qr.PublicQrTests.tearDown
    post=qr.PublicQrTests.post
    rows=qr.PublicQrTests.rows
    login=qr.PublicQrTests.login
    job=qr.PublicQrTests.job
    initial_delivery=qr.PublicQrTests.initial_delivery
    exchange=qr.PublicQrTests.exchange
    url=qr.PublicQrTests.url
    public_post=qr.PublicQrTests.public_post
    request_collection=qr.PublicQrTests.request_collection

    def allocate(self,approve=False,**changes):
        values=dict(request_key=secrets.token_urlsafe(24),site_id=3,warehouse_id=1,
                    scheduled_date=RUN_DATE,collect=[1],deliver=[3],collection_request=1)
        if approve: values['approve_schedule']='yes'
        values.update(changes)
        return self.post('/binflow/pickups/new',values)

    def dashboard(self,query=''):
        response=self.client.get('/admin/bin-management'+query)
        self.assertEqual(response.status_code,200)
        return response.data

    def test_dashboard_shows_decisions_and_keeps_pending_approval_in_attention(self):
        self.request_collection()
        page=self.dashboard()
        for label in (b'QR-000001',b'Refer to BLOCKTEXX',b'Approve schedule',b'Dismiss with a reason',b'Private Contact'):
            self.assertIn(label,page)
        self.assertNotIn(b'data-request-id="1"',self.dashboard('?state=NSW'))
        self.assertIn(b'data-request-id="1"',self.dashboard('?state=QLD&requests=attention'))
        self.allocate()
        page=self.dashboard('?requests=attention')
        self.assertIn(b'Awaiting allocation approval',page)
        self.assertIn(b'Review &amp; approve schedule',page)
        self.assertNotIn(b'data-request-id="1"',self.dashboard('?requests=approved'))
        self.post('/binflow/pickups/2/review',dict(decision='approve'))
        self.assertNotIn(b'data-request-id="1"',self.dashboard('?requests=attention'))
        self.assertIn(b'data-request-id="1"',self.dashboard('?requests=approved'))
        self.assertEqual(self.rows("SELECT COUNT(*) AS n FROM bf_collection_request_history WHERE action='Schedule approved'")[0]['n'],1)
        self.assertEqual(self.client.get('/admin/bin-management?requests=bad').status_code,400)

    def test_referral_preserves_stock_and_stays_visible_without_duplicate_requests(self):
        self.request_collection()
        before=self.rows('SELECT * FROM bf_bins');jobs=self.rows('SELECT * FROM bf_jobs')
        page=self.post('/binflow/collection-requests/1/refer',dict(notes='Check next run <script>bad</script>',return_to='dashboard',state='QLD'))
        self.assertIn(b'Awaiting BLOCKTEXX follow-up',page.data)
        self.assertIn(b'&lt;script&gt;bad&lt;/script&gt;',page.data)
        self.assertNotIn(b'<script>bad</script>',page.data)
        self.assertIn(b'data-request-id="1"',self.dashboard('?requests=referred'))
        self.assertNotIn(b'data-request-id="1"',self.dashboard('?requests=attention'))
        history=self.rows('SELECT * FROM bf_collection_request_history')
        self.post('/binflow/collection-requests/1/refer',dict(notes='Repeated click'))
        self.assertEqual(self.rows('SELECT * FROM bf_collection_request_history'),history)
        self.request_collection()
        self.assertEqual(len(self.rows('SELECT * FROM bf_collection_requests')),1)
        self.assertEqual(self.rows('SELECT * FROM bf_bins'),before)
        self.assertEqual(self.rows('SELECT * FROM bf_jobs'),jobs)
        self.assertTrue(history[-1]['actor']);self.assertTrue(history[-1]['created_at'])

    def test_dismiss_requires_reason_preserves_history_and_blocks_stale_schedule(self):
        self.request_collection();before=self.rows('SELECT * FROM bf_bins')
        result=self.post('/binflow/collection-requests/1/close',dict(resolution='  ',return_to='dashboard'))
        self.assertIn(b'Enter a reason',result.data)
        self.assertEqual(self.rows('SELECT status FROM bf_collection_requests')[0]['status'],'new')
        self.post('/binflow/collection-requests/1/close',dict(resolution='Client requested a later date',return_to='dashboard'))
        history=self.rows('SELECT * FROM bf_collection_request_history')
        self.post('/binflow/collection-requests/1/close',dict(resolution='Duplicate click'))
        self.assertEqual(self.rows('SELECT * FROM bf_collection_request_history'),history)
        self.assertIn(b'Client requested a later date',self.dashboard('?requests=closed'))
        self.allocate(approve=True)
        self.assertEqual(len(self.rows('SELECT * FROM bf_jobs')),1)
        self.assertFalse(self.rows('SELECT * FROM bf_job_bins WHERE active=1'))
        self.assertEqual(self.rows('SELECT * FROM bf_bins'),before)
        self.post('/binflow/collection-requests/1/refer',dict(notes='Stale click'))
        self.assertEqual(self.rows('SELECT status FROM bf_collection_requests')[0]['status'],'closed')
        self.request_collection()
        self.assertEqual(len(self.rows('SELECT * FROM bf_collection_requests')),2)

    def test_reviewed_schedule_is_atomic_idempotent_and_reopens_after_cancellation(self):
        self.request_collection()
        self.post('/binflow/collection-requests/1/refer',dict(notes='Office confirmed collection'))
        before=self.rows('SELECT * FROM bf_bins');key=secrets.token_urlsafe(24)
        page=self.client.get('/binflow/pickups/new?collection_request=1').data
        self.assertIn(b'name="approve_schedule" value="yes"',page)
        page=self.allocate(approve=True,request_key=key)
        self.assertIn(b'Collection schedule approved',page.data)
        self.assertEqual(self.rows('SELECT status,job_id FROM bf_collection_requests')[0],dict(status='scheduled',job_id=2))
        self.assertEqual(self.rows('SELECT approval_status FROM bf_jobs WHERE id=2')[0]['approval_status'],'approved')
        self.assertIn(b'href="/binflow/pickups/2"',self.client.get('/binflow/calendar?date='+RUN_DATE).data)
        self.assertEqual(self.rows('SELECT * FROM bf_bins'),before)
        history=self.rows('SELECT * FROM bf_collection_request_history')
        self.allocate(approve=True,request_key=key)
        self.assertEqual(self.rows('SELECT * FROM bf_collection_request_history'),history)
        self.post('/binflow/collection-requests/1/close',dict(resolution='Stale dismiss'))
        self.assertEqual(self.rows('SELECT status FROM bf_collection_requests')[0]['status'],'scheduled')
        self.post('/binflow/pickups/2/cancel',dict(reason='Reschedule'))
        page=self.dashboard('?requests=attention')
        self.assertIn(b'data-request-id="1"',page)
        self.assertIn(b'Referred to BLOCKTEXX',page)  # Past referral remains in action history.
        self.assertEqual(self.rows('SELECT review_state,job_id FROM bf_collection_requests')[0],dict(review_state='pending',job_id=None))

    def test_decisions_are_admin_only_csrf_protected_and_private(self):
        self.request_collection()
        for action,values in (('refer',dict(notes='Review')),('close',dict(resolution='No pickup'))):
            route='/binflow/collection-requests/1/'+action
            self.assertEqual(self.client.post(route,data=values).status_code,400)
            self.assertEqual(self.public_post(route,values).status_code,200)  # Sign-in redirect only.
            self.assertEqual(self.rows('SELECT status,review_state FROM bf_collection_requests')[0],dict(status='new',review_state='pending'))
        from werkzeug.security import generate_password_hash
        with sqlite3.connect(self.path) as conn:
            uid=conn.execute("INSERT INTO users(role,first_name,last_name,email,username,password_hash) VALUES('employee','Dashboard','Driver','dash@example.com','dashdriver',?)",(generate_password_hash('test-admin-password'),)).lastrowid
            conn.execute('INSERT INTO bf_access(user_id) VALUES(?)',(uid,))
        self.post('/logout');self.login('employee','dashdriver')
        page=self.dashboard()
        self.assertIn(b'Prepare allocation',page)
        self.assertNotIn(b'<summary>Refer to BLOCKTEXX</summary>',page)
        self.assertNotIn(b'<summary>Dismiss with a reason</summary>',page)
        self.assertEqual(self.post('/binflow/collection-requests/1/refer',dict(notes='No')).status_code,403)
        self.assertEqual(self.post('/binflow/collection-requests/1/close',dict(resolution='No')).status_code,403)
        self.assertEqual(self.allocate(approve=True).status_code,403)
        self.assertEqual(len(self.rows('SELECT * FROM bf_jobs')),1)
        self.allocate()
        self.assertEqual(self.rows('SELECT approval_status FROM bf_jobs WHERE id=2')[0]['approval_status'],'pending')
        public=self.public.get(self.url()).data
        for value in (b'Private Contact',b'Private gate instructions',b'Refer to BLOCKTEXX',b'Dismiss with a reason'):
            self.assertNotIn(value,public)

    def test_state_stock_breakdown_reconciles_sizes_reserved_and_locations(self):
        sizes={r['litres']:r['id'] for r in self.rows('SELECT * FROM bf_sizes')}
        self.post('/binflow/bins',dict(warehouse_id=1,size_id=sizes[660],quantity=2))
        self.post('/binflow/bins',dict(warehouse_id=2,size_id=sizes[1100],quantity=3))
        self.request_collection();self.allocate()  # One reserved empty, two bins still at client.
        page=self.dashboard().decode()
        def cells(state,size):
            match=re.search(r'<tr data-stock-state="'+state+r'" data-stock-size="'+str(size)+r'">(.*?)</tr>',page)
            self.assertIsNotNone(match)
            return [unescape(re.sub('<[^>]+>','',cell)).strip() for cell in re.findall(r'<td>(.*?)</td>',match.group(1))]
        self.assertEqual(cells('QLD',sizes[240]),['QLD','240 L','6','3','1','2','0','0'])
        self.assertEqual(cells('QLD',sizes[660]),['QLD','660 L','2','2','0','0','0','0'])
        self.assertEqual(cells('NSW',sizes[1100]),['NSW','1100 L','3','3','0','0','0','0'])
        self.assertEqual(cells('WA',sizes[240]),['WA','240 L','0','0','0','0','0','0'])
        filtered=self.dashboard('?state=NSW')
        self.assertNotIn(b'data-stock-state="QLD"',filtered)
        self.assertIn(b'data-stock-state="NSW"',filtered)
        self.assertIn(('state=NSW&amp;size='+str(sizes[1100])).encode(),filtered)

    def test_review_migration_preserves_existing_request_records(self):
        self.request_collection()
        originals=self.rows('SELECT * FROM bf_collection_requests')
        conn=sqlite3.connect(':memory:');conn.row_factory=sqlite3.Row
        # Simulate the pre-review schema, including an existing request and audit record.
        schema=self.rows("SELECT sql FROM sqlite_master WHERE type='table' AND name='bf_collection_requests'")[0]['sql']
        conn.execute(schema)
        conn.execute('ALTER TABLE bf_collection_requests DROP COLUMN review_state')
        conn.execute('ALTER TABLE bf_collection_requests DROP COLUMN review_note')
        for record in originals:
            record={k:v for k,v in record.items() if k not in ('review_state','review_note')}
            conn.execute('INSERT INTO bf_collection_requests('+','.join(record)+') VALUES('+','.join('?' for _ in record)+')',tuple(record.values()))
        before=[dict(r) for r in conn.execute('SELECT * FROM bf_collection_requests')]
        migrate_qr(conn);migrate_qr(conn)
        for row,original in zip(conn.execute('SELECT * FROM bf_collection_requests'),before):
            self.assertEqual({k:row[k] for k in original},original)
            self.assertEqual(row['review_state'],'pending')
        self.assertEqual(conn.execute('SELECT COUNT(*) FROM bf_collection_request_history').fetchone()[0],0)
        conn.close()


if __name__=='__main__': unittest.main()
