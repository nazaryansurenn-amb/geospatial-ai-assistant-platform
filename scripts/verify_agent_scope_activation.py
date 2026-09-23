"""Verify the actual working review after switching to the sealed v4 launcher."""
from pathlib import Path
import hashlib
import json
import sys
import time
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT));sys.path.insert(0,str(ROOT/'scripts'))
from verify_agent_http import request
from wp_core.agent_scope import fixed_reply

if __name__=='__main__':
    report=ROOT/'server_data/agent_v1/scope_activation_v4.json'
    assert not report.exists(),'Preserve the existing activation report'
    assert request(8526,'/')[1]==request(8527,'/')[1]
    assert hashlib.sha256(request(8525,'/')[1]).hexdigest()=='8dfe5849611a0614c55a20e7f3a0113fd7f618c4a0e0b7b7e6be518d2c9b403d'
    origin={'Origin':'http://127.0.0.1:8526'}
    status,body=request(8526,'/api/agent/session',{},origin);assert status==200
    session=json.loads(body);assert session['configured'];headers={**origin,'X-Agent-Session':session['token']}
    checks=[]
    for question,kind in [('ով է ստեղծել քեզ','identity'),('ֆրանսիայի մայրաքաղաքը որն է','redirect'),('Ակնալճում 2026-ին քանի՞ հեկտար է ակտիվ։ Տուր դիտվող ակտիվ մակերեսը։','measure')]:
        status,body=request(8526,'/api/agent/message',{'text':question,'context':{'scope':'lower_hrazdan','mode':'potential','include_expansion':True,'language':'hy'}},headers);assert status==200
        job=json.loads(body)['job_id'];deadline=time.monotonic()+90
        while True:
            status,body=request(8526,'/api/agent/job/'+job,headers=headers);assert status==200
            result=json.loads(body)
            if result['state']!='running':break
            assert time.monotonic()<deadline,'Answer timed out'
            time.sleep(.2)
        assert result['state']=='complete',result.get('error')
        check={'question':question,'answer':result['answer'],'passed':True}
        if kind=='measure':
            card=next(r for r in result['results'] if r['query']['topic']=='activity')
            assert abs(card['summary']['observed_active_area_ha']-354.1724)<1e-6
            assert card['summary']['parcel_count']==1406
            check['parcel_count']=1406;check['observed_active_area_ha']=354.1724
        else:
            assert result['answer']==fixed_reply(kind,'hy')
            assert result['results']==[]
        checks.append(check)
    report.write_text(json.dumps({'passed':True,'port':8526,'frontend_unchanged':True,'working_8525_unchanged':True,'checks':checks},ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({'passed':True,'port':8526,'identity':True,'trivia_redirect':True,'numerical_result_unchanged':True,'working_8525_unchanged':True}))
