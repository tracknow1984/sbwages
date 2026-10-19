import json
import re
from binflow_runs import CHECKS

SIGNATURE=json.dumps([[[20,20],[90,100],[180,40],[260,120]]])
RUN_DATE='2026-10-19'

def accept(case, truck=1, run_date=RUN_DATE):
    response=case.client.get(f'/binflow/driver/{truck}/day?date={run_date}')
    token=re.search(rb'name="manifest_version" value="([^"]+)"',response.data)
    if not token: raise AssertionError(response.data.decode()[-3000:])
    return case.post(f'/binflow/driver/{truck}/accept',{'date':run_date,'manifest_version':token[1].decode()})

def prestart(case, truck=1, fail=False):
    state=case.client.get(f'/binflow/driver/{truck}/day/state?date={RUN_DATE}').json
    if state and state['stage']=='acceptance': accept(case,truck)

    response=case.client.get(f'/binflow/driver/{truck}/day?date={RUN_DATE}')
    token=re.search(rb'name="submission_token" value="([^"]+)"',response.data)
    if not token: raise AssertionError(response.data.decode()[-3000:])
    values={f'check_{key}':'pass' for key,_,_ in CHECKS}
    values.update(date=RUN_DATE,submission_token=token[1].decode(),reading='12345',fit_for_duty='yes',signature=SIGNATURE,signed='yes')
    if fail: values.update(check_tyres='fail',note_tyres='Damaged tyre requires repair.')
    return case.post(f'/binflow/driver/{truck}/prestart',values)

def depart(case, truck=1):
    return case.post(f'/binflow/driver/{truck}/day/depart',{'date':RUN_DATE})
