"""Read-only preservation and HTTP boundary checks against local agent preview."""
from pathlib import Path
import hashlib
import json
import sys
import urllib.request
import urllib.error
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))

def request(port,path,body=None,headers=None):
    h=dict(headers or {})
    if body is not None:h.setdefault('Content-Type','application/json')
    req=urllib.request.Request(f'http://127.0.0.1:{port}'+path,headers=h,data=json.dumps(body).encode() if body is not None else None)
    try:
        with urllib.request.urlopen(req,timeout=10) as r:return r.status,r.read()
    except urllib.error.HTTPError as e:return e.code,e.read()

if __name__=='__main__':
    origin={'Origin':'http://127.0.0.1:8527'}
    checks=[]
    for p in ['/api/land/delivery','/api/land/activity-2026','/api/land/history-2021-2025','/api/land/use-type','/api/land/consolidation']:
        a=request(8526,p);b=request(8527,p)
        assert a[0]==b[0]==200,(p,a[0],b[0])
        assert json.loads(a[1])==json.loads(b[1]),p
        checks.append({'path':p,'unchanged':True})
    assert request(8527,'/api/agent/session',{})[0]==403
    assert request(8527,'/api/agent/session',{}, {'Origin':'https://other.example'})[0]==403
    assert request(8527,'/api/agent/session',{}, {'Origin':'http://127.0.0.1:8527','Host':'other.example:8527'})[0]==403
    status,data=request(8527,'/api/agent/session',{},origin);assert status==200
    token=json.loads(data)['token']
    status,_=request(8527,'/api/agent/result/'+'a'*32+'/map',headers={'X-Agent-Session':token});assert status==403
    status,_=request(8527,'/api/agent/message',{'text':'test','context':{'scope':'wua'}},{**origin,'X-Agent-Session':token});assert status==400
    assert request(8527,'/api/agent/job/'+'a'*32)[0]==403
    # The immutable source index and geometry hashes remain exactly as prepared.
    manifest=json.loads((ROOT/'server_data/agent_v1/manifest.json').read_text(encoding='utf-8'))
    for name,digest in manifest['source_sha256'].items():
        assert hashlib.sha256((ROOT/name).read_bytes()).hexdigest()==digest,name
    assert hashlib.sha256((ROOT/'server_data/agent_v1/parcels.sqlite3').read_bytes()).hexdigest()==manifest['index_sha256']
    assert hashlib.sha256(request(8525,'/')[1]).hexdigest()=='8dfe5849611a0614c55a20e7f3a0113fd7f618c4a0e0b7b7e6be518d2c9b403d'
    result={'passed':True,'preserved_endpoints':checks,'http_access_boundaries':True,'sources_and_index_unchanged':True,'working_8525_unchanged':True}
    (ROOT/'server_data/agent_v1/http_verification.json').write_text(json.dumps(result,indent=2),encoding='utf-8')
    print(json.dumps(result))
