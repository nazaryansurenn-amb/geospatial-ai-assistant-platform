"""Compare the additive preview against the actual current six-section server."""
from pathlib import Path
import hashlib,json,sys
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT));sys.path.insert(0,str(ROOT/'scripts'))
from verify_agent_http import request

def main():
    directory=ROOT/'server_data/agent_v6';checks=[]
    for path in ['/api/land/delivery','/api/land/activity-2026','/api/land/history-2021-2025','/api/land/use-type','/api/land/consolidation','/api/land/activity-change']:
        a=request(8526,path);b=request(8527,path)
        assert a[0]==b[0]==200,path
        assert json.loads(a[1])==json.loads(b[1]),path
        checks.append(path)
    import pandas as pd
    df=pd.read_parquet(ROOT/'data/analysis/degradation/degradation_screening_20260906_v1/parcels.parquet')
    examples=list(df.groupby('state').head(2).cadastre_code)
    for code in examples:
        a=request(8526,'/api/land/parcel?code='+code);b=request(8527,'/api/land/parcel?code='+code)
        assert a[0]==b[0]==200
        bb=json.loads(b[1]);assert bb.pop('degradation')['state']==df.set_index('cadastre_code').loc[code,'state']
        assert bb==json.loads(a[1]),code
    status,data=request(8527,'/api/land/degradation');assert status==200
    assert json.loads(data)==json.loads((directory/'degradation.json').read_text(encoding='utf-8'))
    for path in ['/server_data/agent_v6/degradation_lookup.json','/data/analysis/degradation/degradation_screening_20260906_v1/metrics.parquet','/config/degradation_screening_20260906_v1.json','/.env']:
        assert request(8527,path)[0] in [403,404],path
    assert request(8527,'/api/agent/session',{})[0]==403
    assert request(8527,'/api/agent/session',{}, {'Origin':'https://other.example'})[0]==403
    assert hashlib.sha256(request(8525,'/')[1]).hexdigest()=='8dfe5849611a0614c55a20e7f3a0113fd7f618c4a0e0b7b7e6be518d2c9b403d'
    report={'passed':True,'preserved_endpoints':checks,'parcel_profiles_preserved':len(examples),'private_paths_rejected':True,'working_8525_unchanged':True}
    (directory/'http_verification.json').write_text(json.dumps(report,indent=2));print(json.dumps(report))
if __name__=='__main__':main()
