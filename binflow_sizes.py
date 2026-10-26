"""Editable bin-size defaults and per-bin rental/purchase amounts."""
import json
import sqlite3
from decimal import Decimal, InvalidOperation

from flask import abort, flash, redirect, request, url_for

from binflow_tares import STANDARD_TARES


def migrate(conn):
    columns = {row['name'] for row in conn.execute('PRAGMA table_info(bf_sizes)')}
    for name in ('weekly_rental_cents', 'capital_purchase_cents'):
        if name not in columns:
            conn.execute(f'ALTER TABLE bf_sizes ADD COLUMN {name} INTEGER CHECK({name} IS NULL OR {name}>=0)')
    if 'version' not in columns:
        # Seed once: a renamed/resized standard bin must not reappear on restart.
        for litres, tare in STANDARD_TARES.items():
            conn.execute('''INSERT OR IGNORE INTO bf_sizes(name,litres,default_tare_g)
                SELECT ?,?,? WHERE NOT EXISTS(SELECT 1 FROM bf_sizes WHERE litres=?)''',
                         (f'{litres} L', litres, tare, litres))
        conn.execute('ALTER TABLE bf_sizes ADD COLUMN version INTEGER NOT NULL DEFAULT 1')
    conn.execute('''CREATE TABLE IF NOT EXISTS bf_size_history (
        id INTEGER PRIMARY KEY, size_id INTEGER NOT NULL REFERENCES bf_sizes(id),
        action TEXT NOT NULL, snapshot TEXT NOT NULL, updated_values TEXT NOT NULL,
        changed_at TEXT NOT NULL, changed_by TEXT NOT NULL)''')


def amount(raw, label, maximum, multiplier, optional=False):
    value = str(raw or '').strip()
    if not value and optional:
        return None
    try:
        number = Decimal(value)
        if not number.is_finite() or number < 0 or number > maximum:
            raise ValueError()
        scaled = number * multiplier
        if scaled != scaled.to_integral_value():
            raise ValueError()
        return int(scaled)
    except (InvalidOperation, ValueError):
        places = 3 if multiplier == 1000 else 2
        raise ValueError(f'{label} must be between 0 and {maximum:,}, with up to {places} decimal places.') from None


def validate(values):
    name = str(values.get('name', '')).strip()
    if not name or len(name) > 60 or '\x00' in name:
        raise ValueError('Size label is required and must be no more than 60 characters.')
    try:
        litres = int(values.get('litres', ''))
        if not 1 <= litres <= 50000:
            raise ValueError()
    except (ValueError, TypeError):
        raise ValueError('Capacity must be a whole number from 1 to 50,000 litres.') from None
    return dict(name=name, litres=litres,
                default_tare_g=amount(values.get('tare', '0'), 'Empty tare', 5000, 1000),
                weekly_rental_cents=amount(values.get('weekly_rental', ''), 'Weekly rental', 1000000, 100, optional=True),
                capital_purchase_cents=amount(values.get('capital_purchase', ''), 'Capital purchase amount', 1000000, 100, optional=True))


def save(conn, values, stamp, actor, existing=None):
    details = validate(values)
    if conn.execute('SELECT 1 FROM bf_sizes WHERE lower(name)=lower(?) AND id!=?',
                    (details['name'], existing['id'] if existing else 0)).fetchone():
        raise ValueError('A bin size with this label already exists. Use a different label.')
    if existing:
        conn.execute('UPDATE bf_sizes SET ' + ','.join(key + '=?' for key in details) +
                     ',version=version+1 WHERE id=?', (*details.values(), existing['id']))
        size_id = existing['id']
    else:
        size_id = conn.execute('INSERT INTO bf_sizes (' + ','.join(details) + ') VALUES (' +
                               ','.join('?' for _ in details) + ')', tuple(details.values())).lastrowid
    conn.execute('''INSERT INTO bf_size_history(size_id,action,snapshot,updated_values,changed_at,changed_by)
        VALUES(?,?,?,?,?,?)''', (size_id, 'edited' if existing else 'created',
                               json.dumps(dict(existing)) if existing else '{}', json.dumps(details), stamp, actor))
    return size_id


def form_values(size):
    return dict(name=size['name'], litres=size['litres'], tare=f"{size['default_tare_g']/1000:g}",
                weekly_rental='' if size['weekly_rental_cents'] is None else f"{size['weekly_rental_cents']/100:.2f}",
                capital_purchase='' if size['capital_purchase_cents'] is None else f"{size['capital_purchase_cents']/100:.2f}")


def register(app, db, require, render, now, actor):
    @app.route('/binflow/setup/bin-sizes/<int:size_id>/edit', methods=['GET', 'POST'])
    @require('admin')
    def bf_edit_size(size_id):
        if request.method == 'POST':
            db().execute('BEGIN IMMEDIATE')
        size = db().execute('SELECT * FROM bf_sizes WHERE id=?', (size_id,)).fetchone()
        if not size:
            abort(404)
        values, error = form_values(size), None
        if request.method == 'POST':
            values = {key: request.form.get(key, '').strip() for key in values}
            try:
                if request.form.get('version') != str(size['version']):
                    raise ValueError('This bin size changed while you were editing. Reload it before saving.')
                save(db(), values, now(), actor(), existing=size)
                db().commit()
                flash('Bin size and pricing saved.', 'success')
                return redirect(url_for('bf_setup', _anchor='bin-sizes'))
            except sqlite3.IntegrityError:
                error = 'A bin size with this label already exists. Use a different label.'
            except ValueError as exc:
                error = str(exc)
            db().rollback()
        return render('size_edit', section='setup', size=size, values=values, error=error), 400 if error else 200
