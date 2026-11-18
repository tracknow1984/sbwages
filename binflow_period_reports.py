"""Shared reporting periods and native Excel / PDF downloads for BinFlow."""
import calendar
import io
import tempfile
import zipfile
from datetime import date,timedelta

from flask import abort,request,send_file
import xlwt

PERIODS = {'day':'Daily','week':'Weekly','fortnight':'Fortnightly','month':'Monthly'}


def bounds(day, period):
    if period=='day': return day,day
    if period=='week':
        first=day-timedelta(days=day.weekday())
        return first,first+timedelta(days=6)
    if period=='fortnight':
        from binflow_auto_invoices import fortnight
        return tuple(date.fromisoformat(x) for x in fortnight(day))
    if period=='month':
        return day.replace(day=1),day.replace(day=calendar.monthrange(day.year,day.month)[1])
    raise ValueError('Choose a daily, weekly, fortnightly or monthly reporting period.')


def period_view(chosen, period):
    if period not in PERIODS: abort(400,'Choose a valid reporting period.')
    if not chosen:
        return dict(key=period,first='',last='',label='All dates',previous='',following='')
    try:
        day=date.fromisoformat(chosen)
        if not 1900<=day.year<=9998: raise ValueError()
        first,last=bounds(day,period)
    except (ValueError,OverflowError):
        abort(400,'Choose a valid reporting date.')
    label=first.strftime('%d %b %Y') if first==last else f'{first:%d %b %Y} – {last:%d %b %Y}'
    return dict(key=period,first=first.isoformat(),last=last.isoformat(),label=label,
                previous=(first-timedelta(days=1)).isoformat(),following=(last+timedelta(days=1)).isoformat())


def download(file,mime,name):
    file.seek(0)
    response=send_file(file,mimetype=mime,as_attachment=True,download_name=name)
    response.cache_control.no_store=True
    return response


def xls(title,headers,rows,name):
    book=xlwt.Workbook()
    head=xlwt.easyxf('font: bold on, colour white; pattern: pattern solid, fore_colour dark_blue;')
    day=xlwt.easyxf(num_format_str='DD/MM/YYYY')
    number=xlwt.easyxf(num_format_str='#,##0.00#')
    plain=xlwt.XFStyle()
    def new_sheet(index):
        sheet=book.add_sheet(title if not index else f'{title} {index+1}')
        sheet.set_panes_frozen(True)
        sheet.set_horz_split_pos(1)
        for col,h in enumerate(headers):
            sheet.write(0,col,h,head)
            sheet.col(col).width=256*max(14,min(48,len(h)+4))
        return sheet
    sheet=new_sheet(0)
    for index,row in enumerate(rows):
        if index and index%65535==0: sheet=new_sheet(index//65535)
        for col,value in enumerate(row):
            style=day if isinstance(value,date) else number if isinstance(value,float) else plain
            sheet.write(1+index%65535,col,value,style)
            if isinstance(value,str): sheet.col(col).width=max(sheet.col(col).width,256*min(58,len(value)+2))
    output=io.BytesIO()
    book.save(output)
    return download(output,'application/vnd.ms-excel',name+'.xls')


def selected_ids():
    ids=set(request.form.getlist('selected'))
    if not ids: abort(400,'Tick at least one row before downloading.')
    return ids


def pdf_data(response):
    response.direct_passthrough=False
    try:
        data=response.get_data()
        if response.status_code!=200 or not data.startswith(b'%PDF-'):
            raise ValueError('A selected PDF is unavailable. Refresh the list and try again.')
        return data
    finally:
        response.close()


def zip_download(entries,name):
    output=tempfile.SpooledTemporaryFile(max_size=8*1024*1024)
    try:
        with zipfile.ZipFile(output,'w',compression=zipfile.ZIP_DEFLATED) as archive:
            for filename,data in entries: archive.writestr(filename,data)
        return download(output,'application/zip',name+'.zip')
    except Exception:
        output.close()
        raise


def pod_download(jobs,kind,pdf,name):
    from binflow_pod_records import status
    if kind=='xls':
        return xls('PODs',['POD','Run date','Client','Site','State','Warehouse','Bins collected','Net kg','POD status','Signed by','Signed at'],
                   ((j['code'],date.fromisoformat(j['scheduled_date']),j['client_name'],j['site_name'],j['state'],j['warehouse_name'],
                     j['collection_bins'], None if j['net'] is None else j['net']/1000,status(j),j['pod_signed_name'],j['pod_signed_at']) for j in jobs),name)
    if kind!='zip': abort(400,'Choose XLS or ZIP.')
    if any(not j['pod_signed_at'] or j['pod_deleted_at'] for j in jobs):
        abort(409,'Select completed, signed PODs to download PDFs together.')
    return zip_download(((j['code']+'.pdf',pdf_data(pdf(j['id']))) for j in jobs),name)
