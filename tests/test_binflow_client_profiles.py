import base64
import gzip
import json
import sqlite3
import unittest
from werkzeug.security import generate_password_hash
from test_binflow import BinFlowTests
from binflow_client_profiles import FIELDS, import_profiles, for_location, seed_locations, validate


def encoded(rows, ident='test-contacts-v1'):
    return base64.b64encode(gzip.compress(json.dumps({'id':ident,'source_label':'Test contacts','rows':rows}).encode())).decode()


def row(**updates):
    values = dict(group_name='Tier 1',customer_site='Alpha QLD',address='4 Updated Road',suburb='Test suburb',
                  state='QLD',postcode='0400',contact_name='Pat / Alex',contact_email='pat@example.com | alex@example.com',
                  contact_number='0400 000 000 | 0400 000 001',schedule='Last Tuesday of the month',
                  asic_required='unknown',contact_verified='no',source_row=5)
    values.update(updates)
    return values


class ClientProfileTests(unittest.TestCase):
    setUp = BinFlowTests.setUp
    tearDown = BinFlowTests.tearDown
    rows = BinFlowTests.rows
    post = BinFlowTests.post
    login = BinFlowTests.login
    job = BinFlowTests.job

    def connection(self):
        conn=sqlite3.connect(self.path)
        conn.row_factory=sqlite3.Row
        conn.execute('PRAGMA foreign_keys=ON')
        self.addCleanup(conn.close)
        return conn

    def test_import_fields_and_preserve_operations_repeat_and_edits(self):
        self.job(deliver=(1,))
        before={t:self.rows('SELECT * FROM '+t) for t in ('bf_bins','bf_jobs','bf_job_bins')}
        conn=self.connection()
        summary,changed=import_profiles(conn,encoded([row()]),'2026-10-23T12:00:00+10:00')
        self.assertTrue(changed);self.assertEqual(summary['linked'],1)
        p=dict(for_location(conn,3))
        for k,_,_ in FIELDS:self.assertEqual(p[k],row()[k])
        self.assertEqual(conn.execute('SELECT address FROM bf_locations WHERE id=3').fetchone()[0],'4 Updated Road')
        for table,values in before.items():self.assertEqual(self.rows('SELECT * FROM '+table),values)
        conn.execute("UPDATE bf_client_site_profiles SET contact_name='Reviewed by office' WHERE id=?",(p['id'],));conn.commit()
        self.assertFalse(import_profiles(conn,encoded([row()]),'later')[1])
        self.assertEqual(for_location(conn,3)['contact_name'],'Reviewed by office')
        self.assertEqual(self.rows('SELECT COUNT(*) AS n FROM bf_client_profile_imports')[0]['n'],1)

    def test_missing_details_and_same_address_different_site_are_not_guessed(self):
        conn=self.connection()
        summary,_=import_profiles(conn,encoded([row(customer_site='Unknown customer',state='',address='',postcode='',source_row=5),
            row(customer_site='Different department',address='3 Client Road',source_row=6),
            row(customer_site='Beta QLD',contact_name='Beta contact',source_row=7)]),'now')
        self.assertEqual((summary['profiles'],summary['linked'],summary['awaiting_link']),(3,1,2))
        self.assertEqual(summary['missing_state'],1)
        self.assertEqual(for_location(conn,4)['contact_name'],'Beta contact')
        self.assertEqual(for_location(conn,3)['contact_name'],'')
        self.assertFalse(self.rows("SELECT 1 FROM bf_clients WHERE name='Tier 1'"))
        self.assertEqual(len(self.rows('SELECT * FROM bf_locations')),5)

    def test_invalid_import_is_atomic(self):
        conn=self.connection()
        before=self.rows('SELECT * FROM bf_client_site_profiles')
        with self.assertRaises(ValueError):import_profiles(conn,encoded([row(),row(source_row=6,postcode='ABC')]),'now')
        self.assertEqual(before,self.rows('SELECT * FROM bf_client_site_profiles'))
        self.assertFalse(self.rows('SELECT * FROM bf_client_profile_imports'))
        self.assertEqual(self.rows('SELECT address FROM bf_locations WHERE id=3')[0]['address'],'3 Client Road')
        with self.assertRaises(ValueError):import_profiles(conn,encoded([row(),row()]),'now')
        self.assertEqual(before,self.rows('SELECT * FROM bf_client_site_profiles'))

    def test_editor_all_fields_and_stale_form_and_location_sync(self):
        conn=self.connection();import_profiles(conn,encoded([row()]),'now')
        p=dict(for_location(conn,3));url=f'/binflow/client-profiles/{p["id"]}'
        page=self.client.get(url);self.assertEqual(page.status_code,200)
        for k,label,_ in FIELDS:self.assertIn(f'name="{k}"'.encode(),page.data)
        result=self.post(url,{**row(), 'version':p['version'],'contact_verified':'yes','asic_required':'yes','contact_name':'New contact'})
        self.assertIn(b'Client profile saved.',result.data)
        saved=dict(for_location(conn,3));self.assertEqual(saved['contact_verified'],'yes')
        self.assertEqual(self.post(url,{**row(),'version':p['version']}).status_code,400)
        self.assertEqual(for_location(conn,3)['contact_name'],'New contact')
        self.post('/binflow/locations/3/edit',{'name':'Revised name','state':'QLD','address':'99 New Street'})
        self.assertEqual(for_location(conn,3)['customer_site'],'Revised name')
        self.assertEqual(for_location(conn,3)['contact_name'],'New contact')
        self.assertEqual(self.client.post(url,data={'version':saved['version']}).status_code,400)
        self.assertIn(b'Post Code',self.client.get('/binflow/client-profiles').data)
        self.assertNotIn(b'Alpha NSW',self.client.get('/binflow/client-profiles?q=New+contact').data)

    def test_link_pending_profile_and_guard_state_and_existing_maintained_profile(self):
        conn=self.connection();import_profiles(conn,encoded([row(customer_site='Alpha revised')]),'now')
        p=dict(conn.execute('SELECT * FROM bf_client_site_profiles WHERE source_key IS NOT NULL').fetchone())
        url=f'/binflow/client-profiles/{p["id"]}'
        self.assertIsNone(p['location_id'])
        # Wrong state does not remove or alter its placeholder profile.
        self.assertEqual(self.post(url,{**row(customer_site='Alpha revised'),'version':p['version'],'location_id':5}).status_code,400)
        self.assertIsNotNone(for_location(conn,5))
        self.assertIn(b'Client profile saved.',self.post(url,{**row(customer_site='Alpha revised'),'version':p['version'],'location_id':3}).data)
        self.assertEqual(for_location(conn,3)['id'],p['id'])
        self.assertEqual(self.rows('SELECT name FROM bf_locations WHERE id=3')[0]['name'],'Alpha revised')
        self.job(deliver=(1,))
        current=for_location(conn,3)
        self.assertEqual(self.post(url,{**row(),'state':'NSW','version':current['version']}).status_code,400)
        self.assertEqual(for_location(conn,3)['state'],'QLD')

    def test_admin_only_and_escaped_contacts(self):
        conn=self.connection();import_profiles(conn,encoded([row(contact_name='<script>alert(1)</script>')]),'now')
        html=self.client.get('/binflow/client-profiles').data
        self.assertIn(b'&lt;script&gt;',html);self.assertNotIn(b'<script>alert',html)
        conn.execute("INSERT INTO users(role,first_name,last_name,email,username,password_hash) VALUES('employee','Test','Driver','driver@example.com','driver',?)",(generate_password_hash('test-admin-password'),));conn.commit()
        self.post('/logout')
        self.login('employee','driver')
        self.assertEqual(self.client.get('/binflow/client-profiles').status_code,403)
        pid=for_location(conn,3)['id']
        self.assertEqual(self.client.get(f'/binflow/client-profiles/{pid}').status_code,403)

    def test_validation_keeps_multiple_contacts_and_leading_zero(self):
        value=validate(row())
        self.assertEqual(value['postcode'],'0400')
        self.assertEqual(value['contact_email'],row()['contact_email'])
        for key,value in [('asic_required','maybe'),('contact_verified','maybe'),('state','ZZ'),('postcode','123')]:
            with self.subTest(key=key),self.assertRaises(ValueError):validate(row(**{key:value}))

    def test_add_client_creates_account_site_and_complete_profile_atomically(self):
        page=self.client.get('/binflow/client-profiles/new')
        self.assertEqual(page.status_code,200)
        for k,_,_ in FIELDS:self.assertIn(f'name="{k}"'.encode(),page.data)
        values={**row(customer_site='New South Site'), 'client_id':'new', 'client_name':'New Business'}
        response=self.post('/binflow/client-profiles/new',values)
        self.assertIn(b'Client site profile added.',response.data)
        account=self.rows("SELECT id FROM bf_clients WHERE name='New Business'")[0]['id']
        loc=self.rows('SELECT * FROM bf_locations WHERE client_id=?',(account,))[0]
        profile=self.rows('SELECT * FROM bf_client_site_profiles WHERE location_id=?',(loc['id'],))[0]
        for k,_,_ in FIELDS:self.assertEqual(profile[k],values[k])
        self.assertEqual(loc['address'],values['address'])
        self.assertEqual(profile['updated_by'],'SB Empire')
        counts=[len(self.rows('SELECT * FROM '+t)) for t in ('bf_clients','bf_locations','bf_client_site_profiles')]
        self.assertEqual(self.post('/binflow/client-profiles/new',values).status_code,400)
        self.assertEqual(counts,[len(self.rows('SELECT * FROM '+t)) for t in ('bf_clients','bf_locations','bf_client_site_profiles')])

    def test_add_existing_client_site_and_validate_before_saving(self):
        start=len(self.rows('SELECT * FROM bf_clients'))
        response=self.post('/binflow/client-profiles/new',{**row(customer_site='Alpha Second Site'),'client_id':'1'})
        self.assertIn(b'Client site profile added.',response.data)
        self.assertEqual(len(self.rows('SELECT * FROM bf_clients')),start)
        self.assertEqual(self.rows("SELECT client_id FROM bf_locations WHERE name='Alpha Second Site'")[0]['client_id'],1)
        for patch in ({'state':''},{'address':''},{'postcode':'123'},{'client_id':'9999'},{'client_id':'bad'}):
            response=self.post('/binflow/client-profiles/new',{**row(customer_site='Invalid Site'),'client_name':'No orphan account','client_id':'new',**patch})
            self.assertEqual(response.status_code,400)
            self.assertFalse(self.rows("SELECT 1 FROM bf_clients WHERE name='No orphan account'"))
            self.assertFalse(self.rows("SELECT 1 FROM bf_locations WHERE name='Invalid Site'"))
        self.assertEqual(self.client.post('/binflow/client-profiles/new',data=row()).status_code,400)

    def test_combined_filters_search_and_pagination_preserve_choices(self):
        conn=self.connection()
        import_profiles(conn,encoded([row(customer_site=f'Test Site {n:02d}',source_row=n+5,
            group_name='Priority' if n<12 else 'Standard',asic_required='yes' if n<12 else 'unknown',
            contact_verified='yes' if n<12 else 'no',schedule='Weekly' if n<12 else 'Adhoc') for n in range(15)]),'now')
        url='/binflow/client-profiles?q=Test&group=Priority&state=QLD&schedule=Weekly&asic=yes&verified=yes&status=unlinked&per_page=10'
        page=self.client.get(url)
        self.assertEqual(page.status_code,200)
        self.assertIn(b'of 12 matching',page.data)
        self.assertIn(b'Test Site 00',page.data);self.assertNotIn(b'Test Site 10',page.data)
        from html import unescape
        import re
        next_url=unescape(re.search(r'href="([^"]+)">Next',page.data.decode())[1])
        for term in ('group=Priority','state=QLD','schedule=Weekly','asic=yes','verified=yes','status=unlinked','q=Test','per_page=10','page=2'):
            self.assertIn(term,next_url)
        next_page=self.client.get(next_url)
        self.assertIn(b'Test Site 10',next_page.data);self.assertNotIn(b'Test Site 00',next_page.data)
        self.assertIn(b'Page 2 of 2',next_page.data)
        self.assertIn(b'No profiles match',self.client.get(url.replace('q=Test','q=does-not-exist')).data)
        self.assertIn(b'Page 2 of 2',self.client.get(url+'&page=999').data)
        self.assertEqual(self.client.get('/binflow/client-profiles?page=bad&per_page=oops').status_code,200)

    def test_missing_and_linked_filters_and_new_route_access(self):
        conn=self.connection()
        import_profiles(conn,encoded([row(customer_site='Missing Site',state='',address='',postcode='',group_name='',schedule='')]),'now')
        page=self.client.get('/binflow/client-profiles?state=__blank__&group=__blank__&schedule=__blank__&status=missing&asic=unknown')
        self.assertIn(b'Missing Site',page.data);self.assertNotIn(b'Alpha QLD',page.data)
        linked=self.client.get('/binflow/client-profiles?status=linked')
        self.assertNotIn(b'Missing Site',linked.data);self.assertIn(b'Alpha QLD',linked.data)
        conn.execute("INSERT INTO users(role,first_name,last_name,email,username,password_hash) VALUES('employee','Test','Driver','other@example.com','other',?)",(generate_password_hash('test-admin-password'),));conn.commit()
        self.post('/logout');self.login('employee','other')
        self.assertEqual(self.client.get('/binflow/client-profiles/new').status_code,403)
        self.assertEqual(self.post('/binflow/client-profiles/new',row()).status_code,403)
