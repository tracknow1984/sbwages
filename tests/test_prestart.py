import json
import unittest
import test_app
from prestart_config import ASSETS, checklist


class PrestartTests(unittest.TestCase):
    setUp = test_app.AppTests.setUp
    tearDown = test_app.AppTests.tearDown
    query = test_app.AppTests.query
    post = test_app.AppTests.post
    login = test_app.AppTests.login
    add = test_app.AppTests.add
    def form(self, asset):
        self.client.get('/employee/prestart', query_string={'asset': asset})
        with self.client.session_transaction() as session:
            token = session['prestart_token']
        data = dict(asset=asset, reading='1234.5', submission_token=token,
                    fit_for_duty='yes', signature_strokes='[[[20,40],[80,100],[160,30],[220,90]]]', signed='yes')
        for key, _, _ in checklist(asset):
            data['check_'+key] = 'yes' if key == 'greased' else 'pass'
        return data

    def prepare(self):
        self.add()
        self.add('jordan')
        self.post('/logout')
        self.login('alex','test-staff-password')

    def test_prestart_asset_checklists_and_delivery(self):
        self.prepare()
        for asset in ASSETS:
            response = self.post('/employee/prestart', self.form(asset))
            self.assertEqual(response.status_code, 200)
            self.assertIn(b'Completed prestart', response.data)
            self.assertIn(b'Alex Example', response.data)
        records = self.query('SELECT * FROM prestarts')
        self.assertEqual(len(records),8)
        self.assertEqual([r['reading_unit'] for r in records], ['hours']*3+['km']*2+['hours']*3)
        self.assertTrue(any(a['key']=='tipper' for a in json.loads(records[4]['checks_json'])))
        self.post('/logout'); self.login('admin','test-admin-password','admin')
        self.assertIn(b'All completed (8)', self.client.get('/admin/prestart').data)
        self.assertIn(b'No prestarts', self.client.get('/admin/prestart?failed=1').data)

    def test_prestart_failures_unfit_signoff_and_duplicates(self):
        self.prepare()
        data = self.form('T595 BOBCAT') | dict(check_tracks='fail', note_tracks='Split track <script>x</script>',
                check_greased='no', note_greased='Grease required', fit_for_duty='no', notes='Too fatigued')
        result = self.post('/employee/prestart',data)
        self.assertIn(b'Failed items &amp; concerns',result.data)
        self.assertIn(b'&lt;script&gt;',result.data)
        self.post('/employee/prestart',data)
        records=self.query('SELECT * FROM prestarts')
        self.assertEqual(len(records),1)
        self.assertEqual(records[0]['failure_count'],2)
        self.post('/logout');self.login('admin','test-admin-password','admin')
        self.assertIn(b'Not fit for duty',self.client.get('/admin/prestart?failed=1').data)
        self.assertIn(b'Grease required',self.client.get('/admin/prestart/1').data)

    def test_prestart_validation_and_access(self):
        self.prepare()
        data=self.form('SUMITOMO EXCAVATOR')
        for invalid in ({'asset':'OTHER'},{'reading':'NaN'},{'reading':'-1'}, {'reading':'1e4'},
                        {'check_oil':''},{'check_oil':'na'},{'check_oil':'fail'},
                        {'check_greased':'no'},{'signed':''},{'signature_strokes':''},{'fit_for_duty':''},
                        {'fit_for_duty':'no'},{'submission_token':'wrong'}):
            self.post('/employee/prestart',data | invalid)
        self.assertEqual(len(self.query('SELECT * FROM prestarts')),0)
        self.assertEqual(self.client.post('/employee/prestart',data=data).status_code,400)
        self.post('/employee/prestart',data)
        self.assertEqual(self.client.get('/admin/prestart').status_code,403)
        self.assertEqual(self.client.get('/admin/prestart/1').status_code,403)
        self.assertEqual(self.client.post('/employee/prestart/1').status_code,400)
        self.post('/logout');self.login('jordan','test-staff-password')
        self.assertEqual(self.client.get('/employee/prestart/1').status_code,404)
        self.assertNotIn(b'Completed prestart #1',self.client.get('/employee/prestart').data)
        self.post('/logout')
        self.assertEqual(self.client.get('/employee/prestart/1').status_code,302)

    def test_prestart_roller_na_and_unfit_only_filter(self):
        self.prepare()
        data=self.form('DEMAG ROLLER') | dict(check_bucket='na',check_hitch='na',fit_for_duty='no',notes='Not rested')
        self.post('/employee/prestart',data)
        record=self.query('SELECT * FROM prestarts')[0]
        self.assertEqual(record['failure_count'],0)
        self.post('/logout');self.login('admin','test-admin-password','admin')
        self.assertIn(b'Completed prestart #1',self.client.get('/admin/prestart?failed=1').data)

    def test_drawn_signature_photo_storage_and_privacy(self):
        import io
        from PIL import Image
        self.prepare()
        photo=io.BytesIO()
        Image.new('RGB',(30,40),'blue').save(photo,format='PNG')
        photo.seek(0)
        data=self.form('T595 BOBCAT')
        data['photo_tracks']=(photo,'tracks.png')
        response=self.post('/employee/prestart',data)
        self.assertIn(b'/prestart/1/media/tracks',response.data)
        self.assertIn(b'/prestart/1/media/signature',response.data)
        self.assertEqual(self.client.get('/prestart/1/media/tracks').mimetype,'image/jpeg')
        signature=self.client.get('/prestart/1/media/signature')
        self.assertEqual(signature.mimetype,'image/png')
        image=Image.open(io.BytesIO(signature.data))
        self.assertEqual(image.size,(600,200))
        self.assertLess(image.getextrema()[0][0],255)
        self.post('/logout');self.login('jordan','test-staff-password')
        for key in ('tracks','signature'):
            self.assertEqual(self.client.get('/prestart/1/media/'+key).status_code,404)
        self.post('/logout');self.login('admin','test-admin-password','admin')
        self.assertEqual(self.client.get('/prestart/1/media/tracks').status_code,200)
        self.assertIn(b'/prestart/1/media/signature',self.client.get('/admin/prestart/1').data)
        self.post('/logout')
        self.assertEqual(self.client.get('/prestart/1/media/signature').status_code,403)

    def test_invalid_media_and_blank_signatures_do_not_submit(self):
        import io
        self.prepare()
        data=self.form('DEMAG ROLLER')
        for raw in ('[]','[[[10,10],[11,11]]]','[[[0,0],[600,0]]]','[[[0,0],[601,100]]]','[[[0,0],[NaN,100]]]','typed name','{}'):
            self.post('/employee/prestart',data | {'signature_strokes':raw})
        self.post('/employee/prestart',data | {'photo_tracks':(io.BytesIO(b'not an image'),'photo.png')})
        self.post('/employee/prestart',data | {'photo_tracks':(io.BytesIO(b'x'*(5*1024*1024+1)),'photo.jpg')})
        self.assertEqual(len(self.query('SELECT * FROM prestarts')),0)
        self.assertEqual(len(self.query('SELECT * FROM prestart_photos')),0)
        page=self.client.get('/employee/prestart?asset=DEMAG+ROLLER').data
        self.assertIn(b'type="radio" name="check_tracks"',page)
        self.assertNotIn(b'name="signature"',page)
        self.assertIn(b'<canvas',page)

    def test_daily_prestart_blocks_other_staff_and_allows_next_day(self):
        import sqlite3
        from datetime import datetime,timedelta
        from zoneinfo import ZoneInfo
        self.prepare()
        first=self.form('CAT D6 DOZER')
        self.post('/employee/prestart',first)
        self.post('/logout');self.login('jordan','test-staff-password')
        page=self.client.get('/employee/prestart?asset=CAT+D6+DOZER').data
        self.assertIn(b'ALREADY COMPLETED TODAY',page)
        self.assertIn(b'Alex Example',page)
        self.assertNotIn(b'class="signature-pad"',page)
        self.post('/employee/prestart',self.form('CAT D6 DOZER'))
        self.assertEqual(len(self.query('SELECT * FROM prestarts')),1)
        self.assertEqual(self.client.get('/employee/prestart/1').status_code,404)
        yesterday=(datetime.now(ZoneInfo('Australia/Brisbane'))-timedelta(days=1)).isoformat()
        with sqlite3.connect(self.path) as conn:
            conn.execute('UPDATE prestarts SET submitted_at=?',(yesterday,))
        self.post('/employee/prestart',self.form('CAT D6 DOZER'))
        self.assertEqual(len(self.query('SELECT * FROM prestarts')),2)

    def test_simultaneous_staff_cannot_duplicate_daily_prestart(self):
        from concurrent.futures import ThreadPoolExecutor
        self.prepare()
        first_client=self.client
        first=self.form('HYSTER FORKLIFT')
        self.client=self.app.test_client()
        self.login('jordan','test-staff-password')
        second_client=self.client
        second=self.form('HYSTER FORKLIFT')
        def submit(client,data):
            with client.session_transaction() as session:
                csrf=session['csrf']
            return client.post('/employee/prestart',data=data | {'csrf':csrf},follow_redirects=True)
        with ThreadPoolExecutor(max_workers=2) as pool:
            results=list(pool.map(lambda pair: submit(*pair),[(first_client,first),(second_client,second)]))
        self.assertTrue(all(r.status_code==200 for r in results))
        self.assertEqual(len(self.query("SELECT * FROM prestarts WHERE asset='HYSTER FORKLIFT'")),1)
        self.assertTrue(any(b'No duplicate was saved' in r.data for r in results))
