from pathlib import Path
import hashlib,json,sys
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT));sys.path.insert(0,str(ROOT/'scripts'))
from verify_agent_http import request
import pandas as pd

def main():
    checks=[]
    for p in ['/api/land/delivery','/api/land/activity-2026','/api/land/history-2021-2025','/api/land/use-type','/api/land/consolidation','/api/land/activity-change','/api/land/degradation']:
        a=request(8526,p);b=request(8527,p);assert a[0]==b[0]==200 and json.loads(a[1])==json.loads(b[1]),p;checks.append(p)
    f=pd.read_parquet(ROOT/'data/analysis/irrigation_stress/irrigation_stress_20260907_v2/parcels.parquet')
    codes=list(f.groupby('state').head(2).cadastre_code)
    for code in codes:
        a=request(8526,'/api/land/parcel?code='+code);b=request(8527,'/api/land/parcel?code='+code)
        aa=json.loads(a[1]);bb=json.loads(b[1]);assert a[0]==b[0]==200
        s=bb.pop('irrigationStress');assert s['state']==f.set_index('cadastre_code').loc[code,'state'];assert aa==bb,code
    for p in ['/.env','/server_data/agent_v7/irrigation_stress_lookup.json','/data/analysis/irrigation_stress/irrigation_stress_20260907_v2/metrics.parquet','/config/irrigation_stress_20260907_v2.json','/data/observations/current_stress_weather_20260907_v1/raw_2026.json']:
        assert request(8527,p)[0] in [403,404],p
    assert request(8527,'/api/agent/session',{})[0]==403
    assert request(8527,'/api/agent/session',{}, {'Origin':'https://other.example'})[0]==403
    status,b=request(8527,'/api/land/irrigation-stress');assert status==200 and json.loads(b)==json.loads((ROOT/'server_data/agent_v7/irrigation_stress.json').read_text())
    assert hashlib.sha256(request(8525,'/')[1]).hexdigest()=='8dfe5849611a0614c55a20e7f3a0113fd7f618c4a0e0b7b7e6be518d2c9b403d'
    css=request(8527,'/assets/readability-20260907-v1.css');assert css[0]==200 and css[1]==(ROOT/'ui/readability-20260907-v1.css').read_bytes()
    report={'passed':True,'preserved_endpoints':checks,'preserved_profiles':len(codes),'private_paths_rejected':True,'readability_css_byte_identical':True,'working_8525_unchanged':True}
    (ROOT/'server_data/agent_v7/http_verification.json').write_text(json.dumps(report,indent=2));print(json.dumps(report))
if __name__=='__main__':main()
