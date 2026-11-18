"""SBTX entry points for the shared transfer booking workflow."""
from flask import Blueprint, abort, g, redirect, request, url_for

from binflow_permissions import is_office


def is_sbtx():
    return request.blueprint in ('sbtx', 'sbtx_client')


def transfer_url(action, external, **values):
    if is_sbtx():
        prefix = 'sbtx_client.' if external else 'sbtx.'
        return url_for(prefix + ('new_job' if action == 'new' else action), **values)
    if action == 'calendar':
        endpoint = 'bf_client_interstate' if external else 'bf_interstate_calendar'
    else:
        endpoint = ('bf_client_interstate_' if external else 'bf_interstate_') + action
    return url_for(endpoint, **values)


def office_detail_url(conn, bid, **values):
    # Shared accounting screens link back to the calendar where the customer booked.
    row = conn.execute('SELECT portal FROM bf_interstate_bookings WHERE id=?', (bid,)).fetchone()
    endpoint = 'sbtx.detail' if row and row['portal'] == 'sbtx' else 'bf_interstate_detail'
    return url_for(endpoint, bid=bid, **values)


def migrate(conn, stamp):
    """Carry forward previously scheduled pickups and original PO bytes, without sending mail."""
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='sbtx_jobs'").fetchone():
        return 0, 0
    bookings = conn.execute("""INSERT INTO bf_interstate_bookings
        (portal,request_key,account_id,created_by,created_at,job_type,handling,origin,destination,
         pickup_date,delivery_date,client_name,pickup_address,pickup_contact,pickup_phone,
         delivery_address,delivery_contact,delivery_phone,email,goods,locked_at,locked_by,email_status,version)
        SELECT 'sbtx','sbtx-legacy:'||j.id,j.account_id,'SBTX calendar import',?,'internal','bulk','','',
               j.pickup_date,'',j.customer_name,'','',j.contact_number,'','','','',j.description,
               ?,'SBTX calendar import','imported',j.version
        FROM sbtx_jobs j WHERE NOT EXISTS (
            SELECT 1 FROM bf_interstate_bookings b WHERE b.request_key='sbtx-legacy:'||j.id)""", (stamp, stamp))
    orders = conn.execute("""INSERT INTO bf_interstate_purchase_orders
        (booking_id,number,filename,mime_type,document,revision,updated_at,updated_by)
        SELECT b.id,j.po_number,j.filename,j.mime_type,j.document,1,?,'SBTX calendar import'
        FROM sbtx_jobs j JOIN bf_interstate_bookings b ON b.request_key='sbtx-legacy:'||j.id AND b.portal='sbtx'
        WHERE (j.po_number!='' OR j.document IS NOT NULL) AND NOT EXISTS (
            SELECT 1 FROM bf_interstate_purchase_orders p WHERE p.booking_id=b.id)""", (stamp,))
    return bookings.rowcount, orders.rowcount


def blueprints(db, client_access):
    office = Blueprint('sbtx', __name__, url_prefix='/sbtx')
    client = Blueprint('sbtx_client', __name__, url_prefix='/sbtx/client')

    @office.before_request
    def office_access():
        if not g.user:
            return redirect(url_for('login', audience='admin', area='sbtx'))
        if not is_office(g.user):
            abort(403)

    def legacy_url(jid, action, external):
        row = db().execute("""SELECT id,account_id FROM bf_interstate_bookings
            WHERE portal='sbtx' AND request_key=?""", ('sbtx-legacy:' + str(jid),)).fetchone()
        if not row or external and row['account_id'] != g.bf_client['id']:
            abort(404)
        return redirect(transfer_url(action, external, bid=row['id']))

    @office.get('/jobs/<int:jid>')
    def job(jid):
        return legacy_url(jid, 'detail', False)

    @client.get('/jobs/<int:jid>', endpoint='job')
    @client_access
    def client_job(jid):
        return legacy_url(jid, 'detail', True)

    @office.get('/jobs/<int:jid>/purchase-order')
    def legacy_po(jid):
        return legacy_url(jid, 'po_file', False)

    @client.get('/jobs/<int:jid>/purchase-order', endpoint='legacy_po')
    @client_access
    def client_legacy_po(jid):
        return legacy_url(jid, 'po_file', True)

    return office, client
