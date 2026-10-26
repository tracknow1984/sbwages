"""Current proposed/actual bin rental and one consolidated BlockTexx estimate."""
import csv
import io
from collections import Counter

from flask import abort, request, send_file


def price_counts(counts, sizes):
    known = 0
    unpriced = 0
    for size_id, quantity in counts.items():
        rate = sizes[size_id]['weekly_rental_cents']
        if rate is None:
            unpriced += quantity
        else:
            known += quantity * rate
    return known, unpriced


def summarise(sites, sizes):
    proposed, actual, billable = Counter(), Counter(), Counter()
    review = Counter()
    for site in sites:
        proposed.update(site['proposed'])
        actual.update(site['actual'])
        billable.update(site['billable'])
        review.update(site['review'])
    proposed_known, proposed_unpriced = price_counts(proposed, sizes)
    actual_known, actual_unpriced = price_counts(billable, sizes)
    missing_targets = sum(site['target_status'] == 'missing' for site in sites)
    proposed_complete = not proposed_unpriced and not missing_targets
    actual_complete = not actual_unpriced and not sum(review.values())
    proposed_cents = proposed_known if proposed_complete else None
    actual_cents = actual_known if actual_complete else None
    fortnight = actual_cents * 2 if actual_complete else None
    gst = (fortnight + 5) // 10 if fortnight is not None else None
    return dict(proposed=proposed, actual=actual, billable=billable, review=review,
                proposed_count=sum(proposed.values()), actual_count=sum(actual.values()),
                billable_count=sum(billable.values()), review_count=sum(review.values()),
                missing_targets=missing_targets, proposed_unpriced=proposed_unpriced, actual_unpriced=actual_unpriced,
                proposed_known_cents=proposed_known, actual_known_cents=actual_known,
                proposed_cents=proposed_cents, actual_cents=actual_cents,
                variance_cents=actual_cents-proposed_cents if proposed_complete and actual_complete else None,
                fortnight_cents=fortnight, gst_cents=gst,
                total_cents=fortnight+gst if fortnight is not None else None)


def report(conn):
    sizes = {r['id']: dict(r) for r in conn.execute('SELECT * FROM bf_sizes ORDER BY litres,id')}
    clients = {r['id']: dict(id=r['id'], name=r['name'], sites=[], pending=False)
               for r in conn.execute('SELECT id,name FROM bf_clients ORDER BY name')}
    sites = {}
    for row in conn.execute('''SELECT l.*,cp.id AS contact_profile_id,cp.customer_site,cp.group_name,cp.suburb,
        sp.active AS target_active,sp.review_note FROM bf_locations l
        LEFT JOIN bf_client_site_profiles cp ON cp.location_id=l.id
        LEFT JOIN bf_site_profiles sp ON sp.location_id=l.id WHERE l.kind='client' '''):
        site = dict(id=row['id'], name=row['customer_site'] or row['name'], state=row['state'],
                    address=row['address'], suburb=row['suburb'] or '', group=row['group_name'] or '',
                    contact_profile_id=row['contact_profile_id'], target_status='saved' if row['target_active'] else
                    'inactive' if row['target_active'] == 0 else 'missing',
                    target_review=row['review_note'] or '', proposed=Counter(), actual=Counter(),
                    billable=Counter(), review=Counter(), review_bins=[])
        sites[row['id']] = site
        clients[row['client_id']]['sites'].append(site)
    for row in conn.execute('''SELECT r.* FROM bf_site_requirements r
        JOIN bf_site_profiles p ON p.location_id=r.location_id WHERE p.active=1 AND r.quantity>0'''):
        if row['location_id'] in sites:
            sites[row['location_id']]['proposed'][row['size_id']] += row['quantity']
    # Current custody only: reservations and rotation pools do not create rent.
    for row in conn.execute('''SELECT b.* FROM bf_bins b JOIN bf_locations l ON l.id=b.location_id
        WHERE l.kind='client' AND b.truck_id IS NULL AND b.status IN ('at_client','full')'''):
        site = sites[row['location_id']]
        if row['service_status'] != 'lost':
            site['actual'][row['size_id']] += 1
        if row['service_status'] == 'in_service':
            site['billable'][row['size_id']] += 1
        else:
            site['review'][row['service_status']] += 1
            site['review_bins'].append(dict(id=row['id'], code=row['code'], status=row['service_status']))
    # Incomplete current customer profiles stay visible, without inventing quantities or accounts.
    from binflow_client_profiles import register_rows
    for row in register_rows(conn):
        if row['location_id'] or row['archived']:
            continue
        pending = dict(id=None, name=row['customer_site'], state=row['state'], address=row['address'],
                       suburb=row['suburb'], group=row['group_name'], contact_profile_id=row['id'],
                       target_status='missing', target_review='Complete the site link and proposed allocation.',
                       proposed=Counter(), actual=Counter(), billable=Counter(), review=Counter(), review_bins=[])
        clients['pending-'+str(row['id'])] = dict(id=None, name=row['customer_site'], sites=[pending], pending=True)
    all_sites = []
    for client in clients.values():
        # Retired targets with physical bins remain billable; empty retired locations add no proposal.
        client['sites'] = [s for s in client['sites'] if s['target_status'] != 'inactive' or s['actual'] or s['review']]
        client['sites'].sort(key=lambda s: (s['state'], s['name'].casefold()))
        for site in client['sites']:
            site['summary'] = summarise([site], sizes)
            used = set(site['proposed']) | set(site['actual']) | set(site['billable'])
            site['lines'] = [dict(size_id=sid, name=size['name'], rate=size['weekly_rental_cents'],
                                 proposed=site['proposed'][sid], actual=site['actual'][sid], billable=site['billable'][sid])
                             for sid, size in sizes.items() if sid in used]
        client['summary'] = summarise(client['sites'], sizes)
        client['states'] = sorted({s['state'] for s in client['sites'] if s['state']})
        client['search'] = ' '.join([client['name']] + [str(s[k]) for s in client['sites']
                                                     for k in ('name','address','state','suburb','group')]).casefold()
        all_sites.extend(client['sites'])
    ordered = sorted(clients.values(), key=lambda c: (c['name'].casefold(), c['pending']))
    missing_sizes = [size for sid, size in sizes.items() if size['weekly_rental_cents'] is None and
                     any(s['proposed'][sid] or s['billable'][sid] for s in all_sites)]
    return dict(clients=ordered, sizes=sizes, totals=summarise(all_sites, sizes),
                missing_sizes=missing_sizes, site_count=len(all_sites),
                pending_count=sum(c['pending'] for c in ordered),
                client_count=sum(not c['pending'] for c in ordered))


def money_text(cents):
    return '' if cents is None else f'{cents / 100:.2f}'


def safe(value):
    text = str(value or '')
    return "'" + text if text.lstrip().startswith(('=', '+', '-', '@')) else text


def register(app, db, require, render, now):
    @app.get('/binflow/rental-income')
    @require('admin')
    def bf_rental_income():
        data = report(db())
        selected = request.args.get('state', '').strip()
        if selected and selected not in ('QLD','NSW','VIC','SA','WA','TAS','ACT','NT'):
            abort(400, 'Choose an Australian state or territory.')
        query = request.args.get('q', '').strip()[:200]
        visible = []
        for client in data['clients']:
            if query and query.casefold() not in client['search']:
                continue
            if selected:
                scoped_sites = [site for site in client['sites'] if site['state'] == selected]
                if not scoped_sites:
                    continue
                client = dict(client, sites=scoped_sites, states=[selected], summary=summarise(scoped_sites, data['sizes']))
            visible.append(client)
        return render('rental_income', section='rental', **data, visible=visible,
                      query=query, selected=selected, as_of=now(),
                      filtered_totals=summarise([s for c in visible for s in c['sites']], data['sizes']))

    @app.get('/binflow/rental-income/export.csv')
    @require('admin')
    def bf_rental_income_export():
        data, stamp = report(db()), now()
        totals = data['totals']
        output = io.StringIO(newline='')
        writer = csv.writer(output)
        writer.writerow(['BlockTexx consolidated bin rental — current snapshot'])
        writer.writerow(['As at', stamp])
        writer.writerow(['Basis', 'AUD ex GST; current weekly amount x 2; no historical proration.'])
        writer.writerow(['Review required', 'Yes' if totals['actual_cents'] is None else 'No'])
        writer.writerow(['Proposed weekly ex GST', money_text(totals['proposed_cents'])])
        writer.writerow(['Known proposed weekly subtotal ex GST', money_text(totals['proposed_known_cents'])])
        writer.writerow(['Actual weekly ex GST', money_text(totals['actual_cents'])])
        writer.writerow(['Known actual weekly subtotal ex GST', money_text(totals['actual_known_cents'])])
        writer.writerow(['BlockTexx fortnight ex GST', money_text(totals['fortnight_cents'])])
        writer.writerow(['GST 10%', money_text(totals['gst_cents'])])
        writer.writerow(['BlockTexx fortnight inc GST', money_text(totals['total_cents'])])
        writer.writerow(['Sites without proposed targets', totals['missing_targets']])
        writer.writerow(['Proposed bins missing rates', totals['proposed_unpriced']])
        writer.writerow(['Actual billable bins missing rates', totals['actual_unpriced']])
        writer.writerow(['Damaged / lost bins awaiting review', totals['review_count']])
        writer.writerow([])
        writer.writerow(['Client / unlinked profile','States','Proposed bins','Proposed weekly ex GST',
                         'Actual on-site bins','Billable actual bins','Actual weekly ex GST','Actual fortnight ex GST',
                         'Missing proposed targets','Unpriced proposed bins','Unpriced actual bins','Bins for review'])
        for client in data['clients']:
            s = client['summary']
            writer.writerow([safe(client['name']), ', '.join(client['states']), s['proposed_count'], money_text(s['proposed_cents']),
                             s['actual_count'], s['billable_count'], money_text(s['actual_cents']), money_text(s['fortnight_cents']),
                             s['missing_targets'], s['proposed_unpriced'], s['actual_unpriced'], s['review_count']])
        writer.writerow([])
        writer.writerow(['Client / profile','Site','State','Bin size','Weekly rate ex GST','Proposed qty',
                         'Actual on-site qty','Billable actual qty','Proposed weekly ex GST','Actual weekly ex GST'])
        for client in data['clients']:
            for site in client['sites']:
                for line in site['lines']:
                    rate = line['rate']
                    writer.writerow([safe(client['name']), safe(site['name']), site['state'], safe(line['name']),
                                     money_text(rate), line['proposed'], line['actual'], line['billable'],
                                     money_text(rate*line['proposed'] if rate is not None else None),
                                     money_text(rate*line['billable'] if rate is not None else None)])
        return send_file(io.BytesIO(output.getvalue().encode('utf-8-sig')), mimetype='text/csv',
                         as_attachment=True, download_name=f'BlockTexx-bin-rental-{stamp[:10]}.csv', max_age=0)
