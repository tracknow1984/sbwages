import copy
import json
import sqlite3
import tempfile
import unittest
from app import create_app
from blocktexx import empty_model, validate_model, summarize


def example():
    model = empty_model()
    d = model['states']['VIC']
    d.update(depot='Example depot', monthly_kg=1000, cost_mode='contractor', hourly_rate=125, minimum_hours=4)
    d['runs'] = [dict(id='one', name='Example day', sequence='Depot / site / depot', notes='', evidence='',
                     status='estimated', site_ids=[], runs_4w=4, km=50, drive_min=60, service_min=30,
                     depot_min=15, prep_min=15, wait_min=0, break_min=30)]
    return model


class CalculationTests(unittest.TestCase):
    def test_calendar_conversion_and_daily_minimum(self):
        s = summarize(validate_model(example()))['VIC']
        self.assertEqual(s['work_4w'], 8)
        self.assertEqual(s['elapsed_4w'], 10)
        self.assertEqual(s['billed_4w'], 16)
        self.assertAlmostEqual(s['monthly_cost'], 16 * 13 / 12 * 125)
        self.assertAlmostEqual(s['collection_per_kg'], 16 * 13 / 12 * 125 / 1000)

    def test_unknown_frequency_blocks_rate_but_preserves_known_subtotal(self):
        m = example()
        missing = copy.deepcopy(m['states']['VIC']['runs'][0])
        missing.update(id='pending', name='Ad hoc', runs_4w=None)
        m['states']['VIC']['runs'].append(missing)
        s = summarize(validate_model(m))['VIC']
        self.assertIsNone(s['collection_per_kg'])
        self.assertGreater(s['monthly_cost'], 0)
        self.assertEqual(len(s['gaps']), 1)

    def test_missing_drive_not_zero_and_nonfinite_rejected(self):
        m = example()
        m['states']['VIC']['runs'][0]['drive_min'] = None
        self.assertIsNone(summarize(validate_model(m))['VIC']['collection_per_kg'])
        m['states']['VIC']['runs'][0]['drive_min'] = float('nan')
        with self.assertRaises(ValueError): validate_model(m)

    def test_verified_needs_evidence_and_unassigned_sites_block_rate(self):
        m = example()
        m['states']['VIC']['runs'][0]['status'] = 'verified'
        with self.assertRaises(ValueError): validate_model(m)
        m['states']['VIC']['runs'][0]['status'] = 'estimated'
        m['sites'] = [dict(id='site', state='VIC', name='Uncovered', address='', frequency='Weekly', equipment='', source_rows='', notes='')]
        self.assertIsNone(summarize(validate_model(m))['VIC']['collection_per_kg'])


class PersistenceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = self.tmp.name + '/test.db'
        self.config = {'TESTING':True, 'DATABASE':self.path, 'SECRET_KEY':'test-'*10,
                       'ADMIN_EMAIL':'test@example.com', 'ADMIN_PASSWORD':'test-password', 'SESSION_COOKIE_SECURE':False}
        self.app = create_app(self.config)
        self.client = self.app.test_client()
        with sqlite3.connect(self.path) as conn:
            self.uid, version = conn.execute("SELECT id,auth_version FROM users WHERE role='admin'").fetchone()
        with self.client.session_transaction() as s: s.update(uid=self.uid, version=version, csrf='test')

    def tearDown(self): self.tmp.cleanup()

    def save(self, model, revision=0, csrf='test'):
        return self.client.post('/admin/blocktexx', data={'csrf':csrf,'revision':revision,'model':json.dumps(model)})

    def test_save_reload_audit_and_conflict(self):
        self.assertEqual(self.client.get('/admin/blocktexx').status_code, 200)
        self.assertEqual(self.save(example()).status_code, 200)
        self.assertEqual(self.save(example()).status_code, 409)
        self.assertEqual(self.save(example(),1).json['revision'],2)
        create_app(self.config)  # additive startup retains model and users
        self.assertEqual(self.client.get('/admin/blocktexx/export').json['states']['VIC']['monthly_kg'],1000)
        with sqlite3.connect(self.path) as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM blocktexx_model_history').fetchone()[0],2)
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM users').fetchone()[0],1)

    def test_role_csrf_and_export_permissions(self):
        self.assertEqual(self.save(example(),csrf='bad').status_code,400)
        with sqlite3.connect(self.path) as conn: conn.execute("UPDATE users SET role='employee' WHERE id=?",(self.uid,))
        for url in ['/admin/blocktexx','/admin/blocktexx/export']:
            self.assertEqual(self.client.get(url).status_code,403)
        self.assertEqual(self.save(example()).status_code,403)
        self.assertEqual(self.app.test_client().get('/admin/blocktexx').status_code,302)

    def test_invalid_import_keeps_saved_and_csv_neutralizes_formulas(self):
        m=example(); m['states']['VIC']['runs'][0]['name']='=1+1'
        self.assertEqual(self.save(m).status_code,200)
        bad=copy.deepcopy(m);bad['states']['VIC']['runs'][0]['km']=-1
        self.assertEqual(self.save(bad,1).status_code,400)
        self.assertEqual(self.client.get('/admin/blocktexx/export').json['states']['VIC']['runs'][0]['km'],50)
        self.assertIn(b"'=1+1",self.client.get('/admin/blocktexx/export?format=csv').data)

    def test_import_validation_normalizes_without_saving(self):
        m=example();m['states']['VIC']['hourly_rate']='125'
        r=self.client.post('/admin/blocktexx/validate',data={'csrf':'test','model':json.dumps(m)})
        self.assertEqual(r.status_code,200)
        self.assertEqual(r.json['model']['states']['VIC']['hourly_rate'],125)
        self.assertEqual(self.client.get('/admin/blocktexx/export').json['states']['VIC']['runs'],[])
        self.assertEqual(self.client.post('/admin/blocktexx/validate',data={'csrf':'bad','model':json.dumps(m)}).status_code,400)


if __name__ == '__main__': unittest.main()
