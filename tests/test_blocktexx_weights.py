import io
import unittest
from openpyxl import Workbook
from blocktexx import validate_model
from blocktexx_weights import import_workbook
from tests.test_blocktexx import example

class WeightTests(unittest.TestCase):
    def test_group_and_map_without_double_counting(self):
        wb=Workbook();s=wb.active;s.title='KG Collected'
        s.append(['Date','Docket','Company','Site','Facility','Qty'])
        s.append(['7/1/2026','d1','Acme','Town','SXVIC - Melbourne',100])
        s.append(['7/1/2026','d1','Acme','Town','SXVIC - Melbourne',200])
        s.append(['7/2/2026','d2','Acme','Town','SXVIC - Melbourne',500])
        s.append(['7/2/2026','d3','Unknown','Elsewhere','SXWA - Perth',20])
        stream=io.BytesIO();wb.save(stream)
        sites=[dict(id='one',name='Blocktexx - Acme - Town',state='VIC')]
        result=import_workbook(stream.getvalue(),'sample.xlsx',sites)
        self.assertEqual(len(result['pickups']),3)
        self.assertEqual(sum(r['kg'] for r in result['pickups']),820)
        self.assertEqual(result['pickups'][0]['kg'],300)
        self.assertEqual(result['pickups'][0]['site_id'],'one')
        self.assertEqual(result['pickups'][2]['site_id'],'')

    def test_new_fields_validate_and_preserve_zero(self):
        m=example();sid='customer';state='VIC'
        m['sites']=[dict(id=sid,state=state,name='Example',frequency='Weekly',equipment='',address='',source_rows='',notes='')]
        m['states'][state]['runs'][0]['site_ids']=[sid]
        m['sites'][0]['scenario_pickup_kg']=0
        m['states'][state]['selling_per_kg']=0.45
        run=m['states'][state]['runs'][0]
        run['planner_slots']=[dict(week=1,day=0,pickup_by_site={sid:0})]
        history=dict(date='2026-07-01',docket='one',company='Example',site='Town',facility='SXVIC',profile='example',site_id=sid,kg=500)
        m['weight_history']=dict(source='test.xlsx',pickups=[history])
        result=validate_model(m)
        self.assertEqual(result['weight_history']['pickups'][0]['kg'],500)
        self.assertEqual(result['sites'][0]['scenario_pickup_kg'],0)
        self.assertEqual(result['states'][state]['runs'][0]['planner_slots'][0]['pickup_by_site'][sid],0)
        history['kg']=-1
        with self.assertRaises(ValueError):validate_model(m)
        history['kg']=500;m['weight_history']['pickups'].append(history.copy())
        with self.assertRaises(ValueError):validate_model(m)
