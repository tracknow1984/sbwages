from datetime import datetime
from html.parser import HTMLParser
from io import BytesIO
import sqlite3
import unittest
from urllib.parse import parse_qs, urlsplit

from openpyxl import load_workbook

from test_binflow_client_permissions import PortalPermissionTests
from test_binflow_interstate import CAL, CLIENT, data


class ExportButtons(HTMLParser):
    def __init__(self):
        super().__init__()
        self.links = []

    def handle_starttag(self, tag, attrs):
        url = dict(attrs).get('href', '')
        if tag == 'a' and parse_qs(urlsplit(url).query).get('export') == ['month']:
            self.links.append(url)


class MonthlyExportTests(unittest.TestCase):
    setUp = PortalPermissionTests.setUp
    tearDown = PortalPermissionTests.tearDown
    rows = PortalPermissionTests.rows
    post = PortalPermissionTests.post
    login = PortalPermissionTests.login
    account = PortalPermissionTests.account
    visit = PortalPermissionTests.visit
    send = PortalPermissionTests.send

    def download(self, client, calendar, date='2026-11-16'):
        page = client.get(calendar + '?view=week&state=WA&date=' + date)
        buttons = ExportButtons()
        buttons.feed(page.text)
        self.assertEqual(len(buttons.links), 1)
        response = client.get(buttons.links[0])
        self.assertEqual(response.status_code, 200)
        self.assertIn('BinFlow-All-Transfers-' + date[:7] + '.xlsx', response.headers['Content-Disposition'])
        self.assertTrue(response.cache_control.no_store)
        self.assertIn('spreadsheetml.sheet', response.mimetype)
        return load_workbook(BytesIO(response.data)).active

    def test_month_boundaries_all_states_and_one_row_per_visible_booking(self):
        self.account('client-one')
        self.account('client-two')
        first = self.visit('client-one')
        other = self.visit('client-two')
        periods = [
            ('2026-10-29', '2026-10-31'), ('2026-10-31', '2026-11-01'),
            ('2026-11-05', '2026-11-08'), ('2026-11-30', '2026-12-02'),
            ('2026-12-01', '2026-12-03'),
        ]
        for start, end in periods:
            self.send(first, CLIENT+'/new', data(pickup_date=start, delivery_date=end,
                      pickup_phone='0400123456', goods='=Example\n10 pallets'))
        self.send(other, CLIENT+'/new', data(pickup_company='Other account'))
        with sqlite3.connect(self.path) as conn:
            conn.execute("""UPDATE bf_interstate_bookings SET locked_at='2026-11-01T09:00:00+10:00',
                locked_by='Office operator',rate_cents=10000,fuel_bps=1250 WHERE id=3""")
            conn.execute("""INSERT INTO bf_interstate_purchase_orders
                (booking_id,number,revision,updated_at,updated_by) VALUES (3,'000109',1,'2026-11-02','Office operator')""")
        page = self.download(first, CLIENT)
        self.assertEqual(page.freeze_panes, 'C5')
        self.assertIn('MonthlyTransfers', page.tables)
        headers = [cell.value for cell in page[4]]
        rows = [dict(zip(headers, row)) for row in page.iter_rows(min_row=5, values_only=True)]
        self.assertEqual([row['Transfer ID'] for row in rows], ['IT-000002','IT-000003','IT-000004'])
        self.assertEqual([row['Status'] for row in rows], ['Requested','Scheduled','Requested'])
        self.assertEqual(rows[1]['Total ex GST'], 112.5)
        self.assertEqual(rows[1]['PO number'], '000109')
        self.assertEqual(rows[1]['Pickup date'], datetime(2026,11,5))
        self.assertEqual(rows[1]['Pickup phone'], '0400123456')
        goods = page.cell(6, headers.index('Goods, quantities and notes')+1)
        self.assertEqual((goods.value, goods.data_type), ('=Example\n10 pallets','s'))
        self.assertNotIn('Confirmed by', headers)
        self.assertNotIn('Manifest reference', headers)
        office = self.download(self.client, CAL)
        self.assertEqual(office.max_row, 8)
        self.assertIn('Confirmed by', [c.value for c in office[4]])

    def test_empty_month_and_export_permissions(self):
        self.account('collection-only', custom_permissions='yes', permission=['calendar.view'])
        client = self.visit('collection-only')
        self.assertEqual(client.get(CLIENT+'?export=month').status_code,403)
        self.assertEqual(self.app.test_client().get(CLIENT+'?export=month').status_code,302)
        empty = self.download(self.client, CAL, '2028-02-29')
        self.assertIn('February 2028', empty['A1'].value)
        self.assertIn('0 transfers', empty['A2'].value)
        self.assertEqual(empty.max_row, 4)
        self.assertEqual(self.client.get(CAL+'?export=month&date=invalid').status_code,400)
