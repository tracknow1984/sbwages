import copy
import unittest
from blocktexx import empty_model, calendar_slots, validate_model, summarize
from blocktexx_costs import FIELDS, cost_comparison, validate_cost_profile


def priced_model():
    model=empty_model()
    state=model['states']['VIC']
    state.update(cost_mode='contractor',monthly_kg=1000)
    p={key:0 for key in FIELDS}
    p.update(enabled=True,contractor_basis='hourly',staff_qty=1,staff_hourly=30,
             paid_hours_week=38,workers_comp_pct=2,super_pct=12,truck_insurance_month=100,
             truck_lease_month=1000,fuel_month=500,building_insurance_month=200,
             building_lease_month=2000,contractor_hourly=125,contractor_daily=900,
             minimum_hours=4,free_wait_minutes=30,demurrage_hourly=80)
    state['cost_profile']=p
    state['runs']=[dict(id=str(i),name='Test run',sequence='',notes='',evidence='',status='estimated',
                       site_ids=[],runs_4w=4,km=30,drive_min=60,service_min=30,depot_min=15,
                       prep_min=15,wait_min=60,break_min=30,
                       planner_slots=[dict(week=w,day=0) for w in range(1,5)]) for i in range(2)]
    return model


class CostTests(unittest.TestCase):
    def test_group_days_minimum_demurrage_and_staff(self):
        d=priced_model()['states']['VIC']
        c=cost_comparison(d,calendar_slots)
        self.assertAlmostEqual(c['wages'],4940)
        self.assertAlmostEqual(c['owned'],9431.6)
        self.assertAlmostEqual(c['days_month'],13/3)
        self.assertAlmostEqual(c['billed_hours_month'],19.5)
        self.assertAlmostEqual(c['demurrage'],520)
        self.assertAlmostEqual(c['hourly'],5157.5)
        self.assertAlmostEqual(c['daily'],6620)
        self.assertTrue(c['schedule_complete'])

    def test_missing_and_zero_prices_are_distinct(self):
        d=priced_model()['states']['VIC']
        d['cost_profile']['fuel_month']=None
        self.assertIsNone(cost_comparison(d,calendar_slots)['owned'])
        d['cost_profile']['fuel_month']=0
        self.assertIsNotNone(cost_comparison(d,calendar_slots)['owned'])
        d['runs'][0]['drive_min']=None
        self.assertIsNone(cost_comparison(d,calendar_slots)['hourly'])
        self.assertFalse(cost_comparison(d,calendar_slots)['schedule_complete'])

    def test_ad_hoc_calendar_days_count_and_unallocated_withholds_rate(self):
        m=priced_model()
        for run in m['states']['VIC']['runs']:
            run['runs_4w']=None
        self.assertAlmostEqual(cost_comparison(m['states']['VIC'],calendar_slots)['daily'],6620)
        # Recurring unallocated work stays visible as provisional cost, but not a quote/kg.
        for run in m['states']['VIC']['runs']:
            run.update(runs_4w=4,planner_slots=[])
        self.assertIsNone(summarize(validate_model(m))['VIC']['collection_per_kg'])

    def test_rates_validate_and_legacy_is_unchanged(self):
        p=priced_model()['states']['VIC']['cost_profile']
        for key,value in [('staff_qty',1.5),('fuel_month',-1),('staff_hourly',float('nan')),
                          ('workers_comp_pct',101),('contractor_basis','both')]:
            bad=copy.deepcopy(p);bad[key]=value
            with self.assertRaises(ValueError):validate_cost_profile(bad)
        m=priced_model();m['states']['VIC'].update(hourly_rate=100,minimum_hours=0)
        m['states']['VIC']['cost_profile']['enabled']=False
        self.assertAlmostEqual(summarize(validate_model(m))['VIC']['monthly_cost'],2600)

if __name__=='__main__':
    unittest.main()
