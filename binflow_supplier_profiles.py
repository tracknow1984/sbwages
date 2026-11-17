"""Independent supplier contact registers, sharing the customer profile fields."""
import json
import sqlite3

from flask import abort, flash, g, redirect, render_template, request, url_for

from binflow_client_profiles import KEYS, STATES, validate

DECOMM = 'decommission-partners'
CATEGORIES = {
    'industry-suppliers': 'Industry Supplier',
    DECOMM: 'Decomm Partners',
}
INDUSTRY_GROUPS = ('TRANSPORT','CONSUMABLES','OTHER')

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
CREATE TABLE IF NOT EXISTS bf_supplier_freight_rates (
 supplier_id INTEGER NOT NULL REFERENCES bf_supplier_site_profiles(id) ON DELETE CASCADE,
 origin TEXT NOT NULL, destination TEXT NOT NULL, vehicle TEXT NOT NULL,
 cents INTEGER NOT NULL CHECK(cents>=0), PRIMARY KEY(supplier_id,origin,destination,vehicle)
);
CREATE TABLE IF NOT EXISTS bf_supplier_freight_terms (
 supplier_id INTEGER PRIMARY KEY REFERENCES bf_supplier_site_profiles(id) ON DELETE CASCADE,
 rigid_hourly_cents INTEGER CHECK(rigid_hourly_cents>=0), fuel_bps INTEGER NOT NULL CHECK(fuel_bps>=0)
);
'''


def migrate(conn):
    conn.executescript(SCHEMA)


def category_label(category):
    if category not in CATEGORIES:
        abort(404)
    return CATEGORIES[category]


def site_label(category):
    return 'Company Name' if category=='industry-suppliers' else 'Supplier Site'


def is_transport_supplier(profile):
    return profile['category'] == 'industry-suppliers' and profile['group_name'].strip().upper() == 'TRANSPORT'


def supplier_details(category, values, previous=None):
    group = values['group_name']
    if category=='industry-suppliers' and group and group not in INDUSTRY_GROUPS and group!=previous:
        raise ValueError('Choose TRANSPORT, CONSUMABLES or OTHER.')
    return validate(values, site_label=site_label(category))


def register_context(conn, category, args=None, *, external=False):
    from binflow_client_profiles import filter_register
    label = category_label(category)
    rows = conn.execute('SELECT * FROM bf_supplier_site_profiles WHERE category=? ORDER BY id',
                        (category,)).fetchall()
    endpoint = 'bf_client_decomm_partners' if external else 'bf_supplier_profiles'
    params = {} if external else {'category': category}
    list_url = url_for(endpoint, **params)
    context = filter_register(rows, args or {}, endpoint, params, supplier=True)
    return dict(context, supplier=True, category=category, register_title=label,
                site_label='Partner site' if external else site_label(category),
                edit_endpoint='bf_client_decomm_edit' if external else 'bf_supplier_profile',
                new_url=url_for('bf_client_decomm_new' if external else 'bf_new_supplier_profile', **params),
                list_url=list_url, clear_url=list_url,
                can_add=not external or 'decomm.add' in g.client_rights,
                can_edit=not external or 'decomm.edit' in g.client_rights,
                add_label='Add partner' if external else 'Add supplier site', view='current', current_count=len(rows), archived_count=0,
                unlinked=0, bin_counts={})


def overview(conn):
    return [register_context(conn, category) for category in CATEGORIES]


def register_suppliers(app, db, require, render, now, actor, client_access):
    app.add_template_global(is_transport_supplier, 'bf_is_transport_supplier')
    app.add_template_global(CATEGORIES, 'bf_supplier_categories')

    def supplier_page(template, external, **values):
        if external:
            return render_template('binflow/' + template + '.html', external=True, account=g.bf_client,
                                   states=STATES, **values)
        return render(template, section='suppliers', **values)

    def register_url(category, external, **params):
        if external:
            return url_for('bf_client_decomm_partners', **params)
        return url_for('bf_supplier_profiles', category=category, **params)

    def changed_by(external):
        return 'Client login ' + g.bf_client['username'] if external else actor()

    @app.get('/binflow/client/decomm-partners')
    @client_access
    def bf_client_decomm_partners():
        context = register_context(db(), DECOMM, request.args, external=True)
        return supplier_page('supplier_profiles', True, **context)

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
        return new_profile(category, external=False)

    @app.route('/binflow/client/decomm-partners/new', methods=['GET', 'POST'])
    @client_access
    def bf_client_decomm_new():
        return new_profile(DECOMM, external=True)

    def new_profile(category, external):
        label = category_label(category)
        values = dict.fromkeys(KEYS, '')
        values.update(asic_required='unknown', contact_verified='no')
        error = None
        if request.method == 'POST':
            values.update({k: request.form.get(k, '').strip() for k in KEYS})
            try:
                details = supplier_details(category,values)
                if not details['state'] or not details['address']:
                    raise ValueError('Enter the site address and choose its state.')
                db().execute('BEGIN IMMEDIATE')
                db().execute('INSERT INTO bf_supplier_site_profiles (' + ','.join(KEYS) +
                             ',category,updated_at,updated_by) VALUES (' +
                             ','.join('?' for _ in range(len(KEYS) + 3)) + ')',
                             (*[details[k] for k in KEYS], category, now(), changed_by(external)))
                db().commit()
                flash('Supplier site profile added to ' + label + '.', 'success')
                return redirect(register_url(category, external, q=details['customer_site']))
            except sqlite3.IntegrityError:
                error = 'This supplier site already exists in this category. Open its profile to update the details.'
            except ValueError as exc:
                error = str(exc)
            db().rollback()
        return supplier_page('supplier_profile', external, list_url=register_url(category, external),
                      category=category, site_label='Partner site' if external else site_label(category),
                      group_options=INDUSTRY_GROUPS if category=='industry-suppliers' else (), category_label=label,
                      profile=None, values=values, creating=True, error=error), 400 if error else 200

    @app.route('/binflow/supplier-profiles/site/<int:profile_id>', methods=['GET', 'POST'])
    @require('admin')
    def bf_supplier_profile(profile_id):
        return edit_profile(profile_id, external=False)

    @app.route('/binflow/client/decomm-partners/<int:profile_id>/edit', methods=['GET', 'POST'])
    @client_access
    def bf_client_decomm_edit(profile_id):
        return edit_profile(profile_id, external=True)

    def edit_profile(profile_id, external):
        if request.method == 'POST':
            db().execute('BEGIN IMMEDIATE')
        query = 'SELECT * FROM bf_supplier_site_profiles WHERE id=?'
        params = (profile_id,)
        if external:
            query += ' AND category=?'
            params += (DECOMM,)
        profile = db().execute(query, params).fetchone()
        if not profile:
            abort(404)
        values, error = dict(profile), None
        category = profile['category']
        if request.method == 'POST':
            values.update({k: request.form.get(k, '').strip() for k in KEYS})
            try:
                if str(profile['version']) != request.form.get('version'):
                    message = 'This profile changed while you were editing. Reload it before saving.'
                    if external:
                        abort(409, message)
                    raise ValueError(message)
                values.update(supplier_details(category,values,profile['group_name']))
                if not values['state'] or not values['address']:
                    raise ValueError('Enter the site address and choose its state.')
                stamp, editor = now(), changed_by(external)
                db().execute('INSERT INTO bf_supplier_profile_history(profile_id,snapshot,changed_at,changed_by) VALUES(?,?,?,?)',
                             (profile_id, json.dumps(dict(profile)), stamp, editor))
                db().execute('UPDATE bf_supplier_site_profiles SET ' + ','.join(k + '=?' for k in KEYS) +
                             ',version=version+1,updated_at=?,updated_by=? WHERE id=?',
                             (*[values[k] for k in KEYS], stamp, editor, profile_id))
                db().commit()
                flash('Supplier site profile saved.', 'success')
                return redirect(register_url(category, external))
            except sqlite3.IntegrityError:
                error = 'This supplier site already exists in this category. Check the site name and address.'
            except ValueError as exc:
                error = str(exc)
            db().rollback()
        return supplier_page('supplier_profile', external, list_url=register_url(category, external),
                      category=category, site_label='Partner site' if external else site_label(category),
                      group_options=INDUSTRY_GROUPS if category=='industry-suppliers' else (), category_label=CATEGORIES[category],
                      profile=profile, values=values, creating=False, error=error), 400 if error else 200
