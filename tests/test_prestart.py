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
                    fit_for_duty='yes', signature='Alex Example', signed='yes')
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
        self.assertEqual(len(records),5)
        self.assertEqual([r['reading_unit'] for r in records], ['hours']*3+['km']*2)
        self.assertTrue(any(a['key']=='tipper' for a in json.loads(records[-1]['checks_json'])))
        self.post('/logout'); self.login('admin','test-admin-password','admin')
        self.assertIn(b'All completed (5)', self.client.get('/admin/prestart').data)
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
                        {'check_greased':'no'},{'signed':''},{'signature':''},{'fit_for_duty':''},
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
