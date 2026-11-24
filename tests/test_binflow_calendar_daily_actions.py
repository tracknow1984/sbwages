import re
import html
from urllib.parse import urlparse,parse_qs
import unittest
from datetime import date, timedelta
from unittest.mock import patch

import test_binflow_schedule as schedule
from binflow_route_calendar import RESET_ID


class CalendarDailyActionsTests(unittest.TestCase):
    for name in ('setUp','tearDown','rows','post','login','connection','user','as_user'):
        locals()[name]=getattr(schedule.WeeklyScheduleTests,name)

    def modern(self):
        with self.connection() as conn:
            conn.execute("INSERT INTO bf_calendar_rebuilds VALUES(?,?,'[]','{}','now')",(RESET_ID,'2026-10-26'))

    def test_week_default_each_day_links_to_prefilled_allocation_on_both_layouts(self):
        for modern in (False,True):
            if modern:self.modern()
            page=self.client.get('/binflow/collection-calendar?date=2026-11-30').text
            links=re.findall(r'href="([^"]+)" data-allocation-popup aria-label="Add allocation for ([^"]+)"',page)
            self.assertEqual(len(links),7)
            self.assertIn('/binflow/collection-calendar?view=week',page)
            for offset,(href,day) in enumerate(links):
                expected=(date(2026,11,30)+timedelta(days=offset)).isoformat()
                self.assertEqual(day,expected)
                href=html.unescape(href)
                self.assertEqual(parse_qs(urlparse(href).query)['date'],[expected])
                self.assertEqual(parse_qs(urlparse(href).query)['calendar'],['1'])
                form=self.client.get(href)
                self.assertEqual(form.status_code,200)
                self.assertIn('name="date" value="'+expected+'"',form.text)
            if modern:self.assertIn('bf-date-grid-week',page)
        month=self.client.get('/binflow/collection-calendar?date=2026-11-30&view=month').text
        self.assertNotIn('bf-date-grid-week',month)
        self.assertGreater(month.count('aria-label="Add allocation for'),7)

    def test_selected_locked_day_blocks_button_manager_can_allocate(self):
        self.modern()
        with patch('binflow_stop_exceptions.closeouts',return_value={('QLD','2026-11-30'):dict(pods=1)}):
            page=self.client.get('/binflow/collection-calendar?date=2026-11-30&state=QLD').text
            self.assertNotIn('aria-label="Add allocation for 2026-11-30"',page)
            self.assertIn('Day locked</button>',page)
            self.assertEqual(page.count('aria-label="Add allocation for'),6)
        self.as_user(self.user('Manager',role='manager'))
        page=self.client.get('/binflow/collection-calendar?date=2026-11-30').text
        self.assertEqual(page.count('aria-label="Add allocation for'),7)


if __name__=='__main__':unittest.main()
