"""Replace allocation targets from a private workbook payload, preserving operations."""
import base64
import gzip
import json
import re
from collections import Counter

from binflow_client_profiles import key, matches, save_profile, validate
from binflow_requirements import converted_requirements

EQUIPMENT = (('bin660', '660L bins'), ('bin240', '240L bins'), ('cage', 'cages'),
             ('pallecon', 'pallecons'), ('bulkbag', 'bulk bags'))


def migrate(conn):
    conn.executescript('''
    CREATE TABLE IF NOT EXISTS bf_allocation_imports (
      id TEXT PRIMARY KEY, summary TEXT NOT NULL, previous_plan TEXT NOT NULL, created_at TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS bf_allocation_workbook_rows (
      profile_id INTEGER PRIMARY KEY REFERENCES bf_client_site_profiles(id), import_id TEXT NOT NULL,
      source_row INTEGER NOT NULL, source_label TEXT NOT NULL, equipment TEXT NOT NULL,
      source_snapshot TEXT NOT NULL, active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1)));
    ''')


def equipment_text(counts):
    return ', '.join(f'{counts[k]} × {label}' for k, label in EQUIPMENT if counts.get(k)) or 'No equipment quantity supplied'


def ensure_location(conn, profile, timestamp, actor):
    """Reuse exact identities; incomplete or ambiguous sites remain visible for review."""
    if profile['location_id']:
        return profile['location_id']
    if not profile['state'] or not profile['address']:
        return None
    candidates = matches(conn, profile)
    if len(candidates) > 1:
        return None
    if candidates:
        lid = candidates[0]['id']
        placeholder = conn.execute('SELECT * FROM bf_client_site_profiles WHERE location_id=?', (lid,)).fetchone()
        if placeholder:
            if placeholder['source_key'] or placeholder['version'] != 1:
                return None
            conn.execute('INSERT INTO bf_client_profile_history(profile_id,snapshot,changed_at,changed_by) VALUES(?,?,?,?)',
                         (profile['id'], json.dumps(dict(placeholder)), timestamp, actor))
            conn.execute('DELETE FROM bf_client_site_profiles WHERE id=?', (placeholder['id'],))
    else:
        group = profile['group_name']
        account = group if group and not re.fullmatch(r'tier\s*\d+', group, re.I) else profile['customer_site']
        clients = [r for r in conn.execute('SELECT id,name FROM bf_clients') if key(r['name']) == key(account)]
        if len(clients) > 1:
            return None
        cid = clients[0]['id'] if clients else conn.execute(
            'INSERT INTO bf_clients(name,created_at) VALUES(?,?)', (account, timestamp)).lastrowid
        name = profile['customer_site']
        if conn.execute("SELECT 1 FROM bf_locations WHERE kind='client' AND client_id=? AND name=?", (cid, name)).fetchone():
            name += ' · ' + profile['state']
        lid = conn.execute("INSERT INTO bf_locations(kind,client_id,name,state,address,created_at) VALUES('client',?,?,?,?,?)",
                           (cid, name, profile['state'], profile['address'], timestamp)).lastrowid
    save_profile(conn, dict(profile), timestamp, actor, profile, lid)
    return lid


def write_target(conn, lid, counts, source_row, timestamp, actor):
    quantities, review, _ = converted_requirements({'containers': counts})
    if counts.get('bulkbag'):
        review = (review + f' {counts["bulkbag"]} bulk bags listed; confirm whether these remain or need a bin replacement.').strip()
    sizes = {r['litres']: r for r in conn.execute("SELECT * FROM bf_sizes WHERE name IN ('240 L','1100 L')")}
    if set(sizes) != {240, 1100}:
        raise ValueError('Standard 240 L and 1100 L bin sizes are required.')
    old = conn.execute('SELECT version FROM bf_site_profiles WHERE location_id=?', (lid,)).fetchone()
    version = old['version'] + 1 if old else 1
    notes = (f'Workbook Equipment row {source_row}: {equipment_text(counts)}. '
             'Proposed targets: cages → 1100L one-for-one; 660L → 1100L by capacity, rounded up per site; '
             '240L retained. Pallecons and bulk bags require a separate decision.')
    conn.execute('''INSERT INTO bf_site_profiles(location_id,version,notes,updated_at,updated_by,review_note,active)
        VALUES(?,?,?,?,?,?,1) ON CONFLICT(location_id) DO UPDATE SET version=excluded.version,
        notes=excluded.notes,updated_at=excluded.updated_at,updated_by=excluded.updated_by,
        review_note=excluded.review_note,active=1''', (lid, version, notes, timestamp, actor, review))
    conn.execute('DELETE FROM bf_site_requirements WHERE location_id=?', (lid,))
    rows = [dict(size_id=sizes[litres]['id'], name=sizes[litres]['name'], quantity=qty) for litres, qty in quantities.items()]
    conn.executemany('INSERT INTO bf_site_requirements VALUES(?,?,?)', [(lid, r['size_id'], r['quantity']) for r in rows])
    conn.execute('INSERT INTO bf_profile_history(location_id,version,snapshot,created_at,actor) VALUES(?,?,?,?,?)',
                 (lid, version, json.dumps(dict(quantities=rows, notes=notes, review_note=review,
                                               workbook_row=source_row, source_counts=counts, active=True)), timestamp, actor))
    return quantities, review


def activate_pending(conn, profile_id, timestamp, actor):
    row = conn.execute('SELECT * FROM bf_allocation_workbook_rows WHERE profile_id=? AND active=1', (profile_id,)).fetchone()
    if not row:
        return
    profile = conn.execute('SELECT * FROM bf_client_site_profiles WHERE id=?', (profile_id,)).fetchone()
    lid = ensure_location(conn, profile, timestamp, actor)
    if lid and not conn.execute('SELECT 1 FROM bf_site_profiles WHERE location_id=? AND active=1', (lid,)).fetchone():
        write_target(conn, lid, json.loads(row['equipment']), row['source_row'], timestamp, actor)


def replace_from_workbook(conn, encoded, timestamp):
    payload = json.loads(gzip.decompress(base64.b64decode(encoded, validate=True)))
    import_id = payload.get('id')
    if not isinstance(import_id, str) or not re.fullmatch(r'[a-z0-9-]{1,100}', import_id):
        raise ValueError('Invalid allocation import ID.')
    done = conn.execute('SELECT summary FROM bf_allocation_imports WHERE id=?', (import_id,)).fetchone()
    if done:
        return json.loads(done['summary']), False
    raw_rows = payload.get('rows')
    if not isinstance(raw_rows, list) or not 1 <= len(raw_rows) <= 1000 or payload.get('expected_rows') != len(raw_rows):
        raise ValueError('Allocation workbook row count does not match.')
    rows, seen, source_totals = [], set(), Counter()
    for raw in raw_rows:
        details = validate(raw['profile'])
        number = raw['source_row']
        source_key = raw['profile_source_key']
        if type(number) is not int or number < 5 or number in seen or not isinstance(source_key, str) or not source_key:
            raise ValueError('Invalid or repeated workbook row.')
        seen.add(number)
        counts = raw['equipment']
        if set(counts) != {k for k, _ in EQUIPMENT} or any(type(n) is not int or not 0 <= n <= 10000 for n in counts.values()):
            raise ValueError('Equipment counts must be whole numbers from 0 to 10,000.')
        source_totals.update(counts)
        rows.append((raw, details))
    if dict(source_totals) != payload.get('expected_equipment'):
        raise ValueError('Allocation workbook equipment totals do not match.')
    conn.execute('BEGIN IMMEDIATE')
    try:
        done = conn.execute('SELECT summary FROM bf_allocation_imports WHERE id=?', (import_id,)).fetchone()
        if done:
            conn.rollback()
            return json.loads(done['summary']), False
        backup = {table: [dict(r) for r in conn.execute('SELECT * FROM '+table)] for table in
                  ('bf_site_profiles', 'bf_site_requirements', 'bf_allocation_workbook_rows')}
        who = 'Workbook allocation reset'
        # Advance versions so an editor opened before the reset cannot restore old targets.
        for old in backup['bf_site_profiles']:
            version = old['version'] + 1
            conn.execute('INSERT INTO bf_profile_history(location_id,version,snapshot,created_at,actor) VALUES(?,?,?,?,?)',
                         (old['location_id'], version, json.dumps(dict(active=False, quantities=[], reset_import=import_id)), timestamp, who))
        conn.execute('UPDATE bf_site_profiles SET active=0,version=version+1,updated_at=?,updated_by=?', (timestamp, who))
        conn.execute('DELETE FROM bf_site_requirements')
        conn.execute('UPDATE bf_allocation_workbook_rows SET active=0')
        summary = dict(sites=len(rows), linked=0, awaiting_location=0, review_sites=0,
                       archived_profiles=len(backup['bf_site_profiles']), equipment=dict(source_totals), states={})
        used_profiles, used_locations = set(), set()
        for raw, details in rows:
            profile = conn.execute('SELECT * FROM bf_client_site_profiles WHERE source_key=?', (raw['profile_source_key'],)).fetchone()
            if not profile:
                pid = save_profile(conn, details, timestamp, who, source_key=raw['profile_source_key'],
                                   source_label=payload.get('source_label', 'National changeover workbook'))
                profile = conn.execute('SELECT * FROM bf_client_site_profiles WHERE id=?', (pid,)).fetchone()
            pid = profile['id']
            if pid in used_profiles:
                raise ValueError('Two workbook rows map to the same client profile.')
            used_profiles.add(pid)
            lid = ensure_location(conn, profile, timestamp, who)
            if lid in used_locations:
                raise ValueError('Two workbook rows map to the same collection site.')
            conn.execute('''INSERT INTO bf_allocation_workbook_rows VALUES(?,?,?,?,?,?,1)
                ON CONFLICT(profile_id) DO UPDATE SET import_id=excluded.import_id,source_row=excluded.source_row,
                source_label=excluded.source_label,equipment=excluded.equipment,source_snapshot=excluded.source_snapshot,active=1''',
                         (pid, import_id, raw['source_row'], payload.get('source_label', 'Equipment workbook'),
                          json.dumps(raw['equipment']), json.dumps(raw)))
            if lid:
                used_locations.add(lid)
                quantities, review = write_target(conn, lid, raw['equipment'], raw['source_row'], timestamp, who)
                state = conn.execute('SELECT state FROM bf_locations WHERE id=?', (lid,)).fetchone()['state']
                target = summary['states'].setdefault(state, {'240 L': 0, '1100 L': 0})
                for litres, qty in quantities.items():
                    target[f'{litres} L'] += qty
                summary['linked'] += 1
                summary['review_sites'] += bool(review)
            else:
                summary['awaiting_location'] += 1
                summary['review_sites'] += 1
        conn.execute('INSERT INTO bf_allocation_imports VALUES(?,?,?,?)', (import_id, json.dumps(summary), json.dumps(backup), timestamp))
        conn.commit()
        return summary, True
    except Exception:
        conn.rollback()
        raise
