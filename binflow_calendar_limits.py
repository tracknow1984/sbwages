"""Booking limits for a single state-day load; never assume another truck or trip."""
import math
from datetime import date

START = '2026-11-26'


def baling_day(day):
    return day >= START and date.fromisoformat(day).weekday() == 2


def ensure_collection_day(day):
    if baling_day(day):
        raise ValueError('Wednesday is reserved for mandatory Warehouse baling day in every state. Choose another collection date.')


def protect_baling(plan):
    for day in plan['days']:
        if not baling_day(day['date']):
            continue
        task = dict(id='mandatory-baling-'+day['date'], kind='baling',
                    title='Warehouse baling day · mandatory', address='',
                    detail='Wednesday reserved for warehouse baling. No collection bookings.', time='06:30', notes='')
        existing = next((i for i in day['items'] if i['kind']=='baling'), None)
        if existing:
            task['notes'] = existing.get('notes','')
        day['items'] = [task] + [i for i in day['items'] if i['kind']!='baling']
        day['baling_reserved'] = True
        day['booking_conflict'] = any(i['kind'] in ('job','visit') for i in day['items'])
        day.update(estimate_minutes=0 if day['booking_conflict'] else 480,
                   estimate_valid=not day['booking_conflict'], loads=[])
        if day['booking_conflict']:
            task['detail'] += ' Existing bookings need rescheduling.'
    return plan


def settings(conn, state):
    from binflow_collections import planning_resources
    truck = planning_resources(conn).get(state,{}).get('truck') or {}
    cap = truck.get('pallet_positions') or 14
    spaces = dict(bin240=.5, bin120=.5, bin660=1)
    spaces.update(truck.get('spaces') or {})
    factors = {1100:1, 240:spaces['bin240'], 120:spaces['bin120'], 660:spaces['bin660']}
    if not math.isfinite(cap) or cap <= 0:
        raise ValueError('Confirm a positive truck capacity in the state truck settings.')
    return cap, factors, not bool(truck.get('pallet_positions'))


def counts_space(conn, counts, factors):
    sizes = {r['id']:r['litres'] for r in conn.execute('SELECT id,litres FROM bf_sizes')}
    total = 0
    for sid, qty in counts.items():
        factor = factors.get(sizes.get(int(sid)))
        if qty and (factor is None or not math.isfinite(factor) or factor<=0):
            raise ValueError('Bin footprint is unconfirmed. Confirm this bin size before adding the booking.')
        total += qty * (factor or 0)
    return total


def item_space(conn, item, factors):
    if item['kind']=='baling':
        return 0
    if item['kind']=='visit':
        rows = conn.execute('SELECT size_id,quantity FROM bf_site_requirements WHERE location_id=? AND quantity>0', (item['site_id'],)).fetchall()
        if not rows:
            raise ValueError('Confirm bin quantities for '+item['title']+' before adding more bookings.')
        return counts_space(conn, {r['size_id']:r['quantity'] for r in rows}, factors)
    job = conn.execute('SELECT status,approval_status FROM bf_jobs WHERE id=?',(item['job_id'],)).fetchone()
    if not job or job['status']=='cancelled' or job['approval_status']=='rejected':
        return 0
    quantities = {'collect':{}, 'deliver':{}}
    for r in conn.execute('''SELECT i.direction,b.size_id,COUNT(*) qty FROM bf_job_bins i
        JOIN bf_bins b ON b.id=i.bin_id WHERE i.job_id=? AND i.active=1 GROUP BY i.direction,b.size_id''',(item['job_id'],)):
        quantities[r['direction']][r['size_id']] = r['qty']
    for r in conn.execute('''SELECT size_id,COUNT(*) qty FROM bf_rollout_slots
        WHERE job_id=? AND item_id IS NULL GROUP BY size_id''',(item['job_id'],)):
        quantities['deliver'][r['size_id']] = quantities['deliver'].get(r['size_id'],0)+r['qty']
    if not any(quantities.values()):
        raise ValueError('Confirm bin quantities for '+item['title']+' before adding more bookings.')
    # Reserve the larger side of each exchange; do not double-count replacement bins.
    return max(counts_space(conn,q,factors) for q in quantities.values())


def summary(conn, state, day, items):
    cap, factors, assumed = settings(conn,state)
    live = {r['id'] for r in conn.execute("SELECT id FROM bf_jobs WHERE status!='cancelled' AND approval_status!='rejected'")}
    job_sites = {i.get('site_id') for i in items if i['kind']=='job' and i.get('job_id') in live}
    used = sum(item_space(conn,i,factors) for i in items
               if not (i['kind']=='visit' and i.get('site_id') in job_sites))
    return dict(capacity=cap,used=used,remaining=max(0,cap-used),assumed=assumed,
                over=used>cap+1e-8,baling=baling_day(day))


def check(conn, state, day, items):
    work = [i for i in items if i['kind'] in ('job','visit')]
    if not work or day<START:
        return
    ensure_collection_day(day)
    value = summary(conn,state,day,items)
    if value['over']:
        raise ValueError(f"Truck overload — {state} {day}: {value['used']:g} of {value['capacity']:g} bin positions booked "
                         f"({value['used']-value['capacity']:g} over capacity). Choose another day or reduce this booking. Nothing was added.")


def check_changes(conn, state, old, plan):
    before = {d['date']:d for d in old['days']}
    for day in plan['days']:
        prior = before.get(day['date'],dict(items=[]))
        identity = lambda i: (i['kind'],i.get('job_id'),i.get('profile_id'))
        old_work = {identity(i) for i in prior['items'] if i['kind']!='baling'}
        # Existing conflicts remain editable/removable, but cannot gain more work.
        if any(identity(i) not in old_work for i in day['items'] if i['kind']!='baling'):
            check(conn,state,day['date'],day['items'])
    return protect_baling(plan)


def booking_items(conn, state, day):
    from binflow_schedule import get_plan, monday
    _, plan = get_plan(conn,state,monday(day))
    from binflow_schedule import job_item
    items=list(next(d['items'] for d in plan['days'] if d['date']==day))
    known={i.get('job_id') for i in items}
    for job in conn.execute("SELECT * FROM bf_jobs WHERE state=? AND scheduled_date=? AND schedule_week_id IS NULL AND schedule_live=1 AND status!='cancelled' AND approval_status!='rejected'",(state,day)):
        if job['id'] not in known:items.append(job_item(job))
    return items


def check_booking(conn, state, day, jid, baseline=None):
    if day<START:
        return
    from binflow_schedule import job_item
    items = booking_items(conn,state,day) if baseline is None else baseline
    if not any(i.get('job_id')==jid for i in items):
        items = items + [job_item(conn.execute('SELECT * FROM bf_jobs WHERE id=?',(jid,)).fetchone())]
    check(conn,state,day,items)


def preview(conn, state, day):
    if not state or day<START:
        return None
    items=booking_items(conn,state,day)
    try:
        return summary(conn,state,day,items)
    except ValueError as exc:
        return dict(error=str(exc),baling=baling_day(day))
