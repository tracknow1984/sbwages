import io
import unittest
from PIL import Image
import test_app

class LicencePhotoTests(unittest.TestCase):
    setUp=test_app.AppTests.setUp
    tearDown=test_app.AppTests.tearDown
    query=test_app.AppTests.query
    post=test_app.AppTests.post
    login=test_app.AppTests.login
    add=test_app.AppTests.add

    def photo(self, colour, size=(90,60)):
        stream=io.BytesIO();Image.new('RGB',size,colour).save(stream,format='PNG');stream.seek(0)
        return stream,'photo.png'

    def test_staff_directory_licence_thumbnail(self):
        person, _ = self.add()
        other, _ = self.add('jordan')
        path = f'/staff/{person["id"]}/licence-photos'
        directory = '/admin/staff'
        missing_link = f'/admin/staff/{person["id"]}/edit#licence-photos'
        self.assertIn(missing_link, self.client.get(directory).get_data(as_text=True))
        self.post(path, {'back': self.photo('red')})
        self.assertIn(path + '/back?thumbnail=1', self.client.get(directory).get_data(as_text=True))
        self.post(path, {'front': self.photo('blue', (900, 600))})
        page = self.client.get(directory).get_data(as_text=True)
        thumbnail = path + '/front?thumbnail=1'
        self.assertIn(thumbnail, page)
        self.assertIn(f'href="{path}/front"', page)
        self.assertNotIn(path + '/back?thumbnail=1', page)
        self.assertNotIn(missing_link, page)
        self.assertIn(f'/admin/staff/{other["id"]}/edit#licence-photos', page)
        preview = self.client.get(thumbnail)
        self.assertEqual(preview.mimetype, 'image/jpeg')
        self.assertTrue(preview.cache_control.no_store)
        with Image.open(io.BytesIO(preview.data)) as image:
            self.assertEqual(image.size, (144, 96))
        with Image.open(io.BytesIO(self.client.get(path + '/front').data)) as image:
            self.assertEqual(image.size, (900, 600))
        self.post('/logout')
        self.assertEqual(self.client.get(thumbnail).status_code, 403)
        self.login('jordan', 'test-staff-password')
        self.assertEqual(self.client.get(thumbnail).status_code, 404)

    def test_upload_replace_preview_download_and_permissions(self):
        person,_=self.add();self.add('jordan')
        path=f'/staff/{person["id"]}/licence-photos'
        self.post('/logout');self.login('alex','test-staff-password')
        self.assertEqual(self.client.post(path).status_code,400)
        self.assertIn(b'Licence photos saved',self.post(path,{'front':self.photo('blue'),'back':self.photo('red')}).data)
        front=self.client.get(path+'/front');back=self.client.get(path+'/back')
        self.assertEqual(front.mimetype,'image/jpeg')
        self.assertIn('attachment',self.client.get(path+'/front?download=1').headers['Content-Disposition'])
        self.post(path,{'front':self.photo('green')})
        self.assertNotEqual(front.data,self.client.get(path+'/front').data)
        self.assertEqual(back.data,self.client.get(path+'/back').data)
        self.post(path,{'front':self.photo('white'),'back':(io.BytesIO(b'invalid'),'bad.jpg')})
        self.assertEqual(back.data,self.client.get(path+'/back').data)
        self.assertEqual(len(self.query('SELECT * FROM licence_photos')),2)
        self.assertIn(b'Preview',self.client.get('/employee/details').data)
        self.post('/logout');self.login('jordan','test-staff-password')
        self.assertEqual(self.client.get(path+'/front').status_code,404)
        self.assertEqual(self.post(path,{'front':self.photo('black')}).status_code,404)
        self.post('/logout');self.login('admin','test-admin-password','admin')
        self.assertEqual(self.client.get(path+'/front').status_code,200)
        self.assertIn(b'Download',self.client.get(f'/admin/staff/{person["id"]}/edit').data)
        self.post('/logout')
        self.assertEqual(self.client.get(path+'/front').status_code,403)
