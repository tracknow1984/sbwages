import unittest
from blocktexx_storage import validate_storage


class StorageTests(unittest.TestCase):
    def test_private_inputs_roundtrip_and_precision(self):
        raw=dict(total_containers=100,occupied_containers=80,monthly_rate=123.4567,
                 free_containers=5,current_pallets=20,new_pallets=50,repack_cost=200,
                 containers_per_day=1,minimum_containers=80,lease_months=36,
                 days_per_week=5,start_date='2026-09-28',recovery='monthly')
        self.assertEqual(validate_storage(raw),raw)
        self.assertIsNone(validate_storage({})['monthly_rate'])
        for key,value in [('occupied_containers',101),('free_containers',101),('new_pallets',0),
                          ('monthly_rate',float('nan')),('minimum_containers',1.5),
                          ('days_per_week',True),('start_date','2026-02-30'),('recovery','other')]:
            with self.assertRaises(ValueError):validate_storage({**raw,key:value})

    def test_section_counts_and_ranges(self):
        raw={'total_containers':70,'occupied_containers':60,'sections':{
            'NSW':{'filled':10,'pallets_min':20,'pallets_max':30,'target':50},
            'BANYO':{'filled':20,'pallets_min':10,'pallets_max':15,'target':30},
            'BAGS':{'filled':30,'pallets_min':20,'pallets_max':25,'target':50}}}
        self.assertEqual(validate_storage(raw)['sections'],raw['sections'])
        import copy
        for key,value in [('filled',11),('filled',9),('filled',True),('pallets_min',31),('target',0)]:
            bad=copy.deepcopy(raw);bad['sections']['NSW'][key]=value
            with self.assertRaises(ValueError):validate_storage(bad)
        partial=copy.deepcopy(raw);partial['sections']['NSW']['filled']=None
        self.assertIsNone(validate_storage(partial)['sections']['NSW']['filled'])
