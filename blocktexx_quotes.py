"""Public operational run sheets and private, immutable provider quotations."""
import json
import re
import secrets
from decimal import Decimal, InvalidOperation
from datetime import datetime, timezone
from pathlib import Path
from flask import request, session, render_template, redirect, url_for

STATES = ('NSW', 'VIC', 'SA', 'WA')
FIELDS = ('name', 'address', 'frequency', 'equipment', 'notes')

def projection(model):
    history = model.get('weight_history', {}).get('pickups', [])
    result = {'source': model.get('source', ''), 'states': {}}
    for state in STATES:
        data = model.get('states', {}).get(state, {})
        sites = []
        for site in model.get('sites', []):
            if site.get('state') != state:
                continue
            rows = [r for r in history if r.get('site_id') == site['id']]
            sites.append({'id': site['id'], **{k: site.get(k, '') for k in FIELDS},
                          'pickup_kg': sum(r['kg'] for r in rows) / len(rows) if rows else None})
        runs = []
        for run in data.get('runs', []):
            times = [run.get(k) for k in ('drive_min','service_min','depot_min','prep_min','wait_min','break_min')]
            runs.append({'id': run['id'], 'name': run['name'], 'sequence': run.get('sequence',''),
                         'monthly_runs': run.get('runs_4w') * 13 / 12 if run.get('runs_4w') is not None else None,
                         'km': run.get('km'), 'hours': sum(times)/60 if all(t is not None for t in times) else None})
        partners = [{k:p.get(k,'') for k in FIELDS} for p in model.get('partners',[]) if p.get('state')==state]
        result['states'][state] = {'depot': data.get('depot','Unconfirmed'), 'monthly_kg':data.get('monthly_kg'),
                                   'sites':sites,'runs':runs,'partners':partners}
    return result

def number(value):
    if value in ('', None): return None
    try: n=Decimal(str(value))
    except InvalidOperation: raise ValueError('Enter valid non-negative prices and quantities.') from None
    if not n.is_finite() or n < 0 or n > 10000000: raise ValueError('Price or quantity is outside the allowed range.')
    return float(n)

def calculate(snapshot, prices):
    summary=[]
    for state,data in snapshot['states'].items():
        rate=prices.get(state,{})
        for kind in ('local','linehaul','decomm'):
            item=rate.get(kind,{})
            total=None; gap=''
            if item.get('skip'): gap='Not offered'
            elif kind=='local':
                hourly=item.get('hourly'); minimum=item.get('minimum'); extra=item.get('extra')
                known=[r for r in data['runs'] if r['hours'] is not None and r['monthly_runs'] is not None]
                if hourly is not None and minimum is not None and known:
                    total=round(sum(max(minimum,r['hours'])*r['monthly_runs']*hourly for r in known)+(extra or 0),2)
                    if len(known)!=len(data['runs']): gap='Regional/ad hoc runs unmeasured'
                else: gap='Route hours or pricing missing'
            else:
                if item.get('rate') is not None and item.get('trips') is not None:
                    total=round(item['rate']*item['trips'],2)
                else: gap='Trip price or monthly trips missing'
            basis = ('Hourly $%s; minimum %s hours; extra $%s/month' % (item.get('hourly'),item.get('minimum'),item.get('extra')) if kind=='local' else '$%s/trip × %s trips/month' % (item.get('rate'),item.get('trips')))
            basis=basis.replace('None','Unpriced')
            summary.append({'basis':basis,'state':state,'kind':kind,'total':total,'gap':gap,'notes':item.get('notes','')})
    return summary

def register_quotes(app, db, require):
    with app.app_context():
        db().execute('CREATE TABLE IF NOT EXISTS blocktexx_quotes (id TEXT PRIMARY KEY, owner TEXT NOT NULL, organisation TEXT NOT NULL, contact TEXT NOT NULL, email TEXT NOT NULL, created_at TEXT NOT NULL, snapshot TEXT NOT NULL, prices TEXT NOT NULL, status TEXT NOT NULL DEFAULT \'Pending\')')
        db().execute('CREATE TABLE IF NOT EXISTS blocktexx_quote_snapshots (id TEXT PRIMARY KEY, data TEXT NOT NULL, created_at TEXT NOT NULL)')
        db().execute('CREATE TABLE IF NOT EXISTS blocktexx_quote_receipts (snapshot_id TEXT PRIMARY KEY, quote_id TEXT NOT NULL)')
        db().commit()
    def source():
        row=db().execute('SELECT data FROM blocktexx_model WHERE id=1').fetchone()
        model=json.loads(row['data']) if row else json.loads((Path(__file__).parent/'quote_seed.json').read_text())
        return projection(model)
    @app.get('/blocktexx/run-sheets')
    def public_run_sheets():
        session.setdefault('quote_owner',secrets.token_urlsafe(32))
        snapshot=source()
        session['quote_snapshot']=secrets.token_urlsafe(16)
        # Server-side snapshot prevents pricing stale or tampered operational data.
        db().execute('CREATE TABLE IF NOT EXISTS blocktexx_quote_snapshots (id TEXT PRIMARY KEY, data TEXT NOT NULL, created_at TEXT NOT NULL)')
        db().execute("DELETE FROM blocktexx_quote_snapshots WHERE created_at < datetime('now','-7 days')")
        db().execute('INSERT INTO blocktexx_quote_snapshots VALUES(?,?,?)',(session['quote_snapshot'],json.dumps(snapshot),datetime.now(timezone.utc).isoformat()))
        db().commit()
        return render_template('blocktexx_run_sheets.html',snapshot=snapshot)
    @app.post('/blocktexx/run-sheets')
    def submit_run_sheet_quote():
        row=db().execute('SELECT data FROM blocktexx_quote_snapshots WHERE id=?',(session.get('quote_snapshot',''),)).fetchone()
        if not row: return 'Refresh the run sheet before submitting.',400
        receipt=db().execute('SELECT quote_id FROM blocktexx_quote_receipts WHERE snapshot_id=?',(session.get('quote_snapshot',''),)).fetchone()
        if receipt:
            session['last_quote']=receipt['quote_id']
            return redirect(url_for('run_sheet_received'))
        snapshot=json.loads(row['data']); prices={}
        try:
            details={k:request.form.get(k,'').strip() for k in ('organisation','contact','email')}
            if any(not v or len(v)>254 for v in details.values()) or not re.fullmatch(r'[^\s@]+@[^\s@]+\.[^\s@]+',details['email']):
                raise ValueError('Enter organisation, contact name and a valid email.')
            offered=False
            for state in STATES:
                prices[state]={}
                for kind in ('local','linehaul','decomm'):
                    prefix=f'{state}_{kind}_'; item={'skip':request.form.get(prefix+'skip')=='on'}
                    fields=('hourly','minimum','extra') if kind=='local' else ('rate','trips')
                    item.update({k:number(request.form.get(prefix+k)) for k in fields})
                    item['notes']=request.form.get(prefix+'notes','').strip()
                    if len(item['notes'])>2000: raise ValueError('Keep notes below 2,000 characters.')
                    if not item['skip'] and any(item[k] is not None for k in fields): offered=True
                    prices[state][kind]=item
            if not offered: raise ValueError('Enter pricing for at least one service.')
        except ValueError as exc:
            return render_template('blocktexx_run_sheets.html',snapshot=snapshot,error=str(exc)),400
        qid=secrets.token_urlsafe(12)
        db().execute('INSERT INTO blocktexx_quotes(id,owner,organisation,contact,email,created_at,snapshot,prices) VALUES(?,?,?,?,?,?,?,?)',
                     (qid,session['quote_owner'],details['organisation'],details['contact'],details['email'],datetime.now(timezone.utc).isoformat(),json.dumps(snapshot),json.dumps(prices)))
        db().execute('INSERT INTO blocktexx_quote_receipts VALUES(?,?)',(session['quote_snapshot'],qid))
        db().commit()
        session['last_quote']=qid
        return redirect(url_for('run_sheet_received'))
    @app.get('/blocktexx/run-sheets/received')
    def run_sheet_received():
        row=db().execute('SELECT id FROM blocktexx_quotes WHERE id=? AND owner=?',(session.get('last_quote',''),session.get('quote_owner',''))).fetchone()
        if not row: return redirect(url_for('public_run_sheets'))
        return render_template('blocktexx_run_sheets.html',received=row['id'])
    @app.get('/admin/blocktexx/quotes')
    @require('admin')
    def review_run_sheet_quotes():
        quotes=[]
        for row in db().execute('SELECT * FROM blocktexx_quotes ORDER BY created_at DESC LIMIT 200').fetchall():
            q=dict(row);q['summary']=calculate(json.loads(q['snapshot']),json.loads(q['prices']))
            q['total']=round(sum(r['total'] or 0 for r in q['summary']),2)
            q['complete']=all(not r['gap'] for r in q['summary'])
            quotes.append(q)
        return render_template('blocktexx_quote_review.html',quotes=quotes)
    @app.post('/admin/blocktexx/quotes/<qid>/review')
    @require('admin')
    def mark_run_sheet_review(qid):
        status=request.form.get('status')
        if status not in ('Pending','Reviewed','Declined'): return 'Invalid review status',400
        db().execute('UPDATE blocktexx_quotes SET status=? WHERE id=?',(status,qid));db().commit()
        return redirect(url_for('review_run_sheet_quotes'))
