import hashlib
import io
import json
from unittest import TestCase
from unittest.mock import patch
from pypdf import PdfReader
from reportlab.pdfgen import canvas
import employee_induction as induction
import test_app as fixtures


class InductionTests(TestCase):
    setUp = fixtures.AppTests.setUp
    tearDown = fixtures.AppTests.tearDown
    post = fixtures.AppTests.post
    login = fixtures.AppTests.login
    add = fixtures.AppTests.add
    query = fixtures.AppTests.query

    def test_publication_signing_access_and_new_employee(self):
        self.add()
        pdf = io.BytesIO()
        c = canvas.Canvas(pdf)
        c.drawString(50,700,'Test company handbook');c.save()
        data = pdf.getvalue()
        contacts='[["Example Manager","0400 000 000"]]'
        digest = hashlib.sha256(data+b'\0'+contacts.encode()+b'\0'+induction.TERMS.encode()).hexdigest()
        route='/induction/install-edition'
        def install(body,token):
            with self.client.session_transaction() as ss:
                csrf=ss['csrf']
            return self.client.post(route,data={'csrf':csrf,'contacts':contacts,
                'handbook':(io.BytesIO(body),'approved.pdf')},headers={'Authorization':token})
        key='Bearer fixture'
        with patch.object(induction,'PACKAGE_DIGEST',digest),patch.object(induction,'PUBLISH_KEY_DIGEST',hashlib.sha256(key.encode()).hexdigest()):
            self.assertEqual(install(data,'wrong').status_code,403)
            self.assertEqual(install(b'wrong',key).status_code,400)
            self.assertEqual(install(data,key).status_code,200)
            self.assertEqual(install(data,key).status_code,200)
        self.assertEqual(len(self.query('SELECT * FROM handbook_editions')),1)
        self.assertIn(b'Pending',self.client.get('/admin/staff').data)
        self.post('/logout')
        self.assertEqual(self.client.get('/employee/induction/handbook.pdf').status_code,302)
        self.login('alex','test-staff-password')
        self.assertIn(b'Read and sign',self.client.get('/employee/dashboard').data)
        self.assertEqual(self.client.get('/employee/induction/handbook.pdf').data,data)
        values={'version':induction.EDITION,'signature':'Alex Example','consent':'yes',
                'ack':[row[0] for row in json.loads(induction.TERMS)['items']]}
        route='/employee/induction'
        self.assertEqual(self.client.post(route,data=values).status_code,400)
        for key,value in [('ack',['fitness']),('consent',''),('signature','Someone Else')]:
            self.post(route,dict(values,**{key:value}))
            self.assertFalse(self.query('SELECT * FROM handbook_signatures'))
        self.assertEqual(self.post(route,dict(values,version='outdated')).status_code,409)
        self.assertIn(b'Induction signed',self.post(route,values).data)
        self.post(route,values)
        self.assertEqual(len(self.query('SELECT * FROM handbook_signatures')),1)
        signed='/employee/induction/records/1/signed.pdf'
        result=self.client.get(signed)
        self.assertEqual(result.status_code,200)
        doc=PdfReader(io.BytesIO(result.data))
        self.assertEqual(len(doc.pages),2)
        self.assertIn('Test company handbook',doc.pages[0].extract_text())
        self.assertIn('Alex Example',doc.pages[1].extract_text())
        self.assertIn('unsafe behaviour',doc.pages[1].extract_text())
        self.post('/logout');self.login('admin','test-admin-password','admin')
        self.assertIn(b'Signed handbook',self.client.get('/admin/staff').data)
        self.assertEqual(self.client.get(signed).status_code,200)
        self.add('jordan')
        self.post('/logout');self.login('jordan','test-staff-password')
        self.assertIn(b'Sign induction',self.client.get(route).data)
        self.assertEqual(self.client.get(signed).status_code,404)

    def test_contractor_notes_and_previous_signed_handbook(self):
        import sqlite3
        person, _ = self.add()
        self.add('jordan')
        old_pdf = io.BytesIO()
        c=canvas.Canvas(old_pdf)
        c.drawString(50,700,'Previously issued handbook');c.save()
        new_pdf = io.BytesIO()
        c=canvas.Canvas(new_pdf)
        c.drawString(50,700,'Current contractor guidance');c.save()
        old=induction.PREVIOUS_EQUIVALENT_EDITION
        new=induction.EDITION
        with sqlite3.connect(self.path) as conn:
            conn.execute('INSERT INTO handbook_editions VALUES(?,?,?,?)',(old,old_pdf.getvalue(),'[]',induction.TERMS))
            conn.execute("UPDATE users SET business_role='labour', extra_business_roles='contractor' WHERE id=?",(person['id'],))
            conn.execute('INSERT INTO handbook_signatures(user_id,version,signature,username,signed_at) VALUES(?,?,?,?,?)',
                         (person['id'],old,'Alex Example','alex','2026-11-11T03:00:00+00:00'))
        self.post('/logout');self.login('alex','test-staff-password')
        self.assertEqual(self.client.get('/employee/induction/handbook.pdf').data,old_pdf.getvalue())
        with sqlite3.connect(self.path) as conn:
            conn.execute('INSERT INTO handbook_editions VALUES(?,?,?,?)',(new,new_pdf.getvalue(),'[]',induction.TERMS))
        page=self.client.get('/employee/induction')
        self.assertIn(b'Contractor hours and insurance',page.data)
        self.assertIn(('Signed edition '+old).encode(),page.data)
        self.assertNotIn(b'>Sign induction<',page.data)
        self.assertEqual(self.client.get('/employee/induction/handbook.pdf').data,new_pdf.getvalue())
        self.assertIn(b'Contractor hours.',self.client.get('/employee/timesheet').data)
        signed=self.client.get('/employee/induction/records/1/signed.pdf')
        self.assertIn('Previously issued handbook',PdfReader(io.BytesIO(signed.data)).pages[0].extract_text())
        self.post('/logout');self.login('jordan','test-staff-password')
        self.assertIn(b'>Sign induction<',self.client.get('/employee/induction').data)
        self.assertNotIn(b'Contractor hours.',self.client.get('/employee/timesheet').data)
        self.post('/logout');self.login('admin','test-admin-password','admin')
        self.assertIn(b'Signed handbook',self.client.get('/admin/staff').data)
