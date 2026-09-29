"""Private pricing policy, stored separately from the operational model and exports."""
import json
import math
import os
from datetime import datetime, timezone
from flask import g, request


def percent(value, label, maximum=1000):
    if isinstance(value, bool):
        raise ValueError(label + ' must be a number.')
    try:
        n = float(value)
    except (TypeError, ValueError):
        raise ValueError(label + ' must be a number.') from None
    if not math.isfinite(n) or not 0 <= n <= maximum:
        raise ValueError(label + ' is outside the allowed range.')
    return n


def validate_policy(raw):
    if not isinstance(raw, dict):
        raise ValueError('Invalid private pricing policy.')
    data = {key: percent(raw.get(key), key) for key in ('administration_pct', 'profit_pct', 'minimum_pct', 'gst_pct')}
    if data['administration_pct'] + data['profit_pct'] + 1e-9 < data['minimum_pct']:
        raise ValueError('Administration plus profit must meet the combined minimum of ' + str(data['minimum_pct']) + '%.')
    return data


def register_pricing(app, db, require):
    with app.app_context():
        db().executescript('''
            CREATE TABLE IF NOT EXISTS blocktexx_private_pricing (
                id INTEGER PRIMARY KEY CHECK(id=1), revision INTEGER NOT NULL,
                data TEXT NOT NULL, updated_by INTEGER NOT NULL REFERENCES users(id), updated_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS blocktexx_private_pricing_history (
                revision INTEGER PRIMARY KEY, data TEXT NOT NULL,
                updated_by INTEGER NOT NULL REFERENCES users(id), updated_at TEXT NOT NULL);
        ''')

    def current():
        row = db().execute('SELECT * FROM blocktexx_private_pricing WHERE id=1').fetchone()
        try:
            raw = json.loads(row['data'] if row else os.environ.get('BLOCKTEXX_PRIVATE_PRICING_JSON', '{}'))
            return {**validate_policy(raw), 'configured': True, 'revision': row['revision'] if row else 0}
        except (ValueError, TypeError):
            return {'configured': False, 'revision': 0}

    @app.post('/admin/blocktexx/private-pricing')
    @require('admin')
    def save_blocktexx_private_pricing():
        try:
            revision = int(request.form.get('revision', '-1'))
            administration = percent(request.form.get('administration_pct'), 'Administration')
            profit = percent(request.form.get('profit_pct'), 'Profit')
        except ValueError as exc:
            return {'ok': False, 'error': str(exc)}, 400
        db().execute('BEGIN IMMEDIATE')
        previous = current()
        if not previous['configured']:
            db().rollback()
            return {'ok': False, 'error': 'Private pricing configuration is not available.'}, 400
        if revision != previous['revision']:
            db().rollback()
            return {'ok': False, 'error': 'Private pricing changed in another session. Reload before saving.'}, 409
        try:
            policy = validate_policy({**previous, 'administration_pct': administration, 'profit_pct': profit})
        except ValueError as exc:
            db().rollback()
            return {'ok': False, 'error': str(exc)}, 400
        now = datetime.now(timezone.utc).isoformat()
        payload = json.dumps(policy, allow_nan=False)
        db().execute('INSERT OR REPLACE INTO blocktexx_private_pricing VALUES(1,?,?,?,?)',
                     (revision+1, payload, g.user['id'], now))
        db().execute('INSERT INTO blocktexx_private_pricing_history VALUES(?,?,?,?)',
                     (revision+1, payload, g.user['id'], now))
        db().commit()
        return {'ok': True, 'pricing': {**policy, 'configured': True, 'revision': revision+1}, 'saved': now}
    return current
