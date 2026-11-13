import json
import re
import sqlite3
import unittest
from datetime import date

from werkzeug.security import generate_password_hash

from binflow_site_requests import migrate
from binflow_collections import profile_rows
from binflow_route_calendar import clients as route_clients, due
import test_binflow_client_permissions as fixtures


class SiteRequestTests(unittest.TestCase):
    setUp = fixtures.PortalPermissionTests.setUp
    tearDown = fixtures.PortalPermissionTests.tearDown
    rows = fixtures.PortalPermissionTests.rows
    post = fixtures.PortalPermissionTests.post
    login = fixtures.PortalPermissionTests.login
    account = fixtures.PortalPermissionTests.account
    visit = fixtures.PortalPermissionTests.visit
    send = fixtures.PortalPermissionTests.send
    directory = '/binflow/client/profiles'
    approvals = '/binflow/client-profiles/approvals'

    def form(self, portal, **changes):
        page = portal.get(self.directory+'/new')
        self.assertIn('Submit for approval', page.text)
        token = re.search(r'name="request_key" value="([^"]+)"', page.text)[1]
        return dict(dict(request_key=token, customer_site='New collection site', address='8 Test Street',
                         state='NSW', client_id='new', client_name='Textile Retail', schedule='Weekly on Monday'),
                    **changes)

    def planner_contains(self, name):
        with sqlite3.connect(self.path) as conn:
            conn.row_factory = sqlite3.Row
            return (any(p['name']==name for p in profile_rows(conn)), any(p['name']==name for p in route_clients(conn)))

    def test_from_request_to_approval_and_both_schedulers(self):
        aid = self.account('BlockTexx1')
        portal = self.visit('blocktexx1')
        form = self.form(portal)
        self.assertIn('Add client', portal.get(self.directory).text)
        for _ in range(2):
            result = self.send(portal, self.directory+'/new', form)
            self.assertIn('Pending approval', result.text)
        self.assertEqual(len(self.rows('SELECT id FROM bf_client_site_requests')), 1)
        self.assertFalse(self.rows('SELECT id FROM bf_clients WHERE name=?', (form['client_name'],)))
        self.assertEqual(self.planner_contains(form['customer_site']), (False,False))
        self.assertIn('awaiting scheduling approval', self.client.get('/admin/bin-management').text)
        self.assertIn('Weekly on Monday', self.client.get(self.approvals).text)
        self.assertEqual(self.client.post(self.approvals+'/1',data={'decision':'approved'}).status_code,400)
        self.assertEqual(self.post(self.approvals+'/1',{'decision':'approved'}).status_code,200)
        self.assertEqual(self.post(self.approvals+'/1',{'decision':'approved'}).status_code,409)
        self.assertEqual(self.planner_contains(form['customer_site']), (True,True))
        status = self.rows('SELECT status,reviewed_by,profile_id FROM bf_client_site_requests')[0]
        self.assertEqual((status['status'],status['reviewed_by']), ('approved','SB Empire'))
        new = self.rows('SELECT location_id,schedule FROM bf_client_site_profiles WHERE id=?',(status['profile_id'],))[0]
        self.assertEqual(new['schedule'],form['schedule'])
        self.assertEqual(self.rows('SELECT site_id FROM bf_client_site_scope WHERE account_id=?',(aid,)),[{'site_id':new['location_id']}])
        self.assertIn('Approved for scheduling', portal.get(self.directory).text)
        self.assertEqual(len(self.rows('SELECT id FROM bf_jobs')),0)

    def test_decline_duplicate_validation_and_access(self):
        self.account('blocktexx1')
        self.account('directory-reader',custom_permissions='yes',permission=['profiles.view'])
        portal, other = self.visit('blocktexx1'), self.visit('directory-reader')
        form = self.form(portal,client_id='1',customer_site='Additional location')
        self.assertEqual(self.send(portal,self.directory+'/new',{**form,'address':''}).status_code,400)
        self.assertEqual(self.send(portal,self.directory+'/new',form).status_code,200)
        fresh = self.form(portal,client_id='1',customer_site=form['customer_site'])
        self.assertEqual(self.send(portal,self.directory+'/new',fresh).status_code,400)
        self.assertNotIn(form['customer_site'], other.get(self.directory).text)
        self.assertEqual(self.send(other,self.directory+'/new',form).status_code,403)
        self.assertNotIn('Approve for scheduling',self.send(portal,self.approvals+'/1',{'decision':'approved'}).text)
        with sqlite3.connect(self.path) as conn:
            for role in ('driver','manager'):
                conn.execute("""INSERT INTO users(role,first_name,last_name,email,username,password_hash,business_role,working_state)
                    VALUES('employee','Test',?,?,?,?,?,'NSW')""",
                    (role,role+'@example.invalid',role,generate_password_hash('test-admin-password'),role))
        self.post('/logout')
        self.login('employee','driver')
        self.assertEqual(self.post(self.approvals+'/1',{'decision':'approved'}).status_code,403)
        self.post('/logout')
        self.login('employee','manager')
        self.assertIn(form['customer_site'], self.client.get(self.approvals).text)
        self.assertEqual(self.post(self.approvals+'/1',{'decision':'declined','reason':'Please confirm access.'}).status_code,200)
        after = portal.get(self.directory).text
        self.assertIn('Declined',after)
        self.assertIn('Please confirm access.',after)
        self.assertEqual(self.planner_contains(form['customer_site']),(False,False))
        self.assertEqual(self.send(portal,self.directory+'/new',fresh).status_code,200)
        self.assertEqual(self.post(self.approvals+'/2',{'decision':'approved'}).status_code,200)
        self.assertEqual(self.planner_contains(form['customer_site']),(True,True))

    def test_approved_sites_never_generate_backdated_visits(self):
        p = dict(cadence='weekly',days=[0,4],available_from='2026-11-12')
        self.assertEqual(due(p,date(2026,11,2)),[])
        self.assertEqual(due(p,date(2026,11,9)),[4])
        self.assertEqual(due(p,date(2026,11,16)),[0,4])
        p.update(days=[],available_from='2026-11-14')
        self.assertEqual(due(p,date(2026,11,9)),[])
        self.assertEqual(due(p,date(2026,11,16)),[None])

    def test_existing_permissions_upgraded_once_for_named_account(self):
        aid = self.account('blocktexx1',custom_permissions='yes',permission=['bookings.view'])
        other = self.account('other',custom_permissions='yes',permission=['bookings.view'])
        with sqlite3.connect(self.path) as conn:
            conn.row_factory = sqlite3.Row
            conn.execute("DELETE FROM bf_portal_migrations WHERE id='blocktexx1-site-submissions-v1'")
            migrate(conn,'today')
            rights = lambda key: set(json.loads(conn.execute('SELECT rights FROM bf_client_permissions WHERE account_id=?',(key,)).fetchone()[0]))
            self.assertEqual(rights(aid),{'bookings.view','profiles.view','profiles.add'})
            self.assertEqual(rights(other),{'bookings.view'})
            conn.execute("UPDATE bf_client_permissions SET rights='[]' WHERE account_id=?",(aid,))
            migrate(conn,'tomorrow')
            self.assertEqual(rights(aid),set())
