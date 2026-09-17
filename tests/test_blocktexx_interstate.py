import copy
import unittest
from blocktexx import empty_model, validate_model, summarize
from blocktexx_capacity import capacity_plans
from blocktexx_interstate import interstate_summary


def transport_model():
    m=empty_model()
    m['interstate']={'lanes':{'sydney_brisbane_bdouble':dict(base_trip=3500,fuel_pct=25,tolls_trip=0,other_trip=100,payload_kg=30000)},'bookings':[
        dict(id='trip1',lane_id='sydney_brisbane_bdouble',week=1,day=0,trips=2,kg_trip=20000,transit_days=2,origin='Sydney depot',destination='Threadtexx Brisbane',notes='Consolidated freight')]}
    m['states']['NSW']['monthly_kg']=40000
    m['partners']=[dict(id='decomm',name='Test partner',state='NSW',address='Test address',frequency='',equipment='',source_rows='',notes='')]
    m['states']['NSW']['runs']=[dict(id='delivery',name='Partner delivery',activity_type='deliver_decomm',partner_id='decomm',
        origin='Depot',destination='Partner',cargo='Textiles',movement_kg=1000,sequence='Depot → Partner',original_sequence='',notes='',evidence='',
        site_ids=[],runs_4w=1,planner_slots=[dict(week=1,day=0)],status='estimated',km=20,drive_min=30,service_min=30,depot_min=0,prep_min=0,wait_min=0,break_min=0)]
    return m


class InterstateTests(unittest.TestCase):
    def test_round_trip_and_costs_do_not_add_intake(self):
        m=validate_model(transport_model())
        self.assertEqual(len(m['interstate']['lanes']),4)
        s=interstate_summary(m['interstate'])
        lane=s['lanes']['sydney_brisbane_bdouble']
        self.assertEqual(lane['rate'],4475)
        self.assertEqual(lane['cost_4w'],8950)
        self.assertEqual(lane['kg'],40000)
        self.assertAlmostEqual(lane['cost_per_kg'],.22375)
        self.assertAlmostEqual(s['monthly_cost'],8950*13/12)
        self.assertEqual(m['states']['NSW']['monthly_kg'],40000)
        self.assertEqual(m['states']['NSW']['runs'][0]['movement_kg'],1000)
        self.assertNotIn('delivery',capacity_plans(m)['NSW'])
        self.assertEqual(summarize(m)['NSW']['work_4w'],1)
        self.assertEqual(validate_model(m),m)

    def test_reject_bad_lane_day_payload_and_negative_rates(self):
        source=transport_model()
        for field,value in [('lane_id','bad'),('week',5),('day',7),('trips',1.5),('kg_trip',40000),('transit_days',-1)]:
            m=copy.deepcopy(source);m['interstate']['bookings'][0][field]=value
            with self.assertRaises(ValueError):validate_model(m)
        m=copy.deepcopy(source);m['interstate']['lanes']['sydney_brisbane_bdouble']['base_trip']=-1
        with self.assertRaises(ValueError):validate_model(m)

    def test_blank_prices_and_weights_not_silently_zero(self):
        m=transport_model();m['interstate']['lanes']['sydney_brisbane_bdouble']['base_trip']=None
        self.assertIsNone(interstate_summary(validate_model(m)['interstate'])['cost_4w'])
        m['interstate']['lanes']['sydney_brisbane_bdouble']['base_trip']=0
        m['interstate']['bookings'][0]['kg_trip']=None
        s=interstate_summary(validate_model(m)['interstate'])
        self.assertEqual(s['cost_4w'],200)
        self.assertIsNone(s['lanes']['sydney_brisbane_bdouble']['cost_per_kg'])

    def test_movements_share_local_hours_but_not_collection_counts(self):
        m=transport_model()
        for kind in ['deliver_decomm','collect_decomm','deliver_threadtexx','deliver_blocktexx','return_storage']:
            m['states']['NSW']['runs'][0]['activity_type']=kind
            self.assertEqual(validate_model(m)['states']['NSW']['runs'][0]['activity_type'],kind)
        m['states']['NSW']['runs'][0]['partner_id']='unknown'
        with self.assertRaises(ValueError):validate_model(m)

if __name__=='__main__':unittest.main()
