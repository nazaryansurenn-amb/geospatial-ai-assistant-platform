"""Verify the actual 8526 data, assistant and protected Excel after activation."""
from pathlib import Path
import hashlib,io,json,sys,time
from zipfile import ZipFile
import xml.etree.ElementTree as ET
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT));sys.path.insert(0,str(ROOT/'scripts'))
from verify_agent_http import request
from wp_core.agent_excel_v6 import NS
from wp_core.agent_scope import fixed_reply

def main():
    directory=ROOT/'server_data/agent_v6';target=directory/'activation.json';assert not target.exists()
    expected=json.loads((directory/'pre_activation_api.json').read_text(encoding='utf-8'))
    for path,value in expected.items():
        status,data=request(8526,path);assert status==200 and json.loads(data)==value,path
    assert request(8526,'/')[1]==request(8527,'/')[1]
    assert hashlib.sha256(request(8525,'/')[1]).hexdigest()=='8dfe5849611a0614c55a20e7f3a0113fd7f618c4a0e0b7b7e6be518d2c9b403d'
    status,data=request(8526,'/api/land/degradation');assert status==200
    assert json.loads(data)==json.loads((directory/'degradation.json').read_text(encoding='utf-8'))
    origin={'Origin':'http://127.0.0.1:8526'}
    status,data=request(8526,'/api/agent/session',{},origin);assert status==200
    token=json.loads(data)['token'];headers={**origin,'X-Agent-Session':token}
    for question,kind in [('ով է ստեղծել քեզ','identity'),('Ֆրանսիայի մայրաքաղաքը որն է','redirect'),('Դեգրադացիայի հնարավոր նշաններով հողամասերի քանակն ու կադաստրային հեկտարները՝ ըստ համայնքի, Ստորին Հրազդան I + II, 2021–2025։','result')]:
        status,data=request(8526,'/api/agent/message',{'text':question,'context':{'scope':'lower_hrazdan','mode':'degradation','include_expansion':False,'language':'hy'}},headers);assert status==200
        job=json.loads(data)['job_id'];deadline=time.monotonic()+90
        while True:
            status,data=request(8526,'/api/agent/job/'+job,headers=headers);assert status==200
            result=json.loads(data)
            if result['state']!='running':break
            assert time.monotonic()<deadline;time.sleep(.25)
        assert result['state']=='complete',result.get('error')
        if kind!='result':assert result['answer']==fixed_reply(kind,'hy')
    card=next(r for r in result['results'] if r['query']['topic']=='degradation')
    assert card['summary']['parcel_count']==299 and len(card['rows'])==22
    endpoint='/api/agent/result/'+card['result_id']
    assert request(8526,endpoint+'/xlsx')[0]==403
    other=json.loads(request(8526,'/api/agent/session',{},origin)[1])['token']
    assert request(8526,endpoint+'/xlsx',headers={'X-Agent-Session':other})[0]==403
    status,data=request(8526,endpoint+'/xlsx',headers=headers);assert status==200
    with ZipFile(io.BytesIO(data)) as z:
        assert len(ET.fromstring(z.read('xl/worksheets/sheet2.xml')).find('x:sheetData',NS))==23
        assert len(ET.fromstring(z.read('xl/worksheets/sheet3.xml')).find('x:sheetData',NS))==300
        assert len([n for n in z.namelist() if '/charts/chart' in n and n.endswith('.xml')])==2
    (directory/'verified-degradation.xlsx').write_bytes(data)
    status,data=request(8526,endpoint+'/map',headers=headers);assert status==200
    selection=json.loads(data);assert len(selection['ids'])==len(set(selection['ids']))==299
    assert set(selection['ids'])==set(json.loads((directory/'degradation.json').read_text(encoding='utf-8'))['scopes']['lower_hrazdan']['ids'])
    report={'passed':True,'port':8526,'six_previous_endpoints_preserved':True,'working_8525_unchanged':True,'identity_preserved':True,'unrelated_trivia_redirected':True,'actual_agent_candidate_count':299,'official_area_ha':card['summary']['official_area_ha'],'community_rows':22,'excel_parcel_rows':299,'excel_native_charts':2,'download_session_ownership_checked':True,'map_ids_reconciled':True}
    target.write_text(json.dumps(report,indent=2),encoding='utf-8');print(json.dumps(report))
if __name__=='__main__':main()
