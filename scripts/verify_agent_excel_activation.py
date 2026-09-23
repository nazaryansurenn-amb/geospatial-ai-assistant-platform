"""Verify a genuine Excel download from the active 8526 agent."""
from pathlib import Path
import hashlib
import io
import json
import sys
import time
from zipfile import ZipFile
import xml.etree.ElementTree as ET
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT));sys.path.insert(0,str(ROOT/'scripts'))
from verify_agent_http import request
from wp_core.agent_excel import NS
from wp_core.agent_scope import fixed_reply

if __name__=='__main__':
    report=ROOT/'server_data/agent_v5/activation.json';assert not report.exists()
    assert request(8526,'/')[1]==request(8527,'/')[1]
    assert hashlib.sha256(request(8525,'/')[1]).hexdigest()=='8dfe5849611a0614c55a20e7f3a0113fd7f618c4a0e0b7b7e6be518d2c9b403d'
    origin={'Origin':'http://127.0.0.1:8526'}
    status,body=request(8526,'/api/agent/session',{},origin);assert status==200
    token=json.loads(body)['token'];headers={**origin,'X-Agent-Session':token}
    for question,kind in [('ով է ստեղծել քեզ','identity'),('Համեմատիր բոլոր համայնքների ներուժը՝ ներառյալ հարակից 1 կմ գոտին։ Տուր թեկնածուների հողամասերի քանակը և կադաստրային հեկտարները ըստ համայնքի։','table')]:
        status,body=request(8526,'/api/agent/message',{'text':question,'context':{'scope':'lower_hrazdan','mode':'potential','include_expansion':True,'language':'hy'}},headers);assert status==200
        job=json.loads(body)['job_id'];deadline=time.monotonic()+90
        while True:
            status,body=request(8526,'/api/agent/job/'+job,headers=headers);assert status==200
            result=json.loads(body)
            if result['state']!='running':break
            assert time.monotonic()<deadline
            time.sleep(.2)
        assert result['state']=='complete',result.get('error')
        if kind=='identity':assert result['answer']==fixed_reply(kind,'hy')
    card=next(r for r in result['results'] if r['query']['topic']=='potential')
    assert card['summary']['parcel_count']==3169 and len(card['rows'])==23
    endpoint='/api/agent/result/'+card['result_id']+'/xlsx'
    assert request(8526,endpoint)[0]==403
    other=json.loads(request(8526,'/api/agent/session',{},origin)[1])['token']
    assert request(8526,endpoint,headers={'X-Agent-Session':other})[0]==403
    start=time.monotonic();status,data=request(8526,endpoint,headers=headers);assert status==200
    with ZipFile(io.BytesIO(data)) as z:
        assert len(ET.fromstring(z.read('xl/worksheets/sheet2.xml')).find('x:sheetData',NS))==24
        assert len(ET.fromstring(z.read('xl/worksheets/sheet3.xml')).find('x:sheetData',NS))==3170
        assert len([n for n in z.namelist() if '/charts/chart' in n and n.endswith('.xml')])==2
    (report.parent/'verified-download.xlsx').write_bytes(data)
    record={'passed':True,'port':8526,'identity_preserved':True,'full_community_rows':23,'parcel_rows':3169,'native_charts':2,'xlsx_bytes':len(data),'download_seconds':round(time.monotonic()-start,3),'result_ownership_checked':True,'working_8525_unchanged':True}
    report.write_text(json.dumps(record,indent=2),encoding='utf-8');print(json.dumps(record))
