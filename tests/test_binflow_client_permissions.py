import json
import re
import sqlite3
from html.parser import HTMLParser
import unittest

import test_binflow as legacy
from binflow_client_access import revision
from binflow_collections import STATES
from binflow_freight_rates import VEHICLES
from pod_helpers import sign_pod
from test_binflow_interstate import CAL, CLIENT, data


def matrix_fields(page):
    class FormInputs(HTMLParser):
        def handle_starttag(self, tag, attributes):
            attrs = dict(attributes)
            name = attrs.get('name', '')
            if tag == 'input' and (':' in name or name in ('version','layout','rigid_hourly')):
                values[name] = attrs.get('value','')

    values = {}
    FormInputs().feed(page)
    return values


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
            fields=matrix_fields(page.text)
            fields['VIC:QLD:SEMI TRAILER']=value
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
                         [dict(handling='bulk',vehicle='SEMI TRAILER',rate_cents=12345)])
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

    def test_weekly_fuel_is_shared_and_booked_prices_are_preserved(self):
        from unittest.mock import patch
        fuel_path=CAL+'/access/fuel'
        self.assertEqual(self.post(fuel_path,dict(version=0,percent='-1')).status_code,400)
        self.assertEqual(self.post(fuel_path,dict(version=0,percent='12.50')).status_code,200)
        self.assertEqual(self.post(fuel_path,dict(version=0,percent='10')).status_code,409)
        self.assertEqual(self.post(fuel_path,dict(version=1,percent='1.001')).status_code,400)

        def configure(name,hourly):
            aid=self.account(name)
            path=f'{CAL}/access/{aid}/rates?origin=VIC'
            page=self.client.get(path).text
            self.assertIn('12.5%',page)
            fields=matrix_fields(page)
            fields.update({'VIC:QLD:SEMI TRAILER':'100.04','rigid_hourly':hourly})
            self.assertEqual(self.post(path,fields).status_code,200)
            return self.visit(name)

        first=configure('first-fuel','100.00')
        second=configure('second-fuel','200.00')
        local='/binflow/client/local-rigid/quote'
        self.assertEqual(first.get(fuel_path).status_code,302)
        self.assertEqual(self.send(first,CLIENT+'/new',data(vehicle='RIGID')).status_code,400)
        self.assertIn('value="RIGID"',first.get(CLIENT+'/new').text)
        self.assertEqual(self.post(CAL+'/new',data(vehicle='RIGID')).status_code,400)
        self.assertIn('$450.00',first.get(local+'?hours=2').text)
        self.assertIn('$450.00',first.get(local+'?hours=4').text)
        self.assertIn('$506.25',first.get(local+'?hours=4.5').text)
        self.assertIn('$900.00',second.get(local+'?hours=2').text)
        self.assertEqual(second.get(local+'?hours=-1').status_code,400)
        # Half-cent surcharge rounds up using the booked base amount.
        form=data(shown_rate='10004+1250')
        response=self.send(first,CLIENT+'/new',form)
        self.assertIn('$112.55',response.text)
        self.assertEqual(self.post(fuel_path,dict(version=1,percent='20')).status_code,200)
        self.assertEqual(self.send(second,CLIENT+'/new',data(shown_rate='10004+1250')).status_code,400)
        self.assertIn('$480.00',first.get(local+'?hours=1').text)
        self.assertIn('$960.00',second.get(local+'?hours=1').text)
        self.assertIn('$112.55',self.send(first,CLIENT+'/new',form).text)
        self.assertIn('$120.05',self.send(second,CLIENT+'/new',data(shown_rate='10004+2000')).text)
        self.app.config.update(SMTP_HOST='smtp.example.invalid',SMTP_FROM='operations@example.invalid')
        with patch('binflow_interstate.send_smtp') as send:
            self.post(CAL+'/1/lock',dict(version=1))
            body=send.call_args.args[1].get_content()
            for line in ('Base freight: AUD $100.04 ex GST','Fuel surcharge (12.5%): AUD $12.51 ex GST','Total: AUD $112.55 ex GST'):
                self.assertIn(line,body)

    def test_preexisting_rigid_freight_is_not_converted_to_hourly(self):
        from binflow_freight_rates import migrate
        aid=self.account('legacy-rigid',custom_permissions='yes',permission=['bookings.add','bookings.edit'])
        client=self.visit('legacy-rigid')
        self.send(client,CLIENT+'/new',data())
        with sqlite3.connect(self.path) as conn:
            conn.row_factory=sqlite3.Row
            conn.execute("UPDATE bf_interstate_bookings SET rate_cents=180000,vehicle='RIGID' WHERE id=1")
            conn.execute("INSERT INTO bf_interstate_rates VALUES(?,'VIC','QLD','RIGID',180000)",(aid,))
            migrate(conn)
        self.assertIn('Price to be confirmed',client.get('/binflow/client/local-rigid/quote').text)
        self.assertNotIn('180000',client.get(CLIENT+'/new').text)
        self.assertIn('$1,800.00',client.get(CLIENT+'/1').text)
        self.post(CAL+'/access/fuel',dict(version=0,percent='25'))
        response=self.send(client,CLIENT+'/1/edit',data(vehicle='RIGID',version='1',shown_rate='180000',origin='VIC',destination='QLD'))
        self.assertEqual(response.status_code,200)
        self.assertEqual(self.send(client,CLIENT+'/1/edit',data(vehicle='RIGID',version='2',destination='NSW')).status_code,400)
        self.assertEqual(self.rows('SELECT rate_cents,fuel_bps FROM bf_interstate_bookings'),[dict(rate_cents=180000,fuel_bps=0)])

    def test_route_matrix_round_trip_prices_are_independent(self):
        from binflow_freight_rates import matrix_version
        aid=self.account('national-rates')
        path=f'{CAL}/access/{aid}/rates'
        with sqlite3.connect(self.path) as conn:
            conn.execute("INSERT INTO bf_interstate_rates VALUES(?,'WA','TAS','B-DOUBLE',99900)",(aid,))
        fields=matrix_fields(self.client.get(path+'?origin=NSW').text)
        expected={f'{start}:{end}:{vehicle}' for start in STATES for end in STATES if start!=end for vehicle in VEHICLES}
        self.assertEqual({key for key in fields if ':' in key},expected)
        self.assertEqual(fields['WA:TAS:B-DOUBLE'],'999.00')
        fields.update({'NSW:QLD:B-DOUBLE':'2000.00','QLD:NSW:B-DOUBLE':'1750.00',
                       'NSW:QLD:SEMI TRAILER':'1350.00','QLD:NSW:SEMI TRAILER':'0.00'})
        self.assertEqual(self.post(path,fields).status_code,200)
        self.assertEqual(self.post(path,fields).status_code,409)
        stored=matrix_fields(self.client.get(path).text)
        for key in expected:
            self.assertEqual(stored[key],fields[key])
        self.assertEqual(self.post(path,dict(stored,**{'NSW:QLD:B-DOUBLE':'123','WA:TAS:B-DOUBLE':'-2'})).status_code,400)
        incomplete=dict(stored)
        del incomplete['NT:ACT:B-DOUBLE']
        self.assertEqual(self.post(path,incomplete).status_code,400)
        self.assertEqual(matrix_fields(self.client.get(path).text),stored)
        # Pre-release per-state forms may still be open. A scoped save must preserve other states' rates.
        older={f'{dest}:{truck}':'' for dest in STATES if dest!='QLD' for truck in VEHICLES}
        older.update({'version':matrix_version({'NSW:B-DOUBLE':175000,'NSW:SEMI TRAILER':0},None),'NSW:B-DOUBLE':'1600'})
        self.assertEqual(self.post(path+'?origin=QLD',older).status_code,200)
        final=matrix_fields(self.client.get(path).text)
        self.assertEqual(final['QLD:NSW:B-DOUBLE'],'1600.00')
        self.assertEqual(final['QLD:NSW:SEMI TRAILER'],'')
        self.assertEqual(final['NSW:QLD:B-DOUBLE'],'2000.00')
        self.assertEqual(final['WA:TAS:B-DOUBLE'],'999.00')

    def test_vehicle_choice_local_booking_edits_confirmation_and_calendar(self):
        from unittest.mock import patch

        aid=self.account('local-customer',custom_permissions='yes',permission=['bookings.add','bookings.edit'])
        client=self.visit('local-customer')
        self.assertNotIn('/binflow/client/local-rigid/quote',client.get(CLIENT).text)
        page=client.get(CLIENT+'/new').text
        vehicle_select=page.split('<select name="vehicle"',1)[1].split('</select>',1)[0]
        self.assertEqual(re.findall(r'>([^<>]+)</option>',vehicle_select),['Select vehicle','RIGID','B DOUBLE','SEMI TRAILER'])

        def set_rate(rate):
            with sqlite3.connect(self.path) as conn:
                conn.execute('INSERT OR REPLACE INTO bf_rigid_hourly_rates VALUES(?,?)',(aid,rate))

        set_rate(12000)
        self.post(CAL+'/access/fuel',dict(version=0,percent='12.5'))
        form=data(vehicle='RIGID',origin='NSW',destination='NSW',hours='2',handling='scanned',shown_rate='48000+1250')
        for changes in [dict(destination='VIC'),dict(hours=''),dict(hours='0'),dict(hours='-1'),dict(hours='2.777'),dict(shown_rate='1')]:
            self.assertEqual(self.send(client,CLIENT+'/new',dict(form,**changes)).status_code,400)
        self.assertEqual(self.rows('SELECT id FROM bf_interstate_bookings'),[])
        response=self.send(client,CLIENT+'/new',form)
        self.assertEqual(response.status_code,200)
        self.assertIn('Billable 4 hours',response.text)
        self.assertIn('$540.00',response.text)
        self.assertEqual(self.rows('SELECT rigid_hours_units,rigid_hourly_cents,rate_cents,fuel_bps,delivery_date,handling FROM bf_interstate_bookings'),
                         [dict(rigid_hours_units=200,rigid_hourly_cents=12000,rate_cents=48000,fuel_bps=1250,delivery_date=form['pickup_date'],handling='bulk')])
        self.assertIn('Local RIGID',self.client.get(CAL).text)
        set_rate(18000)
        self.post(CAL+'/access/fuel',dict(version=1,percent='20'))
        self.send(client,CLIENT+'/new',form)
        self.assertEqual(len(self.rows('SELECT id FROM bf_interstate_bookings')),1)
        edited=self.send(client,CLIENT+'/1/edit',dict(form,hours='5.25',version=1,shown_rate='63000+1250'))
        self.assertIn('$708.75',edited.text)
        self.app.config.update(SMTP_HOST='smtp.example.invalid',SMTP_FROM='office@example.invalid')
        with patch('binflow_interstate.send_smtp') as mail:
            self.post(CAL+'/1/lock',dict(version=2))
            msg=mail.call_args.args[1]
            self.assertIn('local RIGID',msg['Subject'])
            self.assertIn('Billable hours: 5.25',msg.get_content())
            self.assertIn('AUD $708.75 ex GST',msg.get_content())
        self.assertIn('Local RIGID · Pickup',client.get(CLIENT+'?date='+form['pickup_date']).text)
        self.assertEqual(self.send(client,CLIENT+'/1/edit',dict(form,version=3)).status_code,409)

    def test_unpriced_local_booking_gets_hourly_price_when_locked(self):
        aid=self.account('pending-local')
        client=self.visit('pending-local')
        response=self.send(client,CLIENT+'/new',data(vehicle='RIGID',hours='3.5',origin='VIC',destination='VIC'))
        self.assertIn('Price to be confirmed',response.text)
        with sqlite3.connect(self.path) as conn:
            conn.execute('INSERT INTO bf_rigid_hourly_rates VALUES(?,?)',(aid,10100))
        self.post(CAL+'/1/lock',dict(version=1))
        self.assertIn('$404.00',client.get(CLIENT+'/1').text)
