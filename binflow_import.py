"""One-time private register import; customer records never belong in source code."""
import json
import re
import unicodedata

IMPORT_ID = 'blocktexx-clients-2026-10-19-v1'
SCHEMA = '''
CREATE TABLE IF NOT EXISTS bf_import_runs (
 id TEXT PRIMARY KEY, source_revision INTEGER NOT NULL, source_updated_by INTEGER NOT NULL,
 source_updated_at TEXT NOT NULL, summary TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS bf_location_sources (
 source_id TEXT PRIMARY KEY, import_id TEXT NOT NULL REFERENCES bf_import_runs(id),
 location_id INTEGER NOT NULL REFERENCES bf_locations(id), kind TEXT NOT NULL,
 payload TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS bf_location_sources_location ON bf_location_sources(location_id);
'''


def clean_name(value):
    return re.sub(r'^blocktexx\s*[-–—:]\s*', '', value, flags=re.I).strip()


def normal(value):
    return ' '.join(unicodedata.normalize('NFKC', value).casefold().split())


def company_name(name, groups):
    # Only use the reviewed private account groups, not guesses from addresses.
    for group in sorted(groups, key=len, reverse=True):
        if re.match(re.escape(group) + r'(?:\s|[-–—]|$)', name, re.I):
            return group
    return name


def import_blocktexx(conn, groups, timestamp):
    """Import the saved model atomically, preserving all existing operational data."""
    if not isinstance(groups, list) or any(not isinstance(g, str) or not g.strip() or len(g) > 200 for g in groups):
        raise ValueError('Invalid private client account groups.')
    conn.execute('BEGIN IMMEDIATE')
    try:
        done = conn.execute('SELECT summary FROM bf_import_runs WHERE id=?', (IMPORT_ID,)).fetchone()
        if done:
            conn.rollback()
            return json.loads(done['summary']), False
        saved = conn.execute('SELECT * FROM blocktexx_model WHERE id=1').fetchone()
        if not saved:
            conn.rollback()
            return None, False
        model = json.loads(saved['data'])
        rows = [(kind, row) for key, kind in (('sites', 'collection'), ('partners', 'decomm')) for row in model.get(key, [])]
        if not rows:
            conn.rollback()
            return None, False
        if len(rows) > 1000:
            raise ValueError('Too many source locations.')
        summary = dict(source_records=len(rows), collections=0, partners=0, clients_added=0, locations_added=0,
                       locations_matched=0, address_review=0, states={})
        conn.execute('INSERT INTO bf_import_runs VALUES(?,?,?,?,?,?)',
                     (IMPORT_ID, saved['revision'], saved['updated_by'], saved['updated_at'], '{}', timestamp))
        clients = conn.execute('SELECT * FROM bf_clients ORDER BY id').fetchall()
        locations = conn.execute("SELECT * FROM bf_locations WHERE kind='client' ORDER BY id").fetchall()
        clients = [dict(r) for r in clients]
        locations = [dict(r) for r in locations]
        history = model.get('weight_history', {}).get('pickups', [])
        for kind, source in rows:
            sid = source.get('id', '')
            name = clean_name(source.get('name', ''))
            state = source.get('state', '')
            address = source.get('address', '').strip()
            if not sid or not name or len(name) > 200 or len(address) > 500 or state not in ('QLD','NSW','VIC','SA','WA','TAS','ACT','NT'):
                raise ValueError('A source location needs a valid ID, name, state and address length.')
            account = company_name(name, groups)
            client = next((c for c in clients if normal(clean_name(c['name'])) == normal(account)), None)
            if not client:
                cid = conn.execute('INSERT INTO bf_clients(name,created_at) VALUES(?,?)', (account, timestamp)).lastrowid
                client = dict(id=cid, name=account)
                clients.append(client)
                summary['clients_added'] += 1
            # Match exact identity, not address alone: separate departments may share a building.
            existing = next((l for l in locations if l['client_id'] == client['id'] and l['state'] == state
                             and normal(clean_name(l['name'])) == normal(name)
                             and normal(l['address']) == normal(address)), None)
            if existing:
                lid = existing['id']
                summary['locations_matched'] += 1
            else:
                location_name = name
                if any(l['client_id'] == client['id'] and normal(l['name']) == normal(location_name) for l in locations):
                    suffix = f' ({state} · {sid})'
                    location_name = name[:200-len(suffix)] + suffix
                lid = conn.execute("INSERT INTO bf_locations(kind,client_id,name,state,address,created_at) VALUES('client',?,?,?,?,?)",
                                   (client['id'], location_name, state, address, timestamp)).lastrowid
                locations.append(dict(id=lid,client_id=client['id'],name=location_name,state=state,address=address))
                summary['locations_added'] += 1
            review = not address or bool(re.search(r'missing|unconfirmed|\btbc\b|\btbd\b', address, re.I))
            payload = dict(source=source, address_review=review,
                           historical_pickups=[p for p in history if p.get('site_id') == sid])
            conn.execute('INSERT INTO bf_location_sources VALUES(?,?,?,?,?)',
                         (kind + ':' + sid, IMPORT_ID, lid, kind, json.dumps(payload, allow_nan=False)))
            summary['collections' if kind == 'collection' else 'partners'] += 1
            summary['address_review'] += int(review)
            summary['states'][state] = summary['states'].get(state, 0) + 1
        summary['client_accounts'] = conn.execute('''SELECT COUNT(DISTINCT l.client_id) FROM bf_location_sources s
            JOIN bf_locations l ON l.id=s.location_id WHERE s.import_id=?''', (IMPORT_ID,)).fetchone()[0]
        summary['locations'] = conn.execute('SELECT COUNT(DISTINCT location_id) FROM bf_location_sources WHERE import_id=?', (IMPORT_ID,)).fetchone()[0]
        conn.execute('UPDATE bf_import_runs SET summary=? WHERE id=?', (json.dumps(summary), IMPORT_ID))
        conn.commit()
        return summary, True
    except Exception:
        conn.rollback()
        raise


def profiles(conn, location_id):
    labels = {'bin120':'120 L bins','bin240':'240 L bins','bin660':'660 L bins','bin1100':'1,100 L bins','cage':'Cages','pallecon':'Pallecons'}
    result = []
    for row in conn.execute('SELECT s.*,r.source_revision,r.created_at FROM bf_location_sources s JOIN bf_import_runs r ON r.id=s.import_id WHERE s.location_id=? ORDER BY s.source_id', (location_id,)):
        data = json.loads(row['payload'])
        source = data['source']
        weights = [p['kg'] for p in data.get('historical_pickups', []) if isinstance(p.get('kg'), (int,float))]
        result.append(dict(source=source, kind=row['kind'], revision=row['source_revision'], imported_at=row['created_at'],
                           address_review=data['address_review'],
                           containers=[dict(label=labels.get(k,k),quantity=v) for k,v in source.get('containers',{}).items() if v],
                           days=', '.join(('Monday','Tuesday','Wednesday','Thursday','Friday','Saturday','Sunday')[d]
                                          for d in source.get('service_days',[]) if isinstance(d,int) and 0 <= d < 7),
                           average_kg=sum(weights)/len(weights) if weights else None, sample_count=len(weights)))
    return result
