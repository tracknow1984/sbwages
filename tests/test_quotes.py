import json
from test_app import AppTests
from blocktexx_quotes import calculate

class QuoteTests(AppTests):
    def test_submission_private_immutable_and_idempotent(self):
        public=self.app.test_client()
        page=public.get('/blocktexx/run-sheets')
        self.assertEqual(page.status_code,200)
        self.assertIn(b'B-double',page.data)
        with public.session_transaction() as sess: token=sess['csrf']
        form=dict(csrf=token,organisation='Provider Test',contact='Alex',email='alex@example.com',NSW_local_hourly='140',NSW_local_minimum='4')
        self.assertEqual(public.post('/blocktexx/run-sheets',data=form).status_code,302)
        self.assertEqual(public.post('/blocktexx/run-sheets',data=form).status_code,302)
        rows=self.query('SELECT * FROM blocktexx_quotes')
        self.assertEqual(len(rows),1)
        receipt=public.get('/transport/run-sheets/received')
        self.assertIn(b'Saved pricing',receipt.data)
        self.assertIn(b'Hourly $140.0',receipt.data)
        self.assertNotIn(b'(row ',page.data)
        self.assertNotIn(b'quantities from source',page.data)
        summary=calculate(json.loads(rows[0]['snapshot']),json.loads(rows[0]['prices']))
        self.assertGreater(summary[0]['total'],0)
        self.assertTrue(any(r['gap'] for r in summary))
        self.assertEqual(public.get('/admin/blocktexx/quotes').status_code,302)
        other=self.app.test_client()
        self.assertEqual(other.get('/blocktexx/run-sheets/received').status_code,302)
        review=self.client.get('/admin/blocktexx/quotes')
        self.assertEqual(review.status_code,200)
        self.assertIn(b'Provider Test',review.data)
        self.assertIn(b'Hourly $140.0',review.data)
        self.post('/admin/blocktexx/quotes/'+rows[0]['id']+'/review',{'status':'Reviewed'})
        self.assertEqual(self.query('SELECT status FROM blocktexx_quotes')[0][0],'Reviewed')

    def test_validation_and_csrf(self):
        self.client.get('/blocktexx/run-sheets')
        form=dict(organisation='Provider',contact='Alex',email='alex@example.com',NSW_local_hourly='-1')
        self.assertEqual(self.client.post('/blocktexx/run-sheets',data=form).status_code,400)
        self.assertEqual(self.post('/blocktexx/run-sheets',form).status_code,400)
        self.assertEqual(len(self.query('SELECT * FROM blocktexx_quotes')),0)

    def test_public_projection_hides_internal_prices(self):
        from blocktexx_quotes import projection
        model={'states':{'NSW':{'hourly_rate':99999,'fixed_cost':55555,'runs':[]}},'sites':[]}
        text=json.dumps(projection(model))
        self.assertNotIn('99999',text)
        self.assertNotIn('55555',text)
        self.assertNotIn('QLD',text)
