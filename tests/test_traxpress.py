import io
import json
import re
import sqlite3
import unittest
from datetime import datetime
from zoneinfo import ZoneInfo

from PIL import Image
from werkzeug.datastructures import MultiDict

import test_app as fixtures
from app import create_app
from traxpress import charges


class TraxpressTests(unittest.TestCase):
    setUp = fixtures.AppTests.setUp
    tearDown = fixtures.AppTests.tearDown
    post = fixtures.AppTests.post
    query = fixtures.AppTests.query
    login = fixtures.AppTests.login
    add = fixtures.AppTests.add

    def date(self):
        return datetime.now(ZoneInfo('Australia/Brisbane')).date().isoformat()

    def form(self, path):
        response = self.client.get(path)
        self.assertEqual(response.status_code, 200)
        return dict(re.findall(r'<input type="hidden" name="([^"]+)" value="([^"]*)"', response.get_data(as_text=True)))

    def rates(self, **changes):
        data = self.form('/traxpress/setup') | dict(unload='125', prepare='80', loading='10', pallet='20', strap='2.50') | changes
        response = self.post('/traxpress/setup', data)
        self.assertEqual(response.status_code, 200)

    def photo(self):
        image = io.BytesIO()
        Image.new('RGB', (640, 480), 'white').save(image, format='PNG')
        image.seek(0)
        return image, 'delivery.png'

    def pick(self, **changes):
        employee, _ = self.add()
        self.rates()
        return self.form('/traxpress/handling?kind=stock_pick') | dict(kind='stock_pick', work_date=self.date(),
            reference='ORDER-42', employee_id=str(employee['id']), hours='1.50', pallets_loaded='3',
            pallets_supplied='2', straps='6', notes='Ready for collection') | changes

    def test_stock_multiple_sizes_timestamp_downloads_and_idempotency(self):
        values = self.form('/traxpress/stock-take') | dict(stock_date=self.date(),
            tyre_size=['295/80R22.5', '11R22.5'], quantity=['20','0'], notes='Yard count')
        response = self.post('/traxpress/stock-take', values)
        self.assertEqual(response.status_code, 200)
        self.assertIn(b'ST-00001', response.data)
        self.assertEqual([r['quantity'] for r in self.query('SELECT * FROM tx_stock_lines')], [20,0])
        record = self.query('SELECT * FROM tx_stocktakes')[0]
        self.assertTrue(record['created_at'].endswith('+10:00'))
        self.assertEqual(record['recorded_by'], 'SB Empire')
        self.post('/traxpress/stock-take', values)
        self.assertEqual(len(self.query('SELECT * FROM tx_stocktakes')), 1)
        self.assertEqual(len(self.query('SELECT * FROM tx_stock_lines')), 2)
        csv = self.client.get('/traxpress/stock-take/1?download=csv')
        self.assertIn(b'295/80R22.5,20', csv.data)
        self.assertIn(record['created_at'].encode(), csv.data)
        pdf = self.client.get('/traxpress/stock-take/1?download=pdf')
        self.assertTrue(pdf.data.startswith(b'%PDF-'))
        self.assertIn('attachment', pdf.headers['Content-Disposition'])

    def test_stock_rejects_invalid_rows_and_preserves_form(self):
        base = self.form('/traxpress/stock-take') | dict(stock_date=self.date(), tyre_size=['295/80R22.5'], quantity=['2'])
        for change in [dict(quantity=['-1']), dict(quantity=['1.5']),dict(quantity=['1000001']),
                       dict(tyre_size=['']), dict(tyre_size=['295/80R22.5','295 /80R22.5'], quantity=['1','2']),
                       dict(tyre_size=['11R22.5','22R'], quantity=['1']),dict(stock_date='invalid'), dict(stock_date='2999-01-01')]:
            self.assertEqual(self.post('/traxpress/stock-take', base | change).status_code, 400)
        self.assertFalse(self.query('SELECT * FROM tx_stocktakes'))
        self.assertIn(b'295/80R22.5', self.post('/traxpress/stock-take',base | {'quantity':['-1']}).data)

    def test_pick_calculations_and_frozen_rates(self):
        data = self.pick()
        response = self.post('/traxpress/handling', data)
        self.assertEqual(response.status_code, 200)
        row = self.query('SELECT * FROM tx_handling')[0]
        self.assertEqual(row['total_cents'], 20500)  # 1.5*80 + 3*10 + 2*20 + 6*2.5
        self.assertEqual(row['employee_name'], 'Alex Example')
        self.assertEqual(len(json.loads(row['charges_json'])), 4)
        self.rates(prepare='999', loading='999')
        self.assertEqual(self.query('SELECT * FROM tx_handling')[0]['total_cents'], 20500)
        self.assertIn(b'$205.00', self.client.get('/traxpress/handling/1').data)
        self.assertIn(b'205.00', self.client.get('/traxpress/handling/export.csv').data)
        self.assertTrue(self.client.get('/traxpress/handling/1?download=pdf').data.startswith(b'%PDF-'))
        self.assertIn(b'Pallets supplied,2,pallet,20.00,40.00', self.client.get('/traxpress/handling/1?download=csv').data)
        self.post('/traxpress/handling', data)
        self.assertEqual(len(self.query('SELECT * FROM tx_handling')), 1)

    def test_bulk_requires_valid_photo_and_saves_downloadable_photo(self):
        data = self.pick(kind='bulk_unload', hours='2.25')
        self.assertEqual(self.post('/traxpress/handling', data).status_code, 400)
        self.assertEqual(self.post('/traxpress/handling', data | {'delivery_note':(io.BytesIO(b'not an image'),'fake.jpg')}).status_code,400)
        response = self.post('/traxpress/handling',data | {'delivery_note':self.photo()})
        self.assertEqual(response.status_code, 200)
        row = self.query('SELECT * FROM tx_handling')[0]
        self.assertEqual(row['total_cents'],28125)
        self.assertEqual(row['pallets_loaded'],0)
        photo = self.client.get('/traxpress/handling/1/delivery-note?download=1')
        self.assertEqual(photo.mimetype,'image/jpeg')
        self.assertTrue(photo.data.startswith(b'\xff\xd8'))
        self.post('/traxpress/handling',data | {'delivery_note':self.photo()})
        self.assertEqual(len(self.query('SELECT * FROM tx_delivery_photos')),1)

    def test_rates_required_and_stale_rate_forms_rejected(self):
        employee,_ = self.add()
        no_rate = self.form('/traxpress/handling') | dict(kind='bulk_unload',work_date=self.date(),employee_id=str(employee['id']),hours='1')
        self.assertEqual(self.post('/traxpress/handling', no_rate).status_code,400)
        self.rates()
        old = self.form('/traxpress/setup') | dict(unload='1',prepare='1',loading='1',pallet='1',strap='1')
        old_job = self.form('/traxpress/handling') | no_rate
        self.rates(unload='160')
        self.assertEqual(self.post('/traxpress/setup',old).status_code,400)
        self.assertEqual(self.query('SELECT unload FROM tx_rates ORDER BY id DESC LIMIT 1')[0][0],16000)
        self.assertEqual(self.post('/traxpress/handling',old_job | {'delivery_note':self.photo()}).status_code,400)
        self.assertFalse(self.query('SELECT * FROM tx_handling'))

    def test_invalid_rates_and_explicit_zero(self):
        values=self.form('/traxpress/setup') | dict(unload='0',prepare='0',loading='0',pallet='0',strap='0')
        for value in ('', '-1','NaN','Infinity','1.001','100001'):
            self.assertEqual(self.post('/traxpress/setup', values | {'unload':value}).status_code,400)
        self.assertFalse(self.query('SELECT * FROM tx_rates'))
        self.assertEqual(self.post('/traxpress/setup',values).status_code,200)
        self.assertEqual(self.query('SELECT unload FROM tx_rates')[0][0],0)

    def test_invalid_job_input_never_writes(self):
        data=self.pick()
        for change in [dict(hours='0'),dict(hours='NaN'),dict(hours='24.01'),dict(hours='-1'),dict(hours='1.001'),
                       dict(pallets_loaded='1.5'),dict(pallets_supplied='4'),dict(straps='-1'),
                       dict(employee_id='99999'),dict(employee_id='1'),dict(work_date='no'),dict(kind='other')]:
            self.assertEqual(self.post('/traxpress/handling',data | change).status_code,400)
        with sqlite3.connect(self.path) as conn:
            conn.execute("UPDATE users SET status='terminated' WHERE id=?",(data['employee_id'],))
        self.assertEqual(self.post('/traxpress/handling',data).status_code,400)
        self.assertFalse(self.query('SELECT * FROM tx_handling'))

    def test_access_csrf_and_signed_submission(self):
        data=self.pick()
        self.assertEqual(self.client.post('/traxpress/handling',data={k:v for k,v in data.items() if k!='csrf'}).status_code,400)
        self.assertEqual(self.post('/traxpress/handling',data | {'submission_token':'tampered'}).status_code,400)
        self.post('/traxpress/handling',data)
        stock=self.form('/traxpress/stock-take') | dict(stock_date=self.date(),tyre_size=['11R22.5'],quantity=['1'])
        self.post('/traxpress/stock-take',stock)
        self.post('/logout')
        self.assertEqual(self.client.get('/traxpress/stock-take').status_code,302)
        self.login('alex','test-staff-password')
        for path in ('/traxpress/stock-take','/traxpress/stock-take/1?download=pdf','/traxpress/handling',
                     '/traxpress/setup','/traxpress/handling/1','/traxpress/handling/1/delivery-note','/traxpress/handling/export.csv'):
            self.assertEqual(self.client.get(path).status_code,403)
        self.assertEqual(self.post('/traxpress/setup',dict(unload='1',prepare='1',loading='1',pallet='1',strap='1')).status_code,403)

    def test_csv_formula_and_html_escaping(self):
        data=self.form('/traxpress/stock-take') | dict(stock_date=self.date(),tyre_size=['=1+1'],quantity=['1'],notes='<script>alert(1)</script>')
        result=self.post('/traxpress/stock-take',data)
        self.assertNotIn(b'<script>alert(1)</script>',result.data)
        self.assertIn(b'&lt;script&gt;',result.data)
        self.assertIn(b"'=1+1",self.client.get('/traxpress/stock-take/1?download=csv').data)

    def test_decimal_rounding(self):
        lines,total=charges('stock_pick', 1, 3, 2, 4, dict(unload=1,prepare=50,loading=1,pallet=1,strap=1))
        self.assertEqual(lines[0]['total_cents'],1)
        self.assertEqual(total,10)

    def test_startup_is_additive_and_idempotent(self):
        data=self.pick()
        self.post('/traxpress/handling',data)
        before=self.query('SELECT * FROM users')
        create_app(dict(self.app.config))
        self.assertEqual(len(self.query('SELECT * FROM tx_handling')),1)
        self.assertEqual([tuple(r) for r in before],[tuple(r) for r in self.query('SELECT * FROM users')])

    def test_missing_records_and_navigation(self):
        self.assertIn(b'TRAXPRESS', self.client.get('/admin/dashboard').data)
        for path in ('/traxpress/stock-take/99','/traxpress/handling/99','/traxpress/handling/99/delivery-note'):
            self.assertEqual(self.client.get(path).status_code,404)


if __name__ == '__main__':
    unittest.main()
