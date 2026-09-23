"""Versioned public ECMWF IFS model estimates for current and historical checks."""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import hashlib,json,sqlite3,time
import numpy as np
import pandas as pd
import requests
from run_observation_analysis import run_lock,write_json,write_table

ROOT=Path(__file__).resolve().parent
CFG=json.loads((ROOT/'config/irrigation_stress_20260907_v1.json').read_text())
OUT=ROOT/'data/observations'/CFG['weather_version']
VARIABLES=['temperature_2m','relative_humidity_2m','dew_point_2m','precipitation','shortwave_radiation','wind_speed_10m','surface_pressure','et0_fao_evapotranspiration']

def sha(p):return hashlib.sha256(Path(p).read_bytes()).hexdigest()

def main():
    OUT.mkdir(parents=True,exist_ok=True)
    with run_lock(OUT/'run.lock'):
        complete=OUT/'complete.json'
        if complete.exists():
            for name,h in json.loads(complete.read_text())['files'].items():assert sha(ROOT/name)==h,name
            print('Existing weather verified');return
        with sqlite3.connect('file:server_data/agent_v6/parcels.sqlite3?mode=ro',uri=True) as c:
            p=pd.read_sql_query("select cadastre_code,minx,miny,maxx,maxy from parcels where scope='inside_I_II' and household=0 and road_excluded=0 order by cadastre_code",c)
        # Public coarse sample coordinates only are sent, never cadastral records.
        p['latitude']=np.round((p.miny+p.maxy)/2,1);p['longitude']=np.round((p.minx+p.maxx)/2,1)
        points=p[['latitude','longitude']].drop_duplicates().sort_values(['latitude','longitude']).reset_index(drop=True)
        points['point_id']=['p'+str(i) for i in range(len(points))]
        p=p.merge(points,on=['latitude','longitude'],validate='many_to_one')
        write_table(OUT/'parcel_points.parquet',p[['cadastre_code','point_id']])
        write_table(OUT/'points.parquet',points)
        def fetch(year):
            raw=OUT/f'raw_{year}.json'
            params={'latitude':','.join(map(str,points.latitude)),'longitude':','.join(map(str,points.longitude)),
                    'models':'ecmwf_ifs','start_date':f'{year}-08-01','end_date':f'{year}-09-06',
                    'hourly':','.join(VARIABLES),'timezone':'UTC','wind_speed_unit':'ms',
                    'elevation':','.join(['nan']*len(points)),'cell_selection':'nearest'}
            if raw.exists():
                receipt=json.loads(raw.with_suffix('.receipt.json').read_text());assert sha(raw)==receipt['sha256']
                data=json.loads(raw.read_text())
            else:
                for attempt in range(3):
                    r=requests.get('https://archive-api.open-meteo.com/v1/archive',params=params,timeout=90)
                    if r.status_code==200:break
                    if attempt==2:r.raise_for_status()
                    time.sleep(2)
                data=r.json();write_json(raw,data)
                write_json(raw.with_suffix('.receipt.json'),{'params':params,'sha256':sha(raw),'fetched_utc':pd.Timestamp.now(tz='UTC').isoformat(),'source':'ECMWF IFS model estimates assembled by Open-Meteo; not ERA5 or station observations'})
            assert isinstance(data,list) and len(data)==len(points)
            frames=[];grid=[]
            for point,response in zip(points.itertuples(),data):
                assert response['utc_offset_seconds']==0
                assert response['hourly_units']['precipitation']=='mm'
                assert response['hourly_units']['et0_fao_evapotranspiration']=='mm'
                f=pd.DataFrame(response['hourly']);f['point_id']=point.point_id
                f['time']=pd.to_datetime(f.time,utc=True);assert f.time.is_unique
                for k in VARIABLES:f[k]=pd.to_numeric(f[k],errors='coerce')
                f['complete']=f[VARIABLES].notna().all(axis=1)
                assert (f.loc[f.complete,'precipitation']>=0).all()
                assert (f.loc[f.complete,'et0_fao_evapotranspiration']>=0).all()
                frames.append(f)
                grid.append({'point_id':point.point_id,'native_latitude':response['latitude'],'native_longitude':response['longitude'],'units':response['hourly_units']})
            f=pd.concat(frames,ignore_index=True);write_table(OUT/f'hourly_{year}.parquet',f)
            write_json(OUT/f'grid_{year}.json',grid)
            print(json.dumps({'weather_year':year,'points':len(points),'complete_hours':int(f.complete.sum()),'last_complete_utc':str(f.loc[f.complete,'time'].max())}),flush=True)
        with ThreadPoolExecutor(max_workers=2) as pool:list(pool.map(fetch,CFG['backtest_years']+[CFG['current_year']]))
        files={p.relative_to(ROOT).as_posix():sha(p) for p in OUT.rglob('*') if p.is_file() and p.name not in ['complete.json','run.lock']}
        files[Path(__file__).relative_to(ROOT).as_posix()]=sha(__file__)
        files['config/irrigation_stress_20260907_v1.json']=sha(ROOT/'config/irrigation_stress_20260907_v1.json')
        write_json(complete,{'files':files,'model':'ecmwf_ifs','source_kind':'weather_model_estimate','forecast_risk_calculated':False})

if __name__=='__main__':main()
