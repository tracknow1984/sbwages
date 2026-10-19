"""Server-side Wialon Hosting connection and observed circular geofences."""
import base64
import hashlib
import json
import math
import threading
import time
from datetime import datetime
from urllib.error import URLError
from urllib.parse import urlencode
from urllib.request import Request, build_opener, HTTPRedirectHandler
from zoneinfo import ZoneInfo

from cryptography.fernet import Fernet, InvalidToken
from flask import abort, g, jsonify, render_template, request
from binflow_tracking import tracking_snapshot, STATES

HOSTS = ('https://hst-api.wialon.com','https://hst-api.wialon.us','https://hst-api.wialon.eu','https://hst-api.wialon.org')
FRESH_SECONDS = 300


def migrate_gps(conn):
    conn.executescript('''
    CREATE TABLE IF NOT EXISTS bf_gps_config (
        id INTEGER PRIMARY KEY CHECK(id=1),host TEXT NOT NULL,token_cipher BLOB NOT NULL,
        updated_at TEXT NOT NULL,updated_by TEXT NOT NULL,last_poll INTEGER NOT NULL DEFAULT 0,
        last_success INTEGER,last_error TEXT NOT NULL DEFAULT '');
    CREATE TABLE IF NOT EXISTS bf_gps_units (
        unit_id INTEGER PRIMARY KEY,name TEXT NOT NULL,latitude REAL,longitude REAL,
        position_time INTEGER,speed REAL,available INTEGER NOT NULL DEFAULT 1);
    CREATE TABLE IF NOT EXISTS bf_truck_gps (
        truck_id INTEGER PRIMARY KEY REFERENCES bf_trucks(id),unit_id INTEGER NOT NULL UNIQUE REFERENCES bf_gps_units(unit_id));
    CREATE TABLE IF NOT EXISTS bf_geofences (
        location_id INTEGER PRIMARY KEY REFERENCES bf_locations(id),latitude REAL NOT NULL,
        longitude REAL NOT NULL,radius_m INTEGER NOT NULL,updated_at TEXT NOT NULL,updated_by TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS bf_geofence_observations (
        truck_id INTEGER NOT NULL REFERENCES bf_trucks(id),location_id INTEGER NOT NULL REFERENCES bf_locations(id),
        inside INTEGER NOT NULL,position_time INTEGER NOT NULL,PRIMARY KEY(truck_id,location_id));
    CREATE TABLE IF NOT EXISTS bf_geofence_events (
        id INTEGER PRIMARY KEY,truck_id INTEGER NOT NULL,location_id INTEGER NOT NULL,
        inside INTEGER NOT NULL,position_time INTEGER NOT NULL,observed_at TEXT NOT NULL,
        UNIQUE(truck_id,location_id,position_time));
    ''')


def cipher(secret):
    key=hashlib.sha256(b'binflow:wialon:token:v1:'+str(secret).encode()).digest()
    return Fernet(base64.urlsafe_b64encode(key))


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def api_call(host, service, params, sid=None):
    if host not in HOSTS:
        raise ValueError('Choose a supported Wialon Hosting region.')
    values=dict(svc=service,params=json.dumps(params))
    if sid: values['sid']=sid
    req=Request(host+'/wialon/ajax.html',data=urlencode(values).encode(),method='POST',
                headers={'Content-Type':'application/x-www-form-urlencoded','Accept':'application/json'})
    try:
        with build_opener(NoRedirect).open(req,timeout=8) as response:
            body=response.read(4*1024*1024+1)
        if len(body)>4*1024*1024: raise ValueError()
        data=json.loads(body)
    except (URLError,TimeoutError,OSError,ValueError):
        raise ValueError('Wialon could not be reached. Check the region and try again.') from None
    if not isinstance(data,dict):
        raise ValueError('Wialon returned an unexpected response.')
    if data.get('error'):
        raise ValueError('Wialon rejected the request. Check the token, its expiry and permission to view unit locations.')
    return data


def fetch_units(host, token):
    sid=api_call(host,'token/login',dict(token=token,fl=1)).get('eid')
    if not sid: raise ValueError('Wialon did not open a session. Check the token.')
    try:
        data=api_call(host,'core/search_items',dict(spec=dict(itemsType='avl_unit',propName='sys_name',
            propValueMask='*',sortType='sys_name',propType='property'),force=1,flags=1025,**{'from':0,'to':0}),sid)
        units=data.get('items')
        if not isinstance(units,list): raise ValueError('Wialon did not return a unit list.')
        result=[]
        for unit in units:
            if not isinstance(unit,dict): continue
            uid=unit.get('id')
            if type(uid) is not int or uid<=0: continue
            pos=unit.get('pos') or {}
            if not isinstance(pos,dict): pos={}
            lat,lon,stamp=pos.get('y'),pos.get('x'),pos.get('t')
            valid=(type(lat) in (int,float) and type(lon) in (int,float) and math.isfinite(lat) and math.isfinite(lon)
                   and -90<=lat<=90 and -180<=lon<=180 and type(stamp) is int and 0<stamp<=time.time()+60)
            speed=pos.get('s')
            speed=speed if type(speed) in (int,float) and math.isfinite(speed) and speed>=0 else None
            result.append(dict(unit_id=uid,name=str(unit.get('nm',uid))[:200],latitude=lat if valid else None,
                               longitude=lon if valid else None,position_time=stamp if valid else None,speed=speed if valid else None))
        return result
    finally:
        try: api_call(host,'core/logout',{},sid)
        except ValueError: pass


def distance_m(lat1,lon1,lat2,lon2):
    a,b=math.radians(lat1),math.radians(lat2)
    x=math.sin((b-a)/2)**2+math.cos(a)*math.cos(b)*math.sin(math.radians(lon2-lon1)/2)**2
    return 6371000*2*math.asin(min(1,math.sqrt(x)))


def store_units(conn, units, stamp):
    conn.execute('UPDATE bf_gps_units SET available=0')
    for unit in units:
        conn.execute('''INSERT INTO bf_gps_units(unit_id,name,latitude,longitude,position_time,speed,available)
            VALUES(:unit_id,:name,:latitude,:longitude,:position_time,:speed,1)
            ON CONFLICT(unit_id) DO UPDATE SET name=excluded.name,latitude=excluded.latitude,longitude=excluded.longitude,
            position_time=excluded.position_time,speed=excluded.speed,available=1''',unit)
    fences=conn.execute('SELECT * FROM bf_geofences').fetchall()
    for truck in conn.execute('SELECT m.truck_id,u.* FROM bf_truck_gps m JOIN bf_gps_units u ON u.unit_id=m.unit_id WHERE u.available=1'):
        if truck['latitude'] is None or not truck['position_time'] or not -60<=time.time()-truck['position_time']<=FRESH_SECONDS:
            continue
        for fence in fences:
            inside=int(distance_m(truck['latitude'],truck['longitude'],fence['latitude'],fence['longitude'])<=fence['radius_m'])
            old=conn.execute('SELECT * FROM bf_geofence_observations WHERE truck_id=? AND location_id=?',(truck['truck_id'],fence['location_id'])).fetchone()
            if old and old['position_time']>=truck['position_time']: continue
            if not old or old['inside']!=inside:
                conn.execute('INSERT OR IGNORE INTO bf_geofence_events(truck_id,location_id,inside,position_time,observed_at) VALUES(?,?,?,?,?)',
                             (truck['truck_id'],fence['location_id'],inside,truck['position_time'],stamp))
            conn.execute('''INSERT INTO bf_geofence_observations(truck_id,location_id,inside,position_time) VALUES(?,?,?,?)
                ON CONFLICT(truck_id,location_id) DO UPDATE SET inside=excluded.inside,position_time=excluded.position_time''',
                (truck['truck_id'],fence['location_id'],inside,truck['position_time']))


def register_gps(app, db, access, require, render, success, failure, now, actor):
    lock=threading.Lock()

    def refresh():
        # Bounded, cross-worker lease. Polling follows an open operations screen; no new paid worker required.
        if not lock.acquire(blocking=False): return
        try:
            config=db().execute('SELECT * FROM bf_gps_config WHERE id=1').fetchone()
            if not config or time.time()-config['last_poll']<30: return
            epoch=int(time.time())
            cursor=db().execute('UPDATE bf_gps_config SET last_poll=? WHERE id=1 AND last_poll<=?',(epoch,epoch-30))
            db().commit()
            if not cursor.rowcount: return
            try:
                token=cipher(app.config['SECRET_KEY']).decrypt(config['token_cipher']).decode()
                units=fetch_units(config['host'],token)
                db().execute('BEGIN IMMEDIATE')
                # A simultaneous disconnect or token replacement must not reinstate old account locations.
                current=db().execute('SELECT token_cipher FROM bf_gps_config WHERE id=1').fetchone()
                if not current or current['token_cipher']!=config['token_cipher']:
                    db().rollback();return
                store_units(db(),units,now())
                db().execute("UPDATE bf_gps_config SET last_success=?,last_error='' WHERE id=1",(epoch,))
                db().commit()
            except (ValueError,InvalidToken):
                db().rollback()
                db().execute('UPDATE bf_gps_config SET last_error=? WHERE id=1 AND token_cipher=?',
                    ('Unable to refresh Wialon. Check the connection in GPS & Wialon settings.',config['token_cipher']))
                db().commit()
        finally: lock.release()

    @app.route('/binflow/settings/gps',methods=['GET','POST'])
    @require('admin')
    def bf_gps_settings():
        if request.method=='POST':
            try:
                action=request.form.get('action')
                if action=='connect':
                    host=request.form.get('host','')
                    token=request.form.get('token','').strip()
                    if host not in HOSTS or len(token)!=72 or not token.isascii() or not token.isalnum():
                        raise ValueError('Choose your Wialon Hosting region and enter the 72-character API token.')
                    units=fetch_units(host,token)
                    encrypted=cipher(app.config['SECRET_KEY']).encrypt(token.encode())
                    db().execute('BEGIN IMMEDIATE')
                    # Re-link explicitly when credentials change; a unit ID alone does not establish account ownership.
                    db().execute('DELETE FROM bf_truck_gps')
                    db().execute('DELETE FROM bf_geofence_observations')
                    db().execute('DELETE FROM bf_gps_units')
                    epoch=int(time.time())
                    db().execute('''INSERT INTO bf_gps_config(id,host,token_cipher,updated_at,updated_by,last_poll,last_success)
                        VALUES(1,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET host=excluded.host,token_cipher=excluded.token_cipher,
                        updated_at=excluded.updated_at,updated_by=excluded.updated_by,last_poll=excluded.last_poll,
                        last_success=excluded.last_success,last_error='' ''',(host,encrypted,now(),actor(),epoch,epoch))
                    store_units(db(),units,now())
                    return success(f'Wialon connection tested. {len(units)} units available. Link each truck below.','bf_gps_settings')
                if action=='disconnect':
                    db().execute('BEGIN IMMEDIATE')
                    for table in ('bf_truck_gps','bf_geofence_observations','bf_gps_units','bf_gps_config'):
                        db().execute('DELETE FROM '+table)
                    return success('Wialon disconnected. The saved token has been removed.','bf_gps_settings')
                if action=='link':
                    tid=int(request.form.get('truck_id','0'));uid=int(request.form.get('unit_id','0'))
                    db().execute('BEGIN IMMEDIATE')
                    if not db().execute('SELECT 1 FROM bf_trucks WHERE id=?',(tid,)).fetchone(): abort(404)
                    if uid:
                        if not db().execute('SELECT 1 FROM bf_gps_units WHERE unit_id=? AND available=1',(uid,)).fetchone():
                            raise ValueError('Choose an available Wialon unit.')
                        other=db().execute('SELECT truck_id FROM bf_truck_gps WHERE unit_id=? AND truck_id!=?',(uid,tid)).fetchone()
                        if other: raise ValueError('That GPS unit is already linked to another truck.')
                        db().execute('INSERT INTO bf_truck_gps(truck_id,unit_id) VALUES(?,?) ON CONFLICT(truck_id) DO UPDATE SET unit_id=excluded.unit_id',(tid,uid))
                    else: db().execute('DELETE FROM bf_truck_gps WHERE truck_id=?',(tid,))
                    db().execute('DELETE FROM bf_geofence_observations WHERE truck_id=?',(tid,))
                    return success('Truck GPS link saved.','bf_gps_settings')
                raise ValueError('Choose a connection action.')
            except (ValueError,InvalidToken) as error:
                return failure(ValueError(str(error) if isinstance(error,ValueError) else 'Re-enter the Wialon token.'),'bf_gps_settings')
        config=db().execute('SELECT host,updated_at,updated_by,last_success,last_error FROM bf_gps_config WHERE id=1').fetchone()
        trucks=db().execute('SELECT t.*,m.unit_id FROM bf_trucks t LEFT JOIN bf_truck_gps m ON m.truck_id=t.id ORDER BY t.state,t.name').fetchall()
        units=db().execute('SELECT unit_id,name,available FROM bf_gps_units ORDER BY name').fetchall()
        locations=db().execute('''SELECT l.*,c.name AS client_name,f.latitude,f.longitude,f.radius_m FROM bf_locations l
            LEFT JOIN bf_clients c ON c.id=l.client_id LEFT JOIN bf_geofences f ON f.location_id=l.id ORDER BY l.state,l.kind,c.name,l.name''').fetchall()
        return render('gps_settings',section='setup',config=config,hosts=HOSTS,trucks=trucks,units=units,locations=locations)

    @app.route('/binflow/locations/<int:location_id>/geofence',methods=['GET','POST'])
    @require('admin')
    def bf_geofence(location_id):
        location=db().execute('SELECT * FROM bf_locations WHERE id=?',(location_id,)).fetchone()
        if not location: abort(404)
        if request.method=='POST':
            try:
                db().execute('BEGIN IMMEDIATE')
                if request.form.get('action')=='remove':
                    db().execute('DELETE FROM bf_geofences WHERE location_id=?',(location_id,))
                else:
                    lat=float(request.form.get('latitude',''));lon=float(request.form.get('longitude',''));radius=int(request.form.get('radius_m',''))
                    if not math.isfinite(lat) or not math.isfinite(lon) or not -90<=lat<=90 or not -180<=lon<=180 or not 25<=radius<=5000:
                        raise ValueError('Enter valid coordinates and a radius between 25 and 5,000 metres.')
                    db().execute('''INSERT INTO bf_geofences(location_id,latitude,longitude,radius_m,updated_at,updated_by) VALUES(?,?,?,?,?,?)
                        ON CONFLICT(location_id) DO UPDATE SET latitude=excluded.latitude,longitude=excluded.longitude,
                        radius_m=excluded.radius_m,updated_at=excluded.updated_at,updated_by=excluded.updated_by''',
                        (location_id,lat,lon,radius,now(),actor()))
                db().execute('DELETE FROM bf_geofence_observations WHERE location_id=?',(location_id,))
                return success('Site geofence updated.','bf_geofence',location_id=location_id)
            except (ValueError,TypeError):
                return failure(ValueError('Enter valid latitude, longitude and a radius of 25–5,000 metres.'),'bf_geofence',location_id=location_id)
        fence=db().execute('SELECT * FROM bf_geofences WHERE location_id=?',(location_id,)).fetchone()
        return render('geofence',section='setup',location=location,fence=fence)

    @app.get('/binflow/gps/status')
    @access
    def bf_gps_status():
        refresh()
        config=db().execute('SELECT last_success,last_error FROM bf_gps_config WHERE id=1').fetchone()
        rows=db().execute('''SELECT t.id,t.name,u.latitude,u.longitude,u.position_time,u.speed,u.available
            FROM bf_trucks t LEFT JOIN bf_truck_gps m ON m.truck_id=t.id LEFT JOIN bf_gps_units u ON u.unit_id=m.unit_id ORDER BY t.state,t.name''').fetchall()
        trucks=[]
        for row in rows:
            t=dict(row)
            fresh=bool(config and not config['last_error'] and t['available'] and t['latitude'] is not None and t['position_time'] and -60<=time.time()-t['position_time']<=FRESH_SECONDS)
            places=db().execute('''SELECT l.name FROM bf_geofence_observations o JOIN bf_locations l ON l.id=o.location_id
                WHERE o.truck_id=? AND o.inside=1 AND o.position_time=? ORDER BY l.kind,l.name''',(t['id'],t['position_time'])).fetchall() if fresh else []
            t.update(fresh=fresh,places=[r['name'] for r in places],updated=datetime.fromtimestamp(t['position_time'],ZoneInfo('Australia/Brisbane')).strftime('%d %b %H:%M:%S') if t['position_time'] else None)
            trucks.append(t)
        return jsonify(html=render_template('binflow/gps_status.html',config=config,trucks=trucks))


    @app.get('/binflow/gps/map')
    @access
    def bf_gps_map():
        state=request.args.get('state','')
        if state and state not in STATES: abort(400)
        refresh()
        return jsonify(tracking_snapshot(db(),state,include_unlinked=g.user['role']=='admin'))
