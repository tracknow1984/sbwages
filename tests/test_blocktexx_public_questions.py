import json
import sqlite3
import tempfile
import unittest
from app import create_app
from blocktexx import empty_model, validate_model
from blocktexx_public_questions import question_bank

class PublicQuestionsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = self.tmp.name + '/test.db'
        self.app = create_app({'TESTING': True, 'DATABASE': self.path, 'SECRET_KEY': 'test-'*10,
                              'ADMIN_EMAIL': 'admin@example.com', 'ADMIN_PASSWORD': 'test-password', 'SESSION_COOKIE_SECURE': False})
        self.client = self.app.test_client()
        self.client.get('/blocktexx/questions')
        with self.client.session_transaction() as session:
            self.csrf = session['csrf']
        self.payload = {'respondent': 'Test Person', 'email': 'test@example.com', 'organisation': 'BlockTexx',
                        'answers': {'q001': {'answer': 'External response', 'status': 'answered'}}, 'revision': 0, 'submit': False}
    def tearDown(self): self.tmp.cleanup()
    def save(self, payload=None, csrf=None):
        return self.client.post('/blocktexx/questions/save', data={'csrf': self.csrf if csrf is None else csrf, 'reply': json.dumps(payload or self.payload)})
    def admin(self):
        client = self.app.test_client()
        with sqlite3.connect(self.path) as db: uid, version = db.execute("SELECT id,auth_version FROM users WHERE role='admin'").fetchone()
        with client.session_transaction() as s: s.update(uid=uid, version=version, csrf='admin-test')
        return client
    def test_public_only_and_browser_isolation(self):
        model = validate_model(empty_model())
        model['clarification_answers']['q001'] = {'answer': 'PRIVATE-SECRET-ANSWER', 'status': 'answered'}
        self.assertEqual(self.admin().post('/admin/blocktexx', data={'csrf':'admin-test','revision':0,'model':json.dumps(model)}).status_code,200)
        page = self.client.get('/blocktexx/questions').get_data(as_text=True)
        self.assertNotIn('PRIVATE-SECRET-ANSWER',page)
        self.assertNotIn('bx-pricing',page)
        self.assertEqual(self.save().status_code,200)
        self.assertIn('External response',self.client.get('/blocktexx/questions').get_data(as_text=True))
        second = self.app.test_client()
        self.assertNotIn('External response',second.get('/blocktexx/questions').get_data(as_text=True))
        self.assertEqual(second.get('/admin/blocktexx').status_code,302)
        self.assertEqual(second.get('/admin/blocktexx/responses').status_code,302)
        self.assertEqual(len(question_bank()),141)
    def test_csrf_validation_conflict_and_submit(self):
        self.assertEqual(self.save(csrf='wrong').status_code,400)
        bad = {**self.payload,'answers':{'unknown':{'answer':'bad'}}}
        self.assertEqual(self.save(bad).status_code,400)
        bad = {**self.payload,'answers':{'q001':{'answer':'a'*3001,'status':'answered'}}}
        self.assertEqual(self.save(bad).status_code,400)
        bad = {**self.payload,'model':{}}
        self.assertEqual(self.save(bad).status_code,400)
        self.assertEqual(self.save().json['revision'],1)
        self.assertEqual(self.save().status_code,409)
        submitted = {**self.payload,'revision':1,'submit':True}
        self.assertEqual(self.save(submitted).json['status'],'submitted')
        self.assertIn('External response',self.admin().get('/admin/blocktexx/responses').get_data(as_text=True))
    def test_import_only_answers_preserves_private_model(self):
        admin = self.admin()
        model = validate_model(empty_model())
        model['states']['QLD']['monthly_kg'] = 12345
        self.assertEqual(admin.post('/admin/blocktexx',data={'csrf':'admin-test','revision':0,'model':json.dumps(model)}).status_code,200)
        self.assertEqual(self.save({**self.payload,'submit':True}).status_code,200)
        with sqlite3.connect(self.path) as db: response_id = db.execute('SELECT id FROM blocktexx_public_replies').fetchone()[0]
        self.assertEqual(self.client.post('/admin/blocktexx/responses/'+response_id+'/apply',data={'csrf':self.csrf}).status_code,302)
        self.assertEqual(admin.post('/admin/blocktexx/responses/'+response_id+'/apply',data={'csrf':'admin-test'}).status_code,302)
        with sqlite3.connect(self.path) as db:
            revision,raw = db.execute('SELECT revision,data FROM blocktexx_model').fetchone()
        saved = json.loads(raw)
        self.assertEqual(saved['states']['QLD']['monthly_kg'],12345)
        self.assertEqual(saved['clarification_answers']['q001']['answer'],'External response')
        self.assertEqual(revision,2)
    def test_multiple_long_answers_and_script_escape(self):
        answers = {q['id']:{'answer':'a'*3000,'status':'answered'} for q in question_bank()}
        self.assertEqual(self.save({**self.payload,'answers':answers}).status_code,200)
        payload = {**self.payload,'revision':1,'answers':{'q001':{'answer':'</script><script>alert(1)</script>','status':'answered'}}}
        self.assertEqual(self.save(payload).status_code,200)
        self.assertNotIn('</script><script>alert',self.client.get('/blocktexx/questions').get_data(as_text=True))
        self.assertNotIn('<script>alert(1)',self.admin().get('/admin/blocktexx/responses').get_data(as_text=True))

if __name__=='__main__': unittest.main()
