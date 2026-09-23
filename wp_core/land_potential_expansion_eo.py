"""Offline expansion observations using the existing parcel EO numerical functions."""
from __future__ import annotations
import json
import os
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path
import numpy as np
import pandas as pd
import geopandas as gpd
import rasterio
from rasterio.vrt import WarpedVRT
from rasterio.enums import Resampling
from rasterio.features import rasterize
from affine import Affine
from .land_potential import read, write, sha
from .parcel_eo import aggregate_optical_scene_by_parcel
from .observation_rules import coverage, Policy

ROOT=Path(__file__).resolve().parents[1]
OUT=ROOT/'data/analysis/land_potential/land_potential_expansion_1km_20260906_v1'
META=ROOT/'data/source/activity_2026/lower_hrazdan_eo_terrain_grid_2026.json'
FIELDS=None
LABELS=None
COUNTS=None
TRANSFORM=None


def initialize():
    global FIELDS,LABELS,COUNTS,TRANSFORM
    os.environ['GDAL_NUM_THREADS']='1'
    FIELDS=pd.read_parquet(OUT/'history_scope.parquet',columns=['cadastre_code'])
    LABELS=np.load(OUT/'history_labels.npy',mmap_mode='r')
    COUNTS=np.bincount(LABELS.ravel(),minlength=len(FIELDS)+1)[1:]
    TRANSFORM=Affine(*read(META)['transform'])


def scene_job(scene):
    dest=OUT/'observations'/f"{scene['id']}.parquet"
    audit=dest.with_suffix('.json')
    if dest.exists() and audit.exists():
        a=read(audit);assert sha(dest)==a['output_sha256'];return a
    paths=list((ROOT/'data/cache/copernicus').glob(f"*/{scene['collection']}/{scene['id']}/red.tif"))
    if len(paths)!=1:raise ValueError('Expected one local cached scene: '+scene['id'])
    directory=paths[0].parent
    arrays={}; inputs={}
    for band in ['red','green','blue','nir','swir16','scl']:
        path=directory/f'{band}.tif'
        sidecar=read(path.with_suffix('.json'))
        digest=sha(path)
        assert digest==sidecar['sha256']
        inputs[str(path.relative_to(ROOT))]=digest
        with rasterio.open(path) as source:
            with WarpedVRT(source,crs='EPSG:32638',transform=TRANSFORM,width=LABELS.shape[1],height=LABELS.shape[0],
                           resampling=Resampling.nearest if band=='scl' else Resampling.bilinear,
                           dtype='float32',nodata=np.nan) as vrt:
                pixels=vrt.read(1)
        if band=='scl':
            # Reuse the stricter existing screening mask; missing pixels stay invalid.
            arrays[band]=np.where(np.isin(pixels,[4,5]),pixels,0).astype('uint8')
        else:
            asset=scene['assets'][band]
            assert asset['scale']==sidecar['scale'] and asset['offset']==sidecar['offset']
            arrays[band]=pixels*asset['scale']+asset['offset']
    frame=aggregate_optical_scene_by_parcel(cadastre_codes=FIELDS.cadastre_code.tolist(),labels=LABELS,
            parcel_pixel_count=COUNTS,**arrays)
    frame['scene_id']=scene['id']
    frame['observation_date']=pd.Timestamp(scene['properties']['datetime']).date().isoformat()
    frame.to_parquet(dest,index=False)
    record={'scene_id':scene['id'],'rows':len(frame),'inputs':inputs,'output_sha256':sha(dest)}
    write(audit,record)
    return record


def run(workers=4):
    if (OUT/'history_complete.json').exists():
        print(json.dumps(read(OUT/'history_report.json')));return
    fields=gpd.read_parquet(OUT/'history_scope.parquet')
    meta=read(META)
    label_path=OUT/'history_labels.npy'
    if not label_path.exists():
        labels=rasterize([(g,i+1) for i,g in enumerate(fields.to_crs(32638).geometry)],
              out_shape=(meta['height'],meta['width']),transform=Affine(*meta['transform']),fill=0,dtype='int32')
        np.save(label_path,labels)
    scenes=read(ROOT/'data/observations/observations_2021_2025_v1/scene_manifest.json')['scenes']
    scenes=[s for s in scenes if 3<=pd.Timestamp(s['properties']['datetime']).month<=11]
    (OUT/'observations').mkdir(exist_ok=True)
    policy={'years':[2021,2022,2023,2024,2025],'valid_fraction':.6,'minimum_observations':16,'maximum_gap_days':35,
       'minimum_years':3,'latest_completed_years':[2024,2025],'bare_observation_fraction':.6,'vegetation_observation_fraction_max':.2,
       'no_activity_year_fraction':.75,'maximum_used_years':1,'scl_classes':[4,5],'current_rule':'same existing 2026 activity and household/road functions',
       'measurement':'existing aggregate_optical_scene_by_parcel; native cached radiometry; 20m bands resampled to existing 10m calculation grid',
       'terrain_rule':'same sealed inner-area terrain comparator','no_new_downloads':True,'no_crop_identification':True,
       'scope':'outside saved I+II parcel register, whole parcels within 1000m of saved boundary'}
    write(OUT/'history_policy.json',policy)
    print(json.dumps({'history_parcels':len(fields),'cached_scenes':len(scenes),'workers':workers}),flush=True)
    audits=[]
    with ProcessPoolExecutor(max_workers=workers,initializer=initialize) as pool:
        for i,audit in enumerate(pool.map(scene_job,scenes,chunksize=1),1):
            audits.append(audit)
            if i%20==0 or i==len(scenes):
                progress={'complete_scenes':i,'total_scenes':len(scenes),'parcels':len(fields)}
                write(OUT/'progress.json',progress);print(json.dumps(progress),flush=True)
    write(OUT/'cache_audit.json',audits)
    observations=pd.concat([pd.read_parquet(OUT/'observations'/f"{s['id']}.parquet") for s in scenes],ignore_index=True)
    observations['observation_date']=pd.to_datetime(observations.observation_date)
    # One actual scene per parcel/date, selected by greatest clear coverage.
    daily=observations.sort_values(['cadastre_code','observation_date','valid_fraction','scene_id'],ascending=[True,True,False,True]).drop_duplicates(['cadastre_code','observation_date'])
    daily.to_parquet(OUT/'daily.parquet',index=False)
    records=[]
    codes=fields.cadastre_code.tolist()
    for y in range(2021,2026):
        yr=daily[daily.observation_date.dt.year.eq(y)]
        dates=sorted(yr.observation_date.unique())
        matrices={k:yr.pivot(index='cadastre_code',columns='observation_date',values=k).reindex(index=codes,columns=dates).to_numpy(float)
                  for k in ['valid_fraction','ndvi_mean','vegetation_fraction','bare_fraction','valid_pixel_count']}
        good=(matrices['valid_fraction']>=.6)&(matrices['valid_pixel_count']>=3)&np.isfinite(matrices['ndvi_mean'])
        days=pd.DatetimeIndex(dates).dayofyear.to_numpy()
        cov,seasonal=coverage(days,good,y,Policy())
        counts=seasonal.sum(axis=1)
        veg=np.divide(((matrices['vegetation_fraction']>=.15)&seasonal).sum(axis=1),counts,out=np.full(len(codes),np.nan),where=counts>0)
        bare=np.divide(((matrices['bare_fraction']>=.5)&seasonal).sum(axis=1),counts,out=np.full(len(codes),np.nan),where=counts>0)
        no_activity=cov['covered']&(bare>=.6)&(veg<=.2)
        states=np.where(~cov['covered'],0,np.where(no_activity,3,2))
        for i,code in enumerate(codes):
            records.append({'cadastre_code':code,'year':y,'annual_state':int(states[i]),'covered':bool(cov['covered'][i]),
                  'quality_reason':cov['quality_reason'][i],'usable_dates':int(counts[i]),'maximum_gap_days':int(cov['maximum_gap_days'][i]),
                  'bare_observation_rate':float(bare[i]),'vegetation_observation_rate':float(veg[i])})
    seasons=pd.DataFrame(records)
    seasons.to_parquet(OUT/'seasons.parquet',index=False)
    states=seasons.pivot(index='cadastre_code',columns='year',values='annual_state').reindex(codes)
    from .land_potential import history_pass
    selected=states.apply(lambda r:history_pass([int(x) for x in r]),axis=1)
    result=fields[['cadastre_code']].copy()
    result['history_pass']=result.cadastre_code.map(selected)
    result['annual_state_codes']=result.cadastre_code.map(states.apply(lambda r:json.dumps([int(x) for x in r]),axis=1))
    result.to_parquet(OUT/'history_results.parquet',index=False)
    report={'parcels_processed':len(fields),'scenes':len(scenes),'parcel_observation_rows':len(observations),
            'distinct_parcel_dates':len(daily),'five_year_nonuse_candidates':int(selected.sum()),
            'covered_parcel_seasons':int(seasons.covered.sum()),'total_parcel_seasons':len(seasons)}
    write(OUT/'history_report.json',report)
    outputs={p.name:sha(p) for p in [OUT/'daily.parquet',OUT/'seasons.parquet',OUT/'history_results.parquet',OUT/'history_policy.json',OUT/'cache_audit.json']}
    write(OUT/'history_complete.json',{'outputs':outputs,'code_sha256':sha(Path(__file__))})
    print(json.dumps(report),flush=True)
