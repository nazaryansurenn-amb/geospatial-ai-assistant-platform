"""Independent reconciliation of frozen stress evidence, identities and delivery."""
from pathlib import Path
import hashlib,json,sqlite3
import numpy as np
import pandas as pd
ROOT=Path(__file__).resolve().parent
OUT=ROOT/'data/analysis/irrigation_stress/irrigation_stress_20260907_v2'

def main():
    for slug in ['irrigation_stress_20260907_v1','irrigation_stress_20260907_v2']:
        for name,h in json.loads((OUT.parent/slug/'complete.json').read_text())['files'].items():assert hashlib.sha256((ROOT/name).read_bytes()).hexdigest()==h,name
    with sqlite3.connect('file:server_data/agent_v6/parcels.sqlite3?mode=ro',uri=True) as c:
        old=pd.read_sql_query('select * from parcels order by cadastre_code',c)
    with sqlite3.connect('file:server_data/agent_v7/parcels.sqlite3?mode=ro',uri=True) as c:
        new=pd.read_sql_query('select * from parcels order by cadastre_code',c)
    pd.testing.assert_frame_equal(old,new[old.columns])
    baseline=old[(old.scope=='inside_I_II')&(old.household==0)&(old.road_excluded==0)].set_index('cadastre_code')
    f=pd.read_parquet(OUT/'parcels.parquet').set_index('cadastre_code');m=pd.read_parquet(OUT/'metrics.parquet').set_index('cadastre_code')
    assert f.index.is_unique and set(f.index)==set(baseline.index) and len(f)==22802
    for k in ['public_parcel_id','official_area_ha','minx','miny','maxx','maxy']:pd.testing.assert_series_equal(f[k],baseline[k],check_names=False)
    c=f[f.candidate];mm=m.loc[c.index]
    assert len(c)==43 and abs(c.official_area_ha.sum()-15.092812)<1e-9
    assert (c.assessed&c.current_vegetation).all()
    assert (mm.base_ndmi-mm.last_ndmi>=.08).all() and (mm.base_ndmi-mm.previous_ndmi>=.08).all()
    assert (mm.moisture_change-mm.peer_change<=-.05).all() and (mm.moisture_change-mm.historical_change<=-.04).all()
    assert mm.peer_count.ge(20).all() and mm.historical_count.ge(2).all()
    assert (mm.weather_complete&mm.weather_support&mm.stable_vegetation).all()
    assert pd.to_datetime(c.observation_date).between('2026-09-02','2026-09-05').all()
    assert ((pd.to_datetime(c.observation_date)-pd.to_datetime(c.previous_observation_date)).dt.days>=3).all()
    # Recompute the actual antecedent weather sums independently of the runner.
    wroot=ROOT/'data/observations/current_stress_weather_20260907_v1'
    h=pd.read_parquet(wroot/'hourly_2026.parquet');mapping=pd.read_parquet(wroot/'parcel_points.parquet').set_index('cadastre_code').point_id
    for code,row in mm.iterrows():
        for prefix in ['last','previous']:
            end=pd.Timestamp(int(row[prefix+'_time']),unit='s',tz='UTC').floor('h')
            g=h[(h.point_id==mapping[code])&(h.time<=end)&(h.time>end-pd.Timedelta(days=14))].sort_values('time')
            assert len(g)==336 and g.complete.all()
            assert g.time.max()<=pd.Timestamp(int(row[prefix+'_time']),unit='s',tz='UTC')
            assert abs(g.precipitation.sum()-row[prefix+'_rain_14d_mm'])<1e-6
            assert abs(g.et0_fao_evapotranspiration.sum()-row[prefix+'_et0_14d_mm'])<1e-6
            assert g.precipitation.iloc[-48:].sum()<5 and g.precipitation.sum()<=g.et0_fao_evapotranspiration.sum()*.5
    for year in [2024,2025]:
        r=json.loads((OUT/'backtests'/str(year)/'report.json').read_text())
        assert all(y<year for y in r['historical_reference_years']) and r['population']==22802 and not r['accuracy_verified']
    payload=json.loads((ROOT/'server_data/agent_v7/irrigation_stress.json').read_text())
    for scope,s in payload['scopes'].items():
        part=f if scope=='lower_hrazdan' else f[f.activity_stage==scope]
        assert s['ids']==[int(x) for x in part.loc[part.candidate,'public_parcel_id']]
        assert s['included']['count']==s['assessed']['count']+s['unassessed']['count']+s['not_current_vegetation']['count']
        assert s['assessed']['count']==s['candidate']['count']+s['not_flagged']['count']
    report={'passed':True,'population':22802,'candidates':43,'official_ha':15.092812,'old_index_columns_equal':True,'antecedent_weather_recomputed':True,'historical_no_future_reference':True,'independent_field_accuracy':False}
    (ROOT/'server_data/agent_v7/analysis_verification.json').write_text(json.dumps(report,indent=2));print(json.dumps(report))
if __name__=='__main__':main()
