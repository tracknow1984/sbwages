"""Employee QR cards and the limited, current record visible at a site gate."""
import io
import re
from datetime import date
from sqlite3 import IntegrityError

import qrcode
from flask import abort, flash, g, make_response, redirect, render_template, request, send_file, url_for

STATES = {'approved':'Approved', 'pending':'Pending', 'withdrawn':'Withdrawn'}


def register_credentials(app, db, require, today):
    with app.app_context():
        db().executescript("""
            CREATE TABLE IF NOT EXISTS employee_badges (
                user_id INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
                token TEXT NOT NULL UNIQUE DEFAULT (lower(hex(randomblob(24)))));
            CREATE TRIGGER IF NOT EXISTS employee_badge_on_create AFTER INSERT ON users
                WHEN NEW.role='employee' BEGIN
                INSERT INTO employee_badges(user_id) VALUES(NEW.id); END;
            CREATE TRIGGER IF NOT EXISTS employee_badge_on_termination AFTER UPDATE OF status ON users
                WHEN NEW.role='employee' AND NEW.status='terminated' AND OLD.status!='terminated' BEGIN
                UPDATE employee_badges SET token=lower(hex(randomblob(24))) WHERE user_id=NEW.id; END;
            INSERT INTO employee_badges(user_id) SELECT id FROM users u WHERE role='employee'
                AND NOT EXISTS (SELECT 1 FROM employee_badges b WHERE b.user_id=u.id);
            CREATE TABLE IF NOT EXISTS employee_credentials (
                id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                kind TEXT NOT NULL CHECK(kind IN ('security','site')), name TEXT NOT NULL COLLATE NOCASE,
                status TEXT NOT NULL CHECK(status IN ('approved','pending','withdrawn')),
                expires TEXT NOT NULL DEFAULT '', updated_by INTEGER NOT NULL REFERENCES users(id),
                revision INTEGER NOT NULL DEFAULT 1, UNIQUE(user_id,kind,name));
        """)

    def employee(uid):
        person = db().execute("""SELECT u.id,u.first_name,u.last_name,u.status,b.token FROM users u
            JOIN employee_badges b ON b.user_id=u.id WHERE u.id=? AND u.role='employee'""",(uid,)).fetchone()
        if not person:
            abort(404)
        return person

    def records(uid):
        return db().execute("""SELECT id,kind,name,status,expires,revision FROM employee_credentials
            WHERE user_id=? ORDER BY kind,name COLLATE NOCASE""",(uid,)).fetchall()

    @app.get('/employee/qr-card')
    @require('employee')
    def my_employee_qr():
        return render_template('employee_qr_card.html', person=employee(g.user['id']), tab='details')

    @app.get('/staff/<int:user_id>/qr.png')
    def employee_qr_png(user_id):
        if not g.user or (g.user['role']!='admin' and g.user['id']!=user_id):
            abort(404)
        person=employee(user_id)
        if person['status']!='active':
            abort(404)
        base=(app.config['APP_URL'] or request.url_root).rstrip('/')
        image=io.BytesIO()
        qrcode.make(base+url_for('verify_employee',token=person['token']),box_size=8,border=4).save(image,format='PNG')
        image.seek(0)
        return send_file(image,mimetype='image/png',as_attachment=request.args.get('download')=='1',
                         download_name=f'sb-empire-employee-{user_id}-qr.png')

    @app.route('/admin/staff/<int:user_id>/credentials',methods=['GET','POST'])
    @require('admin')
    def staff_credentials(user_id):
        person=employee(user_id)
        error=None
        if request.method=='POST':
            form=request.form
            conn=db()
            try:
                conn.execute('BEGIN IMMEDIATE')
                if form.get('action')=='rotate':
                    if not conn.execute("""UPDATE employee_badges SET token=lower(hex(randomblob(24)))
                            WHERE user_id=? AND token=?""",(user_id,form.get('token',''))).rowcount:
                        abort(409,'This QR code has already changed.')
                else:
                    kind=form.get('kind')
                    name=' '.join(form.get('name','').split())
                    status=form.get('status')
                    expiry=form.get('expires','').strip()
                    if kind not in ('security','site') or not name or len(name)>150 or status not in STATES:
                        raise ValueError('Enter a check or site name and select its status.')
                    if expiry:
                        try:
                            expiry=date.fromisoformat(expiry).isoformat()
                        except ValueError:
                            raise ValueError('Choose a valid expiry date.') from None
                    if form.get('record_id'):
                        changed=conn.execute("""UPDATE employee_credentials SET name=?,status=?,expires=?,
                            updated_by=?,revision=revision+1 WHERE id=? AND user_id=? AND kind=? AND revision=?""",
                            (name,status,expiry,g.user['id'],form.get('record_id',type=int),user_id,kind,form.get('revision',type=int)))
                        if not changed.rowcount:
                            abort(409,'This record changed. Reopen it before editing.')
                    else:
                        conn.execute("""INSERT INTO employee_credentials(user_id,kind,name,status,expires,updated_by)
                            VALUES(?,?,?,?,?,?)""",(user_id,kind,name,status,expiry,g.user['id']))
                conn.commit()
                flash('Employee QR code replaced.' if form.get('action')=='rotate' else 'Credential saved.','success')
                return redirect(url_for('staff_credentials',user_id=user_id))
            except ValueError as exc:
                conn.rollback()
                error=str(exc)
            except IntegrityError:
                conn.rollback()
                error='This check or site is already listed. Open its existing record to make changes.'
        sites=db().execute("""SELECT DISTINCT customer_site || CASE WHEN suburb!='' THEN ' · ' || suburb ELSE '' END ||
            CASE WHEN state!='' THEN ' · ' || state ELSE '' END AS label FROM bf_client_site_profiles ORDER BY label""")
        return render_template('employee_qr_card.html',person=person,manage=True,records=records(user_id),
            states=STATES,sites=sites,error=error,tab='staff'),400 if error else 200

    @app.get('/verify/employee/<token>')
    def verify_employee(token):
        if not re.fullmatch('[0-9a-f]{48}',token):
            abort(404)
        person=db().execute("""SELECT u.first_name,u.last_name,u.id,u.licence_state,u.licence_class,
            u.licence_expiry,CASE WHEN length(trim(u.licence_number))>4 THEN substr(trim(u.licence_number),-4) ELSE '' END AS suffix
            FROM employee_badges b JOIN users u ON u.id=b.user_id WHERE token=? AND u.role='employee' AND u.status='active'""",(token,)).fetchone()
        if not person:
            abort(404)
        day=today().isoformat()
        checks=[]
        sites=[]
        for row in records(person['id']):
            expired=bool(row['expires'] and row['expires']<day)
            if row['kind']=='site':
                if row['status']=='approved' and not expired:
                    sites.append(row)
            else:
                checks.append(dict(row, label='Expired' if expired and row['status']=='approved' else STATES[row['status']]))
        response=make_response(render_template('employee_verification.html',person=person,checks=checks,sites=sites,day=day))
        response.headers['X-Robots-Tag']='noindex, nofollow, noarchive'
        return response
