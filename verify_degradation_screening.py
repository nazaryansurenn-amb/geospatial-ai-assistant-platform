"""Independent identity, coverage, indicator and delivery reconciliation."""
from pathlib import Path
import hashlib,json,sqlite3
import numpy as np
import pandas as pd
ROOT=Path(__file__).resolve().parent
OUT=ROOT/'data/analysis/degradation/degradation_screening_20260906_v1'

def main():
    receipt=json.loads((OUT/'complete.json').read_text())
    for name,digest in receipt['files'].items():assert hashlib.sha256((ROOT/name).read_bytes()).hexdigest()==digest,name
    for year in range(2021,2026):
        item=json.loads((OUT/f'features/{year}.json').read_text())
        for name,digest in item['sources'].items():assert hashlib.sha256((ROOT/name).read_bytes()).hexdigest()==digest,name
        f=pd.read_parquet(OUT/f'features/{year}.parquet')
        ok=f.covered
        assert (f.loc[ok,[f'dates_month_{m}' for m in range(3,12)]]>=2).all().all()
        assert (f.loc[ok,'maximum_gap_days']<=35).all()
        assert f.loc[~ok,['ndvi','evi2']].isna().all().all()
    with sqlite3.connect((ROOT/'server_data/agent_v1/parcels.sqlite3').as_uri()+'?mode=ro',uri=True) as c:
        original=pd.read_sql_query("SELECT * FROM parcels WHERE scope='inside_I_II' AND household=0 AND road_excluded=0 ORDER BY cadastre_code",c)
    f=pd.read_parquet(OUT/'parcels.parquet');m=pd.read_parquet(OUT/'metrics.parquet')
    for column in ['cadastre_code','public_parcel_id','official_area_ha','minx','miny','maxx','maxy']:
        pd.testing.assert_series_equal(f[column],original[column],check_names=False)
    mask=original.annual_state_codes.map(lambda s:any(v in [1,2] for v in json.loads(s)))
    np.testing.assert_array_equal(f.included,mask)
    assert len(f)==22802 and mask.sum()==19421
    assert np.array_equal(f.candidate,f.deterioration|f.low_performance)
    assert not (f.candidate&(~mask|~f.spatial_ok)).any()
    assert not (f.deterioration&~f.deterioration_assessed).any()
    assert not (f.low_performance&~f.low_performance_assessed).any()
    assert not (f.candidate&f.state.eq('unassessed')).any()
    for prefix,drop,slope in [('ndvi',.07,-.015),('evi2',.04,-.01)]:
        a=m[[f'{prefix}_{y}' for y in range(2021,2026)]].to_numpy()
        selected=a[f.deterioration];early=np.median(selected[:,:2],axis=1);late=np.median(selected[:,3:],axis=1)
        assert np.isfinite(selected).all() and (early-late>=drop).all() and (late<=early*.8).all()
        assert (f.loc[f.deterioration,f'{prefix}_slope']<=slope).all()
        peer=m[[f'p90_{prefix}_{y}' for y in range(2021,2026)]].to_numpy()
        low=(a<.5*peer)&np.isfinite(a)&np.isfinite(peer)
        assert (low[f.low_performance].sum(axis=1)>=3).all() and low[f.low_performance,3:].all()
    data=json.loads((ROOT/'server_data/agent_v6/degradation.json').read_text(encoding='utf-8'))
    for scope,entry in data['scopes'].items():
        p=f[f.included & (True if scope=='lower_hrazdan' else f.activity_stage.eq(scope))]
        assert entry['candidate']['count']==p.candidate.sum()
        assert np.isclose(entry['candidate']['area_ha'],p.loc[p.candidate,'official_area_ha'].sum())
        assert set(entry['ids'])==set(p.loc[p.candidate,'public_parcel_id'])
        assert entry['included']['count']==sum(entry[k]['count'] for k in ['candidate','not_flagged','unassessed'])
        assert np.isclose(entry['included']['area_ha'],sum(entry[k]['area_ha'] for k in ['candidate','not_flagged','unassessed']))
    # Every pre-existing index value, including official area and all old classes, stays equal.
    with sqlite3.connect((ROOT/'server_data/agent_v1/parcels.sqlite3').as_uri()+'?mode=ro',uri=True) as c:a=pd.read_sql_query('SELECT * FROM parcels ORDER BY cadastre_code',c)
    with sqlite3.connect((ROOT/'server_data/agent_v6/parcels.sqlite3').as_uri()+'?mode=ro',uri=True) as c:b=pd.read_sql_query('SELECT * FROM parcels ORDER BY cadastre_code',c)
    pd.testing.assert_frame_equal(a,b[a.columns])
    report={'passed':True,'source_hashes_verified':True,'prior_index_values_unchanged':True,'included':int(mask.sum()),'candidate':int(f.candidate.sum()),'unassessed':int(f.state.eq('unassessed').sum()),'candidate_ha':float(f.loc[f.candidate,'official_area_ha'].sum()),'independent_accuracy_measured':False}
    path=ROOT/'server_data/agent_v6/analysis_verification.json';path.write_text(json.dumps(report,indent=2));print(json.dumps(report))

if __name__=='__main__':main()
