import io
import re
import sqlite3
import unittest

import test_binflow_client_permissions as portal_tests
from binflow_permissions import role_fields
from werkzeug.security import generate_password_hash


CLIENT = '/binflow/client/warehouses'
INBOX = '/binflow/warehouse/messages'


class WarehouseMessageTests(unittest.TestCase):
    setUp = portal_tests.PortalPermissionTests.setUp
    tearDown = portal_tests.PortalPermissionTests.tearDown
    post = portal_tests.PortalPermissionTests.post
    rows = portal_tests.PortalPermissionTests.rows
    login = portal_tests.PortalPermissionTests.login
    account = portal_tests.PortalPermissionTests.account
    visit = portal_tests.PortalPermissionTests.visit
    send = portal_tests.PortalPermissionTests.send

    def staff(self, state, role, workspace):
        roles = role_fields([role])
        with sqlite3.connect(self.path) as conn:
            conn.execute("""INSERT INTO users
                (role,first_name,last_name,email,username,password_hash,business_role,extra_business_roles,working_state)
                VALUES('employee','Warehouse','User',?,?,?,?,?,?)""",
                (role+state+'@example.invalid', role+state, generate_password_hash('staff-password'), roles['business_role'], roles['extra_business_roles'], state))
        browser = self.app.test_client()
        browser.get('/employee/login')
        self.send(browser, '/employee/login', dict(username=role+state, password='staff-password'))
        with browser.session_transaction() as session:
            session['binflow_workspace'] = workspace
        return browser

    def message(self, browser, path, body):
        response = browser.get(path)
        self.assertEqual(response.status_code, 200)
        key = re.search(r'name="submission_key" value="([0-9a-f]+)"', response.text)[1]
        values = dict(body=body, submission_key=key)
        sent = self.send(browser, path, values)
        self.assertEqual(sent.status_code, 200)
        self.assertIn(body, sent.text)
        return values

    def test_state_delivery_reply_and_account_isolation(self):
        aid = self.account('first-client', custom_permissions='yes', permission=['bookings.view'])
        self.account('second-client')
        first, second = self.visit('first-client'), self.visit('second-client')
        self.assertIn('Warehouses', first.get('/binflow/client/interstate-transfers').text)
        directory = first.get('/binflow/client/').text
        for state, address in [('NSW','19-26 Durian Place'), ('QLD','4705-4715 Mt Lindsey Highway'),
                               ('VIC','3-5 Leader St')]:
            self.assertIn(address, directory)
            path = CLIENT+'/'+state
            payload = self.message(first, path, state+' client enquiry')
            self.assertEqual(self.send(first, path, payload).status_code, 200)
            self.assertEqual(self.send(first, path, dict(payload, body='different enquiry')).status_code, 409)
            self.assertNotIn(state+' client enquiry', second.get(path).text)
        self.assertEqual(len(self.rows('SELECT id FROM bf_warehouse_messages')), 3)

        nsw = self.staff('NSW','warehouse','warehouse')
        qld = self.staff('QLD','manager','manager')
        driver = self.staff('NSW','driver','warehouse')
        for staff, state in [(nsw,'NSW'),(qld,'QLD'),(self.staff('VIC','warehouse','warehouse'),'VIC')]:
            page = staff.get(INBOX).text
            self.assertIn(state+' client enquiry', page)
            self.assertIn('Awaiting reply', page)
            self.assertIn(INBOX, staff.get('/binflow/warehouse').text)
            reply = f'{INBOX}/{state}/{aid}'
            self.message(staff, reply, 'Reply from '+state)
            self.assertIn('Reply from '+state, first.get(CLIENT+'/'+state).text)
            self.assertNotIn('Reply from '+state, second.get(CLIENT+'/'+state).text)
            self.assertIn('Warehouse replied', first.get(CLIENT).text)
            self.assertIn('Replied', staff.get(INBOX).text)
        self.assertNotIn('Reply from QLD', nsw.get(INBOX).text)
        self.assertEqual(nsw.get(INBOX+'?state=QLD').status_code, 403)
        self.assertEqual(self.send(nsw,f'{INBOX}/QLD/{aid}',dict(body='wrong state')).status_code,403)
        self.assertEqual(nsw.get(f'{INBOX}/NSW/99999').status_code, 404)
        self.assertEqual(driver.get(INBOX).status_code,403)
        self.assertNotIn('Reply from NSW', first.get(CLIENT+'/QLD').text)
        self.assertNotIn('Reply from NSW', first.get(f'{INBOX}/NSW/{aid}',follow_redirects=True).text)
        self.assertIn('Reply from NSW', self.client.get(INBOX+'?state=NSW').text)

    def test_existing_enquiries_and_replies_survive_migration(self):
        from binflow_warehouse_messages import migrate
        aid=self.account('legacy-client')
        with sqlite3.connect(self.path) as conn:
            staff=conn.execute("SELECT id FROM users WHERE role='admin' LIMIT 1").fetchone()[0]
            for sender,text,key in ((None,'Legacy request','c'*32),(staff,'Legacy response','d'*32)):
                conn.execute("""INSERT INTO bf_warehouse_messages(account_id,state,staff_id,body,created_at,submission_key)
                    VALUES(?,'QLD',?,?,?,?)""",(aid,sender,text,'2026-10-19T10:00:00+10:00',key))
            migrate(conn)
            migrate(conn)
        client=self.visit('legacy-client')
        self.assertIn('Legacy request',client.get('/binflow/client/communications').text)
        self.assertIn('predates status tracking',client.get(CLIENT+'/QLD?message=1').text)
        self.assertIn('Legacy response',client.get(CLIENT+'/QLD').text)
        self.assertEqual(self.rows('SELECT status,legacy FROM bf_warehouse_enquiries'),[dict(status='SENT',legacy=1)])

    def test_communication_receipts_files_and_independent_actions(self):
        from PIL import Image
        aid=self.account('blocktexx1')
        self.account('unrelated')
        client=self.visit('blocktexx1')
        other=self.visit('unrelated')
        nsw=self.staff('NSW','warehouse','warehouse')
        qld=self.staff('QLD','warehouse','warehouse')
        url='/binflow/client/communications'
        form=client.get(url)
        self.assertIn('Send to warehouse',form.text)
        key=re.search(r'name="submission_key" value="([^"]+)"',form.text)[1]
        pic=io.BytesIO()
        Image.new('RGB',(4,4)).save(pic,format='PNG')
        def fields(key, filename='dock.png', contents=None):
            return dict(state='NSW',body='Inspect the loading dock',submission_key=key,
                        attachment=(io.BytesIO(pic.getvalue() if contents is None else contents),filename))
        response=self.send(client,url,fields(key))
        self.assertEqual(response.status_code,200)
        self.assertIn('Communication #1 · SENT',response.text)
        self.assertEqual(self.send(client,url,fields(key)).status_code,200)
        self.assertEqual(self.send(client,url,fields(key,'other.png')).status_code,409)
        self.assertEqual(self.send(client,url,fields('a'*32,contents=b'broken')).status_code,400)
        self.send(client,url,dict(state='NSW',body='Second job',submission_key='b'*32))
        self.assertEqual(len(self.rows('SELECT * FROM bf_warehouse_enquiries')),2)
        client_file=CLIENT+'/NSW/files/1'
        self.assertEqual(client.get(client_file).data,pic.getvalue())
        self.assertIn('attachment;',client.get(client_file).headers['Content-Disposition'])
        self.assertEqual(other.get(client_file).status_code,404)
        inbox=f'{INBOX}/NSW/{aid}'
        self.assertEqual(qld.get(inbox+'/files/1').status_code,403)
        self.assertEqual(nsw.get(inbox+'/files/1').data,pic.getvalue())
        self.assertNotIn('Inspect the loading dock',qld.get(INBOX).text)
        self.assertEqual(nsw.get(INBOX).status_code,200)
        self.assertEqual(self.client.get(inbox+'?message=1').status_code,200)
        self.assertEqual(nsw.head(inbox+'?message=1').status_code,200)
        self.assertEqual(self.rows('SELECT status FROM bf_warehouse_enquiries ORDER BY message_id'),
                         [dict(status='SENT'),dict(status='SENT')])
        detail=inbox+'?message=1'
        opened=nsw.get(detail)
        self.assertIn('Communication #1 · OPENED',opened.text)
        self.assertIn('OPENED',client.get(url+'?status=OPENED').text)
        self.assertNotIn('Second job',client.get(url+'?status=OPENED').text)
        changes=inbox+'/1/status'
        self.assertEqual(self.send(qld,changes,dict(version=2,status='ACTIONED')).status_code,403)
        self.assertEqual(self.send(nsw,changes,dict(version=2,status='OTHER')).status_code,400)
        self.assertEqual(self.send(nsw,changes,dict(version=2,status='REFERRED')).status_code,200)
        self.assertEqual(self.send(nsw,changes,dict(version=2,status='CLOSED')).status_code,409)
        self.assertEqual(self.send(nsw,changes,dict(version=3,status='ACTIONED')).status_code,200)
        self.message(nsw,detail,'Dock inspection complete')
        self.assertIn('Dock inspection complete', client.get(CLIENT+'/NSW?message=1').text)
        self.assertNotIn('Dock inspection complete', client.get(CLIENT+'/NSW?message=2').text)
        self.message(client,CLIENT+'/NSW?message=1','Thanks for the update')
        self.assertIn('Thanks for the update',nsw.get(detail).text)
        self.assertEqual(self.send(nsw,changes,dict(version=4,status='CLOSED')).status_code,200)
        self.assertIn('Communication #1 · CLOSED',client.get(CLIENT+'/NSW?message=1').text)
        self.assertEqual(self.rows('SELECT status FROM bf_warehouse_status_history ORDER BY id'),
                         [dict(status=s) for s in ('OPENED','REFERRED','ACTIONED','CLOSED')])
        self.assertNotIn('Inspect the loading dock',other.get(url).text)
        from binflow_warehouse_messages import migrate
        with sqlite3.connect(self.path) as conn:
            migrate(conn)
        self.assertEqual(len(self.rows('SELECT * FROM bf_warehouse_enquiries')),2)

    def test_login_validation_and_revocation(self):
        aid = self.account('limited-client')
        anon = self.app.test_client()
        login = anon.get(CLIENT+'/QLD').location
        self.assertIn('/binflow/client/login', login)
        anon.get(login)
        chat = self.send(anon,login,dict(username='limited-client',password='sample-password'))
        self.assertIn('Message to QLD warehouse', chat.text)
        key = re.search(r'name="submission_key" value="([0-9a-f]+)"', chat.text)[1]
        for body in ('   ', 'x'*4001):
            self.assertEqual(self.send(anon,CLIENT+'/QLD',dict(body=body,submission_key=key)).status_code,400)
        self.assertEqual(anon.post(CLIENT+'/QLD',data=dict(body='no csrf',submission_key=key)).status_code,400)
        self.assertEqual(anon.get(CLIENT+'/WA').status_code,404)
        self.assertFalse(self.rows('SELECT id FROM bf_warehouse_messages'))
        with sqlite3.connect(self.path) as conn:
            conn.execute('INSERT INTO bf_client_permissions VALUES(?,?)',(aid,'[]'))
        self.assertEqual(anon.get(CLIENT).status_code,403)
        self.assertEqual(self.send(anon,CLIENT+'/NSW',dict(body='blocked',submission_key=key)).status_code,403)
