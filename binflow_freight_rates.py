"""Account-specific interstate rate cards. Amounts are AUD cents per vehicle movement, ex GST."""
import hashlib
import json

from flask import abort, flash, redirect, request, url_for
from binflow_collections import STATES
from binflow_sizes import amount

VEHICLES = ('RIGID','SEMI TRAILER','B-DOUBLE')

def migrate(conn):
    conn.execute("""CREATE TABLE IF NOT EXISTS bf_interstate_rates (
        account_id INTEGER NOT NULL REFERENCES bf_client_accounts(id), origin TEXT NOT NULL,destination TEXT NOT NULL,
        vehicle TEXT NOT NULL,cents INTEGER NOT NULL CHECK(cents>=0),PRIMARY KEY(account_id,origin,destination,vehicle))""")
    columns={r['name'] for r in conn.execute('PRAGMA table_info(bf_interstate_bookings)')}
    if 'vehicle' not in columns:
        conn.execute("ALTER TABLE bf_interstate_bookings ADD COLUMN vehicle TEXT NOT NULL DEFAULT ''")
    if 'rate_cents' not in columns:
        conn.execute('ALTER TABLE bf_interstate_bookings ADD COLUMN rate_cents INTEGER')

def rates_for(conn, account_id):
    rows=conn.execute('SELECT origin,destination,vehicle,cents FROM bf_interstate_rates WHERE account_id=?',(account_id,))
    return {f"{r['origin']}:{r['destination']}:{r['vehicle']}":r['cents'] for r in rows}

def register(app,db,require,render):
    @app.route('/binflow/interstate-transfers/access/<int:aid>/rates',methods=['GET','POST'])
    @require('admin')
    def bf_interstate_rates(aid):
        account=db().execute('SELECT id,name,username FROM bf_client_accounts WHERE id=?',(aid,)).fetchone()
        origin=request.args.get('origin','QLD')
        if not account: abort(404)
        if origin not in STATES: abort(400)
        if request.method=='POST': db().execute('BEGIN IMMEDIATE')
        rows=db().execute('SELECT destination,vehicle,cents FROM bf_interstate_rates WHERE account_id=? AND origin=? ORDER BY destination,vehicle',(aid,origin))
        saved={r['destination']+':'+r['vehicle']:r['cents'] for r in rows}
        version=hashlib.sha256(json.dumps(saved,sort_keys=True).encode()).hexdigest()
        fields={state+':'+vehicle for state in STATES if state!=origin for vehicle in VEHICLES}
        values={key:f'{cents/100:.2f}' for key,cents in saved.items()}
        error=None
        if request.method=='POST':
            if request.form.get('version')!=version:
                db().rollback()
                abort(409,'This rate card has changed. Reopen it before saving.')
            try:
                if not fields.issubset(request.form):
                    raise ValueError('The matrix is incomplete. Reload this pickup state before saving.')
                values={key:request.form[key].strip() for key in fields}
                changes=[(aid,origin,*key.split(':'),amount(value,'Rate',1000000,100))
                         for key,value in values.items() if value]
                db().execute('DELETE FROM bf_interstate_rates WHERE account_id=? AND origin=?',(aid,origin))
                db().executemany('INSERT INTO bf_interstate_rates VALUES(?,?,?,?,?)',changes)
                db().commit()
                flash(f'{origin} rates saved for {account["name"]}.','success')
                return redirect(url_for('bf_interstate_rates',aid=aid,origin=origin),code=303)
            except ValueError as exc:
                db().rollback()
                error=str(exc)
        return render('interstate_rates',section='interstate',client=account,regions=STATES,origin=origin,
                      vehicles=VEHICLES,values=values,version=version,error=error),400 if error else 200
