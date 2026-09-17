import unittest
from blocktexx_capacity import KINDS, DEFAULT_SPACES, plan_run


def make_plan(quantities):
    sites = [dict(id=str(i), name='Customer '+str(i), address='Test address',
                  containers={k: quantities[i].get(k, 0) for k in KINDS}) for i in range(len(quantities))]
    truck = dict(pallet_positions=14, payload_kg=None, spaces=DEFAULT_SPACES,
                 weights_kg=dict.fromkeys(KINDS))
    model = dict(sites=sites, states={'NSW': {'truck': truck}})
    return plan_run(model, 'NSW', dict(site_ids=[s['id'] for s in sites], included_loads=1))


class ExchangeTests(unittest.TestCase):
    def test_full_truck_swaps_without_double_counting_space(self):
        p=make_plan([{'bin660':6},{'bin660':8}])
        self.assertEqual(p['load_count'],1)
        load=p['loads'][0]
        self.assertEqual(load['outbound_empty']['bin660'],14)
        self.assertEqual(load['return_full']['bin660'],14)
        first,last=load['stops']
        self.assertEqual(first['onboard_empty_after']['bin660'],8)
        self.assertEqual(first['onboard_full_after']['bin660'],6)
        self.assertEqual(last['onboard_empty_after']['bin660'],0)
        self.assertEqual(last['onboard_full_after']['bin660'],14)
        self.assertTrue(all(s['onboard_spaces_after']==14 for s in load['stops']))
        self.assertEqual(p['customer_container_moves'],28)

    def test_each_depot_load_gets_its_own_empties(self):
        p=make_plan([{'bin660':15}])
        self.assertEqual([l['outbound_empty']['bin660'] for l in p['loads']],[14,1])
        self.assertEqual(p['outbound_empty']['bin660'],15)
        self.assertEqual(p['extra_loads'],1)
        for load in p['loads']:
            self.assertEqual(load['outbound_empty'],load['return_full'])

    def test_matching_types_and_quantity_changes(self):
        p=make_plan([{'cage':5},{'bin240':6,'bin660':3}])
        self.assertEqual(p['load_count'],1)
        self.assertEqual(p['loads'][0]['spaces'],11)
        for stop in p['loads'][0]['stops']:
            self.assertEqual(stop['deliver_empty'],stop['collect_full'])
        changed=make_plan([{'cage':5},{'bin240':6,'bin660':7}])
        self.assertEqual(changed['load_count'],2)
        self.assertEqual(changed['outbound_empty']['bin660'],7)
