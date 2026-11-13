import io
import re
import unittest

from PIL import Image
from reportlab.pdfgen.canvas import Canvas
from werkzeug.datastructures import MultiDict

import test_app


class StaffDocumentBatchTests(unittest.TestCase):
    setUp = test_app.AppTests.setUp
    tearDown = test_app.AppTests.tearDown
    query = test_app.AppTests.query
    post = test_app.AppTests.post
    login = test_app.AppTests.login
    add = test_app.AppTests.add

    def upload_key(self, path):
        return re.search(r'name="upload_key" value="([^"]+)"',self.client.get(path).text)[1]

    def batch(self, uid, key, files, **fields):
        with self.client.session_transaction() as session:
            csrf=session['csrf']
        values=MultiDict(dict(csrf=csrf,upload_key=key,**fields))
        for filename, content in files:
            values.add('document',(io.BytesIO(content),filename))
        return self.client.post(f'/staff/{uid}/documents',data=values,follow_redirects=True)

    def documents(self):
        pdf=io.BytesIO()
        page=Canvas(pdf);page.drawString(30,700,'Forklift licence');page.save()
        image=io.BytesIO()
        Image.new('RGB',(70,40),'blue').save(image,'PNG')
        return [('Forklift_licence.pdf',pdf.getvalue()),('HR_licence.png',image.getvalue())]

    def test_office_employee_visibility_and_replay(self):
        member,_=self.add()
        other,_=self.add('jordan')
        uid=member['id']
        edit=f'/admin/staff/{uid}/edit'
        key=self.upload_key(edit)
        files=self.documents()
        self.assertIn('2 files added to Alex Example',self.batch(uid,key,files).text)
        self.batch(uid,key,files)
        rows=self.query('SELECT user_id,title,filename FROM documents ORDER BY id')
        self.assertEqual([(r['user_id'],r['title'],r['filename']) for r in rows],
                         [(uid,'Forklift licence',files[0][0]),(uid,'HR licence',files[1][0])])
        self.assertIn('HR_licence.png',self.client.get(edit).text)
        self.assertNotIn('HR_licence.png',self.client.get(f"/admin/staff/{other['id']}/edit").text)
        public=self.app.test_client()
        token=self.query('SELECT token FROM employee_badges WHERE user_id=?',(uid,))[0][0]
        self.assertNotIn('HR_licence.png',public.get('/verify/employee/'+token).text)
        self.assertEqual(public.get('/documents/1/file').status_code,403)
        self.post('/logout')
        self.login('jordan','test-staff-password')
        self.assertEqual(self.batch(uid,key,files).status_code,404)
        self.assertEqual(self.client.get('/documents/1/file').status_code,404)
        self.post('/logout')
        self.login('alex','test-staff-password')
        self.assertIn('Forklift licence',self.client.get('/employee/documents').text)
        self.assertEqual(self.client.get('/documents/1/file?download=1').data,files[0][1])
        key=self.upload_key('/employee/documents')
        self.batch(uid,key,[files[0]],title='Forklift renewal')
        self.assertEqual([r['title'] for r in self.query('SELECT title FROM documents ORDER BY id')],
                         ['Forklift licence','HR licence','Forklift renewal'])

    def test_invalid_batch_rolls_back_and_can_be_corrected(self):
        member,_=self.add()
        uid=member['id']
        key=self.upload_key(f'/admin/staff/{uid}/edit')
        files=self.documents()
        failure=self.batch(uid,key,[files[0],('bad.pdf',b'not pdf')])
        self.assertIn('No files were saved',failure.text)
        self.assertEqual(len(self.query('SELECT id FROM documents')),0)
        self.assertEqual(len(self.query('SELECT * FROM staff_document_batches')),0)
        self.assertIn('at least one',self.batch(uid,key,[]).text)
        self.assertIn('up to 10',self.batch(uid,key,[files[0]]*11).text)
        self.assertIn('2 files added',self.batch(uid,key,files).text)
