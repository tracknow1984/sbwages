"""Shared daily paid-hours rule: deduct up to 30 minutes once per worked day."""
from decimal import Decimal, ROUND_HALF_UP
import json


def deduct_lunch(gross_units):
    lunch_units = min(50, max(0, gross_units))
    return max(0, gross_units - lunch_units), lunch_units


def migrate_lunch(conn):
    if 'lunch_units' in {row['name'] for row in conn.execute('PRAGMA table_info(entries)')}:
        return
    conn.execute('ALTER TABLE entries ADD COLUMN lunch_units INTEGER NOT NULL DEFAULT 0')
    conn.execute('CREATE TABLE lunch_migration_backup(kind TEXT NOT NULL, record_key TEXT NOT NULL, before_json TEXT NOT NULL, PRIMARY KEY(kind,record_key))')
    for entry in conn.execute('SELECT * FROM entries').fetchall():
        conn.execute('INSERT INTO lunch_migration_backup VALUES(?,?,?)',
                     ('entry', f"{entry['user_id']}:{entry['work_date']}", json.dumps(dict(entry))))
        net, lunch = deduct_lunch(entry['units'])
        conn.execute('UPDATE entries SET units=?,lunch_units=? WHERE user_id=? AND work_date=?',
                     (net,lunch,entry['user_id'],entry['work_date']))
    for sheet in conn.execute("SELECT * FROM sheets WHERE status='submitted'").fetchall():
        conn.execute('INSERT INTO lunch_migration_backup VALUES(?,?,?)',
                     ('sheet', str(sheet['id']), json.dumps(dict(sheet))))
        units = conn.execute('SELECT COALESCE(SUM(units),0) FROM entries WHERE sheet_id=?',(sheet['id'],)).fetchone()[0]
        cents = int((Decimal(units) * sheet['rate_cents'] / 100).quantize(Decimal('1'), rounding=ROUND_HALF_UP))
        conn.execute('UPDATE sheets SET total_units=?,total_cents=? WHERE id=?',(units,cents,sheet['id']))
    # Payments are immutable snapshots of money already recorded, not new calculations.
