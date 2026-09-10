"""Explicit, manual timesheet reset. Never runs during app startup or deployment."""
import argparse
import os
from pathlib import Path
import sqlite3
from datetime import datetime, timezone


def clear_timesheets(path):
    path = Path(path).resolve(strict=True)
    conn = sqlite3.connect(path.as_uri() + '?mode=rw', uri=True, timeout=30)
    conn.execute('PRAGMA foreign_keys=ON')
    backup_path = path.with_name(path.name + '.before-timesheet-reset-' +
                                 datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ'))
    try:
        conn.execute('BEGIN IMMEDIATE')
        tables = ('payments', 'entries', 'entry_audit', 'sheets')
        counts = {table: conn.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0]
                  for table in tables}
        users = conn.execute('SELECT * FROM users ORDER BY id').fetchall()
        # The write lock prevents changes between this backup and the reset.
        fd = os.open(backup_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        os.close(fd)
        with sqlite3.connect(path.as_uri() + '?mode=ro', uri=True) as source:
            with sqlite3.connect(backup_path) as backup:
                source.backup(backup)
                if backup.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
                    raise RuntimeError('Backup integrity check failed; no records deleted.')
        for table in tables:
            conn.execute(f'DELETE FROM {table}')
        if users != conn.execute('SELECT * FROM users ORDER BY id').fetchall():
            raise RuntimeError('Employee records changed; rolling back.')
        if conn.execute('PRAGMA foreign_key_check').fetchone():
            raise RuntimeError('Foreign key validation failed; rolling back.')
        conn.commit()
        print('Deleted:', counts)
        print('Employee details and unrelated records retained.')
        print('Backup:', backup_path)
        return counts
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Back up and clear ALL timesheets, daily entries, timesheet audit history and linked payments/pay slips. Keep employee accounts and other modules.')
    parser.add_argument('--database', default=os.environ.get('DATABASE_PATH', '/var/data/sbwages.db'))
    parser.add_argument('--confirm-all-timesheets-and-payments', action='store_true', required=True)
    args = parser.parse_args()
    clear_timesheets(args.database)
