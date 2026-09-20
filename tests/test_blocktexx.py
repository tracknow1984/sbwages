import copy
import json
import sqlite3
import tempfile
import unittest
import gzip
import base64
from unittest.mock import patch
from app import create_app
from blocktexx import empty_model, validate_model, summarize


def example():
    model = empty_model()
    model['capacity_version'] = 1
    d = model['states']['VIC']
    d.update(depot='Example depot', monthly_kg=1000, cost_mode='contractor', hourly_rate=125, minimum_hours=4)
    d['runs'] = [dict(id='one', name='Example day', sequence='Depot / site / depot', notes='', evidence='',
                     status='estimated', site_ids=[], runs_4w=4, km=50, drive_min=60, service_min=30,
                     depot_min=15, prep_min=15, wait_min=0, break_min=30)]
    return model


class CalculationTests(unittest.TestCase):
    def test_customer_day_rules_are_preserved_and_source_days_protected(self):
        m=example()
        m['sites']=[dict(id='fixed',state='QLD',name='Customer',address='Street, Eagle Farm, QLD',frequency='Tue Fri - Weekly',equipment='1 × 660L Bin',source_rows='',notes='')]
        saved=validate_model(m)
        self.assertEqual(saved['sites'][0]['day_rule'],'fixed')
        self.assertEqual(saved['sites'][0]['service_days'],[1,4])
        self.assertEqual(saved['sites'][0]['service_area'],'Eagle Farm')
        saved['sites'][0]['day_rule']='flexible'
        self.assertEqual(validate_model(saved)['sites'][0]['day_rule'],'flexible')
        saved['sites'][0].update(day_rule='fixed',service_days=[])
        with self.assertRaises(ValueError):validate_model(saved)

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

    def test_planner_allocations_persist_and_reject_invalid_days(self):
        m=example()
        m['states']['VIC']['runs'][0]['planner_slots']=[{'week':1,'day':2},{'week':3,'day':2}]
        self.assertEqual(self.save(m).status_code,200)
        saved=self.client.get('/admin/blocktexx/export').json
        self.assertEqual(saved['states']['VIC']['runs'][0]['planner_slots'],[{'week':1,'day':2},{'week':3,'day':2}])
        saved['states']['VIC']['runs'][0]['planner_slots'][0]['day']=7
        self.assertEqual(self.save(saved,1).status_code,400)

    def test_import_sample_workbook_is_a_draft_then_persists(self):
        import io
        from openpyxl import Workbook
        wb=Workbook();sheet=wb.active;sheet.title='KG Collected'
        sheet.append(['Date','Docket','Company','Site','Facility','Qty'])
        sheet.append(['7/1/2026','d1','Example','Town','SXVIC - Melbourne',100])
        sheet.append(['7/1/2026','d1','Example','Town','SXVIC - Melbourne',200])
        stream=io.BytesIO();wb.save(stream);stream.seek(0)
        response=self.client.post('/admin/blocktexx/weights/import',data={'csrf':'test','model':json.dumps(example()),'file':(stream,'sample.xlsx')})
        self.assertEqual(response.status_code,200)
        draft=response.json['model']
        self.assertEqual(draft['weight_history']['pickups'][0]['kg'],300)
        self.assertFalse(self.client.get('/admin/blocktexx/export').json.get('weight_history',{}).get('pickups'))
        self.assertEqual(self.save(draft).status_code,200)
        self.assertEqual(self.client.get('/admin/blocktexx/export').json['weight_history'],draft['weight_history'])

    def test_daily_pickup_weights_round_trip_and_validate(self):
        m=example()
        slots=[{'week':1,'day':2,'pickup_kg':1250.5},{'week':3,'day':2,'pickup_kg':0}]
        m['states']['VIC']['runs'][0]['planner_slots']=slots
        self.assertEqual(self.save(m).status_code,200)
        saved=self.client.get('/admin/blocktexx/export').json
        self.assertEqual(saved['states']['VIC']['runs'][0]['planner_slots'],slots)
        for value in (-1, float('nan'), 1000001, True):
            saved['states']['VIC']['runs'][0]['planner_slots'][0]['pickup_kg']=value
            self.assertEqual(self.save(saved,1).status_code,400)

    def test_day_limit_blocks_combined_runs_and_retains_saved_data(self):
        for mode in ('owned', 'contractor'):
            m=example()
            m['states']['VIC']['cost_mode']=mode
            r=m['states']['VIC']['runs'][0]
            r.update(drive_min=450, planner_slots=[dict(week=1,day=0)])
            self.assertEqual(self.save(m).status_code,200) if mode=='owned' else None
            # 450 driving + 90 handling/breaks = exactly 9 hours.
            r['drive_min']=451
            response=self.save(m,1)
            self.assertEqual(response.status_code,400)
            self.assertIn('540-minute',response.json['error'])
        saved=self.client.get('/admin/blocktexx/export').json
        self.assertEqual(saved['states']['VIC']['runs'][0]['drive_min'],450)
        second=copy.deepcopy(saved['states']['VIC']['runs'][0])
        second.update(id='two',drive_min=0)
        saved['states']['VIC']['runs'].append(second)
        self.assertEqual(self.save(saved,1).status_code,400)
        second['planner_slots']=[dict(week=1,day=1)]
        self.assertEqual(self.save(saved,1).status_code,200)
        second['drive_min']=None
        self.assertEqual(self.save(saved,2).status_code,400)

    def test_legacy_days_remain_editable_but_cannot_be_worsened(self):
        from blocktexx import check_calendar_limits
        old=example()
        r=old['states']['VIC']['runs'][0]
        r.update(name='Monday run',drive_min=600)
        new=copy.deepcopy(old)
        check_calendar_limits(new,old)
        new['states']['VIC']['runs'][0]['drive_min']=590
        check_calendar_limits(new,old)
        new['states']['VIC']['runs'][0]['drive_min']=601
        with self.assertRaises(ValueError):check_calendar_limits(new,old)
        new=copy.deepcopy(old)
        new['states']['VIC']['runs'][0]['planner_slots']=[dict(week=1,day=1)]
        with self.assertRaises(ValueError):check_calendar_limits(new,old)

    def test_explicit_overtime_approval_persists_and_is_bounded(self):
        m=example()
        r=m['states']['VIC']['runs'][0]
        r.update(drive_min=510,planner_slots=[dict(week=1,day=0)])
        self.assertEqual(self.save(m).status_code,400)
        r['planner_slots'][0]['overtime_limit_min']=600
        self.assertEqual(self.save(m).status_code,200)
        saved=self.client.get('/admin/blocktexx/export').json
        self.assertEqual(saved['states']['VIC']['runs'][0]['planner_slots'][0]['overtime_limit_min'],600)
        r['drive_min']=511
        self.assertEqual(self.save(m,1).status_code,400)
        r['drive_min']=None
        self.assertEqual(self.save(m,1).status_code,400)
        r['drive_min']=510
        r['planner_slots'].append(dict(week=2,day=0))
        self.assertEqual(self.save(m,1).status_code,400)

    def test_cost_profiles_persist_and_use_selected_comparison(self):
        from tests.test_blocktexx_costs import priced_model
        m=priced_model()
        self.assertEqual(self.save(m).status_code,200)
        saved=self.client.get('/admin/blocktexx/export').json
        self.assertEqual(saved['states']['VIC']['cost_profile']['staff_qty'],1)
        self.assertNotIn('cost_profile',saved['states']['QLD'])
        result=summarize(saved)['VIC']
        self.assertAlmostEqual(result['monthly_cost'],5157.5)
        self.assertAlmostEqual(result['collection_per_kg'],5.1575)
        saved['states']['VIC']['cost_profile']['contractor_basis']='daily'
        self.assertAlmostEqual(self.save(saved,1).json['summary']['VIC']['monthly_cost'],6620)
        saved['states']['VIC']['cost_profile']['staff_qty']=1.5
        self.assertEqual(self.save(saved,2).status_code,400)

    def test_interstate_and_downstream_save_without_changing_intake(self):
        from tests.test_blocktexx_interstate import transport_model
        m=transport_model()
        result=self.save(m)
        self.assertEqual(result.status_code,200)
        self.assertEqual(result.json['interstate_summary']['cost_4w'],8950)
        saved=self.client.get('/admin/blocktexx/export').json
        self.assertEqual(saved['interstate']['bookings'][0]['trips'],2)
        self.assertEqual(saved['states']['NSW']['monthly_kg'],40000)
        self.assertEqual(saved['states']['NSW']['runs'][0]['activity_type'],'deliver_decomm')
        saved['interstate']['bookings'][0]['day']=2
        self.assertEqual(self.save(saved,1).status_code,200)
        self.assertEqual(self.client.get('/admin/blocktexx/export').json['interstate']['bookings'][0]['day'],2)
        create_app(self.config)
        self.assertEqual(self.client.get('/admin/blocktexx/export').json['states']['NSW']['runs'][0]['activity_type'],'deliver_decomm')

    def test_resource_price_profiles_persist_without_conflating_blank_and_zero(self):
        m=example()
        m['states']['VIC']['resource_pricing']={'cage':{'purchase_each':250.50,'weekly_rent_each':4.25,'rental_qty':10},'bin240':{'purchase_each':0,'weekly_rent_each':None,'rental_qty':None}}
        self.assertEqual(self.save(m).status_code,200)
        saved=self.client.get('/admin/blocktexx/export').json
        p=saved['states']['VIC']['resource_pricing']
        self.assertEqual(p['cage'],{'purchase_each':250.50,'weekly_rent_each':4.25,'rental_qty':10})
        self.assertEqual(p['bin240']['purchase_each'],0)
        self.assertIsNone(p['bin240']['weekly_rent_each'])
        self.assertIsNone(saved['states']['NSW']['resource_pricing']['cage']['purchase_each'])
        p['cage']['rental_qty']=1.5
        self.assertEqual(self.save(saved,1).status_code,400)
        p['cage']['rental_qty']=10
        p['cage']['purchase_each']=-1
        self.assertEqual(self.save(saved,1).status_code,400)

    def test_private_depot_update_applies_once_and_preserves_later_edits(self):
        m=example()
        self.assertEqual(self.save(m).status_code,200)
        update=json.dumps({'id':'test-depot-update','state':'QLD','depot':'Test storage — test address'})
        with patch.dict('os.environ',{'BLOCKTEXX_DEPOT_UPDATE_JSON':update}):
            create_app(self.config)
            loaded=self.client.get('/admin/blocktexx/export').json
            self.assertEqual(loaded['states']['QLD']['depot'],'Test storage — test address')
            self.assertEqual(loaded['states']['QLD']['depot_status'],'confirmed')
            self.assertEqual(loaded['states']['VIC']['monthly_kg'],1000)
            loaded['states']['QLD']['depot']='Later user edit'
            self.assertEqual(self.save(loaded,2).status_code,200)
            create_app(self.config)
        self.assertEqual(self.client.get('/admin/blocktexx/export').json['states']['QLD']['depot'],'Later user edit')
        with sqlite3.connect(self.path) as c:
            self.assertEqual(c.execute('SELECT COUNT(*) FROM blocktexx_applied_updates').fetchone()[0],1)
            self.assertEqual(c.execute('SELECT COUNT(*) FROM blocktexx_model_history').fetchone()[0],3)

    def test_import_validation_normalizes_without_saving(self):
        m=example();m['states']['VIC']['hourly_rate']='125'
        r=self.client.post('/admin/blocktexx/validate',data={'csrf':'test','model':json.dumps(m)})
        self.assertEqual(r.status_code,200)
        self.assertEqual(r.json['model']['states']['VIC']['hourly_rate'],125)
        self.assertEqual(self.client.get('/admin/blocktexx/export').json['states']['VIC']['runs'],[])
        self.assertEqual(self.client.post('/admin/blocktexx/validate',data={'csrf':'bad','model':json.dumps(m)}).status_code,400)

    def test_frequency_change_persists_and_flags_route_mismatch(self):
        m=example()
        m['sites']=[dict(id='site',state='VIC',name='Example customer',address='Example address',frequency='Weekly',equipment='',source_rows='1',notes='')]
        m['states']['VIC']['runs'][0]['site_ids']=['site']
        self.assertEqual(self.save(m).status_code,200)
        saved=self.client.get('/admin/blocktexx/export').json
        self.assertEqual(saved['sites'][0]['visits_4w'],4)
        saved['sites'][0].update(visits_4w=8,frequency='Twice weekly')
        result=self.save(saved,1)
        self.assertIsNone(result.json['summary']['VIC']['collection_per_kg'])
        reread=self.client.get('/admin/blocktexx/export').json
        self.assertEqual(reread['sites'][0]['source_frequency'],'Weekly')
        self.assertEqual(reread['sites'][0]['visits_4w'],8)
        reread['states']['VIC']['runs'][0]['runs_4w']=8
        self.assertIsNotNone(self.save(reread,2).json['summary']['VIC']['collection_per_kg'])

    def test_private_bootstrap_runs_once_and_preserves_saved_edits(self):
        m=example()
        m['sites']=[dict(id='site',state='VIC',name='Example customer',address='',frequency='Weekly',equipment='',source_rows='',notes='')]
        seed=base64.b64encode(gzip.compress(json.dumps(m).encode())).decode()
        with patch.dict('os.environ',{'BLOCKTEXX_INITIAL_MODEL_GZIP_B64':seed}):
            create_app(self.config)
            loaded=self.client.get('/admin/blocktexx/export').json
            self.assertEqual(len(loaded['sites']),1)
            loaded['sites'][0]['visits_4w']=2
            self.assertEqual(self.save(loaded,1).status_code,200)
            create_app(self.config)
        self.assertEqual(self.client.get('/admin/blocktexx/export').json['sites'][0]['visits_4w'],2)


if __name__ == '__main__': unittest.main()
