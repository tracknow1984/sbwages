"""Independent supplier contact registers, sharing the customer profile fields."""
import json
import sqlite3

from flask import abort, flash, redirect, request, url_for

from binflow_client_profiles import KEYS, validate

CATEGORIES = {
    'decommission-partners': 'Decommission Partners',
    'industry-suppliers': 'Industry Suppliers',
}
SCHEMA = '''
CREATE TABLE IF NOT EXISTS bf_supplier_site_profiles (
 id INTEGER PRIMARY KEY,
 category TEXT NOT NULL CHECK(category IN ('decommission-partners','industry-suppliers')),
 group_name TEXT NOT NULL DEFAULT '', customer_site TEXT NOT NULL,
 address TEXT NOT NULL DEFAULT '', suburb TEXT NOT NULL DEFAULT '', state TEXT NOT NULL DEFAULT '',
 postcode TEXT NOT NULL DEFAULT '', contact_name TEXT NOT NULL DEFAULT '',
 contact_email TEXT NOT NULL DEFAULT '', contact_number TEXT NOT NULL DEFAULT '', schedule TEXT NOT NULL DEFAULT '',
 asic_required TEXT NOT NULL DEFAULT 'unknown' CHECK(asic_required IN ('unknown','yes','no')),
 contact_verified TEXT NOT NULL DEFAULT 'no' CHECK(contact_verified IN ('yes','no')),
 version INTEGER NOT NULL DEFAULT 1, updated_at TEXT NOT NULL, updated_by TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS bf_supplier_site_identity ON bf_supplier_site_profiles
 (category,group_name COLLATE NOCASE,customer_site COLLATE NOCASE,state,address COLLATE NOCASE);
CREATE TABLE IF NOT EXISTS bf_supplier_profile_history (
 id INTEGER PRIMARY KEY, profile_id INTEGER NOT NULL REFERENCES bf_supplier_site_profiles(id),
 snapshot TEXT NOT NULL, changed_at TEXT NOT NULL, changed_by TEXT NOT NULL
);
'''


def migrate(conn):
    conn.executescript(SCHEMA)


def category_label(category):
    if category not in CATEGORIES:
        abort(404)
    return CATEGORIES[category]


def register_context(conn, category, args=None):
    from binflow_client_profiles import filter_register
    label = category_label(category)
    rows = conn.execute('SELECT * FROM bf_supplier_site_profiles WHERE category=? ORDER BY id',
                        (category,)).fetchall()
    context = filter_register(rows, args or {}, 'bf_supplier_profiles', {'category': category}, supplier=True)
    return dict(context, supplier=True, category=category, register_title=label,
                site_label='Supplier Site', edit_endpoint='bf_supplier_profile',
                new_url=url_for('bf_new_supplier_profile', category=category),
                list_url=url_for('bf_supplier_profiles', category=category),
                clear_url=url_for('bf_supplier_profiles', category=category),
                add_label='Add supplier site', view='current', current_count=len(rows), archived_count=0,
                unlinked=0, bin_counts={})


def overview(conn):
    return [register_context(conn, category) for category in CATEGORIES]


def register_suppliers(app, db, require, render, now, actor):
    @app.get('/binflow/supplier-profiles')
    @require('admin')
    def bf_supplier_register():
        return redirect(url_for('bf_supplier_profiles', category='decommission-partners'))

    @app.get('/binflow/supplier-profiles/<category>')
    @require('admin')
    def bf_supplier_profiles(category):
        context = register_context(db(), category, request.args)
        counts = {r['category']: r['n'] for r in db().execute(
            'SELECT category,COUNT(*) AS n FROM bf_supplier_site_profiles GROUP BY category')}
        return render('supplier_profiles', section='suppliers', categories=CATEGORIES, counts=counts, **context)

    @app.route('/binflow/supplier-profiles/<category>/new', methods=['GET', 'POST'])
    @require('admin')
    def bf_new_supplier_profile(category):
        label = category_label(category)
        values = dict.fromkeys(KEYS, '')
        values.update(asic_required='unknown', contact_verified='no')
        error = None
        if request.method == 'POST':
            values.update({k: request.form.get(k, '').strip() for k in KEYS})
            try:
                details = validate(values, site_label='Supplier Site')
                if not details['state'] or not details['address']:
                    raise ValueError('Enter the site address and choose its state.')
                db().execute('BEGIN IMMEDIATE')
                db().execute('INSERT INTO bf_supplier_site_profiles (' + ','.join(KEYS) +
                             ',category,updated_at,updated_by) VALUES (' +
                             ','.join('?' for _ in range(len(KEYS) + 3)) + ')',
                             (*[details[k] for k in KEYS], category, now(), actor()))
                db().commit()
                flash('Supplier site profile added to ' + label + '.', 'success')
                return redirect(url_for('bf_supplier_profiles', category=category, q=details['customer_site']))
            except sqlite3.IntegrityError:
                error = 'This supplier site already exists in this category. Open its profile to update the details.'
            except ValueError as exc:
                error = str(exc)
            db().rollback()
        return render('supplier_profile', section='suppliers', category=category, category_label=label,
                      profile=None, values=values, creating=True, error=error), 400 if error else 200

    @app.route('/binflow/supplier-profiles/site/<int:profile_id>', methods=['GET', 'POST'])
    @require('admin')
    def bf_supplier_profile(profile_id):
        if request.method == 'POST':
            db().execute('BEGIN IMMEDIATE')
        profile = db().execute('SELECT * FROM bf_supplier_site_profiles WHERE id=?', (profile_id,)).fetchone()
        if not profile:
            abort(404)
        values, error = dict(profile), None
        category = profile['category']
        if request.method == 'POST':
            values.update({k: request.form.get(k, '').strip() for k in KEYS})
            try:
                if str(profile['version']) != request.form.get('version'):
                    raise ValueError('This profile changed while you were editing. Reload it before saving.')
                values.update(validate(values, site_label='Supplier Site'))
                if not values['state'] or not values['address']:
                    raise ValueError('Enter the site address and choose its state.')
                stamp, changed_by = now(), actor()
                db().execute('INSERT INTO bf_supplier_profile_history(profile_id,snapshot,changed_at,changed_by) VALUES(?,?,?,?)',
                             (profile_id, json.dumps(dict(profile)), stamp, changed_by))
                db().execute('UPDATE bf_supplier_site_profiles SET ' + ','.join(k + '=?' for k in KEYS) +
                             ',version=version+1,updated_at=?,updated_by=? WHERE id=?',
                             (*[values[k] for k in KEYS], stamp, changed_by, profile_id))
                db().commit()
                flash('Supplier site profile saved.', 'success')
                return redirect(url_for('bf_supplier_profiles', category=category))
            except sqlite3.IntegrityError:
                error = 'This supplier site already exists in this category. Check the site name and address.'
            except ValueError as exc:
                error = str(exc)
            db().rollback()
        return render('supplier_profile', section='suppliers', category=category, category_label=CATEGORIES[category],
                      profile=profile, values=values, creating=False, error=error), 400 if error else 200
