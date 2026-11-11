import io
import re
import secrets
import sqlite3
import unittest
from html import unescape
from urllib.parse import parse_qs, urlsplit
from unittest.mock import patch

from openpyxl import load_workbook
from reportlab.pdfgen.canvas import Canvas
import test_binflow_client_permissions as permissions
import test_binflow_interstate as interstate
from test_binflow_interstate import CAL, CLIENT, data


class RescheduleTests(unittest.TestCase):
    setUp = permissions.PortalPermissionTests.setUp
    tearDown = permissions.PortalPermissionTests.tearDown
    rows = permissions.PortalPermissionTests.rows
    post = permissions.PortalPermissionTests.post
    login = permissions.PortalPermissionTests.login
    account = permissions.PortalPermissionTests.account
    visit = permissions.PortalPermissionTests.visit
    send = permissions.PortalPermissionTests.send

    def booked(self):
        self.account('first')
        client = self.visit('first')
        self.send(client, CLIENT+'/new', data(pickup_date='2026-11-13',delivery_date='2026-11-16'))
        self.post(CAL+'/1/lock', dict(version=1))
        return client

    def ask(self,client,version,day='2026-11-20',delivery=''):
        return self.send(client,CLIENT+'/1/reschedule',dict(version=version,pickup_date=day,delivery_date=delivery))

    def test_share_request_approval_and_purchase_order(self):
        client = self.booked()
        def shared(page):
            uri = unescape(re.search('href="(mailto:[^"]+)"',page)[1])
            return parse_qs(urlsplit(uri).query)['body'][0]
        self.assertIn('Confirmed', shared(client.get(CLIENT+'/1').text))
        upload = io.BytesIO()
        pdf = Canvas(upload)
        pdf.drawString(30,700,'Test PO')
        pdf.save()
        document=upload.getvalue()
        path=CLIENT+'/1/purchase-order'
        self.send(client,path,dict(po_revision=0,po_number='0010',po_file=(io.BytesIO(document),'po.pdf')))
        po=self.rows('SELECT * FROM bf_interstate_purchase_orders')
        with patch('binflow_interstate.send_smtp') as mail:
            revised=self.ask(client,2)
            mail.assert_not_called()
        self.assertEqual(revised.status_code,200)
        self.assertIn('Date change awaiting confirmation',revised.text)
        self.assertIn('0010',revised.text)
        body=shared(revised.text)
        self.assertIn('Awaiting confirmation',body)
        self.assertIn('Friday 20 November 2026',body)
        self.assertIn('Monday 23 November 2026',body)
        self.assertEqual(client.get(path+'/file').data,document)
        calendar=client.get(CLIENT+'?date=2026-11-20').text
        self.assertIn('Date change requested',calendar)
        self.assertNotIn('class="it-event pickup"',calendar)
        self.assertEqual(self.post(CAL+'/1/bulk',dict(action='dispatch')).status_code,409)
        self.assertEqual(self.post(CAL+'/1/lock',dict(version=2)).status_code,409)
        export=load_workbook(io.BytesIO(client.get(CLIENT+'?date=2026-11-20&export=month').data)).active
        entry=dict(zip([c.value for c in export[4]],[c.value for c in export[5]]))
        self.assertEqual((entry['Status'],entry['PO status'],entry['PO number']),
                         ('Awaiting confirmation','PO received','0010'))
        self.app.config.update(SMTP_HOST='example.invalid',SMTP_FROM='office@example.invalid')
        with patch('binflow_interstate.send_smtp') as mail:
            self.post(CAL+'/1/lock',dict(version=3,decision='approved'))
            self.post(CAL+'/1/lock',dict(version=3,decision='approved'))
            mail.assert_called_once()
            self.assertIn('20 Nov 2026',mail.call_args.args[1].get_content())
        self.assertIn('class="it-event pickup"',client.get(CLIENT+'?date=2026-11-20').text)
        self.assertEqual(self.rows('SELECT decision FROM bf_interstate_date_changes'),[{'decision':'approved'}])
        self.assertEqual(self.rows('SELECT * FROM bf_interstate_purchase_orders'),po)
        self.post(CAL+'/1/bulk',dict(action='dispatch'))
        self.assertEqual(self.ask(client,4).status_code,409)

    def test_permissions_validation_and_keep_original_dates(self):
        client=self.booked()
        self.account('other')
        self.assertEqual(self.ask(self.visit('other'),2).status_code,404)
        aid=self.rows("SELECT id FROM bf_client_accounts WHERE username='first'")[0]['id']
        with sqlite3.connect(self.path) as conn:
            conn.execute('INSERT OR REPLACE INTO bf_client_permissions VALUES(?,?)',(aid,'["bookings.view"]'))
        self.assertIn('Share by email',client.get(CLIENT+'/1').text)
        self.assertNotIn('Submit date change',client.get(CLIENT+'/1').text)
        self.assertEqual(self.ask(client,2).status_code,403)
        with sqlite3.connect(self.path) as conn:
            conn.execute('UPDATE bf_client_permissions SET rights=? WHERE account_id=?',('["bookings.view","bookings.edit"]',aid))
        self.assertEqual(self.ask(client,2,'2026-11-13').status_code,400)
        self.assertEqual(self.ask(client,2,'2026-11-20','2026-11-19').status_code,400)
        self.assertEqual(self.ask(client,2).status_code,200)
        self.assertEqual(self.ask(client,2,'2026-11-27').status_code,409)
        self.assertEqual(self.ask(client,3,'2026-11-27').status_code,200)
        self.assertEqual(client.get(CLIENT+'/1/edit').status_code,409)
        response=self.post(CAL+'/1/lock',dict(version=4,decision='original'))
        self.assertEqual(response.status_code,200)
        row=self.rows('SELECT * FROM bf_interstate_bookings')[0]
        self.assertEqual((row['pickup_date'],row['delivery_date']),('2026-11-13','2026-11-16'))
        self.assertTrue(row['locked_at'])
        self.assertEqual(self.rows('SELECT decision FROM bf_interstate_date_changes'),[{'decision':'original'}])

    def test_manifest_dispatch_waits_for_reapproval(self):
        self.account('first')
        client=self.visit('first')
        aid=self.rows("SELECT id FROM bf_client_accounts WHERE username='first'")[0]['id']
        fixtures=interstate.BookingTests()
        for attr in ('app','client','path'):
            setattr(fixtures,attr,getattr(self,attr))
        fixtures.prepare()
        fixtures.create(wid=1)
        self.post(CAL+'/new',data(handling='scanned',origin='BNE',destination='SYD',job_type='internal'))
        with sqlite3.connect(self.path) as conn:
            conn.execute('UPDATE bf_interstate_bookings SET account_id=? WHERE id=1',(aid,))
        self.post(CAL+'/1/lock',dict(version=1))
        self.post('/binflow/bales/transfers/new?booking=1',dict(origin_id=1,destination_id=2,
            carrier_supplier_id=1,vehicle_type='SINGLE',expected_bales=1,request_key=secrets.token_urlsafe(24)))
        self.assertTrue(fixtures.scan('BA0000001').json['ok'])
        self.assertEqual(self.ask(client,2).status_code,200)
        self.assertIn('Dispatch is on hold',fixtures.action('depart',confirm='yes').text)
        self.assertEqual(self.rows('SELECT status FROM bf_bale_transfers')[0]['status'],'loading')
        self.post(CAL+'/1/lock',dict(version=3,decision='approved'))
        fixtures.action('depart',confirm='yes')
        self.assertEqual(self.rows('SELECT status FROM bf_bale_transfers')[0]['status'],'transit')
        self.assertEqual(self.ask(client,4,'2026-11-27').status_code,409)
