"""Apply the approved empty-bin weights once, preserving recorded weigh-ins."""
import json

STANDARD_TARES = {240: 13000, 660: 42000, 1100: 42000}
UPDATE_ID = 'standard-empty-bin-weights-2026-10-19-v1'


def apply_standard_tares(conn, stamp):
    conn.execute('''CREATE TABLE IF NOT EXISTS bf_tare_updates (
        id TEXT PRIMARY KEY, applied_at TEXT NOT NULL, summary TEXT NOT NULL)''')
    conn.commit()
    conn.execute('BEGIN IMMEDIATE')
    try:
        if conn.execute('SELECT 1 FROM bf_tare_updates WHERE id=?', (UPDATE_ID,)).fetchone():
            conn.rollback()
            return None
        summary = {'defaults': [], 'bins_updated': 0, 'pending_items_updated': 0}
        for litres, tare in STANDARD_TARES.items():
            sizes = conn.execute('SELECT id,default_tare_g FROM bf_sizes WHERE litres=?', (litres,)).fetchall()
            for size in sizes:
                summary['defaults'].append(dict(size_id=size['id'], litres=litres,
                    old_tare_g=size['default_tare_g'], new_tare_g=tare))
                conn.execute('UPDATE bf_sizes SET default_tare_g=? WHERE id=?', (tare, size['id']))
                bins = conn.execute('SELECT id,tare_g FROM bf_bins WHERE size_id=? AND tare_g!=?', (size['id'], tare)).fetchall()
                for b in bins:
                    conn.execute('UPDATE bf_bins SET tare_g=? WHERE id=?', (tare, b['id']))
                    conn.execute('''INSERT INTO bf_events(bin_id,event,actor,notes,created_at) VALUES(?,?,?,?,?)''',
                        (b['id'], 'Standard empty tare updated', 'System · approved bin weights',
                         f"{litres} L: {b['tare_g']/1000:g} kg → {tare/1000:g} kg. Recorded weigh-ins retained.", stamp))
                summary['bins_updated'] += len(bins)
                # Completed, cancelled and already-weighed lines are historical records.
                pending = conn.execute('''SELECT i.id,i.bin_id,i.job_id,i.tare_g FROM bf_job_bins i
                    JOIN bf_bins b ON b.id=i.bin_id JOIN bf_jobs j ON j.id=i.job_id
                    WHERE b.size_id=? AND i.tare_g!=? AND i.active=1
                    AND i.weighed_at IS NULL AND i.gross_g IS NULL AND i.net_g IS NULL
                    AND j.status IN ('scheduled','collected','received')''', (size['id'], tare)).fetchall()
                for item in pending:
                    conn.execute('UPDATE bf_job_bins SET tare_g=? WHERE id=?', (tare, item['id']))
                    conn.execute('''INSERT INTO bf_events(bin_id,job_id,event,actor,notes,created_at) VALUES(?,?,?,?,?,?)''',
                        (item['bin_id'], item['job_id'], 'Pending docket tare updated', 'System · approved bin weights',
                         f"{item['tare_g']/1000:g} kg → {tare/1000:g} kg; awaiting weighing.", stamp))
                summary['pending_items_updated'] += len(pending)
        conn.execute('INSERT INTO bf_tare_updates VALUES(?,?,?)', (UPDATE_ID, stamp, json.dumps(summary)))
        conn.commit()
        return summary
    except Exception:
        conn.rollback()
        raise
