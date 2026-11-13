import re
import sqlite3
import unittest
from unittest.mock import patch
from urllib.parse import urlencode

from werkzeug.security import generate_password_hash

import test_binflow as fixture
from test_binflow_interstate import CAL, data


class TransferDispatchTests(unittest.TestCase):
    setUp = fixture.BinFlowTests.setUp
    tearDown = fixture.BinFlowTests.tearDown
    rows = fixture.BinFlowTests.rows
    post = fixture.BinFlowTests.post
    login = fixture.BinFlowTests.login

    def person(self,name,state):
        with sqlite3.connect(self.path) as conn:
            return conn.execute("""INSERT INTO users(role,first_name,username,email,password_hash,business_role,working_state)
                VALUES('employee',?,?,?,?,'driver',?)""",(name,name,name+'@example.com',
                generate_password_hash('test-admin-password'),state)).lastrowid

    def rigid(self,state='QLD'):
        return data(vehicle='RIGID',hours='4',handling='bulk',job_type='internal',origin=state,destination=state,
                    goods='10 pallets of linen')

    def test_automatic_errol_handoff_and_all_unread_then_cancellation(self):
        errol=self.person('Errol','QLD')
        self.person('Other','QLD')
        self.post(CAL+'/new',self.rigid())
        self.assertEqual(self.rows('SELECT * FROM staff_notices'),[])
        self.assertIn(f'value="driver:{errol}" selected',self.client.get(CAL+'/1').text)
        self.post(CAL+'/1/lock',dict(version=1))
        self.post(CAL+'/1/lock',dict(version=1))
        self.post(CAL+'/1/purchase-order',dict(po_revision=0,po_number='NEVER-SHARE-PO'))
        notice,=self.rows('SELECT * FROM staff_notices')
        self.assertEqual(notice['user_id'],errol)
        for detail in ('13 Nov 2026','1 Depot Street','2 Depot Street','10 pallets of linen'):
            self.assertIn(detail,notice['body'])
        self.post('/admin/hit-me',dict(user_id=errol,title='Yard notice',body='Bring boots',priority='normal'))
        self.post('/logout')
        page=self.login('employee','Errol')
        self.assertIn('aria-label="2 unread messages"',page.text)
        self.assertEqual(self.client.get('/employee/hit-me/status').json['unread'],2)
        board=self.client.get('/employee/hit-me').text
        self.assertIn('10 pallets of linen',board)
        self.assertNotIn('NEVER-SHARE-PO',board)
        self.assertNotIn('freight price',board.lower())
        self.post('/employee/hit-me/'+str(notice['id'])+'/read')
        self.assertEqual(self.client.get('/employee/hit-me/status').json['unread'],1)
        self.post('/logout')
        self.login('employee','Other')
        self.assertNotIn('10 pallets of linen',self.client.get('/employee/hit-me').text)
        self.post('/logout')
        self.login()
        self.post(CAL+'/1/cancel',dict(version=2,reason='Customer postponed'))
        self.assertEqual(self.rows('SELECT title FROM staff_notices WHERE user_id=? AND archived_at IS NULL ORDER BY id',(errol,)),
                         [dict(title='Yard notice'),dict(title='IT-000001 · Cancelled')])

    def test_driver_default_and_changed_approval(self):
        first=self.person('NSWOne','NSW')
        self.person('NSWTwo','NSW')
        wrong=self.person('VicOne','VIC')
        form=self.rigid('NSW')
        self.post(CAL+'/new',form)
        self.assertEqual(self.post(CAL+'/1/lock',dict(version=1,dispatch_to=f'driver:{wrong}')).status_code,400)
        self.assertIsNone(self.rows('SELECT locked_at FROM bf_interstate_bookings')[0]['locked_at'])
        self.post(CAL+'/1/lock',dict(version=1,dispatch_to=f'driver:{first}',remember_driver='yes'))
        self.post(CAL+'/new',self.rigid('NSW'))
        self.post(CAL+'/2/lock',dict(version=1))
        self.post(CAL+'/1/edit',dict(form,pickup_date='2026-11-15',version=2,reason='Customer moved pickup'))
        self.assertIn('On hold',self.rows('SELECT title FROM staff_notices ORDER BY id DESC')[0]['title'])
        self.post(CAL+'/1/lock',dict(version=3))
        self.assertEqual({r['user_id'] for r in self.rows('SELECT user_id FROM staff_notices')},{first})
        self.assertIn('15 Nov 2026',self.rows('SELECT body FROM staff_notices ORDER BY id DESC')[0]['body'])

    def test_supplier_handoff_and_retry_do_not_repeat_confirmation(self):
        with sqlite3.connect(self.path) as conn:
            supplier=conn.execute("""INSERT INTO bf_supplier_site_profiles(category,group_name,customer_site,
                contact_email,updated_at,updated_by) VALUES ('industry-suppliers','TRANSPORT','Carrier',
                'carrier@example.com','today','office')""").lastrowid
        self.post(CAL+'/new',data())
        self.app.config.update(SMTP_HOST='smtp.example.invalid',SMTP_FROM='office@example.invalid')
        with patch('binflow_interstate.send_smtp') as client_mail, patch('binflow_transfer_dispatch.send_smtp',side_effect=OSError):
            self.post(CAL+'/1/lock',dict(version=1,dispatch_to=f'supplier:{supplier}',remember_driver='yes'))
            client_mail.assert_called_once()
        log,=self.rows('SELECT * FROM bf_load_dispatches')
        self.assertEqual(log['status'],'failed')
        with patch('binflow_transfer_dispatch.send_smtp') as mail:
            self.post(CAL+'/1/dispatch-recipient',dict(version=2,retry=log['id']))
            self.post(CAL+'/1/lock',dict(version=1))
            mail.assert_called_once()
            msg=mail.call_args.args[1]
            self.assertEqual(msg['To'],'carrier@example.com')
            self.assertIn('12 pallets linen',msg.get_content())
            self.assertNotIn('freight',msg.get_content().lower())
        self.post(CAL+'/new',data())
        self.assertIn(f'value="supplier:{supplier}" selected',self.client.get(CAL+'/2').text)
        with patch('binflow_transfer_dispatch.send_smtp') as mail, patch('binflow_interstate.send_smtp'):
            self.post(CAL+'/2/lock',dict(version=1))
            mail.assert_called_once()
        self.assertFalse(self.rows('SELECT * FROM staff_notices'))

    def test_vehicle_order_filter_and_month_default(self):
        vehicles=('B-DOUBLE','SEMI TRAILER','RIGID')
        for num,vehicle in enumerate(vehicles,1):
            self.post(CAL+'/new',self.rigid() if vehicle=='RIGID' else data(vehicle=vehicle))
            self.post(CAL+f'/{num}/lock',dict(version=1))
        path=CAL+'?date=2026-11-13'
        page=self.client.get(path).text
        grid=page.split('<tbody>')[1].split('</tbody>')[0]
        self.assertEqual(re.findall(r'data-truck="([^"]+)"',grid),list(reversed(vehicles)))
        self.assertNotIn('bf-date-grid-week',page)
        for v in vehicles:
            page=self.client.get(path+'&'+urlencode(dict(vehicle=v,view='week'))).text
            grid=page.split('<tbody>')[1].split('</tbody>')[0]
            self.assertEqual(re.findall(r'data-truck="([^"]+)"',grid),[v])
            self.assertIn('bf-date-grid-week',page)
            self.assertIn(urlencode(dict(vehicle=v)),page.split('aria-label="Next week"')[1].split('>')[0])
        self.assertEqual(self.client.get(path+'&vehicle=alien').status_code,400)
