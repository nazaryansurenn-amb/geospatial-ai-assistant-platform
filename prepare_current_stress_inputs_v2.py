"""Additive recent-season EO preparation; never rewrites historical collectors."""
from concurrent.futures import ThreadPoolExecutor,as_completed
from pathlib import Path
import json,time,sqlite3
from urllib.parse import urlparse
import numpy as np
import pandas as pd
import requests
from wp_core.data_collector.storage import Store,digest,atomic_json
from wp_core.data_collector.eo import SEARCH,request,normalize,process_scene
ROOT=Path(__file__).resolve().parent
BASE=ROOT/'data/observations/observations_2021_2025_v1'
VERSION='current_stress_inputs_20260907_v2'
PERIOD='2026-08-01T00:00:00Z/2026-09-06T23:59:59Z'

def main():
    config=json.loads((ROOT/'config/data_collector.json').read_text())
    config.update(version=VERSION,years=[2026],purpose='Recent optical inputs only; stress classifications are not calculated here',period=PERIOD)
    store=Store(ROOT,config)
    with store.lock():
        scope=json.loads((BASE/'specification.json').read_text())['scope']
        paths=[Path(__file__),ROOT/'config/data_collector.json',BASE/'parcels.parquet']+[BASE/f'sampling/exact_area_{m}m.npz' for m in [10,20]]
        paths += [ROOT/f'wp_core/data_collector/{name}.py' for name in ['eo','indices','spatial','storage']]
        pins={p.relative_to(ROOT).as_posix():digest(p) for p in paths}
        store.pin({'version':VERSION,'period':PERIOD,'scope':scope,'config':config,'source_sha256':pins})
        parcels=pd.read_parquet(BASE/'parcels.parquet').sort_values('internal_parcel_id').reset_index(drop=True)
        with sqlite3.connect((ROOT/'server_data/agent_v6/parcels.sqlite3').as_uri()+'?mode=ro',uri=True) as c:
            eligible={r[0] for r in c.execute("SELECT cadastre_code FROM parcels WHERE scope='inside_I_II' AND household=0 AND road_excluded=0")}
        assert len(parcels)==22802 and set(parcels.cadastre_code)==eligible
        sampling={}
        for resolution in [10,20]:
            p=BASE/f'sampling/exact_area_{resolution}m.npz'
            assert digest(p)==json.loads(p.with_suffix('.json').read_text())['sha256']
            with np.load(p) as arrays:sampling[resolution]={k:arrays[k] for k in arrays.files}
        manifest=store.base/'scene_manifest.json'
        if not manifest.exists():
            selected={};excluded=[];session=requests.Session()
            for collection in config['eo_collections']:
                body={'collections':[collection],'bbox':scope['bounds_wgs84'],'datetime':PERIOD,'limit':100}
                url,method,payload=SEARCH,'POST',body
                seen=set();items=[]
                while url:
                    assert urlparse(url).hostname=='earth-search.aws.element84.com'
                    marker=json.dumps([url,payload],sort_keys=True)
                    assert marker not in seen,'Catalogue pagination repeated'
                    seen.add(marker)
                    page=request(session,method,url,**({'json':payload} if method=='POST' else {})).json()
                    items.extend(page['features'])
                    link=next((v for v in page.get('links',[]) if v.get('rel')=='next'),None)
                    if not link:break
                    url,method=link['href'],link.get('method','GET')
                    payload={**body,**link.get('body',{})} if link.get('merge') else link.get('body')
                for scene in items:
                    try:s=normalize(scene)
                    except ValueError as e:excluded.append({'id':scene['id'],'reason':str(e)});continue
                    p=s['properties'];tile=p.get('grid:code') or s['id'].split('_')[1].lstrip('T')
                    key=(p['datetime'][:19],str(tile).replace('MGRS-',''))
                    priority=(collection=='sentinel-2-c1-l2a',float(p.get('s2:processing_baseline',0)),s['id'])
                    if key not in selected or priority>selected[key][0]:selected[key]=(priority,s)
            scenes=sorted([r[1] for r in selected.values()],key=lambda s:(s['properties']['datetime'],s['id']))
            assert scenes and scenes[-1]['properties']['datetime'][:10]=='2026-09-05'
            atomic_json(manifest,{'period':PERIOD,'scenes':scenes,'excluded':excluded,'scene_count':len(scenes),'latest_catalogue_date':scenes[-1]['properties']['datetime'][:10]})
        scenes=json.loads(manifest.read_text())['scenes']
        for scene in scenes:store.add('eo_'+scene['id'],'eo',scene)
        store.recover()
        def work(job):
            if job['state']=='complete':
                assert digest(ROOT/job['output'])==job['sha256'];return
            store.transition(job['id'],'running',attempts=job['attempts']+1)
            start=time.monotonic()
            try:
                info=process_scene(store,json.loads(job['payload']),parcels,sampling,scope)
                store.transition(job['id'],'complete',elapsed=time.monotonic()-start,**info)
                print(json.dumps({'scene':job['id'],'completed_rows':info['rows'],'seconds':round(time.monotonic()-start,1)}),flush=True)
            except Exception as e:
                store.transition(job['id'],'failed',error=type(e).__name__)
                raise
        print(json.dumps({'version':VERSION,'scenes':len(scenes),'latest_catalogue_date':'2026-09-05','workers':2}),flush=True)
        with ThreadPoolExecutor(max_workers=2) as pool:
            for future in as_completed([pool.submit(work,j) for j in store.jobs('eo')]):future.result()
        for p,h in pins.items():assert digest(ROOT/p)==h,p
        status=store.status();assert status['complete']
        # Quality inventory only: no irrigation-stress class or threshold is assigned.
        rows=[]
        for job in store.jobs('eo'):
            f=pd.read_parquet(ROOT/job['output'],columns=['observation_date','ndvi_mean','ndmi_mean','ndmi_valid_fraction','clear_fraction_20m'])
            usable=f.ndvi_mean.notna()&f.ndmi_mean.notna()&f.ndmi_valid_fraction.ge(.8)&f.clear_fraction_20m.ge(.8)
            rows.append({'scene':job['id'],'date':str(f.observation_date.iloc[0]),'rows':len(f),'usable_moisture_screening_rows':int(usable.sum())})
        atomic_json(store.base/'readiness.json',{'version':VERSION,'period':PERIOD,'scope_parcels':len(parcels),'scenes':rows,'stress_calculated':False,'weather_choice_pending':True,'previous_data_unchanged':True})
        print(json.dumps({'complete':True,'scenes':len(rows),'latest_date':max(v['date'] for v in rows),'stress_calculated':False}),flush=True)
if __name__=='__main__':main()
