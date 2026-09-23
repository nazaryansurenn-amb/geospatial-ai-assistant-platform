"""Finish the 1 km candidates and create a separate additive review delivery."""
from __future__ import annotations
import json
import shutil
import sqlite3
import sys
from pathlib import Path
import numpy as np
import pandas as pd
import geopandas as gpd
import shapely
from shapely.geometry import box
from shapely.ops import nearest_points
from affine import Affine
import mapbox_vector_tile
from mapbox_vector_tile.encoder import on_invalid_geometry_make_valid

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from wp_core.land_potential import samples,terrain_class,history_pass,read,write,sha
from prepare_land_analytics_delivery import tile_range,tile_bounds_3857,tile_width_m
SLUG='land_potential_1km_review_20260906_v1'
EXTRA=ROOT/'data/analysis/land_potential/land_potential_expansion_1km_20260906_v1'
INNER=ROOT/'data/analysis/land_potential/land_potential_20260906_v1'
REVIEW=ROOT/'server_data/review'/SLUG
FRONTEND=ROOT/'output'/f'frontend_{SLUG}'
SOURCE=REVIEW/'frontend_source'
PREVIOUS=ROOT/'output/frontend_land_potential_20260906_v1'
INDEX=ROOT/'server_data'/f'land_analytics_{SLUG}.sqlite3'
SUMMARY=ROOT/'server_data'/f'land_analytics_summary_{SLUG}.json'


def summarize(f):
    result={}
    for scope in ['lower_hrazdan','stage_1','stage_2']:
        g=f if scope=='lower_hrazdan' else f[f.stage.eq(scope)]
        counts={k:int(g.potential_class.eq(k).sum()) for k in ['gravity_candidate','mechanical_candidate','review','not_candidate','excluded']}
        result[scope]={'eligible_parcel_count':int(g.eligible.sum()),'candidate_count':counts['gravity_candidate']+counts['mechanical_candidate'],
                       'class_counts':counts,'candidate_official_area_ha':float(g.loc[g.potential_class.isin(['gravity_candidate','mechanical_candidate']),'area_official_m2'].sum()/10000)}
    return result


def finish_analysis():
    if (EXTRA/'complete.json').exists():return
    hc=read(EXTRA/'history_complete.json')
    for n,h in hc['outputs'].items():assert sha(EXTRA/n)==h
    f=gpd.read_parquet(EXTRA/'prefilter.parquet')
    h=pd.read_parquet(EXTRA/'history_results.parquet')
    f=f.merge(h,on='cadastre_code',how='left',validate='one_to_one')
    selected=f.history_pass.eq(True)
    seasons=pd.read_parquet(EXTRA/'seasons.parquet')
    recent=seasons[seasons.year.isin([2024,2025])].groupby('cadastre_code').covered.all()
    assessed=seasons.groupby('cadastre_code').covered.sum()
    f.loc[f.prefilter_pass&~selected,'reason']='history_activity_or_insufficient_history'
    f.loc[f.prefilter_pass&~selected&f.cadastre_code.map(recent).eq(True)&f.cadastre_code.map(assessed).ge(3),'potential_class']='not_candidate'
    metric=f.to_crs(32638)
    canals=gpd.read_file(ROOT/'data/source/lower_hrazdan.geojson').to_crs(32638)
    lines={s:shapely.union_all(g.geometry.values) for s,g in canals.groupby('stage')}
    representatives=shapely.point_on_surface(metric.geometry.values)
    distances=np.column_stack([shapely.distance(representatives,lines[s]) for s in ['stage_1','stage_2']])
    f['stage']=np.where(distances[:,0]<=distances[:,1],'stage_1','stage_2')
    f['scope']='nearby_1km'
    z=np.load(ROOT/'data/source/activity_2026/lower_hrazdan_eo_terrain_grid_2026.npz',allow_pickle=False)
    elevation=z['elevation']; transform=Affine(*read(ROOT/'data/source/activity_2026/lower_hrazdan_eo_terrain_grid_2026.json')['transform'])
    for n,idx in enumerate(f.index[selected],1):
        geom=metric.loc[idx].geometry
        rr,cc,w=samples(geom,transform,elevation.shape)
        ref=nearest_points(geom.representative_point(),lines[f.loc[idx,'stage']])[1]
        r2,c2,w2=samples(box(ref.x-45,ref.y-45,ref.x+45,ref.y+45),transform,elevation.shape)
        kind,reason='review','canal_reference_outside_saved_dem'
        if len(w2) and w2.sum()/8100>=.95 and w.sum()/geom.area>=.95:
            vals,cv=elevation[rr,cc],elevation[r2,c2]
            pmin,pmax,cmin,cmax=map(float,[vals.min(),vals.max(),cv.min(),cv.max()])
            f.loc[idx,['parcel_elevation_min','parcel_elevation_max','canal_elevation_min','canal_elevation_max']]=[pmin,pmax,cmin,cmax]
            kind,reason=terrain_class(pmin,pmax,cmin,cmax)
        f.loc[idx,['potential_class','reason']]=[kind,reason]
        f.loc[idx,['canal_reference_x','canal_reference_y']]=[ref.x,ref.y]
    f.to_parquet(EXTRA/'all_parcels.parquet',index=False)
    selected=f.potential_class.isin(['gravity_candidate','mechanical_candidate'])
    f.loc[selected,['cadastre_code','area_official_m2','stage','potential_class','boundary_distance_m']].to_csv(EXTRA/'candidates.csv',index=False,encoding='utf-8-sig')
    inner=gpd.read_parquet(INNER/'all_parcels.parquet')
    assert set(f.cadastre_code).isdisjoint(inner.cadastre_code)
    original=gpd.read_parquet(ROOT/'data/analysis/land_potential/expansion_inventory_20260906_v1/nearby_parcels_unassessed.parquet').set_index('cadastre_code')
    assert f.geometry.to_wkb().tolist()==original.loc[f.cadastre_code].geometry.to_wkb().tolist()
    assert f.area_official_m2.tolist()==original.loc[f.cadastre_code].area_official_m2.tolist()
    assert f.loc[selected,'eligible'].all() and f.loc[selected,'prefilter_pass'].all() and f.loc[selected,'history_pass'].all()
    report={'version':SLUG,'search_buffer_m':1000,'inside':summarize(inner),'outside':summarize(f),
            'combined':summarize(pd.concat([inner,f],ignore_index=True)), 'outside_reason_counts':f.reason.value_counts().to_dict(),
            'outside_search_parcels':len(f),'inner_results_unchanged':True,'hydraulic_feasibility_confirmed':False}
    write(EXTRA/'report.json',report)
    write(EXTRA/'complete.json',{'outputs':{n:sha(EXTRA/n) for n in ['all_parcels.parquet','candidates.csv','report.json']}})
    REVIEW.mkdir(parents=True,exist_ok=True)
    inner_candidates=inner[inner.potential_class.isin(['gravity_candidate','mechanical_candidate'])].copy();inner_candidates['scope']='inside_I_II'
    combined=pd.concat([inner_candidates[['cadastre_code','area_official_m2','stage','potential_class','scope']],f.loc[selected,['cadastre_code','area_official_m2','stage','potential_class','scope']]])
    combined.to_csv(REVIEW/'all_candidates.csv',index=False,encoding='utf-8-sig')
    print(json.dumps(report),flush=True)


def extra_tiles(f):
    target=REVIEW/'expansion_tiles';target.mkdir(exist_ok=False)
    g=f[f.potential_class.isin(['gravity_candidate','mechanical_candidate','review'])].to_crs(3857).reset_index(drop=True)
    index=g.sindex; bounds=tuple(g.total_bounds)
    counts={}
    for zoom in range(10,16):
        xs,ys=tile_range(bounds,zoom);count=0
        for x in xs:
            for y in ys:
                b=tile_bounds_3857(zoom,x,y)
                candidates=g.iloc[index.query(box(*b),predicate='intersects')]
                buffer=tile_width_m(zoom)*8/4096
                clip=box(b[0]-buffer,b[1]-buffer,b[2]+buffer,b[3]+buffer)
                features=[]
                for row in candidates.itertuples(index=False):
                    geom=row.geometry.intersection(clip)
                    if geom.is_empty:continue
                    # Quantized display copies only; source cadastral geometries stay immutable.
                    features.append({'id':int(row.public_parcel_id),'geometry':geom,
                        'properties':{'potential_class':row.potential_class,'stage':row.stage,'scope':'nearby_1km'}})
                payload=mapbox_vector_tile.encode({'name':'potential_expansion','features':features},default_options={'quantize_bounds':b,'extents':4096,'on_invalid_geometry':on_invalid_geometry_make_valid})
                p=target/str(zoom)/str(x)/f'{y}.pbf';p.parent.mkdir(parents=True,exist_ok=True);p.write_bytes(payload);count+=1
        counts[str(zoom)]=count
    return {'url':f'/data/potential_expansion/{SLUG}/{{z}}/{{x}}/{{y}}.pbf','layer_name':'potential_expansion','min_zoom':10,'max_zoom':15,
            'bounds':[float(x) for x in g.to_crs(4326).total_bounds],'tile_counts':counts}


def prepare_delivery():
    finish_analysis()
    if SOURCE.exists() or INDEX.exists():raise ValueError('New preparation already exists')
    f=gpd.read_parquet(EXTRA/'all_parcels.parquet')
    report=read(EXTRA/'report.json')
    tiles=extra_tiles(f)
    shutil.copy2(ROOT/'server_data/land_analytics_land_potential_20260906_v1.sqlite3',INDEX)
    with sqlite3.connect(INDEX) as con:
        f[['cadastre_code','potential_class','household_agriculture','road_excluded']].to_sql('potential_expansion',con,index=False)
        con.execute('CREATE UNIQUE INDEX potential_expansion_code ON potential_expansion(cadastre_code)')
    summary=read(ROOT/'server_data/land_analytics_summary_land_potential_20260906_v1.json')
    summary['delivery_version']=SLUG
    summary['potential']['expansion_assessed']=True
    summary['potential']['scope']='saved_I_II_plus_1km_search'
    summary['potential']['expansion']={'search_buffer_m':1000,'search_parcels':len(f),'summaries':report['outside'],'combined_summaries':report['combined'],'tile_delivery':tiles}
    write(SUMMARY,summary)
    shutil.copytree(ROOT/'server_data/review/land_potential_20260906_v1/frontend_source',SOURCE)
    shutil.copy2(ROOT/'scripts/land_potential_expanded_PotentialView.jsx',SOURCE/'src/PotentialView.jsx')
    config=(SOURCE/'vite.config.mjs').read_text();config=config.replace(str(PREVIOUS).replace('\\','\\\\'),str(FRONTEND).replace('\\','\\\\'))
    assert str(FRONTEND).replace('\\','\\\\') in config
    (SOURCE/'vite.config.mjs').write_text(config)
    print('Expansion review prepared',flush=True)


def finish_delivery():
    assert (FRONTEND/'index.html').exists()
    shutil.copytree(PREVIOUS/'data',FRONTEND/'data')
    shutil.copytree(REVIEW/'expansion_tiles',FRONTEND/'data/potential_expansion'/SLUG)
    with sqlite3.connect(INDEX.as_uri()+'?mode=ro',uri=True) as c:
        new=pd.read_sql_query('SELECT * FROM parcel_analytics ORDER BY cadastre_code',c)
    with sqlite3.connect((ROOT/'server_data/land_analytics_land_potential_20260906_v1.sqlite3').as_uri()+'?mode=ro',uri=True) as c:
        old=pd.read_sql_query('SELECT * FROM parcel_analytics ORDER BY cadastre_code',c)
    pd.testing.assert_frame_equal(new,old)
    previous_summary=read(ROOT/'server_data/land_analytics_summary_land_potential_20260906_v1.json')
    current_summary=read(SUMMARY)
    for key in ['activity','history','land_use_type','annual_cycles','tile_delivery']:
        assert current_summary[key]==previous_summary[key]
    outside=gpd.read_parquet(EXTRA/'all_parcels.parquet')
    lookup={int(r.public_parcel_id):r for r in outside.itertuples(index=False)}
    seen=set();tile_count=0
    for p in (FRONTEND/'data/potential_expansion'/SLUG).rglob('*.pbf'):
        decoded=mapbox_vector_tile.decode(p.read_bytes())['potential_expansion']
        for f in decoded['features']:
            row=lookup[f['id']];assert f['properties']=={'potential_class':row.potential_class,'stage':row.stage,'scope':'nearby_1km'}
            seen.add(f['id'])
        tile_count+=1
    expected=set(outside.loc[outside.potential_class.isin(['gravity_candidate','mechanical_candidate','review']),'public_parcel_id'])
    assert seen==expected
    for name in ['transitions_area_20260906_v1','land_potential_20260906_v1']:
        for n,r in read(ROOT/'config'/f'{name}.review.lock.json')['files'].items():assert sha(ROOT/n)==r['sha256'],n
    write(REVIEW/'delivery_verification.json',{'status':'passed','all_inner_rows_unchanged':True,'public_extra_ids':len(seen),'extra_tiles':tile_count,'previous_releases_sealed':True})
    paths=[ROOT/'app.py',ROOT/'run_land_potential_extended_review.py',INDEX,SUMMARY,ROOT/'server_data/cadastre_search.sqlite3',*[p for p in FRONTEND.rglob('*') if p.is_file()]]
    write(ROOT/'config'/f'{SLUG}.review.lock.json',{'version':SLUG,'port':8526,'files':{p.relative_to(ROOT).as_posix():{'bytes':p.stat().st_size,'sha256':sha(p)} for p in paths}})


if __name__=='__main__':
    {'analysis':finish_analysis,'prepare':prepare_delivery,'finish':finish_delivery}[sys.argv[1]]()
