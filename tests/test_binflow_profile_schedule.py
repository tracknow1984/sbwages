import sqlite3
import unittest
from datetime import date
from werkzeug.datastructures import MultiDict

import test_binflow_client_permissions as portal
from binflow_client_access import upgrade_schedule_access,rights_for
from binflow_profile_schedule import Schedule,backfill,decode,from_form
from binflow_route_calendar import clients,recurring_days


class AgreementTests(unittest.TestCase):
    def test_register_formats_and_ambiguous_text(self):
        for text,expected in [
            ('Weekly',Schedule('once')),('Fortnightly',Schedule('fortnightly')),
            ('Adhoc',Schedule('adhoc')),('Monthly',Schedule('monthly')),('Every 8 weeks',Schedule('eight_weekly')),
            ('Mon Wed Fri - Weekly',Schedule('three',(0,2,4))),('Tue Fri - Weekly',Schedule('twice',(1,4))),
            ('Last Monday of the Month',Schedule('last_monthly',(0,))),
            ('Last Tuesday of the month',Schedule('last_monthly',(1,))),
            ('Everyday',Schedule('daily',tuple(range(7)))),('Mon–Fri',Schedule('weekdays',tuple(range(5)))),
            ('Three times weekly',Schedule('three')),('Tuesday or Friday depending on volume',Schedule('review')),
            ('weekly Tuesday except school holidays',Schedule('review')),('',Schedule('unset')),
            ('Weekly Sun Tue Thu Sat',Schedule('custom',(1,3,5,6)))]:
            with self.subTest(text=text):
                self.assertEqual(decode(text),expected)
        for frequency,days in [('twice',(1,4)),('three',(0,2,4)),('daily',tuple(range(7))),('weekdays',tuple(range(5))),('custom',(0,6)),('last_monthly',(2,))]:
            spec=Schedule(frequency,days)
            self.assertEqual(decode(spec.label()),spec)
        form=MultiDict([('schedule_frequency','three'),('schedule_days','0'),('schedule_days','5')])
        with self.assertRaises(ValueError):
            from_form(form,'')
        self.assertEqual(from_form(MultiDict([('schedule_frequency','once')]),'Weekly'),'Weekly')


class ProfileScheduleTests(unittest.TestCase):
    setUp=portal.PortalPermissionTests.setUp
    tearDown=portal.PortalPermissionTests.tearDown
    post=portal.PortalPermissionTests.post
    rows=portal.PortalPermissionTests.rows
    login=portal.PortalPermissionTests.login
    account=portal.PortalPermissionTests.account
    visit=portal.PortalPermissionTests.visit
    send=portal.PortalPermissionTests.send

    def test_backfill_is_idempotent_and_uses_current_profile(self):
        with sqlite3.connect(self.path) as c:
            c.row_factory=sqlite3.Row
            c.execute("UPDATE bf_client_site_profiles SET schedule='Tue Fri - Weekly' WHERE location_id=3")
            row=c.execute('SELECT * FROM bf_client_site_profiles WHERE location_id=3').fetchone()
            backfill(c); backfill(c)
            new=c.execute('SELECT * FROM bf_client_site_profiles WHERE location_id=3').fetchone()
            self.assertEqual((new['schedule_frequency'],new['schedule_days']),('twice','[1, 4]'))
            self.assertEqual(new['version'],row['version'])
            self.assertEqual(new['schedule'],row['schedule'])
            c.execute("UPDATE bf_client_site_profiles SET schedule='Call first during holidays' WHERE location_id=3")
            backfill(c)
            self.assertEqual(c.execute('SELECT schedule_frequency FROM bf_client_site_profiles WHERE location_id=3').fetchone()[0],'review')

    def test_schedule_only_portal_admin_and_calendar_use_same_days(self):
        self.account('blocktexx1')
        self.account('viewer',custom_permissions='yes',permission=['profiles.view'])
        client=self.visit('blocktexx1')
        viewer=self.visit('viewer')
        profile=self.rows('SELECT * FROM bf_client_site_profiles WHERE location_id=3')[0]
        pid=profile['id']
        detail='/binflow/client/profiles/'+str(pid)
        url=detail+'/schedule'
        self.assertIn('Edit schedule', client.get(detail).text)
        self.assertEqual(client.get(detail+'/edit').status_code,200)
        self.assertEqual(viewer.get(url).status_code,403)
        self.assertEqual(self.send(viewer,url,{'schedule_frequency':'daily','version':profile['version']}).status_code,403)
        response=self.send(client,url,dict(schedule_frequency='three',schedule_days=['0','2','4'],version=profile['version'],address='forged'))
        self.assertEqual(response.status_code,200)
        current=self.rows('SELECT * FROM bf_client_site_profiles WHERE id=?',(pid,))[0]
        self.assertEqual(current['address'],profile['address'])
        self.assertEqual(current['schedule_days'],'[0, 2, 4]')
        self.assertEqual(current['updated_by'],'Client login blocktexx1')
        self.assertEqual(self.send(client,url,dict(schedule_frequency='daily',version=profile['version'])).status_code,409)
        fields={**current,'schedule_frequency':'daily'}
        self.assertEqual(self.post('/binflow/client-profiles/'+str(pid),fields).status_code,200)
        self.assertIn('value="daily" selected',client.get(url).text)
        with sqlite3.connect(self.path) as c:
            c.row_factory=sqlite3.Row
            p=next(p for p in clients(c) if p['profile_id']==pid)
            self.assertEqual(recurring_days(p,date(2026,11,2)),list(range(7)))

    def test_named_account_edits_existing_site_and_instructions(self):
        from binflow_client_access import upgrade_profile_edit_access
        aid=self.account('blocktexx1',custom_permissions='yes',permission=['profiles.schedule'])
        self.account('observer',custom_permissions='yes',permission=['profiles.view'])
        portal=self.visit('blocktexx1')
        other=self.visit('observer')
        p=self.rows('SELECT * FROM bf_client_site_profiles WHERE location_id=3')[0]
        url='/binflow/client/profiles/'+str(p['id'])+'/edit'
        with sqlite3.connect(self.path) as conn:
            conn.row_factory=sqlite3.Row
            upgrade_profile_edit_access(conn,'now')
        self.assertIn('Edit profile',portal.get('/binflow/client/profiles').text)
        changed=self.send(portal,url,dict(p,address='52 Warehouse Road',driver_instructions='Use Gate 3'))
        self.assertEqual(changed.status_code,200)
        self.assertIn('Use Gate 3',changed.text)
        saved=self.rows('SELECT * FROM bf_client_site_profiles WHERE id=?',(p['id'],))[0]
        self.assertEqual((saved['address'],saved['updated_by']),('52 Warehouse Road','Client login blocktexx1'))
        self.assertEqual(self.send(other,url,p).status_code,403)
        with sqlite3.connect(self.path) as conn:
            conn.row_factory=sqlite3.Row
            conn.execute("UPDATE bf_client_permissions SET rights='[]' WHERE account_id=?",(aid,))
            upgrade_profile_edit_access(conn,'later')
        self.assertEqual(portal.get(url).status_code,403)

    def test_named_right_upgrade_is_once_and_revocable(self):
        aid=self.account('blocktexx1',custom_permissions='yes',permission=['bookings.view'])
        other=self.account('other',custom_permissions='yes',permission=['profiles.view'])
        with sqlite3.connect(self.path) as c:
            c.row_factory=sqlite3.Row
            upgrade_schedule_access(c,'now')
            self.assertEqual(rights_for(c,aid),{'bookings.view','profiles.view','profiles.schedule'})
            self.assertEqual(rights_for(c,other),{'profiles.view'})
            c.execute("UPDATE bf_client_permissions SET rights='[]' WHERE account_id=?",(aid,))
            upgrade_schedule_access(c,'later')
            self.assertEqual(rights_for(c,aid),set())
