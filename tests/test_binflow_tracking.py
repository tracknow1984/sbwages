import sqlite3
import time
import unittest
from unittest.mock import patch

import test_binflow as legacy
from binflow_gps import store_units
from werkzeug.security import generate_password_hash


class TrackingTests(unittest.TestCase):
    setUp=legacy.BinFlowTests.setUp
    tearDown=legacy.BinFlowTests.tearDown
    post=legacy.BinFlowTests.post
    rows=legacy.BinFlowTests.rows
    login=legacy.BinFlowTests.login

    def connect(self):
        self.post('/binflow/driver',{'name':'QLD transport','state':'QLD'})
        self.post('/binflow/driver',{'name':'NSW transport','state':'NSW'})
        self.post('/binflow/driver',{'name':'No GPS yet','state':'VIC'})
        self.epoch=int(time.time());self.token='a'*72
        self.units=[dict(unit_id=1,name='QLD GPS <script>bad</script>',latitude=-27.47,longitude=153.02,position_time=self.epoch,speed=42),
                    dict(unit_id=2,name='NSW GPS',latitude=-33.86,longitude=151.2,position_time=self.epoch,speed=0),
                    dict(unit_id=3,name='Unlinked fleet unit',latitude=-37.81,longitude=144.96,position_time=self.epoch,speed=None)]
        with patch('binflow_gps.fetch_units',return_value=self.units):
            self.post('/binflow/settings/gps',{'action':'connect','host':'https://hst-api.wialon.com','token':self.token})
        self.post('/binflow/settings/gps',{'action':'link','truck_id':1,'unit_id':1})
        self.post('/binflow/settings/gps',{'action':'link','truck_id':2,'unit_id':2})
        self.post('/binflow/locations/1/geofence',{'latitude':-27.47,'longitude':153.02,'radius_m':150})
        with sqlite3.connect(self.path) as conn:
            conn.row_factory=sqlite3.Row;store_units(conn,self.units,'test observation')

    def payload(self,state=''):
        response=self.client.get('/binflow/gps/map?state='+state)
        self.assertEqual(response.status_code,200)
        self.assertEqual(response.headers['Cache-Control'],'no-store')
        self.assertNotIn(b'token_cipher',response.data)
        self.assertNotIn(b'a'*72,response.data)
        return response.json

    def test_disconnected_map_and_existing_navigation_routes(self):
        with patch('binflow_gps.fetch_units') as fetch:
            data=self.payload()
            self.assertFalse(data['connected']);self.assertEqual(data['units'],[]);fetch.assert_not_called()
        page=self.client.get('/admin/bin-management?state=QLD')
        self.assertIn(b'Live fleet tracking',page.data)
        self.assertIn(b'/binflow/gps/map?state=QLD',page.data)
        self.assertIn(b'>BINFLOW</a>',page.data)
        self.assertNotIn(b'BIN MANAGEMENT / TRANSPORT',page.data)
        self.assertNotIn(b'>TRANSPORT MODEL</a>',page.data)
        self.assertEqual(self.client.get('/admin/blocktexx').status_code,200)
        self.assertIn('https://tile.openstreetmap.org',page.headers['Content-Security-Policy'])
        self.assertIn("script-src 'self'",page.headers['Content-Security-Policy'])
        self.assertNotIn('unsafe-inline',page.headers['Content-Security-Policy'])
        self.assertNotIn('tile.openstreetmap.org',self.client.get('/admin').headers['Content-Security-Policy'])

    def test_connected_map_state_filters_and_geofences_leave_custody_untouched(self):
        self.connect();stock=self.rows('SELECT * FROM bf_bins');jobs=self.rows('SELECT * FROM bf_jobs')
        data=self.payload();self.assertTrue(data['connected']);self.assertEqual(len(data['units']),4)
        self.assertEqual(data['unlinked_count'],1)
        units={u['key']:u for u in data['units']}
        self.assertEqual(units['truck-1']['status'],'moving');self.assertEqual(units['truck-2']['status'],'stopped')
        self.assertEqual(units['truck-3']['status'],'unlinked');self.assertEqual(units['unit-3']['status'],'current')
        self.assertIn('QLD warehouse',units['truck-1']['places'])
        data=self.payload('QLD');self.assertEqual([u['key'] for u in data['units']],['truck-1'])
        self.assertEqual([g['id'] for g in data['geofences']],[1])
        self.assertEqual(self.payload('NSW')['geofences'],[])
        with patch('binflow_gps.fetch_units') as fetch:
            self.assertEqual(self.client.get('/binflow/gps/map?state=invalid').status_code,400);fetch.assert_not_called()
        self.assertEqual(self.rows('SELECT * FROM bf_bins'),stock);self.assertEqual(self.rows('SELECT * FROM bf_jobs'),jobs)

    def test_stale_error_and_removed_unit_never_appear_live(self):
        self.connect()
        with sqlite3.connect(self.path) as conn:
            conn.execute('UPDATE bf_gps_units SET position_time=? WHERE unit_id=1',(self.epoch-301,))
        unit=self.payload('QLD')['units'][0];self.assertEqual(unit['status'],'stale');self.assertEqual(unit['places'],[])
        with sqlite3.connect(self.path) as conn:
            conn.execute("UPDATE bf_gps_config SET last_poll=0")
        with patch('binflow_gps.fetch_units',side_effect=ValueError('Invalid token')):
            data=self.payload('NSW')
        self.assertTrue(data['error']);self.assertEqual(data['units'][0]['status'],'stale')
        self.assertIsNotNone(data['units'][0]['latitude'])  # Explicitly labelled last known.
        with sqlite3.connect(self.path) as conn:
            conn.row_factory=sqlite3.Row;store_units(conn,[self.units[2]],'permissions changed')
        unit=self.payload('NSW')['units'][0]
        self.assertEqual(unit['status'],'unavailable')
        self.assertIsNone(unit['latitude']);self.assertIsNone(unit['longitude']);self.assertIsNone(unit['position_time']);self.assertIsNone(unit['speed'])
        self.post('/binflow/settings/gps',{'action':'disconnect'})
        self.assertEqual(self.payload()['units'],[])

    def test_staff_only_see_linked_fleet_and_public_has_no_tracking_access(self):
        self.connect();self.post('/logout')
        with patch('binflow_gps.fetch_units') as fetch:
            self.assertEqual(self.client.get('/binflow/gps/map').status_code,302);fetch.assert_not_called()
        with sqlite3.connect(self.path) as conn:
            uid=conn.execute("INSERT INTO users(role,first_name,last_name,email,username,password_hash) VALUES('employee','Map','Driver','map@example.com','map',?)",(generate_password_hash('test-admin-password'),)).lastrowid
        self.login('employee','map')
        self.assertEqual(self.client.get('/binflow/gps/map').status_code,403)
        with sqlite3.connect(self.path) as conn: conn.execute('INSERT INTO bf_access(user_id) VALUES(?)',(uid,))
        data=self.payload();self.assertEqual(len(data['units']),3);self.assertEqual(data['unlinked_count'],0)
        self.assertNotIn('Unlinked fleet unit',str(data));self.assertNotIn('unit-3',[u['key'] for u in data['units']])
        page=self.client.get('/admin/bin-management').data
        self.assertIn(b'>BINFLOW</a>',page);self.assertNotIn(b'>GPS settings</a>',page)
        self.assertEqual(self.client.get('/binflow/settings/gps').status_code,403)

    def test_refresh_is_shared_and_uses_server_saved_token(self):
        self.connect()
        with sqlite3.connect(self.path) as conn: conn.execute('UPDATE bf_gps_config SET last_poll=?',(self.epoch-30,))
        with patch('binflow_gps.time.time',return_value=self.epoch), patch('binflow_gps.fetch_units',return_value=self.units) as fetch:
            self.payload();self.client.get('/binflow/gps/status');self.payload('NSW')
            fetch.assert_called_once_with('https://hst-api.wialon.com',self.token)
        with sqlite3.connect(self.path) as conn:
            conn.execute('UPDATE bf_gps_units SET latitude=NULL,longitude=NULL,position_time=NULL,speed=NULL WHERE unit_id=1')
        self.assertEqual(self.payload('QLD')['units'][0]['status'],'unavailable')

    def test_display_selection_survives_refresh_and_excludes_other_positions(self):
        self.connect()
        before=self.rows('SELECT host,token_cipher FROM bf_gps_config')
        links=self.rows('SELECT * FROM bf_truck_gps')
        self.post('/binflow/settings/gps',{'action':'display','display_unit_id':['1']})
        self.assertEqual([u['unit_id'] for u in self.payload()['units']],[1])
        self.assertEqual(self.payload()['unlinked_count'],0)
        self.assertEqual(self.payload('NSW')['units'],[])
        status=self.client.get('/binflow/gps/status').json['html']
        self.assertIn('QLD transport',status);self.assertNotIn('NSW transport',status)
        with sqlite3.connect(self.path) as conn:
            conn.row_factory=sqlite3.Row
            store_units(conn,self.units+[dict(self.units[2],unit_id=4,name='New unit')],'refresh')
        self.assertEqual([u['unit_id'] for u in self.payload()['units']],[1])
        self.assertEqual(self.rows('SELECT host,token_cipher FROM bf_gps_config'),before)
        self.assertEqual(self.rows('SELECT * FROM bf_truck_gps'),links)
        self.post('/binflow/settings/gps',{'action':'display','display_unit_id':['999']})
        self.assertEqual([u['unit_id'] for u in self.payload()['units']],[1])
        self.post('/binflow/settings/gps',{'action':'display'})
        self.assertEqual(self.payload()['units'],[])
        self.post('/binflow/settings/gps',{'action':'display','display_unit_id':['3']})
        self.assertEqual([u['unit_id'] for u in self.payload()['units']],[3])
        self.assertEqual(self.payload()['unlinked_count'],1)


if __name__=='__main__': unittest.main()
