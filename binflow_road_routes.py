"""Server-side Google road routes using the current saved street addresses."""
import base64
import hashlib
import json
import math
import os
import re
from concurrent.futures import ThreadPoolExecutor
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, build_opener, HTTPRedirectHandler

from cryptography.fernet import Fernet, InvalidToken
from flask import current_app, request


def migrate(conn):
    conn.execute('''CREATE TABLE IF NOT EXISTS bf_road_routing (
        id INTEGER PRIMARY KEY CHECK(id=1),key_cipher BLOB,updated_at TEXT NOT NULL,updated_by TEXT NOT NULL)''')


def cipher():
    key = hashlib.sha256(('binflow:road-routing:v1:'+str(current_app.config['SECRET_KEY'])).encode()).digest()
    return Fernet(base64.urlsafe_b64encode(key))


def api_key(conn):
    for name in ('BINFLOW_GOOGLE_ROUTES_API_KEY','GOOGLE_ROUTES_API_KEY','GOOGLE_MAPS_API_KEY'):
        if os.environ.get(name):
            return os.environ[name]
    row = conn.execute('SELECT key_cipher FROM bf_road_routing WHERE id=1').fetchone()
    if row and row[0]:
        try:
            return cipher().decrypt(row[0]).decode()
        except InvalidToken:
            return ''
    return ''


def address(location):
    raw = (location['address'] or '').strip()
    # Do not route from a suburb placeholder or the missing SA depot.
    if not raw or not re.search(r'\d', raw) or re.search(r'\b(pending|unconfirmed|to confirm|tba|tbc)\b',raw,re.I):
        return None
    return ', '.join((raw,location['state'],'Australia'))


def maps_link(origin, destination):
    if not origin or not destination:
        return None
    return 'https://www.google.com/maps/dir/?'+urlencode(dict(api=1,origin=origin,destination=destination,travelmode='driving'))


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def call(key, method, payload, fields):
    req = Request('https://routes.googleapis.com/'+method, data=json.dumps(payload).encode(), method='POST',
                  headers={'Content-Type':'application/json','X-Goog-Api-Key':key,'X-Goog-FieldMask':fields})
    try:
        with build_opener(NoRedirect).open(req, timeout=6) as response:
            body = response.read(1024*1024+1)
        if len(body)>1024*1024:
            raise ValueError('Road service response was too large.')
        return json.loads(body)
    except HTTPError as error:
        if error.code in (400,401,403):
            raise ValueError('Google Routes connection needs checking in Setup (key, API access or billing).') from None
        raise ValueError('Road routing is temporarily unavailable. Try again shortly.') from None
    except (URLError,OSError,TimeoutError,json.JSONDecodeError):
        raise ValueError('Road routing could not be reached. Try again shortly.') from None


def measures(row):
    try:
        metres=float(row.get('distanceMeters',0))
        seconds=float(row.get('duration','0s').removesuffix('s'))
        if not all(math.isfinite(x) and x>=0 for x in (metres,seconds)):
            return None
        return dict(distance_km=round(metres/1000,1),drive_minutes=math.ceil(seconds/60))
    except (ValueError,TypeError,AttributeError):
        return None


def matrix(key, origin, destinations):
    payload=dict(origins=[dict(waypoint=dict(address=origin))],
                 destinations=[dict(waypoint=dict(address=a)) for a in destinations],
                 travelMode='DRIVE',routingPreference='TRAFFIC_UNAWARE',regionCode='AU')
    rows=call(key,'distanceMatrix/v2:computeRouteMatrix',payload,
              'originIndex,destinationIndex,status,condition,distanceMeters,duration')
    result={}
    if not isinstance(rows,list):
        raise ValueError('Road routing returned an unexpected response.')
    for row in rows:
        index=row.get('destinationIndex',0)
        if (row.get('originIndex',0)!=0 or type(index) is not int or not 0<=index<len(destinations)
                or row.get('status',{}).get('code',0)!=0 or row.get('condition')!='ROUTE_EXISTS'):
            continue
        value=measures(row)
        if value is not None:result[index]=value
    return result


def distances(conn, warehouse, sites):
    key=api_key(conn)
    origin=address(warehouse) if warehouse else None
    if not warehouse:
        return {},'Select a starting depot from Setup.',bool(key)
    if not origin:
        return {},'Depot street address pending in Setup.',bool(key)
    if not key:
        return {},'Connect Google road distances in Setup for address-to-address kilometres. Approximate distances below use suburb centres.',False
    eligible=[(s['id'],address(s)) for s in sites if address(s)]
    if len(eligible)>196:
        return {},'Too many sites for one road-distance lookup; narrow the state client list.',True
    batches=[eligible[i:i+49] for i in range(0,len(eligible),49)]
    def batch(items):
        try:
            values=matrix(key,origin,[a for _,a in items])
            return {items[i][0]:v for i,v in values.items()},''
        except ValueError as error:
            return {},str(error)
    result={};issues=[]
    # No client data or Google route response is written to an external cache.
    with ThreadPoolExecutor(max_workers=4) as pool:
        for values,error in pool.map(batch,batches):
            result.update(values)
            if error:issues.append(error)
    message=issues[0] if issues else ('Some sites need a complete street address or a routable address.' if len(result)!=len(sites) else '')
    return result,message,True


def round_trip(key, origin, stops):
    if not origin or not all(stops) or not stops or len(stops)>25:
        return None
    try:
        result=call(key,'directions/v2:computeRoutes',dict(origin=dict(address=origin),destination=dict(address=origin),
                    intermediates=[dict(address=s) for s in stops],travelMode='DRIVE',
                    routingPreference='TRAFFIC_UNAWARE',regionCode='AU'), 'routes.distanceMeters,routes.duration')
        return measures(result['routes'][0]) if result.get('routes') else None
    except (ValueError,TypeError,KeyError):
        return None


def register(app,db,require,render,success,failure,now,actor):
    @app.route('/binflow/setup/road-distances',methods=['GET','POST'])
    @require('admin')
    def bf_road_settings():
        if request.method=='POST':
            try:
                if any(os.environ.get(n) for n in ('BINFLOW_GOOGLE_ROUTES_API_KEY','GOOGLE_ROUTES_API_KEY','GOOGLE_MAPS_API_KEY')):
                    raise ValueError('Road routing is configured on the server; update that connection through your administrator.')
                key=request.form.get('api_key','').strip()
                if not re.fullmatch(r'[A-Za-z0-9_-]{25,200}',key):
                    raise ValueError('Enter a Google Maps API key with Routes API enabled.')
                depot=db().execute("SELECT * FROM bf_locations WHERE kind='warehouse' AND state IN ('QLD','NSW','VIC') ORDER BY id").fetchall()
                origin=next((address(w) for w in depot if address(w)),None)
                sites=db().execute("SELECT * FROM bf_locations WHERE kind='client' ORDER BY id").fetchall()
                destination=next((address(s) for s in sites if address(s)),None)
                if not origin or not destination:
                    raise ValueError('Save a depot and client street address before connecting road distances.')
                if not matrix(key,origin,[destination]):
                    raise ValueError('Google could not route between the saved addresses. Check the addresses before connecting.')
                with db():
                    db().execute('''INSERT INTO bf_road_routing VALUES(1,?,?,?) ON CONFLICT(id) DO UPDATE SET
                        key_cipher=excluded.key_cipher,updated_at=excluded.updated_at,updated_by=excluded.updated_by''',
                                 (cipher().encrypt(key.encode()),now(),actor()))
                return success('Google road distances connected. Open Add allocation to calculate from the selected depot.','bf_road_settings')
            except ValueError as error:
                return failure(error,'bf_road_settings')
        return render('road_settings',section='setup',connected=bool(api_key(db())),
                      environment_key=any(os.environ.get(n) for n in ('BINFLOW_GOOGLE_ROUTES_API_KEY','GOOGLE_ROUTES_API_KEY','GOOGLE_MAPS_API_KEY')))
