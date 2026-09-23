"""Real model smoke evaluation. Save only sanitized answers and numerical checks."""
from pathlib import Path
import json
import sys
import time
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from wp_core.agent_service_v2 import AgentService
from wp_core.agent_queries import build_result

if __name__=='__main__':
    service=AgentService();token=service.session()['token'];checks=[]
    questions=[
        ('How many hectares are actively used in Aknalich in 2026? Give the measured active area and total official area.', 'Ակնալիճ'),
        ('And in Arshaluys? Give me a breakdown chart too.', 'Արշալույս'),
        ('Explain how Echmiadzin, Lower Hrazdan, stages, communities and parcels relate. Is the whole WUA analysis available, and which sections are actually active?', None),
        ('Համեմատիր ներուժը՝ I և II հերթերում, բոլոր համայնքներով, հարակից 1 կմ գոտին ներառյալ։', 'potential'),
    ]
    try:
        for question,expected in questions:
            start=time.monotonic()
            jid=service.submit(token,question,{'scope':'lower_hrazdan','mode':'potential','include_expansion':True,'language':'hy' if expected=='potential' else 'en'})['job_id']
            while service.status(token,jid)['state']=='running':time.sleep(.4)
            result=service.status(token,jid)
            print(json.dumps({'state':result['state'],'seconds':round(time.monotonic()-start,1),'answer':result.get('answer'),'error':result.get('error'),'queries':[r['query'] for r in result['results']]},ensure_ascii=False),flush=True)
            assert result['state']=='complete'
            if expected in ['Ակնալիճ','Արշալույս']:
                card=next(r for r in result['results'] if r['query']['topic']=='activity')
                direct=build_result({'topic':'activity','communities':[expected]})
                assert card['query']['communities']==[expected]
                assert abs(card['summary']['observed_active_area_ha']-direct['summary']['observed_active_area_ha'])<1e-7
            elif expected=='potential':
                card=next(r for r in result['results'] if r['query']['topic']=='potential')
                assert card['query']['include_expansion'] and card['summary']['parcel_count']==3169
            checks.append({'passed':True,'question':question,'answer':result.get('answer'),'results':result['results'],'seconds':round(time.monotonic()-start,1)})
        path=ROOT/'server_data/agent_v1/live_model_verification_v2.json'
        path.write_text(json.dumps({'passed':True,'checks':checks},ensure_ascii=False,indent=2),encoding='utf-8')
    finally:service.pool.shutdown()
