"""Build the monthly All Transfers workbook from the calendar's authorised bookings."""
from datetime import date, datetime
from io import BytesIO
from zoneinfo import ZoneInfo

from openpyxl import Workbook
from openpyxl.cell.cell import ILLEGAL_CHARACTERS_RE
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.table import Table, TableStyleInfo

from binflow_freight_rates import MIN_RIGID_HOURS, quote

# Each group is a labelled band above the filterable Excel table.
GROUPS = (
    ('Booking', (
        ('reference', 'Transfer ID', 17), ('status', 'Status', 18),
        ('pickup_date', 'Pickup date', 16), ('delivery_date', 'Delivery date', 16),
        ('billing_name', 'Billing client', 29), ('client_name', 'Client on booking', 29),
        ('job_type', 'Job type', 18), ('vehicle', 'Vehicle', 19))),
    ('Pickup', (
        ('origin_name', 'Pickup location', 24), ('origin_state', 'Pickup state', 15),
        ('pickup_company', 'Pickup company', 27), ('pickup_address', 'Pickup address', 44),
        ('pickup_contact', 'Pickup contact', 23), ('pickup_phone', 'Pickup phone', 22))),
    ('Delivery', (
        ('destination_name', 'Delivery location', 24), ('destination_state', 'Delivery state', 16),
        ('delivery_company', 'Delivery company', 27), ('delivery_address', 'Delivery address', 44),
        ('delivery_contact', 'Delivery contact', 23), ('delivery_phone', 'Delivery phone', 22))),
    ('Load', (
        ('handling', 'Handling', 19), ('goods', 'Goods, quantities and notes', 52),
        ('expected_bales', 'Expected bales', 18))),
    ('Purchase order and confirmation', (
        ('po_status', 'PO status', 23), ('po_number', 'PO number', 22),
        ('po_filename', 'PO filename', 32), ('email', 'Confirmation email', 36),
        ('email_status', 'Email status', 23))),
    ('Freight pricing · AUD ex GST', (
        ('hours', 'Requested hours', 19), ('billable_hours', 'Billable hours', 18),
        ('hourly', 'Hourly rate', 17), ('base', 'Base freight', 18),
        ('fuel_rate', 'Fuel levy %', 16), ('fuel', 'Fuel surcharge', 18),
        ('total', 'Total ex GST', 18))),
)
OFFICE_GROUP = ('Office tracking · Brisbane time', (
    ('manifest_code', 'Manifest reference', 22), ('created_by', 'Requested by', 25),
    ('created_at', 'Requested at', 23), ('locked_by', 'Confirmed by', 25),
    ('locked_at', 'Confirmed at', 23), ('bulk_dispatched_at', 'Bulk dispatched at', 23),
    ('bulk_delivered_at', 'Bulk delivered at', 23)))
FORMATS = {
    **dict.fromkeys(('pickup_date', 'delivery_date'), 'dd mmm yyyy'),
    **dict.fromkeys(('base', 'hourly', 'fuel', 'total'), '"$"#,##0.00'),
    **dict.fromkeys(('hours', 'billable_hours'), '0.00'),
    **dict.fromkeys(('created_at', 'locked_at', 'bulk_dispatched_at', 'bulk_delivered_at'),
                    'dd mmm yyyy hh:mm'),
    'fuel_rate': '0.00%',
}


def booking_values(booking, places):
    b = dict(booking)
    b['reference'] = f"IT-{b['id']:06d}"
    b['origin_name'], b['origin_state'] = places[b['origin']]
    b['destination_name'], b['destination_state'] = places[b['destination']]
    for key in ('pickup_date', 'delivery_date'):
        b[key] = date.fromisoformat(b[key])
    b['vehicle'] = (b['vehicle'] or 'Vehicle to confirm').replace('B-DOUBLE', 'B DOUBLE')
    b['job_type'] = 'Subcontracted' if b['job_type'] == 'external' else 'Internal'
    b['handling'] = 'Scanned bales' if b['handling'] == 'scanned' else 'Bulk movement'
    b['po_status'] = 'Awaiting confirmation'
    if b['po_received']:
        b['po_status'] = 'PO received'
    elif b['locked_at']:
        b['po_status'] = 'PO required'
    b['email_status'] = b['email_status'].capitalize() if b['locked_at'] else None
    if b['rigid_hours_units'] is not None:
        b['hours'] = b['rigid_hours_units'] / 100
        b['billable_hours'] = max(MIN_RIGID_HOURS, b['hours'])
        if b['rigid_hourly_cents'] is not None:
            b['hourly'] = b['rigid_hourly_cents'] / 100
    price = quote(b['rate_cents'], b['fuel_bps']) if booking['vehicle'] and b['account_id'] else None
    if price:
        b.update({k: price[k] / 100 for k in ('base', 'fuel', 'total')})
        b['fuel_rate'] = price['bps'] / 10000
    for key in ('created_at', 'locked_at', 'bulk_dispatched_at', 'bulk_delivered_at'):
        if b[key]:
            try:
                stamp = datetime.fromisoformat(b[key])
            except ValueError:
                continue  # Preserve legacy timestamps as recorded.
            if stamp.tzinfo:
                stamp = stamp.astimezone(ZoneInfo('Australia/Brisbane'))
            b[key] = stamp.replace(tzinfo=None)
    return b


def monthly_workbook(bookings, chosen, places, external):
    book = Workbook()
    sheet = book.active
    sheet.title = 'All Transfers'
    sheet.sheet_view.showGridLines = False
    sheet.freeze_panes = 'C5'
    groups = GROUPS if external else (*GROUPS, OFFICE_GROUP)
    columns = [column for _, group in groups for column in group]
    sheet.merge_cells('A1:H1')
    title = sheet['A1']
    title.value = f'BinFlow  /  All Transfers  /  {chosen:%B %Y}'
    title.font = Font(name='Calibri', size=18, bold=True, color='173650')
    sheet.row_dimensions[1].height = 36
    sheet.merge_cells('A2:H2')
    sheet['A2'].font = Font(name='Calibri', size=11, color='415C73')
    sheet.row_dimensions[2].height = 25
    sheet.row_dimensions[3].height = 23
    sheet.row_dimensions[4].height = 30
    first = 1
    for index, (name, group) in enumerate(groups):
        last = first + len(group) - 1
        sheet.merge_cells(start_row=3, start_column=first, end_row=3, end_column=last)
        band = sheet.cell(3, first, name)
        band.font = Font(bold=True, color='FFFFFF')
        band.fill = PatternFill('solid', fgColor='176969' if index % 2 else '244761')
        band.alignment = Alignment(vertical='center', indent=1)
        first = last + 1
    for index, (_, label, width) in enumerate(columns, 1):
        header = sheet.cell(4, index, label)
        header.font = Font(bold=True, color='FFFFFF')
        header.fill = PatternFill('solid', fgColor='173650')
        header.alignment = Alignment(vertical='center', wrap_text=True)
        sheet.column_dimensions[get_column_letter(index)].width = width

    count = 0
    for count, booking in enumerate(bookings, 1):
        values = booking_values(booking, places)
        row = count + 4
        for index, (key, _, _) in enumerate(columns, 1):
            value = values.get(key)
            cell = sheet.cell(row, index)
            if isinstance(value, str):
                cell.value = ILLEGAL_CHARACTERS_RE.sub('', value)
                cell.data_type = 's'  # Preserve phone numbers, PO references and user text literally.
            else:
                cell.value = value
            if key in FORMATS:
                cell.number_format = FORMATS[key]
            cell.alignment = Alignment(vertical='top', wrap_text=True)

    sheet['A2'] = f'{count:,} transfer{"s" if count != 1 else ""}  ·  All states  ·  Pickup or delivery in {chosen:%B %Y}'
    last_column = get_column_letter(len(columns))
    area = f'A4:{last_column}{4 + count}'
    if count:
        table = Table(displayName='MonthlyTransfers', ref=area)
        table.tableStyleInfo = TableStyleInfo(name='TableStyleMedium2', showRowStripes=True,
                                              showFirstColumn=False, showLastColumn=False, showColumnStripes=False)
        sheet.add_table(table)
    else:
        sheet.auto_filter.ref = area
    sheet.print_title_rows = '3:4'
    sheet.print_title_cols = 'A:B'
    sheet.page_setup.orientation = 'landscape'
    output = BytesIO()
    book.save(output)
    output.seek(0)
    return output
