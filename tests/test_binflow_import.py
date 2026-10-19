import copy
import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app import create_app
from binflow_import import IMPORT_ID, import_blocktexx, profiles


class RegisterImportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = str(Path(self.temp.name) / 'import.db')
        self.config = dict(TESTING=True, DATABASE=self.path, SECRET_KEY='test-import-secret-'*4,
                           ADMIN_EMAIL='admin@example.com', ADMIN_PASSWORD='test-admin-password', SESSION_COOKIE_SECURE=False)
        self.app = create_app(self.config)
        self.conn = sqlite3.connect(self.path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute('PRAGMA foreign_keys=ON')
        self.conn.execute("INSERT INTO bf_clients(id,name,contact,created_at) VALUES(1,'GroupCo','Keep this contact','test')")
        self.conn.execute("INSERT INTO bf_locations(id,kind,name,state,address,created_at) VALUES(1,'warehouse','Existing warehouse','NSW','1 Depot Rd','test')")
        self.conn.execute("INSERT INTO bf_locations(id,kind,client_id,name,state,address,created_at) VALUES(2,'client',1,'GroupCo - North','NSW','2 Shared Rd','test')")
        self.conn.execute("INSERT INTO bf_bins(code,token,size_id,state,location_id,status,tare_g,created_at) VALUES('BF000001','keep-token',1,'NSW',1,'empty',5000,'test')")
        self.model = dict(sites=[
            dict(id='one',name='Blocktexx - GroupCo - North',state='NSW',address='2 Shared Rd',frequency='Weekly',containers={'bin240':2},notes='Confirm site access'),
            dict(id='two',name='GroupCo - South',state='NSW',address='2 Shared Rd',frequency='Adhoc',equipment='4 cages'),
            dict(id='three',name='Other business',state='QLD',address='Street address missing',frequency='To confirm')],
            partners=[dict(id='partner',name='Processing partner',state='VIC',address='3 Processing Rd',frequency='Monthly')],
            weight_history={'pickups':[dict(site_id='one',kg=80,date='2026-10-01',docket='sample-1',profile='sample'),
                                       dict(site_id='one',kg=100,date='2026-10-08',docket='sample-2',profile='sample')]})
        self.save_source()

    def save_source(self):
        self.conn.execute('INSERT OR REPLACE INTO blocktexx_model VALUES(1,7,?,1,?)', (json.dumps(self.model),'2026-10-19'))
        self.conn.commit()

    def rows(self, table):
        return [dict(r) for r in self.conn.execute('SELECT * FROM '+table)]

    def tearDown(self):
        self.conn.close()
        self.temp.cleanup()

    def test_import_groups_deduplicates_and_preserves_real_stock(self):
        clients, bins, source = self.rows('bf_clients'), self.rows('bf_bins'), self.rows('blocktexx_model')
        warehouse = dict(self.conn.execute('SELECT * FROM bf_locations WHERE id=1').fetchone())
        summary, changed = import_blocktexx(self.conn, ['GroupCo'], '2026-10-19')
        self.assertTrue(changed)
        self.assertEqual(summary['locations_added'],3)
        self.assertEqual(summary['locations_matched'],1)
        self.assertEqual(summary['client_accounts'],3)
        self.assertEqual(summary['address_review'],1)
        self.assertEqual(self.rows('bf_clients')[0],clients[0])
        self.assertEqual(self.rows('bf_bins'),bins)
        self.assertEqual(self.rows('blocktexx_model'),source)
        self.assertEqual(dict(self.conn.execute('SELECT * FROM bf_locations WHERE id=1').fetchone()),warehouse)
        self.assertFalse(self.rows('bf_jobs'))
        profile = profiles(self.conn,2)[0]
        self.assertEqual(profile['average_kg'],90)
        self.assertEqual(profile['source']['containers'],{'bin240':2})
        self.assertEqual(summary,import_blocktexx(self.conn,['GroupCo'],'later')[0])
        self.assertFalse(import_blocktexx(self.conn,['GroupCo'],'later')[1])
        self.assertEqual(len(self.rows('bf_location_sources')),4)
        # Later edits survive restart/import, including edits to imported names.
        self.conn.execute("UPDATE bf_locations SET name='New branch name' WHERE id=2");self.conn.commit()
        import_blocktexx(self.conn,['GroupCo'],'later')
        self.assertEqual(profiles(self.conn,2)[0]['source']['name'],'Blocktexx - GroupCo - North')
        self.assertEqual(self.conn.execute('SELECT name FROM bf_locations WHERE id=2').fetchone()[0],'New branch name')
        client=self.app.test_client()
        # Use the real login so role/CSRF behavior remains the app's responsibility.
        with client.session_transaction() as session: session['csrf']='import-test'
        client.post('/admin/login',data={'username':'admin','password':'test-admin-password','csrf':'import-test'})
        self.assertIn(b'Blocktexx register populated',client.get('/binflow/setup').data)
        self.assertIn(b'2 \xc3\x97 240 L bins',client.get('/binflow/locations/2/edit').data)
        self.assertIn(b'90.00 kg',client.get('/binflow/pickups/new?site=2&warehouse=1').data)

    def test_invalid_source_rolls_back_every_record(self):
        before = {t:self.rows(t) for t in ('bf_clients','bf_locations','bf_bins','bf_import_runs','bf_location_sources')}
        self.model['partners'][0]['state']='INVALID'
        self.save_source()
        with self.assertRaises(ValueError): import_blocktexx(self.conn,['GroupCo'],'2026-10-19')
        self.assertEqual({t:self.rows(t) for t in before},before)
        self.model['partners'][0]['state']='VIC'
        self.model['sites'].append(copy.deepcopy(self.model['sites'][0]))
        self.save_source()
        with self.assertRaises(sqlite3.IntegrityError): import_blocktexx(self.conn,['GroupCo'],'2026-10-19')
        self.assertEqual({t:self.rows(t) for t in before},before)

    def test_private_opt_in_import_runs_once_on_startup(self):
        # Startup's existing model migrations require the full validated model;
        # the importer only reads it and must not rewrite it.
        from blocktexx import empty_model, validate_model
        m=empty_model();m['capacity_version']=1;m.update(self.model)
        self.model=validate_model(m);self.save_source()
        with patch.dict(os.environ, {'BINFLOW_IMPORT_BLOCKTEXX':IMPORT_ID,'BINFLOW_BLOCKTEXX_GROUPS_JSON':'["GroupCo"]'}):
            create_app(self.config)
            create_app(self.config)
        self.assertEqual(len(self.rows('bf_import_runs')),1)
        self.assertEqual(len(self.rows('bf_location_sources')),4)
        self.assertEqual(len(self.rows('bf_bins')),1)


if __name__ == '__main__':
    unittest.main()
