import os
import re
import sqlite3
import unittest
from unittest.mock import patch

import test_binflow as legacy
import binflow_road_routes as roads


class RoadRoutesTests(unittest.TestCase):
    for name in ('setUp','tearDown','rows','post','login'):
        locals()[name]=getattr(legacy.BinFlowTests,name)

    def test_matrix_uses_addresses_and_maps_unordered_partial_results(self):
        response=[dict(destinationIndex=2,originIndex=0,status={},condition='ROUTE_EXISTS',distanceMeters=12345,duration='650.2s'),
                  dict(destinationIndex=1,status={'code':5},condition='ROUTE_NOT_FOUND'),
                  dict(condition='ROUTE_EXISTS',distanceMeters=1200,duration='60s')]
        with patch.object(roads,'call',return_value=response) as call:
            values=roads.matrix('hidden','21/19-26 Durian Place Wetherill Park NSW Australia',['A','B','C'])
        self.assertEqual(set(values),{0,2})
        self.assertEqual(values[2],dict(distance_km=12.3,drive_minutes=11))
        payload=call.call_args.args[2]
        self.assertEqual(payload['origins'][0]['waypoint']['address'],'21/19-26 Durian Place Wetherill Park NSW Australia')
        self.assertEqual(payload['destinations'][2]['waypoint']['address'],'C')
        self.assertEqual(payload['travelMode'],'DRIVE')

    def test_popup_selects_real_depot_and_sorts_road_distance_not_straight_line(self):
        from binflow_live_cycle import provision
        with sqlite3.connect(self.path) as c:
            c.row_factory=sqlite3.Row;provision(c,'2026-11-24')
        def response(key,origin,addresses):
            self.assertEqual(origin,'1 Depot Road, QLD, Australia')
            self.assertEqual(len(addresses),2)
            return {0:dict(distance_km=12.1,drive_minutes=18),1:dict(distance_km=45.6,drive_minutes=52)}
        with patch.dict(os.environ, BINFLOW_GOOGLE_ROUTES_API_KEY='hidden'),patch.object(roads,'matrix',side_effect=response):
            html=self.client.get('/binflow/pickups/new?calendar=1&state=QLD&date=2026-11-30').text
        selector=re.search(r'<select name="site".*?</select>',html,re.S)[0]
        self.assertLess(selector.index('Beta QLD'),selector.index('Alpha QLD'))
        self.assertIn('45.6 km road',html)
        self.assertIn('Starting point:',html);self.assertNotIn('LIVE TEST',html)
        self.assertNotIn('hidden',selector)

    def test_missing_depot_does_not_call_google_and_address_edits_are_used(self):
        with self.app.app_context(),sqlite3.connect(self.path) as c:
            c.row_factory=sqlite3.Row
            sites=[dict(id=3,address='123 Client Street',state='SA')]
            with patch.dict(os.environ,BINFLOW_GOOGLE_ROUTES_API_KEY='hidden'),patch.object(roads,'matrix',return_value={}) as matrix:
                values,message,_=roads.distances(c,dict(id=4,address='',state='SA'),sites)
                self.assertFalse(values);self.assertIn('pending',message);matrix.assert_not_called()
                roads.distances(c,dict(id=4,address='1 First Road Adelaide',state='SA'),sites)
                self.assertEqual(matrix.call_args.args[1],'1 First Road Adelaide, SA, Australia')
                roads.distances(c,dict(id=4,address='2 New Road Adelaide',state='SA'),sites)
                self.assertEqual(matrix.call_args.args[1],'2 New Road Adelaide, SA, Australia')

    def test_setup_depot_overrides_old_cost_model_depot(self):
        import json
        from binflow_collections import planning_resources
        with sqlite3.connect(self.path) as c:
            c.row_factory=sqlite3.Row
            c.execute("INSERT INTO blocktexx_model VALUES(1,1,?,'now',1)",(json.dumps({'states':{'NSW':{'depot':'Old Ingleburn depot'}}}),))
            c.execute("UPDATE bf_locations SET address='21/19-26 Durian Place Wetherill Park' WHERE id=2")
            self.assertEqual(planning_resources(c)['NSW']['depot'],'21/19-26 Durian Place Wetherill Park')

    def test_round_trip_keeps_stop_order_and_returns_to_same_depot(self):
        with patch.object(roads,'call',return_value={'routes':[dict(distanceMeters=95000,duration='5400s')]}) as call:
            result=roads.round_trip('hidden','Depot',['Farthest','Nearer'])
        payload=call.call_args.args[2]
        self.assertEqual(payload['origin'],payload['destination'])
        self.assertEqual(payload['intermediates'],[{'address':'Farthest'},{'address':'Nearer'}])
        self.assertEqual(result,dict(distance_km=95.0,drive_minutes=90))

    def test_key_is_checked_encrypted_not_echoed_and_csrf_required(self):
        key='AIza'+'x'*35
        path='/binflow/setup/road-distances'
        self.assertEqual(self.client.post(path,data={'api_key':key}).status_code,400)
        with patch.object(roads,'matrix',return_value={0:dict(distance_km=5,drive_minutes=6)}):
            response=self.post(path,{'api_key':key})
        self.assertIn('connected',response.text.lower());self.assertNotIn(key,response.text)
        row=self.rows('SELECT key_cipher FROM bf_road_routing')[0]
        self.assertNotIn(key.encode(),row['key_cipher'])
        with self.app.app_context(),sqlite3.connect(self.path) as c:
            c.row_factory=sqlite3.Row
            self.assertEqual(roads.api_key(c),key)
        with patch.object(roads,'matrix',side_effect=ValueError('Connection unavailable.')):
            response=self.post(path,{'api_key':'AIza'+'y'*35})
        self.assertIn('Connection unavailable',response.text)
        self.assertEqual(row,self.rows('SELECT key_cipher FROM bf_road_routing')[0])


if __name__=='__main__':unittest.main()
