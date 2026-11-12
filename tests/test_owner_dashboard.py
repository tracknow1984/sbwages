import sqlite3
import unittest
from datetime import date

import test_binflow as fixture
from owner_dashboard import owner_overview


class OwnerDashboardTests(unittest.TestCase):
    setUp = fixture.BinFlowTests.setUp
    tearDown = fixture.BinFlowTests.tearDown
    login = fixture.BinFlowTests.login
    post = fixture.BinFlowTests.post
    job = fixture.BinFlowTests.job
    rows = fixture.BinFlowTests.rows

    def test_published_dispatch_confirmations_and_month_end(self):
        from test_binflow_interstate import CAL, data
        for i in range(1, 5):
            self.job(deliver=(i,), legacy=False)
        self.post(CAL + '/new', data(pickup_date='2026-11-01'))
        self.post(CAL + '/new', data(pickup_date='2026-11-01'))
        with sqlite3.connect(self.path) as conn:
            conn.row_factory = sqlite3.Row
            conn.execute("UPDATE bf_jobs SET scheduled_date='2026-10-31',schedule_live=1")
            conn.execute("UPDATE bf_jobs SET status='cancelled' WHERE id=2")
            conn.execute("UPDATE bf_jobs SET schedule_live=0 WHERE id=3")
            conn.execute("UPDATE bf_jobs SET approval_status='pending' WHERE id=4")
            conn.execute("UPDATE bf_interstate_bookings SET locked_at='2026-10-31' WHERE id=1")
            conn.execute("""INSERT INTO tx_rates(unload,prepare,loading,pallet,strap,created_at,created_by)
                VALUES(10,10,10,10,10,'2026-10-01',1)""")
            for i, work_date in enumerate(('2026-09-30','2026-10-01','2026-10-31','2026-11-01')):
                conn.execute("""INSERT INTO tx_handling(kind,work_date,reference,employee_id,employee_name,hours_units,
                    pallets_loaded,pallets_supplied,straps,notes,rate_id,charges_json,total_cents,created_at,
                    created_by,recorded_by,submission_token) VALUES('stock_pick',?,'',1,'Test',100,0,0,0,'',
                    (SELECT MAX(id) FROM tx_rates),'[]',7900,?,1,'Test',?)""",(work_date,work_date,str(i)))
            summary = owner_overview(conn,date(2026,10,31),'2026-11-01')
            self.assertEqual(summary['dispatch']['today']['jobs'],1)
            self.assertEqual(len(summary['dispatch']['days']),7)
            self.assertEqual(summary['dispatch']['selected']['pickups'],1)
            self.assertEqual([p['id'] for p in summary['dispatch']['pickups']],[1])
            self.assertEqual(summary['counts']['confirmations'],1)
            self.assertEqual(summary['counts']['handling_today'],1)
            self.assertEqual((summary['handling']['records'],summary['handling']['cents']),(2,15800))
            fallback = owner_overview(conn,date(2026,10,31),'outside-the-window')['dispatch']
            self.assertEqual(fallback['selected'],fallback['today'])
            self.assertEqual([j['id'] for j in fallback['jobs']],[1])
