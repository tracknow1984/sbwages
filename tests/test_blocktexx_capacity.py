import copy
import json
import sqlite3
from tests.test_blocktexx import example, PersistenceTests
from blocktexx import validate_model, summarize
from blocktexx_capacity import infer_containers, plan_run
from app import create_app
import unittest


def fixture():
    m=example()
    run=m['states']['VIC']['runs'].pop()
    run.update(site_ids=['s'], included_loads=1)
    m['states']['NSW'].update(m['states']['VIC'])
    m['states']['NSW']['runs']=[run]
    m['sites']=[dict(id='s',state='NSW',name='Test collection',address='',frequency='Weekly',
                     equipment='26 × PD Cage\n4 × 240L Bin',source_rows='',notes='')]
    return validate_model(m)


class CapacityTests(unittest.TestCase):
    def test_source_quantities_and_split(self):
        self.assertEqual(infer_containers('7 × 660L Bin\n7 × 660L Bin\n400 × Recycling Per KG')['bin660'],14)
        m=fixture();p=plan_run(m,'NSW',m['states']['NSW']['runs'][0])
        self.assertEqual(p['load_count'],2)
        self.assertEqual([l['spaces'] for l in p['loads']],[14,14])
        self.assertEqual(p['bin_count'],4)
        self.assertEqual(p['extra_loads'],1)
        self.assertFalse(p['payload_checked'])
        self.assertIsNone(summarize(m)['NSW']['collection_per_kg'])

    def test_editable_footprints_and_payload(self):
        m=fixture();r=m['states']['NSW']['runs'][0]
        m['states']['NSW']['truck']['spaces']['cage']=2
        self.assertEqual(plan_run(m,'NSW',r)['load_count'],4)
        truck=m['states']['NSW']['truck']
        truck['payload_kg']=1000
        truck['weights_kg'].update(cage=200,bin240=100)
        p=plan_run(m,'NSW',r)
        self.assertTrue(p['payload_checked'])
        self.assertTrue(all(l['known_kg']<=1000 for l in p['loads']))
        m['sites'][0]['containers']=dict.fromkeys(m['sites'][0]['containers'],0)
        self.assertTrue(plan_run(m,'NSW',r)['issues'])

    def test_fractional_counts_rejected(self):
        m=fixture();m['sites'][0]['containers']['cage']=1.5
        with self.assertRaises(ValueError):validate_model(m)


class CapacityPersistenceTests(PersistenceTests):
    def test_capacity_endpoint_and_save(self):
        m=fixture()
        r=self.client.post('/admin/blocktexx/capacity',data={'csrf':'test','model':json.dumps(m)})
        self.assertEqual(r.status_code,200)
        self.assertEqual(r.json['plans']['NSW']['one']['load_count'],2)
        self.assertEqual(self.client.post('/admin/blocktexx/capacity',data={'csrf':'bad','model':json.dumps(m)}).status_code,400)
        self.assertEqual(self.save(m).status_code,200)
        self.assertEqual(self.client.get('/admin/blocktexx/export').json['sites'][0]['containers']['cage'],26)
        with sqlite3.connect(self.path) as c:c.execute("UPDATE users SET role='employee' WHERE id=?",(self.uid,))
        self.assertEqual(self.client.post('/admin/blocktexx/capacity',data={'csrf':'test','model':json.dumps(m)}).status_code,403)

    def test_migration_audited_once_invalidates_extra_trip_measurements(self):
        m=fixture();m['capacity_version']=0
        self.assertEqual(self.save(m).status_code,200)
        create_app(self.config)
        saved=self.client.get('/admin/blocktexx/export').json
        r=saved['states']['NSW']['runs'][0]
        self.assertEqual(r['included_loads'],2)
        self.assertIsNone(r['km'])
        self.assertIsNone(r['drive_min'])
        self.assertIn('14 cages',r['sequence'])
        self.assertEqual(saved['sites'][0]['visits_4w'],4)
        self.assertEqual(saved['states']['NSW']['monthly_kg'],1000)
        create_app(self.config)
        with sqlite3.connect(self.path) as c:
            self.assertEqual(c.execute('SELECT revision FROM blocktexx_model').fetchone()[0],2)
            original=json.loads(c.execute('SELECT data FROM blocktexx_model_history WHERE revision=1').fetchone()[0])
            self.assertEqual(original['states']['NSW']['runs'][0]['km'],50)
