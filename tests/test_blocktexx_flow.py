import unittest
from blocktexx import empty_model, validate_model

class FlowLayoutTests(unittest.TestCase):
    def test_positions_round_trip(self):
        model = empty_model()
        model['process_flow_layout'] = {'dispatch': {'x': 50, 'y': 110}, 'net_hold': {'x': 475, 'y': 740}}
        self.assertEqual(validate_model(model)['process_flow_layout'], model['process_flow_layout'])
        self.assertEqual(validate_model(empty_model())['process_flow_layout'], {})

    def test_invalid_positions_rejected(self):
        for layout in ({'extra': {'x': 1, 'y': 1}}, {'dispatch': {'x': -1, 'y': 0}}, {'dispatch': {'x': 1, 'y': 900}}):
            model = empty_model()
            model['process_flow_layout'] = layout
            with self.assertRaises(ValueError):
                validate_model(model)
