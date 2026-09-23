"""Read-only cached-data expansion screen for the owner-selected 1 km search."""
from __future__ import annotations
import json
import sys
from pathlib import Path
import numpy as np
import pandas as pd
import geopandas as gpd
from affine import Affine
import shapely

ROOT=Path(__file__).resolve().parent
sys.path.insert(0,str(ROOT/'scripts'))
import prepare_land_activity_2026 as activity
from road_exclusions import build_road_exclusion_metrics
from wp_core.land_potential import samples, terrain_class, read, write, sha
SLUG='land_potential_expansion_1km_20260906_v1'
OUT=ROOT/'data/analysis/land_potential'/SLUG
INVENTORY=ROOT/'data/analysis/land_potential/expansion_inventory_20260906_v1/nearby_parcels_unassessed.parquet'


def prepare():
    if (OUT/'prefilter.parquet').exists():
        print(json.dumps(read(OUT/'prefilter_report.json')));return
    OUT.mkdir(parents=True,exist_ok=True)
    parcels=gpd.read_parquet(INVENTORY)
    assert len(parcels)==26123 and parcels.cadastre_code.is_unique
    arrays=dict(np.load(activity.GRID_CACHE_SOURCE,allow_pickle=False))
    transform=Affine(*read(activity.GRID_METADATA_SOURCE)['transform'])
    stats=activity.extract_activity_from_grid(parcels,arrays,transform)
    f=parcels.merge(stats,on='cadastre_code',validate='one_to_one')
    roads,quality=build_road_exclusion_metrics(parcels,gpd.read_file(activity.ROAD_SOURCE))
    f=f.merge(roads,on='cadastre_code',validate='one_to_one')
    prior=pd.read_parquet(activity.LAND_REVIEW_SOURCE).set_index('cadastre_code')
    known_house=f.cadastre_code.map(prior.household_agriculture).eq(True)
    small=(f.area_official_m2<=activity.MAX_HOUSEHOLD_PLOT_AREA_M2)&(f.osm_urban_fraction>=activity.MIN_HOUSEHOLD_URBAN_CONTEXT_FRACTION)&((f.osm_urban_fraction>=activity.MIN_HOUSEHOLD_URBAN_DOMINANT_FRACTION)|(f.worldcover_built_fraction>=activity.MIN_HOUSEHOLD_BUILT_FRACTION))
    extended=f.area_official_m2.between(activity.MAX_HOUSEHOLD_PLOT_AREA_M2,activity.MAX_EXTENDED_HOUSEHOLD_PLOT_AREA_M2,inclusive='right')&(f.osm_urban_fraction>=activity.MIN_EXTENDED_HOUSEHOLD_URBAN_FRACTION)&(f.worldcover_built_fraction>=activity.MIN_EXTENDED_HOUSEHOLD_BUILT_FRACTION)
    f['household_agriculture']=(known_house|small|extended)&~f.road_excluded
    f['greenhouse_review']=f.cadastre_code.map(prior.greenhouse_review_candidate).eq(True)
    f['current_supported']=f.eo_pixel_count.ge(activity.MINIMUM_PIXEL_COUNT)&f.raw_usable_fraction.ge(activity.MINIMUM_USABLE_FRACTION)&f.within_saved_terrain_grid
    f['eligible']=~(f.household_agriculture|f.road_excluded)
    f['prefilter_pass']=f.eligible&f.current_supported&f.raw_active_fraction.lt(activity.MINIMUM_PARTIAL_ACTIVITY_FRACTION)&~f.greenhouse_review
    f['potential_class']='not_candidate'
    f['reason']='current_activity'
    f.loc[f.eligible&~f.current_supported,['potential_class','reason']]=['review','current_coverage_or_spatial_limit']
    f.loc[f.household_agriculture,['potential_class','reason']]=['excluded','household_excluded']
    f.loc[f.road_excluded,['potential_class','reason']]=['excluded','road_excluded']
    f.loc[f.eligible&f.greenhouse_review,['potential_class','reason']]=['review','greenhouse_review']
    f.loc[f.prefilter_pass,['potential_class','reason']]=['review','pending_history']
    metric=f.to_crs(32638)
    for n,idx in enumerate(f.index[f.prefilter_pass],1):
        rr,cc,w=samples(metric.loc[idx].geometry,transform,arrays['elevation'].shape)
        coverage=w.sum()/metric.loc[idx].geometry.area
        wc=arrays['worldcover'][rr,cc]
        open_fraction=w[np.isin(wc,[30,40,60])].sum()/w.sum() if w.sum() else 0
        built_water=w[np.isin(wc,[50,80])].sum()/w.sum() if w.sum() else 1
        f.loc[idx,'open_fraction']=open_fraction
        f.loc[idx,'built_water_fraction']=built_water
        if coverage<.95 or open_fraction<.8 or built_water>.1:
            f.loc[idx,'prefilter_pass']=False;f.loc[idx,'reason']='landcover_constraint_or_mixed_cover'
        if n%250==0:print(json.dumps({'prefilter_measured':n}),flush=True)
    f['public_parcel_id']=f.cadastre_code.map(activity.public_parcel_id).astype('int64')
    f.to_parquet(OUT/'prefilter.parquet',index=False)
    chosen=f[f.prefilter_pass].copy()
    chosen.to_parquet(OUT/'history_scope.parquet',index=False)
    report={'search_buffer_m':1000,'outside_parcels':len(f),'household_excluded':int(f.household_agriculture.sum()),'road_excluded':int(f.road_excluded.sum()),'eligible':int(f.eligible.sum()),'history_to_process':len(chosen),'reason_counts':f.reason.value_counts().to_dict()}
    write(OUT/'prefilter_report.json',report)
    write(OUT/'prefilter_inputs.json',{str(p.relative_to(ROOT)):sha(p) for p in [INVENTORY,activity.GRID_CACHE_SOURCE,activity.GRID_METADATA_SOURCE,activity.ROAD_SOURCE,activity.LAND_REVIEW_SOURCE,ROOT/'scripts/prepare_land_activity_2026.py',ROOT/'scripts/road_exclusions.py']})
    print(json.dumps(report),flush=True)


if __name__=='__main__':
    if len(sys.argv)>1 and sys.argv[1]=='history':
        from wp_core.land_potential_expansion_eo import run
        run(workers=4)
    else:
        prepare()
