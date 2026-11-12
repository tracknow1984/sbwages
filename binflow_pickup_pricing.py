"""National pickup pricing in ten-thousandths of an AUD per net kilogram."""
from flask import request

from binflow_sizes import amount


def migrate(conn):
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS bf_pickup_rates (
            id INTEGER PRIMARY KEY, units INTEGER NOT NULL CHECK(units >= 0),
            saved_at TEXT NOT NULL, saved_by TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS bf_job_pickup_rates (
            job_id INTEGER PRIMARY KEY REFERENCES bf_jobs(id) ON DELETE CASCADE,
            rate_id INTEGER NOT NULL REFERENCES bf_pickup_rates(id));
        CREATE TRIGGER IF NOT EXISTS bf_freeze_pickup_rate AFTER UPDATE OF collected_at ON bf_jobs
        WHEN OLD.collected_at IS NULL AND NEW.collected_at IS NOT NULL
        BEGIN
            INSERT OR IGNORE INTO bf_job_pickup_rates(job_id,rate_id)
                SELECT NEW.id,id FROM bf_pickup_rates ORDER BY id DESC LIMIT 1;
        END;
    """)


def rate_text(units):
    if units is None:
        return ''
    whole, fraction = divmod(units, 10000)
    digits = f'{fraction:04d}'.rstrip('0').ljust(2, '0')
    return f'{whole}.{digits}'


def pickup_cents(job):
    if not job['pod_signed_at'] or not job['collection_bins'] or job['rate_units'] is None or job['net'] is None:
        return None
    return (job['net'] * job['rate_units'] + 50_000) // 100_000


def register(app, db, require, render, success, failure, now, actor):
    app.jinja_env.filters['pickup_rate'] = rate_text
    app.jinja_env.globals['pickup_cents'] = pickup_cents

    @app.route('/binflow/setup/pickup-pricing', methods=['GET','POST'])
    @require('admin')
    def bf_pickup_pricing():
        if request.method == 'POST':
            try:
                units = amount(request.form.get('rate'), 'Pickup price per kg', 1000000, 10000)
                db().execute('BEGIN IMMEDIATE')
                latest = db().execute('SELECT * FROM bf_pickup_rates ORDER BY id DESC LIMIT 1').fetchone()
                if request.form.get('version',type=int) != (latest['id'] if latest else 0):
                    raise ValueError('The pickup price changed. Check the current rate before saving.')
                rate_id = latest['id'] if latest and latest['units']==units else db().execute(
                    'INSERT INTO bf_pickup_rates(units,saved_at,saved_by) VALUES(?,?,?)',(units,now(),actor())).lastrowid
                if request.form.get('price_historic')=='yes':
                    db().execute("""INSERT OR IGNORE INTO bf_job_pickup_rates(job_id,rate_id)
                        SELECT j.id,? FROM bf_jobs j WHERE j.pod_signed_at IS NOT NULL AND j.pod_deleted_at IS NULL
                        AND j.status='complete' AND j.approval_status='approved'
                        AND EXISTS(SELECT 1 FROM bf_job_bins i WHERE i.job_id=j.id AND i.direction='collect')
                        AND NOT EXISTS(SELECT 1 FROM bf_auto_claims c WHERE c.source='pod:'||j.id)""",(rate_id,))
                if latest is None:
                    db().execute("""INSERT OR IGNORE INTO bf_job_pickup_rates(job_id,rate_id)
                        SELECT j.id,? FROM bf_jobs j WHERE j.collected_at IS NOT NULL
                        AND j.pod_signed_at IS NULL AND j.pod_deleted_at IS NULL
                        AND j.approval_status='approved' AND j.status IN ('collected','received','complete')
                        AND EXISTS (SELECT 1 FROM bf_job_bins i WHERE i.job_id=j.id AND i.direction='collect')""",(rate_id,))
                return success('Business-wide pickup price saved.', 'bf_pickup_pricing')
            except ValueError as error:
                return failure(error,'bf_pickup_pricing')
        rates = db().execute('SELECT * FROM bf_pickup_rates ORDER BY id DESC').fetchall()
        return render('pickup_pricing',section='pickup_pricing',rates=rates)
