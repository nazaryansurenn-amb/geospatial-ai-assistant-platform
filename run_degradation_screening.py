"""Read cached EO, run deterministic screening and preserve a new version."""
from concurrent.futures import ThreadPoolExecutor,as_completed
from pathlib import Path
import hashlib,json,sqlite3,time
import numpy as np
import pandas as pd
import pyarrow.parquet as pq
from shapely import points, STRtree
from wp_core.degradation_rules import annual_features,flags
from run_observation_analysis import run_lock,write_json,write_table
ROOT=Path(__file__).resolve().parent
CONFIG=ROOT/'config/degradation_screening_20260906_v1.json'
CFG=json.loads(CONFIG.read_text())
OUT=ROOT/'data/analysis/degradation'/CFG['version']
BASE=ROOT/'data/observations/observations_2021_2025_v1'
INDEX=ROOT/'server_data/agent_v1/parcels.sqlite3'
def digest(path):
    h=hashlib.sha256()
    with Path(path).open('rb') as f:
        for b in iter(lambda:f.read(4*1024*1024),b''):h.update(b)
    return h.hexdigest()

def year_features(year,parcels,jobs,quality_ok):
    output=OUT/'features'/f'{year}.parquet'
    receipt=OUT/'features'/f'{year}.json'
    if output.exists():
        assert digest(output)==json.loads(receipt.read_text())['output_sha256']
        return pd.read_parquet(output)
    selected=[j for j in jobs if j['date'].startswith(str(year)) and 3<=int(j['date'][5:7])<=11]
    dates=sorted(set(j['date'] for j in selected));di={d:i for i,d in enumerate(dates)}
    shape=(len(parcels),len(dates));n=np.full(shape,np.nan);e=n.copy();score=np.full(shape,-1.)
    ids=pd.Index(parcels.cadastre_code);pins={}
    columns=['cadastre_code','observation_date','radiometry','ndvi_mean','evi2_mean','ndvi_count','ndvi_valid_fraction','evi2_valid_fraction','clear_fraction_10m','water_fraction']
    for j in sorted(selected,key=lambda x:(x['date'],x['id'])):
        path=(ROOT/j['output']).resolve();assert path.is_relative_to(BASE/'eo')
        assert digest(path)==j['sha256'];pins[path.relative_to(ROOT).as_posix()]=j['sha256']
        f=pq.ParquetFile(path).read(columns=columns,use_threads=False).to_pandas()
        assert len(f)==CFG['population'] and f.cadastre_code.is_unique
        assert f.observation_date.eq(j['date']).all()
        assert f.radiometry.isin(['c1_asset_scale_offset','legacy_already_offset']).all()
        f=f.set_index('cadastre_code').reindex(ids)
        support=np.minimum(f.ndvi_valid_fraction.to_numpy(),f.evi2_valid_fraction.to_numpy())
        good=quality_ok&(support>=CFG['quality']['valid_fraction'])&(f.clear_fraction_10m.to_numpy()>=CFG['quality']['valid_fraction'])
        good &= (f.water_fraction.to_numpy()<=CFG['quality']['maximum_water_fraction'])&(f.ndvi_count.to_numpy()>=3)
        good &= f.ndvi_mean.between(-1,1).to_numpy()&f.evi2_mean.between(-1,2).to_numpy()
        sc=np.where(good,support,-1);k=di[j['date']];better=sc>score[:,k]
        score[better,k]=sc[better];n[better,k]=f.ndvi_mean.to_numpy()[better];e[better,k]=f.evi2_mean.to_numpy()[better]
    a=annual_features(dates,n,e,score>=0,year,CFG['quality'])
    frame=parcels[['cadastre_code']].copy();frame['year']=year
    for name in ['covered','ndvi','evi2','usable_dates','maximum_gap_days']:frame[name]=a[name]
    for m in range(9):
        frame[f'ndvi_month_{m+3}']=a['monthly_ndvi'][:,m];frame[f'evi2_month_{m+3}']=a['monthly_evi2'][:,m];frame[f'dates_month_{m+3}']=a['monthly_dates'][:,m]
    write_table(output,frame);write_json(receipt,{'year':year,'output_sha256':digest(output),'sources':pins,'covered':int(frame.covered.sum()),'distinct_dates':len(dates)})
    print(json.dumps({'year':year,'covered':int(frame.covered.sum()),'processed_scenes':len(selected)}),flush=True)
    return frame

def main():
    OUT.mkdir(parents=True,exist_ok=True)
    with run_lock(OUT/'run.lock'):
        complete=OUT/'complete.json'
        if complete.exists():
            receipt=json.loads(complete.read_text())
            for name,h in receipt['files'].items():assert digest(ROOT/name)==h,name
            print('Existing completed version verified');return
        start=time.monotonic()
        source_paths=[CONFIG,Path(__file__),ROOT/'wp_core/degradation_rules.py',INDEX,BASE/'parcels.parquet',BASE/'sampling/exact_area_10m.npz']
        pins={p.relative_to(ROOT).as_posix():digest(p) for p in source_paths}
        manifest=OUT/'inputs.json'
        if manifest.exists():assert json.loads(manifest.read_text())==pins,'Inputs changed; create a new version'
        else:write_json(manifest,pins)
        with sqlite3.connect(INDEX.as_uri()+'?mode=ro',uri=True) as c:
            parcels=pd.read_sql_query("SELECT * FROM parcels WHERE scope='inside_I_II' AND household=0 AND road_excluded=0 ORDER BY cadastre_code",c)
        assert len(parcels)==CFG['population']
        parcels['included']=parcels.annual_state_codes.map(lambda s:any(v in (1,2) for v in json.loads(s)))
        assert int(parcels.included.sum())==CFG['historically_active_population']
        sampling=pd.read_parquet(BASE/'parcels.parquet').sort_values('internal_parcel_id').reset_index(drop=True)
        w=np.load(BASE/'sampling/exact_area_10m.npz');total=np.bincount(w['parcel'],weights=w['area'],minlength=len(sampling))
        pure=w['area']>=90;full=np.bincount(w['parcel'][pure],minlength=len(sampling));full_area=np.bincount(w['parcel'][pure],weights=w['area'][pure],minlength=len(sampling))
        sampling['spatial_ok']=(full>=CFG['quality']['minimum_full_pixels'])&(full_area/total>=CFG['quality']['minimum_full_pixel_area_fraction'])
        quality_ok=parcels.cadastre_code.map(sampling.set_index('cadastre_code').spatial_ok).to_numpy(dtype=bool)
        with sqlite3.connect((ROOT/'server_data/collector/observations_2021_2025_v1/jobs.sqlite3').as_uri()+'?mode=ro',uri=True) as c:
            c.row_factory=sqlite3.Row;jobs=[dict(r) for r in c.execute("SELECT id,state,output,sha256,payload FROM jobs WHERE kind='eo'")]
        assert all(j['state']=='complete' for j in jobs)
        for j in jobs:j['date']=json.loads(j.pop('payload'))['properties']['datetime'][:10]
        frames={}
        with ThreadPoolExecutor(max_workers=CFG['workers']) as pool:
            futures={pool.submit(year_features,y,parcels,jobs,quality_ok):y for y in CFG['years']}
            for future in as_completed(futures):frames[futures[future]]=future.result()
        ndvi=np.column_stack([frames[y].ndvi for y in CFG['years']]);evi=np.column_stack([frames[y].evi2 for y in CFG['years']])
        # Project centroids only for distance comparisons; no geometry is changed.
        from pyproj import Transformer
        transform=Transformer.from_crs(4326,32638,always_xy=True)
        x,y=transform.transform((parcels.minx+parcels.maxx)/2,(parcels.miny+parcels.maxy)/2)
        locations=points(x,y);tree=STRtree(locations)
        neighbors=(np.sort(tree.query(point,predicate='dwithin',distance=CFG['peers']['radius_m'])) for point in locations)
        types=parcels.crop_type.fillna('undetermined').to_numpy();cycles=parcels.annual_cycle.fillna('').to_numpy()
        cohort=np.where(types=='annual','annual_'+cycles,types)
        med_n=np.full_like(ndvi,np.nan);med_e=med_n.copy();p90_n=med_n.copy();p90_e=med_n.copy();peer_counts=np.zeros_like(ndvi,dtype=int)
        included=parcels.included.to_numpy();minimum=CFG['peers']['minimum_peers']
        for i,nb in enumerate(neighbors):
            if not included[i]:continue
            nb=np.asarray(nb,dtype=int);nb=nb[(nb!=i)&included[nb]]
            matched=nb[cohort[nb]==cohort[i]] if types[i]!='undetermined' else np.array([],dtype=int)
            regional=matched if len(matched)>=minimum else nb
            for j in range(5):
                good=regional[np.isfinite(ndvi[regional,j])&np.isfinite(evi[regional,j])]
                if len(good)>=minimum:med_n[i,j]=np.median(ndvi[good,j]);med_e[i,j]=np.median(evi[good,j])
                good=matched[np.isfinite(ndvi[matched,j])&np.isfinite(evi[matched,j])];peer_counts[i,j]=len(good)
                if len(good)>=minimum:p90_n[i,j]=np.percentile(ndvi[good,j],90);p90_e[i,j]=np.percentile(evi[good,j],90)
        indicators=flags(ndvi,evi,med_n,med_e,p90_n,p90_e,CFG)
        result=parcels[['cadastre_code','public_parcel_id','activity_stage','community','official_area_ha','included','minx','miny','maxx','maxy']].copy()
        result['spatial_ok']=quality_ok
        for key,value in indicators.items():result[key]=value
        for key in ['deterioration_assessed','low_performance_assessed','deterioration','low_performance']:result[key]&=included
        result['candidate']=result.deterioration|result.low_performance
        result['assessed']=result.deterioration_assessed|result.low_performance_assessed
        result['state']=np.select([~included,result.candidate,~result.assessed],['outside_mask','candidate','unassessed'],default='not_flagged')
        write_table(OUT/'parcels.parquet',result)
        detail=result[['cadastre_code']].copy()
        for j,year in enumerate(CFG['years']):
            for key,array in [('ndvi',ndvi),('evi2',evi),('peer_ndvi',med_n),('peer_evi2',med_e),('p90_ndvi',p90_n),('p90_evi2',p90_e),('peer_count',peer_counts)]:detail[f'{key}_{year}']=array[:,j]
        write_table(OUT/'metrics.parquet',detail)
        report={'version':CFG['version'],'population':len(result),'included':int(included.sum()),'included_ha':float(result.loc[included,'official_area_ha'].sum()),
                'states':result.state.value_counts().to_dict(),'deterioration':int(result.deterioration.sum()),'low_performance':int(result.low_performance.sum()),'both':int((result.deterioration&result.low_performance).sum()),
                'deterioration_assessed':int(result.deterioration_assessed.sum()),'low_performance_assessed':int(result.low_performance_assessed.sum()),
                'candidate_ha':float(result.loc[result.candidate,'official_area_ha'].sum()),'seconds':round(time.monotonic()-start,2),'accuracy_verified':False}
        write_json(OUT/'report.json',report)
        for p,h in pins.items():assert digest(ROOT/p)==h,p
        files={**pins,**{p.relative_to(ROOT).as_posix():digest(p) for p in OUT.rglob('*') if p.is_file() and p.name not in ('run.lock','complete.json')}}
        write_json(complete,{'version':CFG['version'],'files':files,'accuracy_verified':False})
        print(json.dumps(report),flush=True)

if __name__=='__main__':main()
