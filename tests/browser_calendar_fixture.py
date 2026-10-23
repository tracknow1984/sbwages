"""Isolated local calendar fixture. No live client, job or bin data is loaded."""
import sys
from pathlib import Path
sys.path[:0] = [str(Path(__file__).resolve().parents[1]), str(Path(__file__).resolve().parent)]
from test_binflow_calendar_edit import CalendarEditTests
from binflow_route_calendar import RESET_ID

if __name__ == '__main__':
    fixture = CalendarEditTests()
    fixture.setUp()
    try:
        driver = fixture.user()
        plan = fixture.visit_plan()
        plan['days'][0]['items'].append(dict(id='beta', kind='visit', profile_id=2, time='08:00', notes='Rear loading bay'))
        plan['days'][2]['items'] = [dict(id='baling', kind='baling', time='06:30', notes='Prepare empty bins')]
        fixture.save(plan, action='approve', driver=driver)
        second = fixture.plan(week='2026-11-02')['plan']
        for day in second['days']: day['items'] = []
        second['days'][4]['items'] = [dict(id='second-baling', kind='baling', time='06:30', notes='')]
        fixture.save(second, action='approve', driver=driver, week='2026-11-02')
        with fixture.connection() as conn:
            conn.execute("INSERT INTO bf_calendar_rebuilds VALUES(?,?,'[]','{}','2026-10-23')", (RESET_ID,'2026-10-26'))
        fixture.app.run(host='127.0.0.1', port=5093, debug=False)
    finally:
        fixture.tearDown()
