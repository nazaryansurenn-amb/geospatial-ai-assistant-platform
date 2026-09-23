"""Verify actual activated HTTP/GPT progress, not just a running process."""
from pathlib import Path
import csv
import hashlib
import io
import json
import sys
import time
import urllib.request
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))

if __name__=='__main__':
    token=None
    def request(path,body=None):
        headers={'Origin':'http://127.0.0.1:8526'}
        if token:headers['X-Agent-Session']=token
        if body is not None:headers['Content-Type']='application/json'
        req=urllib.request.Request('http://127.0.0.1:8526'+path,headers=headers,data=json.dumps(body,ensure_ascii=False).encode() if body is not None else None)
        with urllib.request.urlopen(req,timeout=10) as r:return r.read()
    assert request('/')==(ROOT/'output/frontend_agent_review_20260906_v2/index.html').read_bytes()
    assert json.loads(request('/health'))['status']=='ready'
    session=json.loads(request('/api/agent/session',{}));assert session['configured'];token=session['token']
    start=time.monotonic()
    job=json.loads(request('/api/agent/message',{'text':'How many hectares are actively used in Aknalich in 2026? Give the observed active area and official area.',
             'context':{'scope':'lower_hrazdan','mode':'activity_2026','include_expansion':False,'language':'en'}}))['job_id']
    phases=set()
    while time.monotonic()-start<100:
        state=json.loads(request('/api/agent/job/'+job));phases.add(state['phase'])
        if state['state']!='running':break
        time.sleep(.5)
    assert state['state']=='complete',state.get('error','Incomplete job')
    card=next(r for r in state['results'] if r['query']['topic']=='activity')
    assert card['summary']['parcel_count']==1406
    assert abs(card['summary']['observed_active_area_ha']-354.1724)<1e-6
    rid=card['result_id'];selection=json.loads(request('/api/agent/result/'+rid+'/map'))
    rows=list(csv.DictReader(io.StringIO(request('/api/agent/result/'+rid+'/csv').decode('utf-8-sig'))))
    assert len(rows)==len(selection['ids'])==1406
    assert {r['cadastre_code'] for r in rows}==set(selection['codes'])
    assert abs(sum(float(r['official_area_ha']) for r in rows)-card['summary']['official_area_ha'])<1e-6
    with urllib.request.urlopen('http://127.0.0.1:8525/',timeout=10) as r:
        assert hashlib.sha256(r.read()).hexdigest()=='8dfe5849611a0614c55a20e7f3a0113fd7f618c4a0e0b7b7e6be518d2c9b403d'
    result={'passed':True,'port':8526,'launcher':'run_agent_review_v2.py','live_gpt_complete':True,'seconds':round(time.monotonic()-start,1),
            'observed_phases':sorted(phases),'answer':state['answer'],'summary':card['summary'],'csv_and_map_match':True,'working_8525_unchanged':True,
            'old_agent_prompts_and_history_loaded':False}
    (ROOT/'server_data/agent_v1/activation_verified.json').write_text(json.dumps(result,indent=2,ensure_ascii=False),encoding='utf-8')
    print(json.dumps(result,ensure_ascii=False))
