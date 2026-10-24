import json
SIGNATURE=json.dumps([[[10,20],[70,80],[140,30],[220,90]]])

def sign_pod(case,jid):
    collected=case.rows("SELECT * FROM bf_job_bins WHERE job_id=? AND direction='collect'",(jid,))
    if collected:
        case.post('/binflow/setup/materials',dict(name='Test textiles',active='yes'))
        mid=case.rows("SELECT id FROM bf_material_classes WHERE name='Test textiles'")[0]['id']
        version=case.rows('SELECT pod_version FROM bf_jobs WHERE id=?',(jid,))[0]['pod_version']
        case.post(f'/binflow/pickups/{jid}/pod/materials',dict(version=version,**{'material_'+str(i['id']):mid for i in collected}))
    version=case.rows('SELECT pod_version FROM bf_jobs WHERE id=?',(jid,))[0]['pod_version']
    response=case.post(f'/binflow/pickups/{jid}/pod/sign',dict(version=version,confirm='yes',signature=SIGNATURE))
    case.assertIn(b'POD signed off.',response.data)
    return response
