"""Live regression for the reported Armenian overview and unchanged measured area."""
from pathlib import Path
import json
import sys
import time
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from wp_core.agent_service_v3 import AgentService,openai_response
from wp_core.agent_presentation import presentation_issues

if __name__=='__main__':
    calls=[]
    def responder(messages):
        calls.append(time.monotonic());return openai_response(messages)
    service=AgentService(responder);token=service.session()['token'];checks=[]
    questions=[
        'Բացատրի՛ր այս հարթակի կառուցվածքն ու բաժինները։',
        'Ի՞նչ են նշանակում ակտիվության և օգտագործման տեսակի դասերը։',
        'Ի՞նչ է նշանակում ինքնահոս կամ մեխանիկական թեկնածու, և հիմա ո՞ր տարածքն է ընտրված։',
        'Ակնալճում 2026-ին քանի՞ հեկտար է ակտիվ։ Տուր դիտվող ակտիվ մակերեսը։',
    ]
    try:
        for question in questions:
            start=time.monotonic();before=len(calls)
            job=service.submit(token,question,{'scope':'lower_hrazdan','mode':'potential','include_expansion':True,'language':'hy'})['job_id']
            while service.status(token,job)['state']=='running' and time.monotonic()-start<150:time.sleep(.3)
            value=service.status(token,job)
            check={'question':question,'state':value['state'],'answer':value.get('answer'),'error':value.get('error'),'seconds':round(time.monotonic()-start,1),'api_calls':len(calls)-before}
            print(json.dumps(check,ensure_ascii=False),flush=True)
            assert value['state']=='complete'
            assert not presentation_issues(value['answer'],'hy')
            if question==questions[0]:
                assert all(name in value['answer'] for name in ['2026','Օգտագործման տեսակ','2021','Ներուժ','կոնսոլիդացի'])
                assert not value['results']
            if question==questions[-1]:
                card=next(r for r in value['results'] if r['query']['topic']=='activity')
                assert abs(card['summary']['observed_active_area_ha']-354.1724)<1e-6
                assert card['summary']['parcel_count']==1406
            checks.append(check)
        (ROOT/'server_data/agent_v1/language_verification_v3.json').write_text(json.dumps({'passed':True,'checks':checks},ensure_ascii=False,indent=2),encoding='utf-8')
    finally:service.pool.shutdown()
