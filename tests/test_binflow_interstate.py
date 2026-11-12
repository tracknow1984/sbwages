import secrets
import sqlite3
import unittest
from unittest.mock import patch

from binflow_interstate import LANES, parse_booking
import test_binflow_bales as bales

CAL = '/binflow/interstate-transfers'
CLIENT = '/binflow/client/interstate-transfers'


def data(**changes):
    return dict(dict(job_type='external',handling='bulk',vehicle='SEMI TRAILER',origin='MEL',destination='BNE',
        pickup_date='2026-11-13',delivery_date='',client_name='Freight test',pickup_address='1 Depot Street',
        pickup_company='Sending Co',delivery_company='Receiving Co',pickup_contact='Pat',delivery_address='2 Depot Street',delivery_contact='Kim',
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

    def test_in_transit_amendment_preserves_dispatch_and_delivered_load_is_closed(self):
        form=data()
        self.post(CAL+'/new',form)
        self.post(CAL+'/1/lock',dict(version=1))
        self.post(CAL+'/1/bulk',dict(action='dispatch'))
        original=self.rows('SELECT * FROM bf_interstate_bookings')[0]
        change=dict(form,reason='New receiving contact',version=original['version'],delivery_contact='Lee')
        self.assertEqual(self.post(CAL+'/1/edit',dict(change,destination='SYD')).status_code,400)
        self.assertEqual(self.post(CAL+'/1/edit',change).status_code,200)
        amended=self.rows('SELECT * FROM bf_interstate_bookings')[0]
        self.assertEqual(amended['delivery_contact'],'Lee')
        self.assertEqual(amended['locked_at'],original['locked_at'])
        self.assertEqual(amended['bulk_dispatched_at'],original['bulk_dispatched_at'])
        self.post(CAL+'/1/bulk',dict(action='deliver'))
        self.assertEqual(self.post(CAL+'/1/cancel',dict(version=amended['version'],reason='Wrong docket')).status_code,409)

    def test_modify_confirmed_load_and_cancel_with_audit_and_po_retention(self):
        form=data(handling='bulk')
        self.post(CAL+'/new',form)
        self.post(CAL+'/1/lock',dict(version=1))
        row=self.rows('SELECT * FROM bf_interstate_bookings')[0]
        self.post(CAL+'/1/purchase-order',dict(po_number='PO-RETAIN',po_revision=0))
        changed=dict(form,version=row['version'],goods='24 pallets')
        self.assertEqual(self.post(CAL+'/1/edit',changed).status_code,400)
        self.post(CAL+'/1/edit',dict(changed,reason='Customer revised quantity'))
        row=self.rows('SELECT * FROM bf_interstate_bookings')[0]
        self.assertEqual(row['goods'],'24 pallets')
        self.assertIsNone(row['locked_at'])
        self.assertIn('PO-RETAIN',self.client.get(CAL+'/1').text)
        self.assertEqual(self.post(CAL+'/1/cancel',dict(version=row['version'],reason=' ')).status_code,400)
        self.assertEqual(self.post(CAL+'/1/cancel',dict(version=row['version']-1,reason='Wrong version')).status_code,409)
        cancelled=self.post(CAL+'/1/cancel',dict(version=row['version'],reason='Receiver closed'))
        self.assertIn('Receiver closed',cancelled.text)
        self.assertIn('Cancelled loads',self.client.get(CAL+'?view=month&date='+row['pickup_date']).text)
        self.assertEqual([r['action'] for r in self.rows('SELECT action FROM bf_load_changes ORDER BY id')],['Modified','Cancelled'])
        self.assertEqual(self.post(CAL+'/1/lock',dict(version=row['version']+1)).status_code,409)
        self.assertEqual(self.post(CAL+'/1/bulk',dict(action='dispatch')).status_code,409)
        self.assertEqual(self.client.get(CAL+'/1/edit').status_code,409)
        self.assertEqual(self.rows('SELECT number FROM bf_interstate_purchase_orders')[0]['number'],'PO-RETAIN')

    def test_bulk_lock_calendar_email_retry_and_manual_movements(self):
        for field in ('client_name','pickup_address','pickup_contact','delivery_address','delivery_contact','goods','email'):
            self.assertEqual(self.post(CAL+'/new',data(**{field:''})).status_code,400)
        form = data()
        self.assertIn('Requested',self.post(CAL+'/new',form).text)
        self.post(CAL+'/new',form)
        self.assertEqual(len(self.rows('SELECT id FROM bf_interstate_bookings')),1)
        week = CAL+'?date=2026-11-13'
        self.assertNotIn('class="it-event',self.client.get(week).text)
        form.update(version=1,pickup_date='2026-11-14',reason='Pickup site moved its window')
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

    def test_calendar_clients_keep_their_colours_and_preview_the_correct_booking(self):
        import re
        def colour(page,bid):
            return re.search(fr'data-transfer-preview="{bid}" data-client-colour="(\d+)"',page)[1]
        for name,day in [('First client','2026-11-12'),('Second client','2026-11-12'),(' First CLIENT ','2026-11-19')]:
            self.post(CAL+'/new',data(client_name=name,pickup_date=day))
        self.post(CAL+'/1/lock',dict(version=1))
        self.post(CAL+'/2/lock',dict(version=1))
        self.post(CAL+'/1/bulk',dict(action='dispatch'))
        week=self.client.get(CAL+'?date=2026-11-12').text
        self.assertNotEqual(colour(week,1),colour(week,2))
        self.assertEqual(colour(week,1),colour(week,3))
        details=week.split('<template id="it-booking-1">')[1].split('</template>')[0]
        for field in ['IT-000001','In transit','First client','SEMI TRAILER','Sending Co','Receiving Co',
                      '1 Depot Street','2 Depot Street','Pat','Kim','12 pallets linen','/interstate-transfers/1']:
            self.assertIn(field,details)
        self.post(CAL+'/3/lock',dict(version=1))
        for params in ('?date=2026-11-19&state=VIC','?date=2026-11-12&view=month'):
            self.assertEqual(colour(week,1),colour(self.client.get(CAL+params).text,3))
        self.assertNotIn('it-booking-1',self.client.get(CAL+'?date=2026-11-12&state=WA').text)

    def test_scanned_reverse_lane_stock_and_dispatch_guards(self):
        self.prepare()
        self.create(wid=1);self.create(wid=1)
        self.post(CAL+'/new',data(handling='scanned',origin='BNE',destination='SYD',job_type='internal'))
        manifest='/binflow/bales/transfers/new?booking=1'
        self.assertEqual(self.client.get(manifest).status_code,404)
        self.post(CAL+'/1/lock',dict(version=1))
        self.assertIn('Scheduled bale pickups',self.client.get('/binflow/bales').text)
        form=dict(origin_id=1,destination_id=2,carrier_supplier_id=1,vehicle_type='SINGLE',vehicle='Truck',payload='1200',
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
        calendar=self.client.get(CAL+'?date=2026-11-13&state=QLD').text
        self.assertIn('IT-000001 · In transit',calendar)
        self.assertIn('Scanned bales · 2 bales',calendar)
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

    def test_admin_sees_new_and_existing_portal_logins(self):
        access = CAL + '/access'
        with sqlite3.connect(self.path) as conn:
            conn.execute("""INSERT INTO bf_client_accounts
                (username,name,password_hash,active,created_at,updated_at,updated_by)
                VALUES('older-login','Previous client','test-only-hash',0,'2024-02-01','2024-02-01','Admin')""")
        form = dict(name='Freight office',username='new-freight',password='test-only-password',active='yes')
        response = self.post(access,form,follow=False)
        aid = self.rows('SELECT id FROM bf_client_accounts WHERE username=?',(form['username'],))[0]['id']
        self.assertEqual(response.status_code,303)
        self.assertTrue(response.location.endswith(f'#login-{aid}'))
        page = self.client.get(access).text
        self.assertIn('Saved logins (2)',page)
        self.assertLess(page.index('Username <strong>new-freight'),page.index('Username <strong>older-login'))
        self.assertIn('Disabled',page)
        self.assertIn('Client portal · Created',page)
        self.assertNotIn(form['password'],page)
        self.assertNotIn('test-only-hash',page)
        self.assertIn('View saved booking logins',self.client.get('/binflow/client/login?transfers=1').text)
        invalid = self.post(access,dict(form,name='Another company'))
        self.assertEqual(invalid.status_code,400)
        self.assertIn('That username is already in use.',invalid.text)
        self.assertIn('value="Another company"',invalid.text)
        self.assertNotIn(form['password'],invalid.text)
        guest = self.app.test_client()
        self.assertEqual(guest.get(access).status_code,302)
        self.assertNotIn('new-freight',guest.get('/binflow/client/login?transfers=1').text)
