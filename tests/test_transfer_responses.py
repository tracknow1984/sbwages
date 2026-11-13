import sqlite3
import unittest
from unittest.mock import patch

import test_binflow_transfer_dispatch as fixture
from test_binflow_interstate import CAL,data


class TransferResponseTests(unittest.TestCase):
    setUp=fixture.TransferDispatchTests.setUp
    tearDown=fixture.TransferDispatchTests.tearDown
    login=fixture.TransferDispatchTests.login
    post=fixture.TransferDispatchTests.post
    rows=fixture.TransferDispatchTests.rows
    rigid=fixture.TransferDispatchTests.rigid
    person=fixture.TransferDispatchTests.person

    def test_driver_denies_reassign_another_approves_and_old_actions_are_stale(self):
        self.person('Errol','QLD')
        second=self.person('Cover','QLD')
        self.post(CAL+'/new',self.rigid())
        self.post(CAL+'/1/lock',dict(version=1))
        first=self.rows('SELECT * FROM bf_load_dispatches')[0]
        path='/employee/hit-me/'+str(first['notice_id'])+'/load-response'
        self.assertTrue(first['sent_at'])
        calendar=CAL+'?date=2026-11-13'
        preview=self.client.get(calendar).text.split('<template id="it-booking-1">')[1]
        self.assertIn('Pending driver',preview)
        self.assertIn('Sent ',preview)
        self.post('/logout')
        self.login('employee','Cover')
        self.assertEqual(self.post(path,dict(decision='denied')).status_code,404)
        self.post('/logout')
        self.login('employee','Errol')
        self.assertIn('>Approve</button>',self.client.get('/employee/hit-me').text)
        self.assertEqual(self.post(path,dict(decision='denied')).status_code,200)
        self.assertEqual(self.post(path,dict(decision='approved')).status_code,409)
        self.assertEqual(self.client.get('/employee/hit-me/status').json['unread'],0)
        self.post('/logout')
        self.login()
        self.assertIn('Denied by driver',self.client.get(calendar).text)
        attempt=dict(version=2,declined_id=first['id'],dispatch_to='driver:'+str(second))
        self.assertEqual(self.post(CAL+'/1/dispatch-recipient',attempt).status_code,200)
        self.assertEqual(self.post(CAL+'/1/dispatch-recipient',attempt).status_code,409)
        current=self.rows('SELECT * FROM bf_load_dispatches ORDER BY id DESC')[0]
        self.assertEqual(current['version'],first['version'])
        self.post('/logout')
        self.login('employee','Cover')
        self.post('/employee/hit-me/'+str(current['notice_id'])+'/load-response',dict(decision='approved'))
        self.post('/logout')
        self.login()
        self.assertIn('Approved by driver',self.client.get(calendar).text)

    def test_migrate_prior_handoffs_with_timestamp_and_resend_existing_supplier_link(self):
        self.person('Errol','QLD')
        self.post(CAL+'/new',self.rigid())
        self.post(CAL+'/1/lock',dict(version=1))
        old=self.rows('SELECT * FROM bf_load_dispatches')[0]
        from binflow_transfer_dispatch import migrate
        with sqlite3.connect(self.path) as conn:
            conn.row_factory=sqlite3.Row
            conn.execute('ALTER TABLE bf_load_dispatches RENAME TO old_assignments')
            conn.execute('DROP INDEX bf_load_dispatch_lookup')
            conn.execute('DROP INDEX bf_dispatch_notice')
            conn.execute("""CREATE TABLE bf_load_dispatches(id INTEGER PRIMARY KEY,booking_id INTEGER,
                version INTEGER,event TEXT,target TEXT,recipient TEXT,address TEXT,title TEXT,body TEXT,actor_id INTEGER,
                status TEXT,attempted_at TEXT,notice_id INTEGER,UNIQUE(booking_id,version,event))""")
            fields='id,booking_id,version,event,target,recipient,address,title,body,actor_id,status,attempted_at,notice_id'
            conn.execute(f'INSERT INTO bf_load_dispatches SELECT {fields} FROM old_assignments')
            conn.execute('DROP TABLE old_assignments')
            migrate(conn)
            migrated=conn.execute('SELECT * FROM bf_load_dispatches').fetchone()
            self.assertEqual((migrated['id'],migrated['sent_at']),(old['id'],old['sent_at']))
            conn.execute("""UPDATE bf_load_dispatches SET target='supplier:1',address='old@example.com',
                notice_id=NULL,recipient='Existing carrier'""")
        self.assertIn('Send supplier response link',self.client.get(CAL+'/1').text)
        self.app.config.update(SMTP_HOST='smtp.example.invalid',SMTP_FROM='office@example.invalid')
        with patch('binflow_transfer_dispatch.send_smtp') as mail:
            self.post(CAL+'/1/dispatch-recipient',dict(version=2,retry=old['id']))
            msg=mail.call_args.args[1]
            self.assertIn('/transfers/supplier-response/',msg.get_content())
        self.assertEqual(len(self.rows('SELECT * FROM bf_load_dispatches')),1)

    def test_supplier_link_requires_explicit_decision_then_updates_calendar(self):
        with sqlite3.connect(self.path) as conn:
            sid=conn.execute("""INSERT INTO bf_supplier_site_profiles(category,group_name,customer_site,
                contact_email,updated_at,updated_by) VALUES ('industry-suppliers','TRANSPORT','Carrier',
                'carrier@example.com','today','office')""").lastrowid
        self.post(CAL+'/new',data())
        self.app.config.update(SMTP_HOST='smtp.example.invalid',SMTP_FROM='office@example.invalid')
        with patch('binflow_interstate.send_smtp'),patch('binflow_transfer_dispatch.send_smtp') as mail:
            self.post(CAL+'/1/lock',dict(version=1,dispatch_to=f'supplier:{sid}'))
        current=self.rows('SELECT * FROM bf_load_dispatches')[0]
        url='/transfers/supplier-response/'+current['response_token']
        self.assertIn(url,mail.call_args.args[1].get_content())
        guest=self.app.test_client()
        self.assertEqual(guest.get(url).status_code,200)
        self.assertIn('Pending external supplier',self.client.get(CAL+'?date=2026-11-13').text)
        self.assertEqual(guest.post(url,data=dict(decision='approved')).status_code,400)
        with guest.session_transaction() as session:
            csrf=session['csrf']
        for _ in range(2):
            self.assertEqual(guest.post(url,data=dict(csrf=csrf,decision='approved'),follow_redirects=True).status_code,200)
        self.assertIn('Approved by supplier',self.client.get(CAL+'?date=2026-11-13').text)
        with patch('binflow_transfer_dispatch.send_smtp'):
            self.post(CAL+'/1/cancel',dict(version=2,reason='Site closed'))
        self.assertEqual(guest.post(url,data=dict(csrf=csrf,decision='denied')).status_code,409)
        self.assertEqual(guest.get(url+'x').status_code,404)
