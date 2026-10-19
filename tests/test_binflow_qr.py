import secrets
import sqlite3
import unittest

import test_binflow as legacy
from binflow_helpers import prestart,depart,RUN_DATE


class PublicQrTests(unittest.TestCase):
    tearDown=legacy.BinFlowTests.tearDown
    post=legacy.BinFlowTests.post
    rows=legacy.BinFlowTests.rows
    login=legacy.BinFlowTests.login
    job=legacy.BinFlowTests.job
    initial_delivery=legacy.BinFlowTests.initial_delivery
    exchange=legacy.BinFlowTests.exchange

    def setUp(self):
        legacy.BinFlowTests.setUp(self)
        self.initial_delivery()
        self.tokens={r['id']:r['token'] for r in self.rows('SELECT id,token FROM bf_bins')}
        self.public=self.app.test_client()
        self.public.get(self.url(suffix='/collection'))

    def url(self,bid=1,suffix=''):
        return '/binflow/qr/'+self.tokens[bid]+suffix

    def public_post(self,path,values,csrf=True):
        if csrf:
            with self.public.session_transaction() as session: token=session['csrf']
            values={'csrf':token,**values}
        return self.public.post(path,data=values,follow_redirects=True)

    def request_collection(self,**overrides):
        values=dict(request_key=secrets.token_urlsafe(24),requester='Private Contact',contact='private-contact@example.com',
                    preferred_date=RUN_DATE,notes='Private gate instructions')
        values.update(overrides)
        return self.public_post(self.url(suffix='/collection'),values)

    def schedule(self,rid=1):
        key=secrets.token_urlsafe(24)
        result=self.post('/binflow/pickups/new',dict(request_key=key,site_id=3,warehouse_id=1,
            scheduled_date=RUN_DATE,collect=[1],deliver=[3],collection_request=rid))
        saved=self.rows('SELECT id FROM bf_jobs WHERE request_key=?',(key,))
        if saved:self.post(f'/binflow/pickups/{saved[0]["id"]}/review',{'decision':'approve'})
        return result

    def test_public_three_actions_request_privacy_csrf_and_duplicate_safety(self):
        before=self.rows('SELECT * FROM bf_bins');jobs=self.rows('SELECT * FROM bf_jobs')
        page=self.public.get(self.url())
        self.assertEqual(page.status_code,200)
        for label in (b'NEW COLLECTION',b'DRIVER PICKUP',b'DRIVER DELIVERY'): self.assertIn(label,page.data)
        for private in (b'Client Alpha',b'QLD warehouse',b'Cancel',b'Add bin',b'Movement &amp; stock history'): self.assertNotIn(private,page.data)
        bad=self.public_post(self.url(suffix='/collection'),{'requester':'Private'},csrf=False)
        self.assertEqual(bad.status_code,400)
        self.assertFalse(self.rows('SELECT * FROM bf_collection_requests'))
        key=secrets.token_urlsafe(24)
        self.assertIn(b'QR-000001 received',self.request_collection(request_key=key).data)
        self.request_collection(request_key=key);self.request_collection()
        self.assertEqual(len(self.rows('SELECT * FROM bf_collection_requests')),1)
        self.assertEqual(self.rows('SELECT * FROM bf_bins'),before)
        self.assertEqual(self.rows('SELECT * FROM bf_jobs'),jobs)
        page=self.public.get(self.url()).data
        for private in (b'Private Contact',b'private-contact@example.com',b'Private gate instructions'): self.assertNotIn(private,page)
        self.assertEqual(self.public.get('/binflow/collection-requests').status_code,302)
        office=self.client.get('/binflow/collection-requests')
        self.assertIn(b'private-contact@example.com',office.data)
        self.assertIn(b'Private gate instructions',office.data)
        self.assertEqual(self.public.get('/binflow/qr/not-a-valid-token').status_code,404)

    def test_request_scheduling_and_qr_driver_actions_preserve_gates_and_timestamps(self):
        self.request_collection()
        page=self.client.get('/binflow/pickups/new?collection_request=1').data
        self.assertIn(b'name="collection_request" value="1"',page)
        self.schedule()
        self.assertEqual(self.rows('SELECT status,job_id FROM bf_collection_requests')[0],dict(status='scheduled',job_id=2))
        self.post('/binflow/driver',dict(name='QR truck',state='QLD'))
        self.post('/binflow/pickups/2/truck',dict(truck_id=1))
        delivery=self.url(3,'/driver/delivery');pickup=self.url(1,'/driver/pickup')
        result=self.post(delivery,dict(job_id=2))
        self.assertIn('prestart',result.data.decode().lower())
        self.assertIsNone(self.rows('SELECT delivered_at FROM bf_job_bins WHERE job_id=2 AND bin_id=3')[0]['delivered_at'])
        prestart(self)
        self.post('/binflow/driver/1/scan',dict(job_id=2,action='load',code='BF000003'))
        depart(self)
        # Pickup cannot bypass dropping this stop's allocated empties.
        self.post(pickup,dict(job_id=2))
        self.assertIsNone(self.rows('SELECT picked_up_at FROM bf_job_bins WHERE job_id=2 AND bin_id=1')[0]['picked_up_at'])
        # Bind the scanned token, ignoring a forged code for a different bin.
        result=self.post(delivery,dict(job_id=2,code='BF000004'))
        self.assertIn(b'Delivery recorded',result.data)
        stamp=self.rows('SELECT delivered_at FROM bf_job_bins WHERE job_id=2 AND bin_id=3')[0]['delivered_at']
        self.assertTrue(stamp.endswith('+10:00'))
        self.assertIn(stamp[11:19].encode(),result.data)
        self.assertEqual(self.rows('SELECT location_id FROM bf_bins WHERE id=3')[0]['location_id'],3)
        self.assertEqual(self.rows('SELECT location_id FROM bf_bins WHERE id=4')[0]['location_id'],1)
        events=self.rows('SELECT * FROM bf_events')
        self.post(delivery,dict(job_id=2))
        self.assertEqual(self.rows('SELECT * FROM bf_events'),events)
        self.assertEqual(self.rows('SELECT delivered_at FROM bf_job_bins WHERE job_id=2 AND bin_id=3')[0]['delivered_at'],stamp)
        result=self.post(pickup,dict(job_id=2,code='BF000002'))
        self.assertIn(b'Pickup recorded',result.data)
        self.assertEqual(self.rows('SELECT status FROM bf_collection_requests')[0]['status'],'collected')
        self.assertEqual(self.rows('SELECT truck_id FROM bf_bins WHERE id=1')[0]['truck_id'],1)
        self.assertEqual(self.rows('SELECT location_id FROM bf_bins WHERE id=2')[0]['location_id'],3)
        self.assertEqual(self.rows('SELECT status FROM bf_jobs WHERE id=2')[0]['status'],'scheduled')  # Still needs recipient handover.

    def test_driver_signin_returns_to_qr_and_requires_transport_permission(self):
        route=self.url(1,'/driver/pickup')
        self.assertEqual(self.public.get(route).status_code,302)
        self.assertNotIn(b'Pickup recorded',self.public_post(route,dict(job_id=1)).data)
        self.assertEqual(self.public.get('/binflow/bins/1').status_code,302)
        from werkzeug.security import generate_password_hash
        with sqlite3.connect(self.path) as conn:
            uid=conn.execute("INSERT INTO users(role,first_name,last_name,email,username,password_hash) VALUES('employee','QR','Staff','qr@example.com','qrstaff',?)",(generate_password_hash('test-admin-password'),)).lastrowid
        self.post('/logout');self.client.get(route)
        self.assertEqual(self.login('employee','qrstaff').status_code,403)
        self.assertEqual(self.post(route,dict(job_id=1)).status_code,403)
        with sqlite3.connect(self.path) as conn:conn.execute('INSERT INTO bf_access(user_id) VALUES(?)',(uid,))
        self.assertEqual(self.client.get(route).status_code,200)
        self.assertEqual(self.client.get('/binflow/qr/'+self.tokens[1]).status_code,200)

    def test_request_validation_and_office_resolution_preserve_stock(self):
        self.assertEqual(self.request_collection(requester='').status_code,400)
        self.assertEqual(self.request_collection(preferred_date='bad').status_code,400)
        values=dict(request_key=secrets.token_urlsafe(24),requester='Name',contact='12345678')
        self.assertEqual(self.public_post(self.url(3,'/collection'),values).status_code,400)
        self.request_collection()
        # Booking a different bin must not silently mark this request scheduled.
        self.post('/binflow/pickups/new',dict(request_key=secrets.token_urlsafe(24),site_id=3,warehouse_id=1,
            scheduled_date=RUN_DATE,collect=[2],collection_request=1))
        self.assertEqual(self.rows('SELECT COUNT(*) AS n FROM bf_jobs')[0]['n'],1)
        self.assertEqual(self.rows('SELECT status FROM bf_collection_requests')[0]['status'],'new')
        self.schedule()
        self.post('/binflow/pickups/2/cancel',dict(reason='Office rescheduling'))
        self.assertEqual(self.rows('SELECT status,job_id FROM bf_collection_requests')[0],dict(status='new',job_id=None))
        before=self.rows('SELECT * FROM bf_bins')
        self.post('/binflow/collection-requests/1/close',dict(resolution='Client contacted office; no collection needed.'))
        self.assertEqual(self.rows('SELECT status FROM bf_collection_requests')[0]['status'],'closed')
        self.assertEqual(self.rows('SELECT * FROM bf_bins'),before)
        self.request_collection()
        self.assertEqual(len(self.rows('SELECT * FROM bf_collection_requests')),2)

    def test_public_request_rate_limit_and_existing_labels_survive_restart(self):
        from app import create_app
        before=self.rows('SELECT id,code,token FROM bf_bins')
        for _ in range(15):self.assertEqual(self.request_collection().status_code,200)
        self.assertEqual(self.request_collection().status_code,429)
        self.assertEqual(len(self.rows('SELECT * FROM bf_collection_requests')),1)
        create_app(dict(self.app.config))
        self.assertEqual(self.rows('SELECT id,code,token FROM bf_bins'),before)
        self.assertEqual(self.public.get(self.url()).status_code,200)


if __name__=='__main__':unittest.main()
