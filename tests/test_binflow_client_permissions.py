import json
import re
import sqlite3
import unittest

import test_binflow as legacy
from binflow_client_access import revision
from binflow_collections import STATES
from binflow_freight_rates import VEHICLES
from pod_helpers import sign_pod
from test_binflow_interstate import CAL, CLIENT, data


class PortalPermissionTests(unittest.TestCase):
    setUp=legacy.BinFlowTests.setUp
    tearDown=legacy.BinFlowTests.tearDown
    post=legacy.BinFlowTests.post
    rows=legacy.BinFlowTests.rows
    login=legacy.BinFlowTests.login
    job=legacy.BinFlowTests.job
    exchange=legacy.BinFlowTests.exchange
    initial_delivery=legacy.BinFlowTests.initial_delivery

    def account(self,name,**fields):
        result=self.post(CAL+'/access',dict(name=name,username=name,password='sample-password',active='yes',**fields))
        self.assertEqual(result.status_code,200,result.text[:80])
        return self.rows('SELECT id FROM bf_client_accounts WHERE username=?',(name,))[0]['id']

    def visit(self,name):
        client=self.app.test_client()
        client.get('/binflow/client/login')
        with client.session_transaction() as session:
            csrf=session['csrf']
        result=client.post('/binflow/client/login',data=dict(csrf=csrf,username=name,password='sample-password'),follow_redirects=True)
        self.assertEqual(result.status_code,200)
        return client

    def send(self,client,path,fields):
        with client.session_transaction() as session:
            csrf=session['csrf']
        return client.post(path,data={**fields,'csrf':csrf},follow_redirects=True)

    def rights(self,aid,original,selection,scope='scoped'):
        return self.post(f'{CAL}/access/{aid}/permissions',dict(version=revision(original),permission=selection,pod_scope=scope))

    def test_create_with_selected_permissions_and_change_without_relogin(self):
        before=['profiles.view','profiles.add']
        aid=self.account('site-editor',custom_permissions='yes',permission=['profiles.add'])
        client=self.visit('site-editor')
        directory='/binflow/client/profiles'
        self.assertIn('Add client profile',client.get(directory).text)
        self.assertEqual(client.get(CLIENT).status_code,403)
        form=dict(customer_site='New portal site',client_id='new',state='NSW',address='10 QA Road')
        response=self.send(client,directory+'/new',form)
        self.assertEqual(response.status_code,200)
        profile=self.rows("SELECT * FROM bf_client_site_profiles WHERE customer_site='New portal site'")[0]
        detail=f"{directory}/{profile['id']}"
        self.assertNotIn('Edit profile',client.get(detail).text)
        self.assertEqual(self.send(client,detail+'/edit',form).status_code,403)
        after=['profiles.view','profiles.edit']
        self.assertEqual(self.rights(aid,before,['profiles.edit']).status_code,200)
        self.assertEqual(self.rights(aid,before,before).status_code,409)
        self.assertEqual(client.get(directory+'/new').status_code,403)
        form.update(version='1',address='12 QA Road')
        self.assertEqual(self.send(client,detail+'/edit',form).status_code,200)
        self.assertEqual(self.send(client,detail+'/edit',form).status_code,409)
        self.assertEqual(self.rows('SELECT address FROM bf_locations WHERE id=?',(profile['location_id'],))[0]['address'],'12 QA Road')
        self.assertEqual(self.rights(aid,after,[]).status_code,200)
        self.assertEqual(client.get(detail).status_code,403)
        self.assertIn('no portal permissions',self.send(client,'/binflow/client/login',dict(username='site-editor',password='sample-password')).text)

    def test_named_account_can_read_national_data_only_when_granted(self):
        aid=self.account('BlockTexx1')
        self.account('another-client')
        main=self.visit('blocktexx1')
        other=self.visit('another-client')
        national='/binflow/client/local-pickups?week=2026-10-19'
        plan={'days':[{'date':'2026-10-19','items':[{'kind':'visit','site_id':3,'notes':'PRIVATE'}]}]}
        with sqlite3.connect(self.path) as conn:
            conn.execute("""INSERT INTO bf_schedule_weeks(id,state,week_start,revision,status,plan_json,updated_at,updated_by)
                VALUES(1,'QLD','2026-10-19',1,'draft',?,'now',1)""",(json.dumps(plan),))
        self.assertNotIn('Alpha QLD',main.get(national).text)
        with sqlite3.connect(self.path) as conn:
            conn.execute("UPDATE bf_schedule_weeks SET status='published'")
        self.assertIn('Alpha QLD',main.get(national).text)
        self.assertNotIn('PRIVATE',main.get(national).text)
        self.assertNotIn('Alpha QLD',main.get(national+'&state=NSW').text)
        self.assertEqual(other.get(national).status_code,403)
        self.assertEqual(main.get('/binflow/client/profiles/new').status_code,403)
        self.assertIn('Alpha NSW',main.get('/binflow/client/profiles').text)
        self.initial_delivery()
        token=self.rows('SELECT pod_token FROM bf_jobs WHERE id=1')[0]['pod_token']
        pdf='/binflow/client/pods/1.pdf'
        docket='/binflow/pod/'+token
        self.assertEqual(main.get(pdf).status_code,404)
        sign_pod(self,1)
        self.assertIn('BF-000001',main.get('/binflow/client/pods').text)
        self.assertEqual(main.get(pdf).mimetype,'application/pdf')
        self.assertEqual(main.get(docket).status_code,200)
        self.assertEqual(other.get(pdf).status_code,403)
        self.assertEqual(other.get(docket).status_code,404)
        all_rights=['bookings.view','bookings.add','pods.view','pods.all','profiles.view','national.view']
        self.rights(aid,all_rights,['pods.view'])
        self.assertEqual(main.get(pdf).status_code,404)
        self.assertEqual(main.get(docket+'/signature.png').status_code,404)
        sites=f'{CAL}/access/{aid}/sites'
        version=re.search('name="version" value="([^"]+)"',self.client.get(sites).text)[1]
        self.post(sites,dict(version=version,site_id=[3]))
        self.assertEqual(main.get(pdf).mimetype,'application/pdf')
        self.assertEqual(main.get(national).status_code,403)
        with sqlite3.connect(self.path) as conn:
            conn.execute("UPDATE bf_jobs SET pod_deleted_at='now' WHERE id=1")
        self.assertEqual(main.get(pdf).status_code,404)
        self.assertEqual(main.get(docket).status_code,404)

    def test_rate_card_snapshot_client_isolation_and_bulk_vehicle_rules(self):
        aid=self.account('freight-client',custom_permissions='yes',permission=['bookings.add','bookings.edit'])
        self.account('second-client')
        path=f'{CAL}/access/{aid}/rates?origin=VIC'
        def save_rate(value):
            page=self.client.get(path)
            fields={state+':'+vehicle:'' for state in STATES if state!='VIC' for vehicle in VEHICLES}
            fields.update({'QLD:RIGID':value,'version':re.search('name="version" value="([^"]+)"',page.text)[1]})
            self.assertEqual(self.post(path,fields).status_code,200)
        save_rate('123.45')
        first=self.visit('freight-client')
        second=self.visit('second-client')
        self.assertNotIn('12345',second.get(CLIENT+'/new').text)
        form=data(origin='VIC',destination='QLD',handling='scanned',shown_rate='12345',rate_cents='1')
        self.assertEqual(self.send(first,CLIENT+'/new',dict(form,vehicle='VAN')).status_code,400)
        self.assertEqual(self.send(first,CLIENT+'/new',dict(form,goods='')).status_code,400)
        self.assertEqual(self.send(first,CLIENT+'/new',form).status_code,200)
        self.assertEqual(self.rows('SELECT handling,vehicle,rate_cents FROM bf_interstate_bookings'),
                         [dict(handling='bulk',vehicle='RIGID',rate_cents=12345)])
        self.assertEqual(second.get(CLIENT+'/1').status_code,404)
        self.assertEqual(second.get(CLIENT+'/1/edit').status_code,403)
        save_rate('0.00')
        self.assertEqual(self.send(first,CLIENT+'/new',form).status_code,200)
        self.assertEqual(self.send(first,CLIENT+'/new',data(shown_rate='12345')).status_code,400)
        self.assertEqual(self.send(first,CLIENT+'/1/edit',dict(form,version=1)).status_code,200)
        self.assertIn('123.45',first.get(CLIENT+'/1').text)
        self.assertIn('0.00',self.send(first,CLIENT+'/new',data(shown_rate='0')).text)
        reverse=data(origin='QLD',destination='VIC',shown_rate='')
        self.assertIn('Price to be confirmed',self.send(first,CLIENT+'/new',reverse).text)
        self.post(CAL+'/1/lock',dict(version=2))
        self.assertEqual(self.send(first,CLIENT+'/1/edit',dict(form,version=3)).status_code,409)
