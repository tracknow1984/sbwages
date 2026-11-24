import json
import sqlite3
import unittest
import zlib
from unittest.mock import patch

import test_binflow as legacy
from binflow_live_cycle import provision, authenticate
from binflow_test_retirement import retire, VERSION, clear_unconfirmed_sa


class RetirementTests(unittest.TestCase):
    for name in ('setUp','tearDown','rows','post','login'):
        locals()[name]=getattr(legacy.BinFlowTests,name)

    def connection(self):
        c=sqlite3.connect(self.path);c.row_factory=sqlite3.Row;c.execute('PRAGMA foreign_keys=ON');return c

    def test_removal_is_verified_recoverable_and_idempotent(self):
        with self.connection() as c:
            uid=provision(c,'2026-11-24')
            c.commit()
            before=[dict(r) for r in c.execute("SELECT * FROM bf_bins WHERE code NOT LIKE 'LT-%'")]
            result=retire(c,'2026-11-24')
            self.assertEqual(result['removed']['bf_locations'],4)
            self.assertEqual(result['removed']['bf_bins'],6)
            self.assertEqual(c.execute('SELECT status FROM users WHERE id=?',(uid,)).fetchone()[0],'terminated')
            self.assertEqual(before,[dict(r) for r in c.execute('SELECT * FROM bf_bins')])
            self.assertFalse(c.execute('PRAGMA foreign_key_check').fetchall())
            data=json.loads(zlib.decompress(c.execute('SELECT rows_zlib FROM bf_test_retirement WHERE id=?',(VERSION,)).fetchone()[0]))
            self.assertEqual(len(data['removed']['bf_bins']),6)
            self.assertEqual(retire(c,'tomorrow')['status'],'already retired')
            self.assertFalse(c.execute('SELECT * FROM bf_live_cycle').fetchall())

    def test_shared_real_stock_blocks_and_rolls_back_everything(self):
        with self.connection() as c:
            provision(c,'2026-11-24')
            jid=c.execute("SELECT id FROM bf_jobs WHERE code='LIVE-TEST-POD-01'").fetchone()[0]
            c.execute("INSERT INTO bf_job_bins(job_id,bin_id,direction,bin_code,size_name,tare_g) VALUES(?,1,'deliver','BF000001','240L',5000)",(jid,))
            c.commit()
            with self.assertRaisesRegex(ValueError,'six test bins'):
                retire(c,'2026-11-24')
            self.assertEqual(c.execute('SELECT COUNT(*) FROM bf_bins').fetchone()[0],12)
            self.assertFalse(c.execute('SELECT * FROM bf_test_retirement').fetchall())
            self.assertEqual(c.execute('SELECT COUNT(*) FROM bf_live_cycle').fetchone()[0],1)

    def test_only_test_items_are_removed_from_saved_plans(self):
        with self.connection() as c:
            provision(c,'2026-11-24')
            site=c.execute("SELECT id FROM bf_locations WHERE name='LIVE TEST · Smithfield'").fetchone()[0]
            plan={'days':[{'date':'2026-11-30','items':[{'id':'real','site_id':3},{'id':'sample','site_id':site}]}]}
            c.execute("INSERT INTO bf_schedule_weeks(state,week_start,revision,status,plan_json,updated_at,updated_by) VALUES('QLD','2026-11-30',1,'published',?,'now',1)",(json.dumps(plan),))
            c.commit();retire(c,'2026-11-24')
            row=c.execute('SELECT * FROM bf_schedule_weeks').fetchone()
            self.assertEqual(json.loads(row['plan_json'])['days'][0]['items'],[{'id':'real','site_id':3}])
            self.assertEqual(row['revision'],2);self.assertEqual(row['status'],'published')
            payload=json.loads(zlib.decompress(c.execute('SELECT rows_zlib FROM bf_test_retirement WHERE id=?',(VERSION,)).fetchone()[0]))
            self.assertEqual(json.loads(payload['previous_plans'][0][1]['plan_json']),plan)

    def test_new_unknown_dependency_blocks_without_orphans(self):
        with self.connection() as c:
            provision(c,'2026-11-24')
            c.execute('CREATE TABLE protected_real_records(id INTEGER PRIMARY KEY, depot_id REFERENCES bf_locations(id))')
            c.execute('INSERT INTO protected_real_records VALUES(1,(SELECT origin_id FROM bf_live_cycle))');c.commit()
            with self.assertRaisesRegex(ValueError,'protected_real_records'):
                retire(c,'2026-11-24')
            self.assertFalse(c.execute('PRAGMA foreign_key_check').fetchall())
            self.assertEqual(c.execute('SELECT COUNT(*) FROM bf_live_cycle').fetchone()[0],1)

    def test_booking_dispatch_link_survives_archiving_test_recipient(self):
        from test_binflow_interstate import data
        self.post('/binflow/interstate-transfers/new',data(client_name='Real customer'))
        with self.connection() as c:
            uid=provision(c,'2026-11-24')
            booking=dict(c.execute('SELECT * FROM bf_interstate_bookings').fetchone())
            nid=c.execute("INSERT INTO staff_notices(user_id,admin_id,title,body,sent_at) VALUES(?,1,'Assignment','Real booking','now')",(uid,)).lastrowid
            c.execute("""INSERT INTO bf_load_dispatches(booking_id,version,event,target,recipient,address,title,body,actor_id,status,notice_id)
                VALUES(?,1,'Assigned',?,'Test Driver','','Assignment','Real booking',1,'sent',?)""",(booking['id'],'driver:'+str(uid),nid))
            dispatch=dict(c.execute('SELECT * FROM bf_load_dispatches').fetchone());c.commit()
            result=retire(c,'2026-11-24')
            self.assertEqual(result['status'],'retired');self.assertEqual(result['archived_test_notices'],1)
            self.assertEqual(dict(c.execute('SELECT * FROM bf_interstate_bookings').fetchone()),booking)
            self.assertEqual(dict(c.execute('SELECT * FROM bf_load_dispatches').fetchone()),dispatch)
            self.assertEqual(c.execute('SELECT archived_at FROM staff_notices WHERE id=?',(nid,)).fetchone()[0],'2026-11-24')
            self.assertFalse(c.execute('PRAGMA foreign_key_check').fetchall())

    def test_completed_cycle_and_pods_can_be_removed(self):
        from test_binflow_live_cycle import LiveCycleTests
        case=LiveCycleTests('test_employee_actions_reach_the_normal_office_registers')
        try:
            case.setUp()
            case.test_employee_actions_reach_the_normal_office_registers()
            with sqlite3.connect(case.path) as c:
                c.row_factory=sqlite3.Row;c.execute('PRAGMA foreign_keys=ON')
                result=retire(c,'2026-11-24')
                self.assertEqual(result['status'],'retired')
                self.assertFalse(c.execute('PRAGMA foreign_key_check').fetchall())
        finally:
            case.tearDown();case.doCleanups()

    def test_sa_placeholder_removed_once_and_test_logins_cannot_recreate_data(self):
        with self.connection() as c:
            c.execute("INSERT INTO bf_locations(kind,name,state,address,created_at) VALUES('warehouse','SB EMPIRE - SA','SA','PORT HEADLAND','now')");c.commit()
            retire(c,'now');clear_unconfirmed_sa(c,'now')
            self.assertEqual(c.execute("SELECT address FROM bf_locations WHERE state='SA'").fetchone()[0],'')
            c.execute("UPDATE bf_locations SET address='12 Real Street Adelaide' WHERE state='SA'");c.commit()
            clear_unconfirmed_sa(c,'later')
            self.assertEqual(c.execute("SELECT address FROM bf_locations WHERE state='SA'").fetchone()[0],'12 Real Street Adelaide')
            with patch.dict(self.app.config,TESTING=False):
                self.assertIsNone(authenticate(c,self.app,'testdriver','unused','now'))
                self.assertEqual(self.client.get('/admin/binflow/live-test').status_code,410)
                self.assertEqual(self.client.get('/binflow/driver/practice-login').status_code,410)


if __name__=='__main__':unittest.main()
