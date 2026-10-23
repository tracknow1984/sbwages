import unittest
from unittest.mock import patch
from datetime import date
from test_binflow_client_profiles import ClientProfileTests
from binflow_calendar_progress import progress

class CalendarProgressTests(unittest.TestCase):
    setUp=ClientProfileTests.setUp
    tearDown=ClientProfileTests.tearDown
    rows=ClientProfileTests.rows
    post=ClientProfileTests.post
    login=ClientProfileTests.login
    job=ClientProfileTests.job
    connection=ClientProfileTests.connection

    def test_active_stop_only_and_completed_before_weighing_and_cancelled_visible(self):
        for n in range(1,5):self.job(deliver=(n,))
        conn=self.connection()
        conn.execute("INSERT INTO bf_trucks(name,state,created_at) VALUES('Progress truck','QLD','now')")
        conn.execute("UPDATE bf_jobs SET truck_id=1,schedule_live=1,run_order=id")
        conn.execute("INSERT INTO bf_runs(truck_id,run_date,departed_at) VALUES(1,'2026-10-19','now')")
        conn.execute("UPDATE bf_jobs SET status='collected' WHERE id=1")
        conn.execute("UPDATE bf_jobs SET status='cancelled' WHERE id=4")
        conn.commit()
        data=progress(conn,date(2026,10,19),date(2026,10,25),'QLD')['QLD:2026-10-19']
        self.assertEqual(data['counts'],dict(booked=1,on_route=1,completed=1,cancelled=1))
        self.assertEqual([e['status'] for e in data['entries']],['completed','on_route','booked','cancelled'])
        self.assertEqual(progress(conn,date(2026,10,19),date(2026,10,25),'NSW'),{})
        response=self.client.get('/binflow/schedule/calendar-progress?first=2026-10-19&last=2026-10-25&state=QLD')
        self.assertEqual(response.status_code,200)
        self.assertEqual(response.json['days']['QLD:2026-10-19']['counts'],data['counts'])
        self.assertEqual(response.headers['Cache-Control'],'no-store')

    def test_plan_deduplicates_actual_visits_and_excludes_baling_and_notes(self):
        self.job(deliver=(1,))
        conn=self.connection()
        conn.execute("UPDATE bf_jobs SET scheduled_date='2026-10-26'");conn.commit()
        plan={'days':[dict(date='2026-10-26',items=[
            dict(id='a',kind='visit',site_id=3,title='Actual site'),
            dict(id='b',kind='visit',site_id=4,title='Planned site'),
            dict(id='c',kind='baling',title='Baling'),
            dict(id='d',kind='note',title='Note'),
            dict(id='e',kind='job',job_id=1)])]}
        with patch('binflow_schedule.get_plan',return_value=(None,plan)):
            data=progress(conn,date(2026,10,26),date(2026,11,1),'QLD')['QLD:2026-10-26']
        self.assertEqual(len(data['entries']),2)
        self.assertEqual(data['counts']['booked'],2)
        self.assertEqual(data['entries'][0]['title'],'Planned site')

    def test_validation_and_admin_only(self):
        for query in ['first=bad','first=2026-01-01&last=2026-12-31','first=2026-10-26&last=2026-11-01&state=BAD']:
            self.assertEqual(self.client.get('/binflow/schedule/calendar-progress?'+query).status_code,400)
        self.post('/logout')
        response=self.client.get('/binflow/schedule/calendar-progress?first=2026-10-26&last=2026-11-01')
        self.assertIn(response.status_code,(302,403))
