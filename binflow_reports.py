"""Server-recorded driver stage timings and admin-only operational reporting."""
import csv
import io
from collections import Counter
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from flask import abort, request, send_file

PHASES = {'load': 'Load truck at depot', 'deliver': 'Unload empties at client',
          'collect': 'Reload full bins at client', 'return': 'Unload full bins at depot'}
COLUMNS = {'load': 'loaded_at', 'deliver': 'delivered_at', 'collect': 'picked_up_at', 'return': 'returned_at'}


def migrate_timings(conn):
    conn.execute('''CREATE TABLE IF NOT EXISTS bf_step_timings (
        id INTEGER PRIMARY KEY, workday_id INTEGER NOT NULL REFERENCES bf_workdays(id),
        truck_id INTEGER NOT NULL REFERENCES bf_trucks(id), driver_id INTEGER NOT NULL REFERENCES users(id),
        work_date TEXT NOT NULL, state TEXT NOT NULL, timezone TEXT NOT NULL,
        driver_name TEXT NOT NULL, truck_name TEXT NOT NULL,
        phase TEXT NOT NULL CHECK(phase IN ('load','deliver','collect','return')), scope_id INTEGER NOT NULL,
        location_name TEXT NOT NULL, started_at TEXT NOT NULL, finished_at TEXT,
        basis TEXT NOT NULL CHECK(basis IN ('workflow','partial')),
        bin_count INTEGER NOT NULL, scanned_count INTEGER NOT NULL DEFAULT 0, bin_mix TEXT NOT NULL,
        UNIQUE(workday_id,truck_id,phase,scope_id))''')
    conn.execute('CREATE INDEX IF NOT EXISTS bf_step_timings_date ON bf_step_timings(work_date,state,driver_id,truck_id)')


def phase_rows(data, phase, scope):
    direction = 'deliver' if phase in ('load', 'deliver') else 'collect'
    return [r for r in data['rows'] if r['direction'] == direction and
            (phase == 'load' or (r['warehouse_id'] == scope if phase == 'return' else r['job_id'] == scope))]


def begin_current(conn, user, truck_id, run_date, stamp, basis='workflow'):
    from binflow_runs import run_data
    from binflow_workday import driver_today
    data = run_data(conn, truck_id, run_date, user['id'], driver_today(conn, user, stamp))
    phase = data['stage']
    if not data['simple_flow'] or phase not in PHASES:
        return
    scope = 0 if phase == 'load' else data['depot']['id'] if phase == 'return' else data['current']['id']
    rows = phase_rows(data, phase, scope)
    if not rows:
        return
    # No backdating: an in-flight stage first observed at a scan is explicitly partial.
    scanned = sum(bool(r[COLUMNS[phase]]) for r in rows)
    if scanned:
        basis = 'partial'
    location = (', '.join(sorted({r['warehouse_name'] for r in rows})) if phase == 'load' else
                data['depot']['name'] if phase == 'return' else
                data['current']['client_name'] + ' · ' + data['current']['site_name'])
    mix = ', '.join(f'{count} × {size}' for size, count in sorted(Counter(r['size_name'] for r in rows).items()))
    day = data['workday']
    conn.execute('''INSERT OR IGNORE INTO bf_step_timings
        (workday_id,truck_id,driver_id,work_date,state,timezone,driver_name,truck_name,phase,scope_id,
         location_name,started_at,basis,bin_count,scanned_count,bin_mix) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
        (day['id'],truck_id,user['id'],run_date,day['state'],day['timezone'],
         (user['first_name']+' '+user['last_name']).strip(),data['truck']['name'],phase,scope,
         location,stamp,basis,len(rows),scanned,mix))


def finish_scan(conn, user, job, action, stamp):
    from binflow_runs import run_data
    from binflow_workday import driver_today
    phase = 'load' if action == 'unload' else action
    if phase not in PHASES:
        return
    data = run_data(conn, job['truck_id'], job['scheduled_date'], user['id'], driver_today(conn,user,stamp))
    if not data['simple_flow']:
        return
    scope = 0 if phase == 'load' else job['warehouse_id'] if phase == 'return' else job['id']
    rows = phase_rows(data, phase, scope)
    count = sum(bool(r[COLUMNS[phase]]) for r in rows)
    mix = ', '.join(f'{amount} × {size}' for size, amount in sorted(Counter(r['size_name'] for r in rows).items()))
    conn.execute('''UPDATE bf_step_timings SET scanned_count=?,finished_at=?,bin_count=?,bin_mix=?
        WHERE workday_id=? AND truck_id=? AND phase=? AND scope_id=?''',
        (count,stamp if rows and count == len(rows) else None,len(rows),mix,data['workday']['id'],job['truck_id'],phase,scope))
    # The last delivery scan opens collection immediately, with no extra driver tap.
    begin_current(conn, user, job['truck_id'], job['scheduled_date'], stamp)


def timing_values(row, stamp):
    result = dict(row)
    start = datetime.fromisoformat(row['started_at'])
    end = datetime.fromisoformat(row['finished_at'] or row['day_finished_at'] or stamp)
    seconds = (end-start).total_seconds()
    paused = 0
    if row['break_started_at']:
        pause_start = datetime.fromisoformat(row['break_started_at'])
        pause_end = datetime.fromisoformat(row['break_finished_at'] or stamp)
        paused = max(0, (min(end,pause_end)-max(start,pause_start)).total_seconds())
    valid = seconds >= 0
    result.update(seconds=max(0,round(seconds-paused)), break_seconds=round(paused),
                  eligible=bool(row['finished_at'] and row['basis']=='workflow' and valid),
                  label=PHASES[row['phase']], status=('Partial' if row['basis']=='partial' else
                  'Complete' if row['finished_at'] and valid else 'Incomplete' if row['day_finished_at'] or not valid else 'In progress'))
    zone = ZoneInfo(row['timezone'])
    result['start_local'] = start.astimezone(zone).strftime('%d/%m/%Y %H:%M:%S %Z')
    result['end_local'] = datetime.fromisoformat(row['finished_at']).astimezone(zone).strftime('%d/%m/%Y %H:%M:%S %Z') if row['finished_at'] else '—'
    return result


def duration(seconds):
    if seconds is None:
        return '—'
    seconds = round(seconds)
    hours, rest = divmod(seconds,3600)
    minutes, seconds = divmod(rest,60)
    return (f'{hours}h ' if hours else '') + f'{minutes}m {seconds:02d}s'


def register_reports(app, db, require, render, now):
    app.jinja_env.filters['bf_duration'] = duration

    @app.get('/binflow/reports')
    @require('admin')
    def bf_reports():
        today = date.fromisoformat(now()[:10])
        try:
            start = date.fromisoformat(request.args.get('start') or (today-timedelta(days=29)).isoformat())
            end = date.fromisoformat(request.args.get('end') or today.isoformat())
            if end < start or (end-start).days > 365:
                raise ValueError()
            state = request.args.get('state','')
            if state not in ('','QLD','NSW','VIC','SA','WA','ACT','TAS','NT'):
                raise ValueError()
            driver = int(request.args.get('driver') or 0)
            truck = int(request.args.get('truck') or 0)
            if driver < 0 or truck < 0:
                raise ValueError()
        except ValueError:
            abort(400, 'Choose valid filters and a date range of no more than 366 days.')
        where = ['t.work_date BETWEEN ? AND ?']
        params = [start.isoformat(),end.isoformat()]
        for column,value in (('state',state),('driver_id',driver),('truck_id',truck)):
            if value:
                where.append('t.'+column+'=?');params.append(value)
        rows = [timing_values(r,now()) for r in db().execute('''SELECT t.*,d.break_started_at,d.break_finished_at,
            d.finished_at AS day_finished_at FROM bf_step_timings t JOIN bf_workdays d ON d.id=t.workday_id
            WHERE '''+' AND '.join(where)+' ORDER BY t.work_date DESC,t.started_at DESC,t.id DESC',params)]
        summaries = []
        for phase,label in PHASES.items():
            completed = [r for r in rows if r['phase']==phase and r['eligible']]
            total = sum(r['seconds'] for r in completed)
            bins = sum(r['bin_count'] for r in completed)
            summaries.append(dict(label=label,count=len(completed),bins=bins,total=total,
                average=total/len(completed) if completed else None,per_bin=total/bins if bins else None))
        filters = dict(start=start.isoformat(),end=end.isoformat(),state=state,driver=driver or '',truck=truck or '')
        if request.args.get('export') == 'csv':
            output=io.StringIO();writer=csv.writer(output)
            writer.writerow(['Run date','State','Driver','Truck','Stage','Client / depot','Bin mix','Expected bins',
                             'Scanned bins','Started (local)','Finished (local)','Duration seconds (break excluded)',
                             'Recorded break seconds','Status','Included in averages'])
            def safe(value):
                text=str(value)
                return "'"+text if text.lstrip().startswith(('=','+','-','@')) or text.startswith(('\t','\r','\n')) else text
            for r in rows:
                writer.writerow([safe(v) for v in (r['work_date'],r['state'],r['driver_name'],r['truck_name'],r['label'],
                    r['location_name'],r['bin_mix'],r['bin_count'],r['scanned_count'],r['start_local'],r['end_local'],
                    r['seconds'],r['break_seconds'],r['status'],'Yes' if r['eligible'] else 'No')])
            return send_file(io.BytesIO(output.getvalue().encode('utf-8-sig')),mimetype='text/csv',as_attachment=True,
                             download_name=f'binflow-step-timings-{start}-{end}.csv')
        drivers=db().execute('''SELECT DISTINCT t.driver_id AS id,u.first_name||' '||u.last_name AS name
            FROM bf_step_timings t JOIN users u ON u.id=t.driver_id ORDER BY name''').fetchall()
        trucks=db().execute('''SELECT DISTINCT t.truck_id AS id,b.name FROM bf_step_timings t
            JOIN bf_trucks b ON b.id=t.truck_id ORDER BY b.name''').fetchall()
        return render('reports',section='reports',rows=rows,summaries=summaries,filters=filters,drivers=drivers,trucks=trucks)
