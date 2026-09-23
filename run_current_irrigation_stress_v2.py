"""Current optical/weather screening and past-season checks over immutable parcels."""
from concurrent.futures import ThreadPoolExecutor,as_completed
from pathlib import Path
import hashlib,json,sqlite3,time
import numpy as np
import pandas as pd
import pyarrow.parquet as pq
from pyproj import Transformer
from shapely import points,STRtree
from run_observation_analysis import run_lock,write_json,write_table
from wp_core.current_irrigation_stress import seasonal_features,classify,median,INDICES

ROOT=Path(__file__).resolve().parent
CONFIG=ROOT/'config/irrigation_stress_20260907_v2.json';CFG=json.loads(CONFIG.read_text())
OUT=ROOT/'data/analysis/irrigation_stress'/CFG['version']
BASE=ROOT/'data/observations/observations_2021_2025_v1'
WEATHER=ROOT/'data/observations'/CFG['weather_version']
INDEX=ROOT/'server_data/agent_v6/parcels.sqlite3'

def sha(p):return hashlib.sha256(Path(p).read_bytes()).hexdigest()

def features(year,parcels,spatial,jobs):
    path=OUT/'features'/f'{year}.parquet';receipt=path.with_suffix('.json')
    if path.exists():
        assert sha(path)==json.loads(receipt.read_text())['sha256'];return pd.read_parquet(path)
    jobs=sorted([j for j in jobs if j['date']>=f'{year}-08-01' and j['date']<=f'{year}-09-05'],key=lambda j:(j['date'],j['id']))
    dates=sorted(set(j['date'] for j in jobs));assert dates
    di={d:i for i,d in enumerate(dates)};shape=(len(parcels),len(dates));score=np.full(shape,-1.)
    values={k:np.full(shape,np.nan) for k in INDICES};times=np.full(shape,-1,dtype=np.int64)
    columns=['cadastre_code','observation_date','observation_time_utc','radiometry','ndvi_mean','evi2_mean','ndmi_mean','vegetation_fraction','bare_fraction','water_fraction','ndvi_valid_fraction','evi2_valid_fraction','ndmi_valid_fraction','clear_fraction_10m','clear_fraction_20m','ndmi_count']
    pins={};q=CFG['quality']
    for j in jobs:
        p=ROOT/j['output'];assert sha(p)==j['sha256'];pins[j['output']]=j['sha256']
        f=pq.ParquetFile(p).read(columns=columns,use_threads=False).to_pandas()
        assert len(f)==22802 and f.cadastre_code.is_unique and f.observation_date.eq(j['date']).all()
        assert f.radiometry.isin(['c1_asset_scale_offset','legacy_already_offset']).all()
        f=f.set_index('cadastre_code').reindex(parcels.cadastre_code)
        support=f[['ndvi_valid_fraction','evi2_valid_fraction','ndmi_valid_fraction','clear_fraction_10m','clear_fraction_20m']].min(axis=1,skipna=False).to_numpy()
        good=spatial&(support>=q['valid_fraction'])&(f.water_fraction.to_numpy()<=q['maximum_water_fraction'])&(f.ndmi_count.to_numpy()>=3)
        good &= np.isfinite(f[['ndvi_mean','evi2_mean','ndmi_mean','vegetation_fraction','bare_fraction']]).all(axis=1).to_numpy()
        good &= f.ndvi_mean.between(-1,1).to_numpy()&f.ndmi_mean.between(-1,1).to_numpy()
        sc=np.where(good,support,-1);k=di[j['date']];better=sc>score[:,k]
        score[better,k]=sc[better]
        for name,column in zip(INDICES,['ndvi_mean','evi2_mean','ndmi_mean','vegetation_fraction','bare_fraction']):values[name][better,k]=f[column].to_numpy()[better]
        ts=pd.to_datetime(f.observation_time_utc,utc=True).astype('int64').to_numpy()//10**9
        times[better,k]=ts[better]
    a=seasonal_features(dates,values,score>=0,times,year,CFG)
    frame=pd.DataFrame({'cadastre_code':parcels.cadastre_code.to_numpy(),**a})
    write_table(path,frame);write_json(receipt,{'sha256':sha(path),'sources':pins,'scenes':len(jobs),'dates':dates})
    print(json.dumps({'features_year':year,'scenes':len(jobs),'covered':int(frame.covered.sum()),'currently_vegetated':int(frame.current_vegetation.sum())}),flush=True)
    return frame

def weather_features(year,f,parcels):
    mapping=pd.read_parquet(WEATHER/'parcel_points.parquet').set_index('cadastre_code').point_id.reindex(parcels.cadastre_code).to_numpy()
    h=pd.read_parquet(WEATHER/f'hourly_{year}.parquet');h['seconds']=h.time.astype('int64')//10**9
    weather={k:g.sort_values('seconds').set_index('seconds') for k,g in h.groupby('point_id')};cache={};w=CFG['weather']
    complete=np.ones(len(f),dtype=bool);support=np.ones(len(f),dtype=bool)
    detail={}
    for field in ['previous','last']:
        p14=np.full(len(f),np.nan);e14=p14.copy();p48=p14.copy()
        for i,(point,stamp) in enumerate(zip(mapping,f[field+'_time'])):
            key=(point,int(stamp))
            if key not in cache:
                if stamp<0:cache[key]=(False,np.nan,np.nan,np.nan)
                else:
                    end=(int(stamp)//3600)*3600
                    hours=np.arange(end-(w['days']*24-1)*3600,end+1,3600,dtype=np.int64)
                    sample=weather[point].reindex(hours)
                    ok=sample.complete.fillna(False).all() and sample.precipitation.notna().all() and sample.et0_fao_evapotranspiration.notna().all()
                    cache[key]=(bool(ok),float(sample.precipitation.sum()) if ok else np.nan,float(sample.et0_fao_evapotranspiration.sum()) if ok else np.nan,float(sample.precipitation.iloc[-w['recent_hours']:].sum()) if ok else np.nan)
            ok,p,e,r=cache[key];complete[i]&=ok;p14[i]=p;e14[i]=e;p48[i]=r
            support[i] &= ok and e>=w['minimum_et0_mm'] and p<=e*w['maximum_rain_et0_ratio'] and r<w['maximum_recent_rain_mm']
        detail.update({field+'_rain_14d_mm':p14,field+'_et0_14d_mm':e14,field+'_rain_48h_mm':p48})
    return complete,support,detail

def analyze(year,f,all_features,parcels,neighbors):
    count=np.zeros(len(f),dtype=int);change=np.full(len(f),np.nan);p=CFG['peers']
    for i,nb in enumerate(neighbors):
        if not f.stable_vegetation.iloc[i]:continue
        nb=nb[(nb!=i)&f.stable_vegetation.to_numpy()[nb]]
        for col,tolerance in [('base_ndvi',p['baseline_ndvi_difference']),('base_evi2',p['baseline_evi2_difference']),('base_vegetation',p['baseline_vegetation_difference']),('last_ndvi',p['recent_ndvi_difference']),('last_day',p['maximum_date_difference_days']),('previous_day',p['maximum_date_difference_days'])]:
            a=f[col].to_numpy();nb=nb[np.abs(a[nb]-a[i])<=tolerance]
        count[i]=len(nb)
        if len(nb)>=p['minimum']:change[i]=np.median(f.moisture_change.to_numpy()[nb])
    histories=[];hist_years=[];hc=CFG['history']
    for prior,g in sorted(all_features.items()):
        if prior>=year:continue
        good=g.stable_vegetation.to_numpy()&(np.abs(g.base_ndvi.to_numpy()-f.base_ndvi.to_numpy())<=hc['baseline_ndvi_difference'])&(np.abs(g.last_ndvi.to_numpy()-f.last_ndvi.to_numpy())<=hc['recent_ndvi_difference'])
        histories.append(np.where(good,g.moisture_change,np.nan));hist_years.append(prior)
    hist=np.column_stack(histories);hist_count=np.isfinite(hist).sum(1);hist_change=median(hist)
    complete,support,detail=weather_features(year,f,parcels)
    flags=classify({k:f[k].to_numpy() for k in f.columns if k!='cadastre_code'},change,count,hist_change,hist_count,complete,support,CFG)
    result=parcels[['cadastre_code','public_parcel_id','activity_stage','community','official_area_ha','minx','miny','maxx','maxy']].copy()
    result['current_vegetation']=f.current_vegetation.to_numpy()
    result['observation_date']=[str(np.datetime64(int(v),'D')) if v>=0 else None for v in f.last_day]
    result['previous_observation_date']=[str(np.datetime64(int(v),'D')) if v>=0 else None for v in f.previous_day]
    for k,v in flags.items():result[k]=v
    metrics=f.copy();metrics['peer_count']=count;metrics['peer_change']=change;metrics['historical_count']=hist_count;metrics['historical_change']=hist_change
    metrics['weather_complete']=complete;metrics['weather_support']=support
    for k,v in detail.items():metrics[k]=v
    dest=OUT if year==2026 else OUT/'backtests'/str(year)
    write_table(dest/'parcels.parquet',result);write_table(dest/'metrics.parquet',metrics)
    report={'year':year,'as_of':f'{year}-09-05','population':len(result),'states':result.state.value_counts().to_dict(),
            'current_vegetation':int(result.current_vegetation.sum()),'assessed':int(result.assessed.sum()),'candidates':int(result.candidate.sum()),
            'candidate_official_ha':float(result.loc[result.candidate,'official_area_ha'].sum()),'historical_reference_years':hist_years,
            'accuracy_verified':False,'weather_model':'ecmwf_ifs','weather_kind':'model_estimates','actual_irrigation_amount_calculated':False}
    write_json(dest/'report.json',report);print(json.dumps(report),flush=True)

def main():
    OUT.mkdir(parents=True,exist_ok=True)
    with run_lock(OUT/'run.lock'):
        receipt=OUT/'complete.json'
        if receipt.exists():
            for name,h in json.loads(receipt.read_text())['files'].items():assert sha(ROOT/name)==h,name
            print('Existing completed version verified');return
        start=time.monotonic()
        for name,h in json.loads((WEATHER/'complete.json').read_text())['files'].items():assert sha(ROOT/name)==h,name
        paths=[Path(__file__),ROOT/'wp_core/current_irrigation_stress.py',CONFIG,INDEX,BASE/'parcels.parquet',BASE/'sampling/exact_area_20m.npz',WEATHER/'complete.json']
        pins={p.relative_to(ROOT).as_posix():sha(p) for p in paths};manifest=OUT/'inputs.json'
        if manifest.exists():assert json.loads(manifest.read_text())==pins,'Inputs changed: use a new version'
        else:write_json(manifest,pins)
        with sqlite3.connect(INDEX.as_uri()+'?mode=ro',uri=True) as c:
            parcels=pd.read_sql_query("select * from parcels where scope='inside_I_II' and household=0 and road_excluded=0 order by cadastre_code",c)
        sampling=pd.read_parquet(BASE/'parcels.parquet').sort_values('internal_parcel_id').reset_index(drop=True)
        assert len(parcels)==22802 and set(parcels.cadastre_code)==set(sampling.cadastre_code)
        with np.load(BASE/'sampling/exact_area_20m.npz') as w:
            total=np.bincount(w['parcel'],weights=w['area'],minlength=len(sampling))
            square=np.bincount(w['parcel'],weights=w['area']**2,minlength=len(sampling))
        # Mixed boundary pixels are retained as shared screening evidence.
        # Require both physical sample area and effective number, so many tiny
        # intersections cannot make a tiny parcel spatially assessable.
        effective=np.divide(total**2,square,out=np.zeros_like(total),where=square>0)
        sampling['spatial_ok']=(total>=CFG['quality']['minimum_sampling_area_m2'])&(effective>=CFG['quality']['minimum_effective_pixels_20m'])
        spatial=parcels.cadastre_code.map(sampling.set_index('cadastre_code').spatial_ok).to_numpy()
        jobs=[]
        for slug in ['observations_2021_2025_v1',CFG['eo_version']]:
            db=ROOT/'server_data/collector'/slug/'jobs.sqlite3'
            with sqlite3.connect(db.as_uri()+'?mode=ro',uri=True) as c:
                c.row_factory=sqlite3.Row;part=[dict(r) for r in c.execute("select id,state,output,sha256,payload from jobs where kind='eo'")]
            assert all(j['state']=='complete' for j in part)
            for j in part:j['date']=json.loads(j.pop('payload'))['properties']['datetime'][:10]
            jobs.extend(part)
        frames={}
        with ThreadPoolExecutor(max_workers=CFG['workers']) as pool:
            futures={pool.submit(features,y,parcels,spatial,jobs):y for y in CFG['historical_years']+[2026]}
            for future in as_completed(futures):frames[futures[future]]=future.result()
        x,y=Transformer.from_crs(4326,32638,always_xy=True).transform((parcels.minx+parcels.maxx)/2,(parcels.miny+parcels.maxy)/2)
        xy=points(x,y);tree=STRtree(xy)
        neighbors=[np.sort(tree.query(point,predicate='dwithin',distance=CFG['peers']['radius_m'])) for point in xy]
        for year in CFG['backtest_years']+[2026]:analyze(year,frames[year],frames,parcels,neighbors)
        for p,h in pins.items():assert sha(ROOT/p)==h,p
        files={**pins,**{p.relative_to(ROOT).as_posix():sha(p) for p in OUT.rglob('*') if p.is_file() and p.name not in ['complete.json','run.lock']}}
        write_json(receipt,{'files':files,'seconds':round(time.monotonic()-start,2),'independent_accuracy_verified':False})
        print(json.dumps({'complete':True,'seconds':round(time.monotonic()-start,2)}))

if __name__=='__main__':main()
