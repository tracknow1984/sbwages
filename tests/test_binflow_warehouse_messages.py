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
