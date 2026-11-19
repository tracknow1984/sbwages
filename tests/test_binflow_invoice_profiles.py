import re
import unittest
from unittest.mock import patch
from uuid import uuid4

import binflow_invoice_profiles as profiles
import binflow_xero as xero
import test_binflow_auto_invoices as auto_tests
from test_binflow_interstate import CAL, data
from test_binflow_xero import TENANT, CONTACT, connected_organisation


class InvoiceProfileTests(unittest.TestCase):
    setUp = auto_tests.AutoInvoiceTests.setUp
    tearDown = auto_tests.AutoInvoiceTests.tearDown
    rows = auto_tests.AutoInvoiceTests.rows
    login = auto_tests.AutoInvoiceTests.login
    post = auto_tests.AutoInvoiceTests.post
    connect = auto_tests.AutoInvoiceTests.connect
    connection = auto_tests.AutoInvoiceTests.connection
    account = auto_tests.AutoInvoiceTests.account
    link = auto_tests.AutoInvoiceTests.link

    def book(self, number, **fields):
        form = data(**fields)
        self.post(CAL+'/new', form)
        bid = self.rows('SELECT id FROM bf_interstate_bookings WHERE request_key=?',(form['request_key'],))[0]['id']
        self.post(f'{CAL}/{bid}/lock',dict(version=1))
        self.post(f'{CAL}/{bid}/purchase-order',dict(po_revision=0,po_number=number))
        return f'{CAL}/{bid}/invoice-draft'

    def setup_form(self, action, aid):
        review = self.post(action,dict(tenant=TENANT)).request.path
        page = self.client.get(review+'?billing_client='+str(aid))
        version = re.search('name="profile_revision" value="([^"]+)"',page.text)[1]
        return review+'/billing-profile',dict(tenant=TENANT,billing_client=aid,profile_revision=version,
            contact=CONTACT,account_code='200',tax_code='OUTPUT',base='240',fuel='12.5',
            remember_client='yes',remember_rate='yes')

    @connected_organisation
    def test_two_bookings_one_profile_and_one_xero_draft_per_booking(self):
        self.connect()
        aid = self.account('billing-client')
        first = self.book('PO-ONE')
        second = self.book('PO-TWO')
        path,fields = self.setup_form(first,aid)
        count = []
        def create(app,conn,path,method,payload,**kwargs):
            invoice = payload['Invoices'][0]
            count.append(invoice['Reference'])
            self.assertEqual((invoice['Status'],invoice['Contact']['ContactID'],kwargs['tenant_id']),('DRAFT',CONTACT,TENANT))
            self.assertEqual([line['UnitAmount'] for line in invoice['LineItems']],[240,30])
            return {'Invoices':[dict(invoice,InvoiceID=str(uuid4()),InvoiceNumber='INV-TEST',Total=297)]}
        with patch('binflow_xero.api',side_effect=create):
            self.assertIn('Saved as a draft in Xero',self.post(path,fields).text)
            self.post(path,fields)
            self.assertIn('Saved as a draft in Xero',self.post(second,dict(tenant=TENANT)).text)
            self.post(second,dict(tenant=TENANT))
        self.assertEqual(count,['PO-ONE','PO-TWO'])
        self.assertEqual(self.rows('SELECT rental_account FROM bf_invoice_defaults'),[dict(rental_account=None)])
        self.assertIn('Freight test bills to',self.client.get('/binflow/setup/invoice-billing').text)
        different_route = self.book('PO-OTHER',origin='SYD')
        self.assertIn('Agreed freight price',self.post(different_route,dict(tenant=TENANT)).text)
        other_customer = self.book('PO-OTHER-CUSTOMER',client_name='Different customer')
        self.assertIn('Choose billing client',self.post(other_customer,dict(tenant=TENANT)).text)
        self.assertEqual(len(self.rows('SELECT * FROM bf_rental_invoices')),2)

    @connected_organisation
    def test_profile_survives_failed_connection_and_uses_frozen_booking_price(self):
        self.connect()
        aid = self.account('billing-client')
        action = self.book('PO-RETRY')
        path,fields = self.setup_form(action,aid)
        with patch('binflow_xero.verify_draft_connection',side_effect=xero.XeroError('Offline')):
            self.assertIn('Billing profile saved',self.post(path,fields).text)
        self.assertEqual(len(self.rows('SELECT * FROM bf_invoice_contacts')),1)
        self.assertFalse(self.rows('SELECT * FROM bf_rental_invoices'))
        admin = self.client.get(action.rsplit('/',1)[0])
        self.assertIn('Create draft in Xero',admin.text)
        with self.connection() as conn:
            conn.execute('UPDATE bf_recurring_freight_terms SET unit_cents=99000')
        did=self.rows("SELECT id FROM bf_auto_drafts WHERE kind='Transport'")[0]['id']
        review=self.client.get('/binflow/invoices/review/'+did).text
        self.assertNotIn('Save billing profile',review)
        self.assertIn('$297.00',review)
        self.assertFalse(self.rows('SELECT * FROM bf_auto_claims'))

    def test_legacy_settings_tenant_isolation_and_hourly_pricing(self):
        self.connect()
        aid=self.account('billing-client')
        with self.connection() as conn:
            conn.execute('INSERT INTO bf_billing_defaults VALUES(1,?,?,?,?)',(TENANT,'200','OUTPUT',aid))
            profiles.migrate(conn)
            profiles.migrate(conn)
            self.assertEqual(profiles.defaults(conn,TENANT)['rental_account'],aid)
            self.assertIsNone(profiles.defaults(conn,'other-organisation'))
            b=dict(portal='sbtx',job_type='internal',origin='BNE',destination='QLD',vehicle='RIGID',rigid_hours_units=500)
            conn.execute('INSERT INTO bf_recurring_freight_terms VALUES(?,?,?,?,?,?,?,?)',(aid,'sbtx','QLD','QLD','RIGID','internal',12500,1000))
            self.assertEqual(profiles.price_from_profile(conn,b,aid),(62500,1000))
            self.assertEqual(profiles.price_from_profile(conn,dict(b,rigid_hours_units=100),aid),(50000,1000))
            self.assertIsNone(profiles.price_from_profile(conn,dict(b,portal='binflow'),aid))
            conn.execute('INSERT INTO bf_rigid_hourly_rates VALUES(?,?)',(aid,13500))
            self.assertEqual(profiles.price_from_profile(conn,b,aid),(67500,0))
        self.assertIn('value="200" selected',self.client.get('/binflow/invoices/new').text)

    @connected_organisation
    def test_unlinked_manual_invoice_saves_contact_and_remembers_account_choices(self):
        self.connect()
        aid=self.account('manual-client')
        fields=dict(tenant=TENANT,client=aid,new_contact=CONTACT,category=2,draft_key=str(uuid4()),
                    po_number='PO-MANUAL',invoice_date='2026-11-19',due_date='2026-12-03',
                    account_code='200',tax_type='OUTPUT',description='Freight',quantity='1',unit_rate='200')
        self.post('/binflow/invoices/new',dict(fields,quantity='0'))
        self.assertFalse(self.rows('SELECT * FROM bf_invoice_contacts'))
        response=self.post('/binflow/invoices/new',fields)
        self.assertTrue(response.request.path.startswith('/binflow/rental-invoices/'))
        with self.connection() as conn:
            self.assertEqual(profiles.previous_manual_choices(conn,TENANT)[aid]['account_code'],'200')
            self.assertEqual(profiles.previous_manual_choices(conn,'other'),{})
        self.assertIn('data-account="200" data-tax="OUTPUT"', self.client.get('/binflow/invoices/new').text)
        self.assertEqual(self.rows('SELECT contact_id FROM bf_invoice_contacts'),[dict(contact_id=CONTACT)])

    @connected_organisation
    def test_invalid_and_stale_profile_forms_cannot_create_a_draft_or_change_saved_choices(self):
        self.connect()
        aid=self.account('billing-client')
        action=self.book('PO-SAFE')
        path,fields=self.setup_form(action,aid)
        for invalid in (dict(contact='not-in-directory'),dict(base='0'),dict(tenant='different-org')):
            with patch('binflow_xero.api') as remote:
                self.assertEqual(self.post(path,dict(fields,**invalid)).status_code,400)
                remote.assert_not_called()
            for table in ('bf_invoice_contacts','bf_invoice_defaults','bf_auto_transfer_terms','bf_rental_invoices'):
                self.assertFalse(self.rows('SELECT * FROM '+table))
        self.link(aid)
        with patch('binflow_xero.api') as remote:
            self.assertIn('Billing details changed',self.post(path,fields).text)
            remote.assert_not_called()
        self.assertFalse(self.rows('SELECT * FROM bf_invoice_defaults'))

    @connected_organisation
    def test_opt_out_keeps_single_load_pricing_and_setup_remembers_selected_client(self):
        self.connect()
        aid=self.account('one-off')
        first=self.book('PO-FLAT')
        path,values=self.setup_form(first,aid)
        values.pop('remember_rate')
        with patch('binflow_xero.verify_draft_connection',side_effect=xero.XeroError('Offline')):
            self.post(path,values)
        self.assertFalse(self.rows('SELECT * FROM bf_recurring_freight_terms'))
        second=self.book('PO-NEXT')
        page=self.post(second,dict(tenant=TENANT)).text
        self.assertIn('Agreed freight price',page)
        self.assertNotIn('name="contact"',page)
        self.assertNotIn('name="account_code"',page)
        self.assertNotIn('name="tax_code"',page)
        setup='/binflow/setup/invoice-billing'
        self.post(setup,dict(action='email',tenant=TENANT,account_id=aid,email='ops@example.com'))
        stored=self.client.get(setup+'?billing_client='+str(aid)).text
        self.assertIn('value="ops@example.com"',stored)
        self.assertIn('value="'+CONTACT+'" selected',stored)
