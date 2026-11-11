import secrets
import sqlite3
import unittest
from unittest.mock import patch

from binflow_interstate import LANES, parse_booking
import test_binflow_bales as bales

CAL = '/binflow/interstate-transfers'
CLIENT = '/binflow/client/interstate-transfers'


def data(**changes):
    return dict(dict(job_type='external',handling='bulk',origin='MEL',destination='BNE',
        pickup_date='2026-11-13',delivery_date='',client_name='Freight test',pickup_address='1 Depot Street',
        pickup_contact='Pat',delivery_address='2 Depot Street',delivery_contact='Kim',
        email='booking@example.invalid',goods='12 pallets linen\n3 cartons cotton',
        request_key=secrets.token_urlsafe(24)),**changes)


class TransitTests(unittest.TestCase):
    def test_both_directions_and_either_date(self):
        for pair,days in LANES.items():
            a,b = pair.split('-')
            for origin,destination in ((a,b),(b,a)):
                form = data(origin=origin,destination=destination)
                self.assertEqual(parse_booking(form)['delivery_date'],f'2026-11-{13+days:02d}')
                form.update(pickup_date='',delivery_date=f'2026-11-{13+days:02d}')
                self.assertEqual(parse_booking(form)['pickup_date'],'2026-11-13')
        with self.assertRaises(ValueError):
            parse_booking(data(origin='NSW',destination='SA'))
        self.assertEqual(parse_booking(data(origin='NSW',destination='SA',delivery_date='2026-11-22'))['delivery_date'],'2026-11-22')


class BookingTests(unittest.TestCase):
    setUp = bales.BaleTests.setUp
    tearDown = bales.BaleTests.tearDown
    post = bales.BaleTests.post
    login = bales.BaleTests.login
    rows = bales.BaleTests.rows
    prepare = bales.BaleTests.prepare
    settings = bales.BaleTests.settings
    create = bales.BaleTests.create
    source = bales.BaleTests.source
    scan_source = bales.BaleTests.scan_source
    scan = bales.BaleTests.scan
    action = bales.BaleTests.action

    def test_bulk_lock_calendar_email_retry_and_manual_movements(self):
        for field in ('client_name','pickup_address','pickup_contact','delivery_address','delivery_contact','goods','email'):
            self.assertEqual(self.post(CAL+'/new',data(**{field:''})).status_code,400)
        form = data()
        self.assertIn('Requested',self.post(CAL+'/new',form).text)
        self.post(CAL+'/new',form)
        self.assertEqual(len(self.rows('SELECT id FROM bf_interstate_bookings')),1)
        week = CAL+'?date=2026-11-13'
        self.assertNotIn('class="it-event',self.client.get(week).text)
        form.update(version=1,pickup_date='2026-11-14')
        self.post(CAL+'/1/edit',form)
        self.assertEqual(self.post(CAL+'/1/lock',dict(version=1)).status_code,409)
        self.app.config.update(SMTP_HOST='smtp.example.invalid',SMTP_FROM='office@example.invalid')
        with patch('binflow_interstate.send_smtp',side_effect=OSError()):
            self.assertIn('Email not sent',self.post(CAL+'/1/lock',dict(version=2)).text)
        with patch('binflow_interstate.send_smtp') as mail:
            self.post(CAL+'/1/email')
            self.post(CAL+'/1/email')
            self.post(CAL+'/1/lock',dict(version=2))
            mail.assert_called_once()
            msg = mail.call_args.args[1]
            self.assertEqual(msg['To'],'booking@example.invalid')
            self.assertIn('17 Nov 2026',msg.get_content())
        self.assertIn('class="it-event pickup"',self.client.get(week).text)
        self.assertIn('class="it-event delivery"',self.client.get(CAL+'?date=2026-11-17').text)
        self.assertNotIn('class="it-event',self.client.get(week+'&state=WA').text)
        self.assertEqual(self.client.get('/binflow/bales/transfers/new?booking=1').status_code,404)
        self.assertEqual(self.post(CAL+'/1/bulk',dict(action='deliver')).status_code,409)
        self.post(CAL+'/1/bulk',dict(action='dispatch'))
        self.assertIn('Delivered',self.post(CAL+'/1/bulk',dict(action='deliver')).text)
        self.assertFalse(self.rows('SELECT * FROM bf_bale_transfer_items'))

    def test_scanned_reverse_lane_stock_and_dispatch_guards(self):
        self.prepare()
        self.create(wid=1);self.create(wid=1)
        self.post(CAL+'/new',data(handling='scanned',origin='BNE',destination='SYD',job_type='internal'))
        manifest='/binflow/bales/transfers/new?booking=1'
        self.assertEqual(self.client.get(manifest).status_code,404)
        self.post(CAL+'/1/lock',dict(version=1))
        self.assertIn('Scheduled bale pickups',self.client.get('/binflow/bales').text)
        form=dict(origin_id=1,destination_id=2,carrier='Carrier',vehicle='Truck',payload='1200',
                  expected_bales=2,request_key=secrets.token_urlsafe(24))
        self.post(manifest,dict(form,destination_id=1))
        self.assertFalse(self.rows('SELECT * FROM bf_bale_transfers'))
        self.post(manifest,form)
        self.post(manifest,dict(form,request_key=secrets.token_urlsafe(24)))
        self.assertEqual(len(self.rows('SELECT * FROM bf_bale_transfers')),1)
        self.assertTrue(self.scan('BA0000001').json['ok'])
        self.assertIn('Scan all 2 booked bales',self.action('depart',confirm='yes').text)
        self.scan('BA0000002');self.action('depart',confirm='yes')
        self.assertIn('In transit',self.client.get(CAL+'/1').text)
        self.scan('BA0000001','receive')
        self.assertTrue(self.scan('BA0000001','receive').json['duplicate'])
        self.assertEqual(self.rows('SELECT status FROM bf_bale_transfers')[0]['status'],'transit')
        self.scan('BA0000002','receive')
        self.assertEqual(self.rows('SELECT status FROM bf_bale_transfers')[0]['status'],'received')
        self.assertEqual(self.rows('SELECT DISTINCT status,warehouse_id FROM bf_bales'),[dict(status='stored',warehouse_id=2)])
        self.assertNotIn('Hand over to BlockTexx storage',self.client.get('/binflow/bales/transfers/1').text)

    def test_booking_logins_are_private_and_cannot_lock(self):
        for name in ('first','second'):
            self.post(CAL+'/access',dict(name=name,username=name,password='example-password',active='yes'))
        self.post(CAL+'/new',data(client_name='Office only'))
        def enter(name):
            self.post('/binflow/client/logout')
            page=self.post('/binflow/client/login?transfers=1',dict(username=name,password='example-password'))
            self.assertIn('Request a transfer',page.text)
            self.assertNotIn('Daily PODs',page.text)
        enter('first')
        self.assertIn('Requested',self.post(CLIENT+'/new',data(client_name='Customer one')).text)
        self.assertNotIn('Office only',self.client.get(CLIENT).text)
        self.assertEqual(self.client.get(CLIENT+'/1').status_code,404)
        self.assertEqual(self.client.get('/binflow/client/calendar').status_code,403)
        self.assertNotIn('Lock dates',self.client.get(CLIENT+'/2').text)
        self.post(CAL+'/2/lock',dict(version=1))
        self.assertIsNone(self.rows('SELECT locked_at FROM bf_interstate_bookings WHERE id=2')[0]['locked_at'])
        enter('second')
        self.assertEqual(self.client.get(CLIENT+'/2').status_code,404)
        self.post('/binflow/client/logout')
        self.assertIn('login',self.client.get(CLIENT).location)
