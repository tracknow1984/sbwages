import io
from unittest import TestCase

from PIL import Image
import test_app


class CompanyDocumentTests(TestCase):
    setUp = test_app.AppTests.setUp
    tearDown = test_app.AppTests.tearDown
    post = test_app.AppTests.post
    login = test_app.AppTests.login
    query = test_app.AppTests.query
    add = test_app.AppTests.add

    def test_folders_batches_and_access(self):
        folder = self.post('/admin/documents/folders', {'name': 'Vehicles'}).request.path
        self.assertEqual(self.post('/admin/documents/folders', {'name': 'vehicles'}).request.path, folder)
        with io.BytesIO() as photo:
            Image.new('RGB', (4, 4), 'blue').save(photo, format='PNG')
            png = photo.getvalue()

        def batch(key, items):
            return self.post(folder + '/upload', {
                'upload_key': key * 22,
                'document': [(io.BytesIO(data), name) for data, name in items]
            })
        items = [(png, 'service.png'), (png, 'insurance.png')]
        batch('a', [items[0], (b'bad', 'invalid.pdf')])
        self.assertEqual(len(self.query('SELECT id FROM company_documents')), 0)
        batch('b', items)
        batch('b', items)
        self.assertEqual(len(self.query('SELECT id FROM company_documents')), 2)
        self.assertIn(b'Quick view', self.client.get(folder).data)
        first = folder + '/files/1'
        self.assertEqual(self.client.get(first).data, png)
        self.assertIn('attachment', self.client.get(first + '?download=1').headers['Content-Disposition'])
        second = self.post('/admin/documents/folders', {'name': 'Insurance'}).request.path
        self.assertEqual(self.client.get(second + '/files/1').status_code, 404)
        self.add()
        self.post('/logout')
        self.login('alex', 'test-staff-password')
        self.assertEqual(self.client.get(first).status_code, 403)
        self.assertEqual(self.client.get(folder).status_code, 403)
        self.assertNotIn(b'insurance.png', self.client.get('/employee/documents').data)
