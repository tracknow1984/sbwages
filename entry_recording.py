"""Server-owned logging metadata, separate from the dates and times worked."""
from datetime import datetime, timezone
from zoneinfo import ZoneInfo


CONTENT_FIELDS = ('start_time', 'finish_time', 'units', 'lunch_units', 'activity')


def migrate_recording(conn):
    columns = {row['name'] for row in conn.execute('PRAGMA table_info(entries)')}
    for name, definition in (
        ('first_logged_at', 'TEXT'), ('first_logged_by', 'INTEGER REFERENCES users(id)'),
        ('last_updated_at', 'TEXT'), ('last_updated_by', 'INTEGER REFERENCES users(id)'),
    ):
        if name not in columns:
            conn.execute(f'ALTER TABLE entries ADD COLUMN {name} {definition}')
    # Historical rows deliberately remain unknown. A work/commit date is not a save date.


def recording_now():
    return datetime.now(timezone.utc).isoformat()


def stamp_entry(conn, user_id, work_date, before, actor_id):
    """Call inside the same transaction as a successful entry write."""
    after = conn.execute('SELECT * FROM entries WHERE user_id=? AND work_date=?',
                         (user_id, work_date)).fetchone()
    if before is None:
        conn.execute('UPDATE entries SET first_logged_at=?,first_logged_by=? WHERE user_id=? AND work_date=?',
                     (recording_now(), actor_id, user_id, work_date))
    elif any((before[key] or '') != (after[key] or '') for key in CONTENT_FIELDS):
        conn.execute('UPDATE entries SET last_updated_at=?,last_updated_by=? WHERE user_id=? AND work_date=?',
                     (recording_now(), actor_id, user_id, work_date))
    return conn.execute('SELECT * FROM entries WHERE user_id=? AND work_date=?',
                        (user_id, work_date)).fetchone()


def recording_datetime(value):
    if not value:
        return 'Not recorded'
    try:
        parsed = datetime.fromisoformat(str(value))
        if parsed.tzinfo is None:
            return 'Not recorded'
        return parsed.astimezone(ZoneInfo('Australia/Brisbane')).strftime('%d/%m/%Y · %H:%M:%S AEST')
    except ValueError:
        return 'Not recorded'


def recording_details(conn, entry):
    if entry is None:
        return []
    def actor(uid):
        person = conn.execute('SELECT first_name,last_name,role FROM users WHERE id=?', (uid,)).fetchone() if uid else None
        if not person:
            return ''
        name = f"{person['first_name']} {person['last_name']}".strip()
        return f"{name} · {'Admin' if person['role'] == 'admin' else 'Staff'}"
    lines = [{'label': 'First logged', 'at': entry['first_logged_at'],
              'text': recording_datetime(entry['first_logged_at']) if entry['first_logged_at'] else 'Not recorded for this older entry',
              'by': actor(entry['first_logged_by'])}]
    if entry['last_updated_at']:
        lines.append({'label': 'Last updated', 'at': entry['last_updated_at'],
                      'text': recording_datetime(entry['last_updated_at']), 'by': actor(entry['last_updated_by'])})
    return lines
