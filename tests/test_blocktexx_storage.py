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
