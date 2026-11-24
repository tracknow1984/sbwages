import io
import json
import sqlite3
import unittest
from unittest.mock import patch
from pypdf import PdfReader

from test_binflow import BinFlowTests
from test_binflow_interstate import CAL, CLIENT, data
from pod_helpers import SIGNATURE
from binflow_transfer_pods import migrate


class TransferPodTests(unittest.TestCase):
    setUp = BinFlowTests.setUp
    tearDown = BinFlowTests.tearDown
    post = BinFlowTests.post
    rows = BinFlowTests.rows
    login = BinFlowTests.login

    def create(self, **changes):
        self.assertEqual(self.post(CAL+'/new',data(pickup_date='2026-09-01',**changes)).status_code,200)
        return self.rows('SELECT MAX(id) AS id FROM bf_interstate_bookings')[0]['id']

    def values(self, **changes):
        return dict(dict(action='draft',revision=0,booking_version=1,service_date='2026-09-04',
                         work_done='Delivered 12 pallets. Unloaded and checked quantities.',driver_name='Test driver',
                         registration='TEST123',exceptions='',signed_name='Test recipient',signer_role='recipient',
                         signed_place='Brisbane receiving depot',signature=SIGNATURE,confirm='yes'),**changes)

    def pod_text(self, client, path):
        response=client.get(path)
        self.assertEqual(response.status_code,200)
        self.assertIn('no-store',response.headers['Cache-Control'])
        self.assertEqual(response.headers['X-Frame-Options'],'SAMEORIGIN')
        self.assertIn("frame-ancestors 'self'",response.headers['Content-Security-Policy'])
        return '\n'.join(page.extract_text() for page in PdfReader(io.BytesIO(response.data)).pages)

    def test_previous_delivered_job_can_raise_draft_then_sign_without_rebooking(self):
        self.create()
        self.post(CAL+'/1/lock',dict(version=1))
        self.post(CAL+'/1/purchase-order',dict(po_number='PO-HISTORY',po_revision=0))
        self.post(CAL+'/1/bulk',dict(action='deliver'))
        before=self.rows('SELECT * FROM bf_interstate_bookings')[0]
        self.assertIn('Raise POD',self.client.get(CAL+'/1').text)
        with patch('binflow_interstate.send_smtp') as mail:
            draft_response=self.post(CAL+'/1/pod',self.values(booking_version=before['version']),follow=False)
            self.assertEqual(draft_response.status_code,303)
            self.assertEqual(draft_response.location,CAL+'/1/pod')
            page=self.client.get(CAL+'/1').text
            self.assertIn('Quick view POD',page)
            self.assertIn('Draft POD · not signed',page)
            self.assertEqual(page.count('<dialog class="register-preview"'),1)
            download=self.client.get(CAL+'/1/pod.pdf?download=1')
            self.assertTrue(download.headers['Content-Disposition'].startswith('attachment;'))
            self.assertEqual(download.headers['X-Frame-Options'],'DENY')
            draft=self.pod_text(self.client,CAL+'/1/pod.pdf')
            self.assertIn('DRAFT',draft)
            self.assertIn('PO-HISTORY',draft)
            submitted=self.post(CAL+'/1/pod',self.values(action='sign',revision=1,booking_version=before['version']),follow=False)
            self.assertEqual(submitted.status_code,303)
            self.assertEqual(submitted.location,CAL+'?date=2026-09-01')
            self.assertIn('POD signed off and saved.',self.client.get(submitted.location).text)
            mail.assert_not_called()
        pod=self.rows('SELECT * FROM bf_transfer_pods')[0]
        self.assertEqual(pod['service_date'],'2026-09-04')
        self.assertTrue(pod['signed_at'].startswith('2026-10-19'))
        self.assertEqual(pod['signature'][:8],b'\x89PNG\r\n\x1a\n')
        self.assertEqual(self.rows('SELECT * FROM bf_interstate_bookings')[0],before)
        self.assertEqual(len(self.rows('SELECT * FROM bf_transfer_pod_history')),2)
        self.assertEqual(self.rows('SELECT * FROM bf_auto_drafts'),[])
        signed=self.pod_text(self.client,CAL+'/1/pod.pdf')
        for text in ('SIGNED','04/09/2026','19/10/2026','Delivered 12 pallets','Test recipient','PO-HISTORY'):
            self.assertIn(text,signed)
        self.assertEqual(self.post(CAL+'/1/pod',self.values(action='sign',revision=2)).status_code,409)
        # Later booking/PO edits cannot rewrite the signed evidence.
        with sqlite3.connect(self.path) as conn:
            conn.execute("UPDATE bf_interstate_bookings SET goods='Changed',client_name='Changed client' WHERE id=1")
            conn.execute("UPDATE bf_interstate_purchase_orders SET number='CHANGED-PO' WHERE booking_id=1")
            migrate(conn)
        self.assertEqual(self.pod_text(self.client,CAL+'/1/pod.pdf'),signed)

    def test_scanned_and_unconfirmed_historical_bookings_are_supported(self):
        self.create(job_type='internal',handling='scanned')
        response=self.post(CAL+'/1/pod',self.values(action='sign',signer_role='office',exceptions='Reviewed original delivery docket and driver timesheet.'))
        self.assertEqual(response.status_code,200)
        self.assertEqual(response.request.path,CAL)
        self.assertIn('Office retrospective record',self.client.get(CAL+'/1/pod').text)
        text=self.pod_text(self.client,CAL+'/1/pod.pdf')
        self.assertIn('not a customer receipt signature',text)
        self.assertEqual(self.rows('SELECT * FROM bf_interstate_manifests'),[])
        self.assertIsNone(self.rows('SELECT locked_at FROM bf_interstate_bookings')[0]['locked_at'])

    def test_validation_stale_forms_and_cancelled_records(self):
        self.create()
        for change in [dict(signature='[]'),dict(confirm='no'),dict(signed_name=''),dict(signed_place=''),
                       dict(service_date='2027-01-01'),dict(service_date='bad'),dict(work_done=''),
                       dict(signer_role='office'),dict(signer_role='invented'),dict(work_done='x'*6001)]:
            with self.subTest(change=change):
                self.assertEqual(self.post(CAL+'/1/pod',self.values(action='sign',**change)).status_code,400)
                self.assertFalse(self.rows('SELECT * FROM bf_transfer_pods'))
        self.assertEqual(self.post(CAL+'/1/pod',self.values(service_date='2027-01-01')).status_code,200)
        self.assertEqual(self.post(CAL+'/1/pod',self.values()).status_code,409)
        with sqlite3.connect(self.path) as conn:
            conn.execute('UPDATE bf_interstate_bookings SET version=version+1 WHERE id=1')
        self.assertEqual(self.post(CAL+'/1/pod',self.values(revision=1)).status_code,409)
        with sqlite3.connect(self.path) as conn:
            conn.execute("UPDATE bf_interstate_bookings SET cancelled_at='2026-10-19' WHERE id=1")
        self.assertEqual(self.post(CAL+'/1/pod',self.values(revision=1,booking_version=2)).status_code,409)
        self.assertEqual(self.client.get(CAL+'/1/pod.pdf').status_code,200)

    def test_customer_sees_only_own_signed_pods_and_cannot_sign(self):
        for name in ('first','second'):
            self.post(CAL+'/access',dict(name=name,username=name,password='example-password',active='yes'))
        self.create()
        with sqlite3.connect(self.path) as conn:
            conn.execute('UPDATE bf_interstate_bookings SET account_id=1 WHERE id=1')
            conn.execute("INSERT OR REPLACE INTO bf_client_permissions(account_id,rights) VALUES(1,?)",(json.dumps(['bookings.view','pods.view','pods.all']),))
        clients=[]
        for aid in (1,2):
            client=self.app.test_client()
            with client.session_transaction() as session:
                session.update(bf_client_uid=aid,bf_client_version=1)
            clients.append(client)
        self.post(CAL+'/1/pod',self.values())
        for client in clients:
            self.assertEqual(client.get(CLIENT+'/1/pod').status_code,404)
            self.assertEqual(client.get(CLIENT+'/1/pod.pdf').status_code,404)
        self.post(CAL+'/1/pod',self.values(action='sign',revision=1))
        self.assertEqual(clients[0].get(CLIENT+'/1/pod').status_code,200)
        self.assertIn('Quick view POD',clients[0].get(CLIENT+'/1').text)
        self.assertIn('SIGNED',self.pod_text(clients[0],CLIENT+'/1/pod.pdf'))
        self.assertIn('Signed transfer PODs',clients[0].get('/binflow/client/pods').text)
        self.assertNotIn('Signed transfer PODs',clients[0].get('/binflow/client/pods?date=2026-08-01').text)
        self.assertIn('Signed transfer PODs',clients[0].get('/binflow/client/pods?date=2026-09-04').text)
        for suffix in ('/pod','/pod.pdf'):
            self.assertEqual(clients[1].get(CLIENT+'/1'+suffix).status_code,404)
            self.assertEqual(self.app.test_client().get(CAL+'/1'+suffix).status_code,302)
        self.assertIn(clients[0].post(CLIENT+'/1/pod',data=self.values(action='sign')).status_code,(400,405))
        with sqlite3.connect(self.path) as conn:
            conn.execute("UPDATE bf_client_permissions SET rights='[]' WHERE account_id=1")
        self.assertEqual(clients[0].get(CLIENT+'/1/pod.pdf').status_code,403)

    def test_sbtx_portal_is_separate_and_uses_its_own_routes(self):
        self.create()
        with sqlite3.connect(self.path) as conn:
            conn.execute("UPDATE bf_interstate_bookings SET portal='sbtx' WHERE id=1")
        self.assertEqual(self.client.get(CAL+'/1/pod').status_code,404)
        response=self.post('/sbtx/transfers/1/pod',self.values(action='sign'))
        self.assertEqual(response.status_code,200)
        self.assertEqual(response.request.path,'/sbtx/')
        self.assertEqual(response.request.args['date'],'2026-09-01')
        self.assertIn('/sbtx/transfers/1/pod.pdf',self.client.get('/sbtx/transfers/1/pod').text)
        self.assertIn('SIGNED',self.pod_text(self.client,'/sbtx/transfers/1/pod.pdf'))

    def test_calendar_shows_completed_pod_only_after_signing(self):
        import re
        self.create()
        def badge(page):
            card=page.split('data-transfer-preview="1"',1)[1].split('</a>',1)[0]
            return re.search(r'<span class="it-pod-badge"[^>]*>',card)[0]
        self.post(CAL+'/1/pod',self.values())
        self.assertIn('hidden',badge(self.client.get(CAL+'?date=2026-09-01').text))
        self.post(CAL+'/1/lock',dict(version=1))
        version=self.rows('SELECT version FROM bf_interstate_bookings')[0]['version']
        updates=CAL+'?date=2026-09-01&responses=1'
        self.assertFalse(self.client.get(updates).json['bookings']['1']['pod_signed'])
        self.post(CAL+'/1/pod',self.values(action='sign',revision=1,booking_version=version))
        for view in ('month','week'):
            page=self.client.get(CAL+'?date=2026-09-01&view='+view).text
            self.assertNotIn('hidden',badge(page))
            self.assertIn('Signed ',badge(page))
        reply=self.client.get(updates).json['bookings']['1']
        self.assertTrue(reply['pod_signed'])
        self.assertTrue(reply['pod_signed_at'])
        self.assertFalse(reply['invoiced'])
