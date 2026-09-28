import unittest
from blocktexx import empty_model, validate_model

class ClarificationTests(unittest.TestCase):
    def test_answers_round_trip(self):
        model = empty_model()
        model['clarification_answers'] = {
            'q001': {'answer': 'Agreed scope', 'status': 'answered'},
            'state-wa-1': {'answer': 'Awaiting confirmation', 'status': 'awaiting'}}
        self.assertEqual(validate_model(model)['clarification_answers'], model['clarification_answers'])
        self.assertEqual(validate_model(empty_model())['clarification_answers'], {})

    def test_invalid_answers(self):
        for invalid in ({'bad': {'answer': 'a'}}, {'q001': {'answer': 'x' * 3001}}, {'q001': {'status': 'invalid'}}):
            model = empty_model()
            model['clarification_answers'] = invalid
            with self.assertRaises(ValueError):
                validate_model(model)
