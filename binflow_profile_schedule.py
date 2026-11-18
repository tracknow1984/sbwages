"""Read imported collection agreements and edit them through structured controls."""
import json
import re
from collections import Counter
from dataclasses import dataclass

DAYS = ('Monday','Tuesday','Wednesday','Thursday','Friday','Saturday','Sunday')
OPTIONS = {
    'unset':'Schedule to confirm', 'once':'1 per week', 'twice':'2 times per week',
    'three':'3 times per week', 'daily':'Every day (Mon–Sun)', 'weekdays':'Weekdays (Mon–Fri)',
    'custom':'Select days of the week', 'fortnightly':'Fortnightly', 'four_weekly':'Every 4 weeks',
    'eight_weekly':'Every 8 weeks', 'monthly':'Monthly', 'last_monthly':'Last weekday of the month',
    'adhoc':'On request / ad hoc', 'paused':'Paused',
}
COUNTS = {'once':1,'twice':2,'three':3,'fortnightly':1,'four_weekly':1,'eight_weekly':1,'last_monthly':1}
ALIASES = {
    'daily':'daily','everyday':'daily','every day':'daily','7 days a week':'daily',
    'weekdays':'weekdays','every weekday':'weekdays','business days':'weekdays',
    'weekly':'once','once weekly':'once','1 per week':'once','once a week':'once',
    'twice weekly':'twice','twice a week':'twice','three times weekly':'three','thrice weekly':'three',
    'fortnightly':'fortnightly','every fortnight':'fortnightly','every 2 weeks':'fortnightly','every two weeks':'fortnightly',
    'every 4 weeks':'four_weekly','every four weeks':'four_weekly','four weekly':'four_weekly',
    'every 8 weeks':'eight_weekly','every eight weeks':'eight_weekly','eight weekly':'eight_weekly',
    'monthly':'monthly','calendar monthly':'monthly','adhoc':'adhoc','ad hoc':'adhoc','on request':'adhoc',
    'as required':'adhoc','as needed':'adhoc','paused':'paused','on hold':'paused','unconfirmed':'unset','tbc':'unset',
}
DAY_RE = re.compile(r'\b(mon(?:day)?|tue(?:s(?:day)?)?|wed(?:nesday)?|thu(?:rs(?:day)?)?|fri(?:day)?|sat(?:urday)?|sun(?:day)?)s?\b',re.I)
DAY_IDS = {name[:3].lower():i for i,name in enumerate(DAYS)}


@dataclass(frozen=True)
class Schedule:
    frequency: str
    days: tuple = ()

    @property
    def review(self):
        return self.frequency in ('unset','review') or self.frequency in COUNTS and not self.days

    def calendar(self):
        frequency = self.frequency
        if frequency in ('once','twice','three','daily','weekdays','custom'):
            if frequency in ('twice','three') and not self.days:
                return 'unconfirmed', [], None
            return 'weekly', list(self.days), 4 * max(1,len(self.days))
        cadence = {'last_monthly':'monthly','unset':'unconfirmed','review':'unconfirmed'}.get(frequency,frequency)
        return cadence, list(self.days), {'fortnightly':2,'four_weekly':1,'eight_weekly':.5,'paused':0}.get(cadence)

    def label(self):
        names = ', '.join(DAYS[i] for i in self.days)
        if self.frequency == 'last_monthly':
            return 'Last '+names+' of the month'
        if self.frequency == 'custom':
            return 'Selected days — '+names
        if self.frequency in ('once','twice','three') and names:
            return 'Weekly — '+names
        base = {'once':'Weekly','twice':'Twice weekly','three':'Three times weekly','daily':'Every day',
                'weekdays':'Weekdays','adhoc':'Ad hoc','unset':''}.get(self.frequency, OPTIONS.get(self.frequency,''))
        return base+' — '+names if names and self.frequency in COUNTS else base


def decode(raw):
    text = ' '.join((raw or '').strip().casefold().split())
    if not text:
        return Schedule('unset')
    # Expand an explicit day range before extracting individual weekday names.
    def expand(match):
        left,right = (DAY_IDS[v[:3].lower()] for v in match.groups())
        return ' '.join(DAYS[(left+i)%7] for i in range((right-left)%7+1))
    span = r'(?:\s+to\s+|\s*[-–—]\s*)'
    if len(DAY_RE.findall(text)) == 2:
        text = re.sub(DAY_RE.pattern+span+DAY_RE.pattern,expand,text,flags=re.I)
    days = tuple(sorted({DAY_IDS[m[:3].lower()] for m in DAY_RE.findall(text)}))
    rest = DAY_RE.sub(' ',text)
    rest = re.sub(r'[(),./&+:–—-]',' ',rest)
    rest = re.sub(r'\b(and|on)\b',' ',rest) if days else rest
    rest = ' '.join(rest.split())
    if days and rest in ('last of the month','last of month','last each month') and len(days)==1:
        return Schedule('last_monthly',days)
    if days and rest=='selected days':
        return Schedule('custom',days)
    if days and rest in ('weekly',''):
        if days == tuple(range(7)): return Schedule('daily',days)
        if days == tuple(range(5)): return Schedule('weekdays',days)
        return Schedule({1:'once',2:'twice',3:'three'}.get(len(days),'custom'),days)
    freq = ALIASES.get(rest)
    if not freq:
        count = re.fullmatch(r'(one|two|three|[123])\s*(?:x|times?)?\s*(?:per|a|each)?\s*week(?:ly)?',rest)
        if count:
            freq = {'1':'once','one':'once','2':'twice','two':'twice','3':'three','three':'three'}[count[1]]
    if freq in ('daily','weekdays'):
        expected = tuple(range(7 if freq=='daily' else 5))
        return Schedule(freq,expected) if not days or days==expected else Schedule('review')
    if not freq or days and (freq not in COUNTS or COUNTS[freq]!=len(days)):
        return Schedule('review')
    return Schedule(freq,days)


def from_form(form, previous):
    # Existing API callers can continue to submit the original schedule field.
    if 'schedule_frequency' not in form:
        return form.get('schedule',previous).strip()
    freq = form.get('schedule_frequency')
    old = decode(previous)
    if freq == 'review' and old.frequency == 'review':
        return previous
    if freq not in OPTIONS:
        raise ValueError('Choose a valid collection frequency.')
    if freq in ('daily','weekdays'):
        days = tuple(range(7 if freq=='daily' else 5))
    elif freq in COUNTS or freq=='custom':
        key = 'schedule_day' if COUNTS.get(freq)==1 else 'schedule_days'
        raw = [v for v in form.getlist(key) if v!='']
        if len(raw)!=len(set(raw)) or any(v not in tuple('0123456') for v in raw):
            raise ValueError('Choose each day once, from Monday to Sunday.')
        days = tuple(sorted(map(int,raw)))
        if not days and freq==old.frequency and not old.days:
            return previous
        if freq=='custom' and not days or freq in COUNTS and len(days)!=COUNTS[freq]:
            raise ValueError('Select the collection day or days to match the chosen frequency.')
    else:
        days=()
    spec = Schedule(freq,days)
    return previous if old==spec else spec.label()


def form_state(form, existing):
    spec = decode(existing)
    if not form or 'schedule_frequency' not in form:
        return spec
    freq = form.get('schedule_frequency')
    key = 'schedule_day' if COUNTS.get(freq)==1 else 'schedule_days'
    return Schedule(freq,tuple(int(x) for x in form.getlist(key) if x in tuple('0123456')))


def migrate(conn):
    columns = {r[1] for r in conn.execute('PRAGMA table_info(bf_client_site_profiles)')}
    for field,decl in [('schedule_frequency',"TEXT NOT NULL DEFAULT 'unset'"),('schedule_days',"TEXT NOT NULL DEFAULT '[]'"),('schedule_source','TEXT')]:
        if field not in columns:
            conn.execute(f'ALTER TABLE bf_client_site_profiles ADD COLUMN {field} {decl}')


def store(conn, pid, raw):
    spec = decode(raw)
    conn.execute('UPDATE bf_client_site_profiles SET schedule_frequency=?,schedule_days=?,schedule_source=? WHERE id=?',
                 (spec.frequency,json.dumps(spec.days),raw,pid))


def backfill(conn):
    for row in conn.execute('SELECT id,schedule FROM bf_client_site_profiles WHERE schedule_source IS NOT schedule').fetchall():
        store(conn,row['id'],row['schedule'])


def stored(row):
    if 'schedule_source' in row.keys() and row['schedule_source']==row['schedule']:
        return Schedule(row['schedule_frequency'],tuple(json.loads(row['schedule_days'])))
    return decode(row['schedule'])


def summary(rows):
    counter = Counter()
    for row in rows:
        if not row['archived']:
            s = stored(row)
            counter['current'] += 1
            counter[s.frequency] += 1
            if s.review:
                counter['needs_review'] += 1
    return dict(counter)


def sync_calendar(conn, location_id, raw, stamp, actor):
    if not location_id:
        return
    row = conn.execute('SELECT * FROM bf_collection_profiles WHERE location_id=?',(location_id,)).fetchone()
    if not row or not row['active']:
        return
    from binflow_collections import record_profile
    spec = decode(raw)
    cadence,days,_ = spec.calendar()
    conn.execute("""UPDATE bf_collection_profiles SET cadence=?,service_days=?,monthly_rule=?,flexible=?,
        source_frequency=?,version=version+1,updated_at=?,updated_by=? WHERE location_id=?""",
        (cadence,json.dumps(days),'last_weekday' if spec.frequency=='last_monthly' else '',int(not days),
         raw,stamp,actor,location_id))
    updated = conn.execute('SELECT * FROM bf_collection_profiles WHERE location_id=?',(location_id,)).fetchone()
    record_profile(conn,dict(updated),actor,stamp)
