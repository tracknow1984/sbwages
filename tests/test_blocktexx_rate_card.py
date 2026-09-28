"""Use synthetic commercial data to test private deployment defaults and persistence."""
import json
import sqlite3
import unittest
from unittest.mock import patch
import test_blocktexx as fixtures
from test_blocktexx import example
from app import create_app
from blocktexx import validate_model
from blocktexx_interstate import QUOTED_LANES, interstate_summary

DEFAULTS = dict(base_rates={key: 100 + i * 10 for i, key in enumerate(QUOTED_LANES)},
                fuel_pct=20, mainfreight_base_total=1400)
ENV = {'BLOCKTEXX_INTERSTATE_DEFAULTS_JSON': json.dumps(DEFAULTS)}

class RateCardTests(unittest.TestCase):
    @patch.dict('os.environ', ENV)
    def test_defaults_edits_zero_and_blank_round_trip(self):
        model = validate_model(example())
        rates = model['interstate']
        self.assertEqual(rates['rates_version'], 1)
        self.assertEqual(rates['mainfreight_base_total'], 1400)
        for key in QUOTED_LANES:
            self.assertAlmostEqual(interstate_summary(rates)['lanes'][key]['rate'], DEFAULTS['base_rates'][key] * 1.2)
        first = rates['lanes'][QUOTED_LANES[0]]
        first.update(base_trip=777, fuel_pct=0, tolls_trip=9)
        rates['lanes'][QUOTED_LANES[1]]['base_trip'] = None
        rates['mainfreight_base_total'] = 1500
        self.assertEqual(validate_model(model), model)
        self.assertEqual(interstate_summary(rates)['lanes'][QUOTED_LANES[0]]['rate'], 786)
        self.assertIsNone(interstate_summary(rates)['lanes'][QUOTED_LANES[1]]['rate'])

class RateCardPersistenceTests(unittest.TestCase):
    setUp = fixtures.PersistenceTests.setUp
    tearDown = fixtures.PersistenceTests.tearDown
    save = fixtures.PersistenceTests.save
    def test_migration_and_saved_edits_survive_restart(self):
        model = example()
        model['interstate'] = {'lanes': {}, 'bookings': [dict(id='legacy', lane_id='sydney_brisbane_semi', origin='Old origin', destination='Old destination', week=1, day=0, trips=1, transit_days=2, kg_trip=None, notes='')]}
        self.assertEqual(self.save(model).status_code, 200)
        with patch.dict('os.environ', ENV):
            create_app(self.config)
            saved = self.client.get('/admin/blocktexx/export').json
            self.assertEqual(saved['interstate']['bookings'][0]['origin'], 'Old origin')
            self.assertEqual(saved['interstate']['lanes'][QUOTED_LANES[0]]['base_trip'], 100)
            saved['interstate']['lanes'][QUOTED_LANES[0]]['base_trip'] = 321
            saved['interstate']['mainfreight_base_total'] = 1600
            self.assertEqual(self.save(saved, 2).status_code, 200)
            create_app(self.config)
            self.assertEqual(self.client.get('/admin/blocktexx/export').json, saved)
            with sqlite3.connect(self.path) as conn:
                self.assertEqual(conn.execute('SELECT revision FROM blocktexx_model').fetchone()[0], 3)
                self.assertEqual(conn.execute('SELECT COUNT(*) FROM blocktexx_model_history').fetchone()[0], 3)
