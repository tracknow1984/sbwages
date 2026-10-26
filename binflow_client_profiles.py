"""Site-specific client contacts and private, repeat-safe workbook imports."""
import base64
import gzip
import json
import re
import sqlite3
import unicodedata

from flask import abort, flash, g, redirect, request, url_for

FIELDS = (
    ('group_name', 'Group', 200), ('customer_site', 'Customer Site', 200),
    ('address', 'Address', 500), ('suburb', 'Suburb', 200), ('state', 'State', 3),
    ('postcode', 'Post Code', 4), ('contact_name', 'Contact Name', 500),
    ('contact_email', 'Contact Email', 1500), ('contact_number', 'Contact Number', 500),
    ('schedule', 'Schedule', 500), ('asic_required', 'ASIC Required', 7),
    ('contact_verified', 'Contact Verified?', 3),
)
KEYS = tuple(f[0] for f in FIELDS)
STATES = ('QLD', 'NSW', 'VIC', 'SA', 'WA', 'TAS', 'ACT', 'NT')
SCHEMA = '''
CREATE TABLE IF NOT EXISTS bf_client_site_profiles (
 id INTEGER PRIMARY KEY, location_id INTEGER UNIQUE REFERENCES bf_locations(id),
 group_name TEXT NOT NULL DEFAULT '', customer_site TEXT NOT NULL,
 address TEXT NOT NULL DEFAULT '', suburb TEXT NOT NULL DEFAULT '', state TEXT NOT NULL DEFAULT '',
 postcode TEXT NOT NULL DEFAULT '', contact_name TEXT NOT NULL DEFAULT '',
 contact_email TEXT NOT NULL DEFAULT '', contact_number TEXT NOT NULL DEFAULT '', schedule TEXT NOT NULL DEFAULT '',
 asic_required TEXT NOT NULL DEFAULT 'unknown' CHECK(asic_required IN ('unknown','yes','no')),
 contact_verified TEXT NOT NULL DEFAULT 'no' CHECK(contact_verified IN ('yes','no')),
 source_key TEXT UNIQUE, source_label TEXT NOT NULL DEFAULT '',
 version INTEGER NOT NULL DEFAULT 1, updated_at TEXT NOT NULL, updated_by TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS bf_client_profile_history (
 id INTEGER PRIMARY KEY, profile_id INTEGER NOT NULL REFERENCES bf_client_site_profiles(id),
 snapshot TEXT NOT NULL, changed_at TEXT NOT NULL, changed_by TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS bf_client_profile_imports (
 id TEXT PRIMARY KEY, summary TEXT NOT NULL, imported_at TEXT NOT NULL
);
'''


def migrate(conn):
    conn.executescript(SCHEMA)


def validate(values, site_label="Customer Site"):
    result = {}
    for key, label, maximum in FIELDS:
        if key == 'customer_site':
            label = site_label
        value = str(values.get(key, '') or '').strip()
        if len(value) > maximum or '\x00' in value:
            raise ValueError(f'{label} must be no more than {maximum} characters.')
        result[key] = value
    if not result['customer_site']:
        raise ValueError(f'{site_label} is required.')
    if result['state'] and result['state'] not in STATES:
        raise ValueError('Choose an Australian state or territory, or Not confirmed.')
    if result['postcode'] and not re.fullmatch(r'[0-9]{4}', result['postcode']):
        raise ValueError('Post Code must contain four digits, including any leading zero.')
    result['asic_required'] = result['asic_required'] or 'unknown'
    result['contact_verified'] = result['contact_verified'] or 'no'
    if result['asic_required'] not in ('unknown', 'yes', 'no'):
        raise ValueError('Choose Yes, No or Not confirmed for ASIC Required.')
    if result['contact_verified'] not in ('yes', 'no'):
        raise ValueError('Choose Yes or No for Contact Verified?.')
    # A site may have several names, emails or numbers. Preserve their pairing and separators.
    return result


def seed_locations(conn, timestamp):
    conn.execute('''INSERT INTO bf_client_site_profiles(location_id,customer_site,address,state,updated_at,updated_by)
        SELECT l.id,l.name,l.address,l.state,?,'Existing site'
        FROM bf_locations l WHERE l.kind='client' AND NOT EXISTS
        (SELECT 1 FROM bf_client_site_profiles p WHERE p.location_id=l.id)''', (timestamp,))


def save_profile(conn, values, timestamp, actor, profile=None, location_id=None, source_key=None, source_label=''):
    values = validate(values)
    if profile:
        conn.execute('INSERT INTO bf_client_profile_history(profile_id,snapshot,changed_at,changed_by) VALUES(?,?,?,?)',
                     (profile['id'], json.dumps(dict(profile)), timestamp, actor))
        conn.execute('UPDATE bf_client_site_profiles SET ' + ','.join(k+'=?' for k in KEYS) +
                     ',location_id=?,version=version+1,updated_at=?,updated_by=?,source_key=?,source_label=? WHERE id=?',
                     (*[values[k] for k in KEYS], location_id, timestamp, actor,
                      source_key or profile['source_key'], source_label or profile['source_label'], profile['id']))
        return profile['id']
    return conn.execute('INSERT INTO bf_client_site_profiles (' + ','.join(KEYS) +
                        ',location_id,updated_at,updated_by,source_key,source_label) VALUES ('+
                        ','.join('?' for _ in range(len(KEYS)+5))+')',
                        (*[values[k] for k in KEYS], location_id, timestamp, actor, source_key, source_label)).lastrowid


def key(value):
    value = re.sub(r'^blocktexx\s*[-–—:]\s*', '', value, flags=re.I)
    return ''.join(c for c in unicodedata.normalize('NFKC', value).casefold() if c.isalnum())


def matches(conn, row):
    """Exact site identity within a state; never match on a shared address alone."""
    if not row['state']:
        return []
    wanted = {key(row['customer_site'])}
    if row['group_name'] and not re.fullmatch(r'tier\s*\d+', row['group_name'], re.I):
        wanted.add(key(row['group_name'] + ' ' + row['customer_site']))
    result = []
    for loc in conn.execute("SELECT l.*,c.name AS client_name FROM bf_locations l JOIN bf_clients c ON c.id=l.client_id WHERE l.kind='client' AND l.state=?", (row['state'],)):
        names = {key(loc['name']), key(loc['client_name'] + ' ' + loc['name'])}
        for source in conn.execute('SELECT payload FROM bf_location_sources WHERE location_id=?', (loc['id'],)):
            names.add(key(json.loads(source['payload']).get('source', {}).get('name', '')))
        if wanted & names:
            result.append(loc)
    if len(result) > 1 and row['address']:
        exact = [l for l in result if key(l['address']) == key(row['address'])]
        if len(exact) == 1:
            return exact
    return result


def import_profiles(conn, encoded, timestamp):
    payload = json.loads(gzip.decompress(base64.b64decode(encoded, validate=True)))
    import_id = payload['id']
    if not isinstance(import_id, str) or not import_id or len(import_id) > 100:
        raise ValueError('Invalid client profile import ID.')
    rows = payload['rows']
    if not isinstance(rows, list) or not 1 <= len(rows) <= 1000:
        raise ValueError('Invalid client profile import rows.')
    conn.execute('BEGIN IMMEDIATE')
    try:
        done = conn.execute('SELECT summary FROM bf_client_profile_imports WHERE id=?', (import_id,)).fetchone()
        if done:
            conn.rollback()
            return json.loads(done['summary']), False
        seed_locations(conn, timestamp)
        summary = dict(profiles=len(rows), linked=0, awaiting_link=0, missing_address=0, missing_state=0,
                       unverified=0, asic_not_confirmed=0, review_rows=[])
        seen = set()
        for number, raw in enumerate(rows, 1):
            row = validate(raw)
            source_key = import_id + ':' + str(raw['source_row'])
            if source_key in seen:
                raise ValueError('Duplicate source row in client profile import.')
            seen.add(source_key)
            candidates = matches(conn, row)
            loc = candidates[0] if len(candidates) == 1 else None
            profile = conn.execute('SELECT * FROM bf_client_site_profiles WHERE location_id=?', (loc['id'],)).fetchone() if loc else None
            # Never overwrite an already imported or subsequently edited site profile.
            if profile and (profile['source_key'] or profile['version'] != 1):
                loc = profile = None
            if loc and conn.execute("SELECT 1 FROM bf_locations WHERE kind='client' AND client_id=? AND name=? AND id!=?",
                                    (loc['client_id'], row['customer_site'], loc['id'])).fetchone():
                loc = profile = None
            if loc:
                row['address'] = row['address'] or loc['address']
                conn.execute('UPDATE bf_locations SET name=?,address=? WHERE id=?',
                             (row['customer_site'], row['address'], loc['id']))
                summary['linked'] += 1
            else:
                summary['awaiting_link'] += 1
                summary['review_rows'].append(raw['source_row'])
            pid = save_profile(conn, row, timestamp, 'Workbook import', profile,
                               loc['id'] if loc else None, source_key, payload.get('source_label', 'Contacts workbook'))
            conn.execute('INSERT INTO bf_client_profile_history(profile_id,snapshot,changed_at,changed_by) VALUES(?,?,?,?)',
                         (pid, json.dumps({'workbook_row': raw}), timestamp, 'Workbook source'))
            summary['missing_address'] += not bool(raw.get('address'))
            summary['missing_state'] += not bool(raw.get('state'))
            summary['unverified'] += row['contact_verified'] == 'no'
            summary['asic_not_confirmed'] += row['asic_required'] == 'unknown'
        conn.execute('INSERT INTO bf_client_profile_imports VALUES(?,?,?)', (import_id, json.dumps(summary), timestamp))
        conn.commit()
        return summary, True
    except Exception:
        conn.rollback()
        raise


def for_location(conn, location_id):
    return conn.execute('SELECT * FROM bf_client_site_profiles WHERE location_id=?', (location_id,)).fetchone()


def sync_location(conn, location, timestamp, actor):
    """Keep the three shared location fields in sync after the existing location editor."""
    existing = for_location(conn, location['id'])
    values = dict(existing) if existing else {}
    values.update(customer_site=location['name'], state=location['state'], address=location['address'])
    save_profile(conn, values, timestamp, actor, existing, location['id'])


def register_rows(conn):
    """Keep retired allocation sites out of the current customer register."""
    return conn.execute('''SELECT p.*,c.name AS client_name,
        CASE WHEN sp.active=0 AND COALESCE(a.active,0)=0 THEN 1 ELSE 0 END AS archived
        FROM bf_client_site_profiles p
        LEFT JOIN bf_locations l ON l.id=p.location_id LEFT JOIN bf_clients c ON c.id=l.client_id
        LEFT JOIN bf_site_profiles sp ON sp.location_id=p.location_id
        LEFT JOIN bf_allocation_workbook_rows a ON a.profile_id=p.id
        ORDER BY p.group_name,p.customer_site,p.state,p.id''').fetchall()



def site_bins(conn, location_id=None):
    """Current custody and active outbound reservations; never infer from history."""
    where = ' AND p.location_id=?' if location_id is not None else ''
    return conn.execute("""
        SELECT p.id AS profile_id,b.id,b.code,s.name AS size_name,b.status,
            COALESCE(t.name,l.name,'In transit') AS location_name,
            CASE WHEN b.location_id=p.location_id THEN 'at_site' ELSE 'allocated' END AS relationship,
            j.id AS job_id,j.code AS job_code,j.scheduled_date,j.approval_status
        FROM bf_client_site_profiles p
        JOIN bf_bins b ON b.location_id=p.location_id OR b.id IN (
            SELECT i.bin_id FROM bf_job_bins i JOIN bf_jobs j ON j.id=i.job_id
            WHERE j.site_id=p.location_id AND i.direction='deliver' AND i.active=1
              AND j.status NOT IN ('cancelled','complete') AND j.approval_status IN ('pending','approved'))
        JOIN bf_sizes s ON s.id=b.size_id
        LEFT JOIN bf_locations l ON l.id=b.location_id
        LEFT JOIN bf_trucks t ON t.id=b.truck_id
        LEFT JOIN bf_job_bins i ON i.bin_id=b.id AND i.active=1 AND i.direction='deliver'
        LEFT JOIN bf_jobs j ON j.id=i.job_id AND j.site_id=p.location_id
            AND j.status NOT IN ('cancelled','complete') AND j.approval_status IN ('pending','approved')
        WHERE p.location_id IS NOT NULL""" + where + """
        ORDER BY relationship DESC,s.litres,b.code""",
        (location_id,) if location_id is not None else ()).fetchall()


def register_counts(conn):
    rows = register_rows(conn)
    return dict(total=len(rows),current=sum(not r['archived'] for r in rows),
                archived=sum(r['archived'] for r in rows),
                current_unlinked=sum(not r['archived'] and not r['location_id'] for r in rows),
                workbook=conn.execute('SELECT COUNT(*) FROM bf_allocation_workbook_rows WHERE active=1').fetchone()[0])


def filter_register(rows, args, endpoint, endpoint_args=None, supplier=False):
    """Shared search, filters, sorting and bounded pagination for site registers."""
    query = args.get('q', '').strip()[:200]
    filters = {name: args.get(name, '').strip()[:500]
               for name in ('group', 'state', 'schedule', 'asic', 'verified', 'status', 'sort')}
    if args.get('review') == '1' and not filters['status']:
        filters['status'] = 'review'
    options = {name: sorted({r[column] for r in rows if r[column]}, key=str.casefold)
               for name, column in (('group', 'group_name'), ('schedule', 'schedule'))}
    total, unlinked = len(rows), 0 if supplier else sum(not r['location_id'] for r in rows)
    if query:
        rows = [r for r in rows if query.casefold() in
                ' '.join(str(r[k] or '') for k in (KEYS if supplier else (*KEYS, 'client_name'))).casefold()]
    for name, column in (('group', 'group_name'), ('state', 'state'), ('schedule', 'schedule'),
                         ('asic', 'asic_required'), ('verified', 'contact_verified')):
        if filters[name]:
            wanted = '' if filters[name] == '__blank__' else filters[name]
            rows = [r for r in rows if r[column].casefold() == wanted.casefold()]
    status = filters['status']
    if status == 'linked' and not supplier:
        rows = [r for r in rows if r['location_id']]
    elif status == 'unlinked' and not supplier:
        rows = [r for r in rows if not r['location_id']]
    elif status == 'missing':
        rows = [r for r in rows if not all(r[k] for k in ('address','suburb','state','postcode'))]
    elif status == 'review':
        rows = [r for r in rows if (not supplier and not r['location_id']) or not r['state'] or
                not r['address'] or r['contact_verified'] == 'no']
    sort_key = {'group':'group_name', 'state':'state', 'suburb':'suburb'}.get(filters['sort'], 'customer_site')
    rows = sorted(rows, key=lambda r: (r[sort_key].casefold(), r['customer_site'].casefold(), r['id']))
    try:
        per_page = int(args.get('per_page', '20'))
        if per_page not in (10, 20, 50): per_page = 20
        page = max(1, int(args.get('page', '1')))
    except ValueError:
        per_page, page = 20, 1
    filtered = len(rows)
    pages = max(1, (filtered + per_page - 1) // per_page)
    page = min(page, pages)
    offset = (page - 1) * per_page
    page_args = {**(endpoint_args or {}), **filters, 'q':query, 'per_page':per_page}
    if not supplier:
        page_args['view'] = args.get('view', 'current')
    return dict(profiles=rows[offset:offset+per_page], query=query, filters=filters,
                options=options, total=total, unlinked=unlinked, filtered=filtered,
                page=page, pages=pages, per_page=per_page,
                first=offset+1 if filtered else 0, last=min(offset+per_page, filtered),
                previous_url=url_for(endpoint, page=page-1, **page_args) if page>1 else None,
                next_url=url_for(endpoint, page=page+1, **page_args) if page<pages else None)


def register_profiles(app, db, require, render, now, actor):
    @app.get('/binflow/client-profiles')
    @require('admin')
    def bf_client_profiles():
        seed_locations(db(), now())
        db().commit()
        rows = register_rows(db())
        bin_counts = {}
        for b in site_bins(db()):
            counts = bin_counts.setdefault(b['profile_id'], dict(at_site=0, allocated=0))
            counts[b['relationship']] += 1
        current_count = sum(not r['archived'] for r in rows)
        archived_count = sum(r['archived'] for r in rows)
        view = 'archived' if request.args.get('view')=='archived' else 'current'
        rows = [r for r in rows if bool(r['archived'])==(view=='archived')]
        from binflow_supplier_profiles import overview
        return render('client_profiles', section='profiles',
                      **filter_register(rows, request.args, 'bf_client_profiles'),
                      bin_counts=bin_counts, fields=FIELDS, view=view,
                      current_count=current_count, archived_count=archived_count,
                      supplier_registers=overview(db()))

    @app.route('/binflow/client-profiles/new', methods=['GET', 'POST'])
    @require('admin')
    def bf_new_client_profile():
        values = {k:'' for k in KEYS}
        values.update(asic_required='unknown', contact_verified='no', client_id='new', client_name='')
        error = None
        if request.method == 'POST':
            values.update({k:request.form.get(k, '').strip() for k in (*KEYS, 'client_id', 'client_name')})
            try:
                details = validate(values)
                if not details['state'] or not details['address']:
                    raise ValueError('Enter the site address and choose its state so it is ready for collections.')
                db().execute('BEGIN IMMEDIATE')
                if values['client_id'] in ('', 'new'):
                    name = values['client_name'] or details['customer_site']
                    if len(name) > 200 or '\x00' in name:
                        raise ValueError('Client / business name must be no more than 200 characters.')
                    if db().execute('SELECT 1 FROM bf_clients WHERE name=?', (name,)).fetchone():
                        raise ValueError('That client already exists. Select it under Client account to add another site.')
                    cid = db().execute('INSERT INTO bf_clients(name,created_at) VALUES(?,?)', (name,now())).lastrowid
                else:
                    try:
                        cid = int(values['client_id'])
                    except ValueError:
                        raise ValueError('Choose a valid client account.') from None
                    if not db().execute('SELECT 1 FROM bf_clients WHERE id=?', (cid,)).fetchone():
                        raise ValueError('Choose a valid client account.')
                lid = db().execute("INSERT INTO bf_locations(kind,client_id,name,state,address,created_at) VALUES('client',?,?,?,?,?)",
                                   (cid,details['customer_site'],details['state'],details['address'],now())).lastrowid
                save_profile(db(), details, now(), actor(), location_id=lid)
                db().commit()
                flash('Client site profile added. It is ready for collections.', 'success')
                return redirect(url_for('bf_client_profiles', q=details['customer_site']))
            except sqlite3.IntegrityError:
                error = 'This site already exists for that client. Open its profile to update the details.'
            except ValueError as exc:
                error = str(exc)
            db().rollback()
        clients = db().execute('SELECT id,name FROM bf_clients ORDER BY name').fetchall()
        return render('client_profile', section='profiles', profile=None, values=values, creating=True,
                      fields=FIELDS, clients=clients, locations=[], error=error), 400 if error else 200

    @app.route('/binflow/client-profiles/<int:profile_id>', methods=['GET', 'POST'])
    @require('admin')
    def bf_client_profile(profile_id):
        if request.method == 'POST':
            db().execute('BEGIN IMMEDIATE')
        profile = db().execute('SELECT * FROM bf_client_site_profiles WHERE id=?', (profile_id,)).fetchone()
        if not profile:
            abort(404)
        values, error = dict(profile), None
        if request.method == 'POST':
            values.update({k: request.form.get(k, '').strip() for k in KEYS})
            try:
                if str(profile['version']) != request.form.get('version'):
                    raise ValueError('This profile changed while you were editing. Reload it before saving.')
                values = {**values, **validate(values)}
                lid = profile['location_id']
                if not lid and request.form.get('location_id'):
                    lid = int(request.form['location_id'])
                    target = for_location(db(), lid)
                    if target and (target['source_key'] or target['version'] != 1):
                        raise ValueError('That collection site already has a maintained profile. Choose another site.')
                    # Only unedited placeholders may be absorbed; preserve their audit snapshot.
                    if target:
                        db().execute('INSERT INTO bf_client_profile_history(profile_id,snapshot,changed_at,changed_by) VALUES(?,?,?,?)',
                                     (profile_id, json.dumps(dict(target)), now(), actor()))
                        db().execute('DELETE FROM bf_client_site_profiles WHERE id=?', (target['id'],))
                if lid:
                    location = db().execute("SELECT * FROM bf_locations WHERE id=? AND kind='client'", (lid,)).fetchone()
                    if not location:
                        raise ValueError('Choose a valid collection site.')
                    if not values['state'] or not values['address']:
                        raise ValueError('A linked collection site needs its state and street address.')
                    if not profile['location_id'] and location['state'] != values['state']:
                        raise ValueError('The selected collection site must be in the same state as this profile.')
                    locked = db().execute('''SELECT EXISTS(SELECT 1 FROM bf_bins WHERE location_id=?)
                        OR EXISTS(SELECT 1 FROM bf_jobs WHERE site_id=? OR warehouse_id=?)''', (lid,lid,lid)).fetchone()[0]
                    if locked and values['state'] != location['state']:
                        raise ValueError('State is fixed once bins or jobs are linked. Add a new location for another state.')
                    db().execute('UPDATE bf_locations SET name=?,state=?,address=? WHERE id=?',
                                 (values['customer_site'], values['state'], values['address'], lid))
                save_profile(db(), values, now(), actor(), profile, lid)
                from binflow_allocation_import import activate_pending
                activate_pending(db(), profile_id, now(), actor())
                db().commit()
                flash('Client profile saved.', 'success')
                return redirect(url_for('bf_client_profiles'))
            except sqlite3.IntegrityError:
                error = 'A site with this name already exists for that client. Check the collection site link.'
            except (ValueError, TypeError) as exc:
                error = str(exc) or 'Check the profile details.'
            db().rollback()
        locations = db().execute('''SELECT l.*,c.name AS client_name FROM bf_locations l
            JOIN bf_clients c ON c.id=l.client_id LEFT JOIN bf_client_site_profiles p ON p.location_id=l.id
            WHERE l.kind='client' AND (p.id IS NULL OR (p.source_key IS NULL AND p.version=1))
            ORDER BY c.name,l.state,l.name''').fetchall()
        return render('client_profile', section='profiles', profile=profile, values=values,
                      attached_bins=site_bins(db(), profile['location_id']) if profile['location_id'] else [],
                      fields=FIELDS, locations=locations, error=error), 400 if error else 200
