import sqlite3
import unittest
import test_app
from app import create_app, SCHEMA

class DeductionTests(unittest.TestCase):
    setUp=test_app.AppTests.setUp
    tearDown=test_app.AppTests.tearDown
    query=test_app.AppTests.query
    post=test_app.AppTests.post
    login=test_app.AppTests.login
    add=test_app.AppTests.add
    last_week=test_app.AppTests.last_week
    day_form=test_app.AppTests.day_form
    prepare_payment_sheet=test_app.AppTests.prepare_payment_sheet

    def test_deduction_validation_and_staff_net_pay(self):
        _,_,sheet,_=self.prepare_payment_sheet()
        route=f'/admin/timesheets/{sheet["id"]}/payment'
        data={'cash_amount':'100','transfer_amount':'134','deductions_amount':'50','expected_total':'28400'}
        for invalid in ('-1','NaN','0.001','285','49.99'):
            self.post(route,data | {'deductions_amount':invalid})
        self.assertEqual(len(self.query('SELECT * FROM payments')),0)
        response=self.post(route,data)
        self.assertIn(b'$234.00',response.data)
        self.assertIn(b'Deductions',response.data)
        self.assertEqual(self.query('SELECT deduction_cents FROM payments')[0][0],5000)
        self.post('/logout');self.login('alex','test-staff-password')
        self.assertIn(b'$234.00',self.client.get('/employee/payslips').data)
        self.assertIn(b'$50.00',self.client.get('/employee/dashboard').data)

    def test_legacy_payment_migration_preserves_snapshot(self):
        _,_,sheet,_=self.prepare_payment_sheet()
        self.post(f'/admin/timesheets/{sheet["id"]}/payment',{'cash_amount':'100','transfer_amount':'184','expected_total':'28400'})
        record=dict(self.query('SELECT * FROM payments')[0]);record.pop('deduction_cents')
        definition=SCHEMA.split('CREATE TABLE IF NOT EXISTS payments (',1)[1].split(');',1)[0]
        definition=definition.replace('deduction_cents INTEGER NOT NULL DEFAULT 0 CHECK(deduction_cents>=0),','').replace('+deduction_cents','')
        with sqlite3.connect(self.path) as conn:
            conn.execute('DROP TABLE payments')
            conn.execute('CREATE TABLE payments ('+definition+')')
            conn.execute('INSERT INTO payments ('+','.join(record)+') VALUES ('+','.join('?' for _ in record)+')',tuple(record.values()))
        create_app(dict(self.app.config))
        migrated=dict(self.query('SELECT * FROM payments')[0])
        self.assertEqual(migrated.pop('deduction_cents'),0)
        self.assertEqual(migrated,record)
        create_app(dict(self.app.config))
        self.assertEqual(len(self.query('SELECT * FROM payments')),1)
