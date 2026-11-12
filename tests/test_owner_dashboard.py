import sqlite3
import unittest
from datetime import date, datetime
from html.parser import HTMLParser
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

import test_binflow as fixture
from owner_dashboard import next_week_approvals, owner_overview


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

    def test_next_week_uses_calendar_approval_and_links_to_that_week(self):
        import json
        from binflow_schedule import STATES
        from binflow_route_calendar import RESET_ID
        monday='2026-11-16'
        def statuses(day=date(2026,11,13)):
            with sqlite3.connect(self.path) as conn:
                conn.row_factory=sqlite3.Row
                summary=next_week_approvals(conn,day)
            return {s['state']:s for s in summary['states']},summary

        with sqlite3.connect(self.path) as conn:
            uid=conn.execute("SELECT id FROM users WHERE role='admin'").fetchone()[0]
            driver=conn.execute("""INSERT INTO users(role,first_name,last_name,email,username,password_hash,business_role,working_state)
                VALUES('employee','Queensland','Driver','driver@example.com','driver','unused','driver','QLD')""").lastrowid
            conn.execute("INSERT OR IGNORE INTO bf_calendar_rebuilds VALUES(?,?,'{}','{}',?)",(RESET_ID,monday,monday))
        rows,summary=statuses()
        self.assertEqual(set(rows),set(STATES))
        self.assertEqual((summary['start'],summary['end']),(date(2026,11,16),date(2026,11,22)))
        self.assertEqual(rows['QLD']['status'],'pending')  # Generated depot work without a saved week.
        self.assertEqual(rows['NT']['status'],'empty')
        plan=self.client.get('/binflow/schedule/options?state=QLD&date='+monday).json['plan']
        form=dict(state='QLD',week=monday,revision=0,plan=json.dumps(plan),action='save')
        self.assertEqual(self.post('/binflow/schedule/save',form,follow=False).status_code,200)
        self.assertEqual(statuses()[0]['QLD']['status'],'pending')
        form.update(action='approve',reviewed='yes',driver_id=driver,revision=1)
        self.assertEqual(self.post('/binflow/schedule/save',form,follow=False).status_code,200)
        self.assertEqual(statuses()[0]['QLD']['status'],'approved')

        self.job(deliver=(1,),legacy=False)
        with sqlite3.connect(self.path) as conn:
            conn.execute('UPDATE bf_jobs SET scheduled_date=? WHERE id=1',(monday,))
        self.assertEqual(statuses()[0]['QLD']['status'],'review')
        with sqlite3.connect(self.path) as conn:
            conn.execute("UPDATE bf_jobs SET status='cancelled' WHERE id=1")
            conn.execute("INSERT INTO bf_schedule_weeks(state,week_start,revision,status,plan_json,updated_at,updated_by) VALUES(?,?,1,'draft',?,?,?)",
                         ('NSW',monday,json.dumps(dict(days=[dict(items=[]) for i in range(7)])),monday,uid))
        self.assertEqual(statuses()[0]['QLD']['status'],'approved')
        self.assertEqual(statuses()[0]['NSW']['status'],'empty')  # Cleared plans supersede generated work.
        self.assertEqual(statuses(date(2026,11,15))[1]['start'],date(2026,11,16))
        self.assertEqual(statuses(date(2026,11,16))[1]['start'],date(2026,11,23))
        self.assertEqual(statuses(date(2026,12,31))[1]['end'],date(2027,1,10))

        class ScheduleLinks(HTMLParser):
            def __init__(self):
                super().__init__()
                self.links=[]
            def handle_starttag(self,tag,attrs):
                attrs=dict(attrs)
                if tag=='a' and 'schedule-state' in attrs.get('class','').split():
                    self.links.append(attrs['href'])
        with patch('app.datetime',wraps=datetime) as clock:
            clock.now.return_value=datetime(2026,11,13,9,30)
            page=self.client.get('/admin/dashboard')
        self.assertEqual(page.status_code,200)
        self.assertIn('Next week’s schedule approvals',page.text)
        parser=ScheduleLinks();parser.feed(page.text)
        filters=[parse_qs(urlsplit(link).query) for link in parser.links]
        self.assertEqual({f['state'][0] for f in filters},set(STATES))
        self.assertTrue(all(f['view']==['week'] and f['date']==[monday] for f in filters))
        self.assertEqual(self.client.get(parser.links[0]).status_code,200)
