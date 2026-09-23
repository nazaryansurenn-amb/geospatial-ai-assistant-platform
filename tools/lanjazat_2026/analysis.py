"""Isolated, bounded EO screening inside OSM's Lanjazat administrative area."""
from datetime import date, timedelta
import hashlib
import json
import math
from pathlib import Path
import sys
import time
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
import geopandas as gpd
import numpy as np
import pandas as pd
import requests
import rasterio
from rasterio.enums import Resampling
from rasterio.vrt import WarpedVRT
from rasterio.transform import Affine
from shapely.geometry import shape, mapping
from wp_core.data_collector.eo import normalize, cache_band, read_grid, SEARCH
from wp_core.data_collector.spatial import build_weights
from wp_core.data_collector.storage import atomic_json

HERE = Path(__file__).resolve().parent
OUT = HERE / 'output'
AS_OF = '2026-09-07'
VERSION = 'lanjazat_admin_2026_screen_v1'
BOUNDARY_ID = 15030684
WC = 'https://esa-worldcover.s3.eu-central-1.amazonaws.com/v200/2021/map/ESA_WorldCover_10m_2021_v200_N39E042_Map.tif'


def fetch(url, params=None):
    for attempt in range(3):
        try:
            r = requests.get(url, params=params, timeout=40,
                             headers={'User-Agent': 'LanjazatBoundaryReview/1.0 (local research)'})
            r.raise_for_status()
            return r.json()
        except requests.RequestException:
            if attempt == 2:
                raise
            time.sleep(2 ** attempt)


def boundary():
    path = OUT / 'boundary.geojson'
    if path.exists():
        return json.loads(path.read_text(encoding='utf-8'))
    rows = fetch('https://nominatim.openstreetmap.org/lookup',
                 {'osm_ids': 'R15030684', 'format': 'jsonv2', 'polygon_geojson': 1})
    row = rows[0]
    if row['osm_id'] != BOUNDARY_ID or row['type'] != 'administrative':
        raise ValueError('Unexpected administrative boundary')
    geom = shape(row['geojson'])
    if geom.geom_type not in ('Polygon', 'MultiPolygon') or not geom.is_valid:
        raise ValueError('Invalid administrative geometry')
    feature = {'type': 'Feature', 'properties': {
        'name': 'Lanjazat', 'osm_relation': BOUNDARY_ID, 'boundary': 'administrative',
        'source': 'https://www.openstreetmap.org/relation/15030684',
        'licence': 'ODbL, OpenStreetMap contributors', 'retrieved_at': date.today().isoformat()},
        'geometry': row['geojson']}
    atomic_json(path, feature)
    return feature


def scenes(geom):
    path = OUT / 'scenes.json'
    if path.exists():
        return json.loads(path.read_text())['selected']
    body = {'collections': ['sentinel-2-c1-l2a'], 'bbox': list(geom.bounds),
            'datetime': '2026-03-01T00:00:00Z/' + AS_OF + 'T23:59:59Z', 'limit': 100}
    all_items = []
    while True:
        r = requests.post(SEARCH, json=body, timeout=40)
        r.raise_for_status()
        page = r.json()
        all_items.extend(page['features'])
        link = next((x for x in page.get('links', []) if x['rel'] == 'next'), None)
        if not link:
            break
        if len(all_items) > 500 or not link.get('body'):
            raise ValueError('Unexpected discovery pagination')
        body.update(link['body'])
    buckets = {}
    for item in all_items:
        if not shape(item['geometry']).covers(geom):
            continue
        scene = normalize(item)
        d = scene['properties']['datetime'][:10]
        bucket = (d[:7], int(d[8:]) > 15)
        score = (float(scene['properties'].get('eo:cloud_cover', 100)), d, scene['id'])
        if bucket not in buckets or score < buckets[bucket][0]:
            buckets[bucket] = score, scene
    selected = sorted([v[1] for v in buckets.values()], key=lambda s: s['properties']['datetime'])
    if not selected:
        raise ValueError('No fully covering 2026 scenes found')
    atomic_json(path, {'selection': 'lowest scene cloud fraction in each half-month; all local clear pixels checked',
                      'found': len(all_items), 'selected': selected})
    return selected


def sample_scene(scene, weights, bounds):
    path = OUT / 'samples' / (scene['id'] + '.npz')
    if path.exists():
        with np.load(path) as d:
            return {k: d[k] for k in d.files}
    store = SimpleNamespace(root=HERE, config={'minimum_free_gb': 1})
    a = {}
    for name in ['red', 'nir', 'swir16', 'scl']:
        source = cache_band(store, scene, name, list(bounds))
        a[name] = read_grid(source, scene['assets'][name], weights, 'EPSG:32638', name == 'scl').ravel()[weights['pixel']]
    clear = np.isin(a['scl'], [4, 5, 6]) & np.isfinite(a['red']) & np.isfinite(a['nir']) & np.isfinite(a['swir16'])
    with np.errstate(divide='ignore', invalid='ignore'):
        ndvi = (a['nir']-a['red'])/(a['nir']+a['red'])
        ndmi = (a['nir']-a['swir16'])/(a['nir']+a['swir16'])
    clear &= np.isfinite(ndvi) & np.isfinite(ndmi) & (ndvi >= -1) & (ndvi <= 1) & (ndmi >= -1) & (ndmi <= 1)
    result = {'ndvi': np.where(clear, ndvi, np.nan).astype('float32'),
              'ndmi': np.where(clear, ndmi, np.nan).astype('float32'),
              'clear': clear, 'water': a['scl'] == 6}
    path.parent.mkdir(exist_ok=True)
    np.savez_compressed(path, **result)
    return result


def landcover(weights):
    path = OUT / 'worldcover_2021.npz'
    if path.exists():
        with np.load(path) as d:
            return d['values']
    with rasterio.Env(GDAL_DISABLE_READDIR_ON_OPEN='EMPTY_DIR', GDAL_HTTP_TIMEOUT=40,
                      GDAL_HTTP_MAX_RETRY=1, CPL_VSIL_CURL_ALLOWED_EXTENSIONS='.tif'):
        with rasterio.open(WC) as src:
            with WarpedVRT(src, crs='EPSG:32638', transform=Affine(*weights['transform'][:6]),
                           width=int(weights['width']), height=int(weights['height']),
                           resampling=Resampling.nearest) as vrt:
                values = vrt.read(1).ravel()[weights['pixel']]
    np.savez_compressed(path, values=values)
    return values


def weather(geom):
    path = OUT / 'weather.json'
    if path.exists():
        return json.loads(path.read_text())
    center = geom.centroid
    data = fetch('https://archive-api.open-meteo.com/v1/archive', {
        'latitude': center.y, 'longitude': center.x, 'start_date': '2026-03-01', 'end_date': AS_OF,
        'daily': 'precipitation_sum,et0_fao_evapotranspiration', 'models': 'ecmwf_ifs', 'timezone': 'Asia/Yerevan'})
    atomic_json(path, data)
    return data


def summarize(samples, dates, areas, lc, meteo):
    ndvi = np.stack([r['ndvi'] for r in samples])
    ndmi = np.stack([r['ndmi'] for r in samples])
    clear = np.stack([r['clear'] for r in samples])
    valid_count = clear.sum(axis=0)
    summer = np.array([d >= '2026-06-01' for d in dates])
    times = pd.to_datetime(dates).dayofyear.to_numpy()
    veg = clear & (ndvi >= .4)
    # Require repeated dates separated by a month, not a one-date green pixel.
    first = np.min(np.where(veg, times[:, None], 999), axis=0)
    last = np.max(np.where(veg, times[:, None], -999), axis=0)
    covered = (valid_count >= 4) & (clear[~summer].sum(axis=0) >= 1) & (clear[summer].sum(axis=0) >= 2)
    repeated = covered & (veg.sum(axis=0) >= 3) & ((last-first) >= 30)
    cropland = lc == 40
    active = repeated & cropland
    latest_i = np.max(np.where(clear, np.arange(len(samples))[:, None], -1), axis=0)
    recent = latest_i >= 0
    latest_veg = recent & (ndvi[np.maximum(latest_i, 0), np.arange(len(lc))] >= .4)
    max_doy = times.max()
    recent &= np.where(latest_i >= 0, times[np.maximum(latest_i, 0)], 0) >= max_doy - 20
    dry = []
    rain, eto = {}, {}
    if meteo:
        daily = meteo['daily']
        rain = dict(zip(daily['time'], daily['precipitation_sum']))
        eto = dict(zip(daily['time'], daily['et0_fao_evapotranspiration']))
    for d in dates:
        day = date.fromisoformat(d)
        preceding = [(day-timedelta(days=i)).isoformat() for i in range(1, 15)]
        vals = [(rain.get(k), eto.get(k)) for k in preceding]
        complete = all(r is not None and e is not None and r >= 0 and e >= 0 for r, e in vals)
        dry.append(d >= '2026-06-01' and complete and sum(e for _, e in vals) >= 35 and
                   sum(r for r, _ in vals) <= .35*sum(e for _, e in vals))
    dry = np.array(dry)
    supported = veg & (ndmi >= 0) & dry[:, None]
    sfirst = np.min(np.where(supported, times[:, None], 999), axis=0)
    slast = np.max(np.where(supported, times[:, None], -999), axis=0)
    irrig_proxy = active & (supported.sum(axis=0) >= 3) & (slast-sfirst >= 30)
    ha = lambda mask: round(float(areas[mask].sum()/10000), 3)
    metrics = {'boundary_ha': round(float(areas.sum()/10000), 3), 'historical_cropland_mask_ha': ha(cropland),
               'covered_ha': ha(covered), 'insufficient_observations_ha': ha(~covered),
               'repeated_vegetation_ha': ha(repeated), 'active_in_historical_cropland_ha': ha(active),
               'recent_vegetation_ha': ha(latest_veg & recent), 'recent_observation_coverage_ha': ha(recent),
               'summer_irrigation_compatible_proxy_ha': ha(irrig_proxy) if meteo else None,
               'confirmed_irrigated_ha': None, 'cropland_insufficient_observations_ha': ha(cropland & ~covered)}
    rows = []
    for i, d in enumerate(dates):
        rows.append({'date': d, 'clear_ha': ha(clear[i]), 'vegetated_clear_ha': ha(veg[i]),
                     'cropland_vegetated_clear_ha': ha(veg[i] & cropland), 'dry_context': bool(dry[i])})
    sensitivity = {}
    for t in [.35, .45]:
        hit = clear & (ndvi >= t)
        lo = np.min(np.where(hit, times[:, None], 999), axis=0)
        hi = np.max(np.where(hit, times[:, None], -999), axis=0)
        sensitivity[str(t)] = ha(covered & cropland & (hit.sum(axis=0) >= 3) & (hi-lo >= 30))
    assert metrics['active_in_historical_cropland_ha'] <= metrics['historical_cropland_mask_ha']+.001
    assert np.isclose(areas.sum()/10000, metrics['covered_ha']+metrics['insufficient_observations_ha'], atol=.002)
    return metrics, rows, sensitivity


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    feature = boundary(); geom = shape(feature['geometry'])
    frame = gpd.GeoDataFrame({'geometry': [geom]}, crs=4326)
    weights = build_weights(frame, 20, 'EPSG:32638')
    print(json.dumps({'boundary_ha': float(weights['area'].sum()/10000), 'pixels': len(weights['area'])}), flush=True)
    selected = scenes(geom)
    print('Selected '+str(len(selected))+' real scenes', flush=True)
    lc = landcover(weights)
    samples, dates = [], []
    for i, scene in enumerate(selected):
        d = scene['properties']['datetime'][:10]
        samples.append(sample_scene(scene, weights, geom.bounds)); dates.append(d)
        atomic_json(OUT/'progress.json', {'completed': i+1, 'total': len(selected), 'date': d})
        print(f'{i+1}/{len(selected)} {d}', flush=True)
    try:
        meteo = weather(geom)
    except Exception as exc:
        meteo = None
        print('Weather unavailable: '+str(exc), flush=True)
    metrics, rows, sensitivity = summarize(samples, dates, weights['area'], lc, meteo)
    result = {'analysis_version': VERSION, 'as_of': AS_OF, 'boundary': feature['properties'],
              'geometry_area_ha': float(frame.to_crs(32638).area.iloc[0]/10000), 'metrics': metrics,
              'dates': rows, 'sensitivity_not_confidence_interval': sensitivity,
              'weather_available': meteo is not None, 'method': {
                  'grid': '20m UTM38N; exact boundary pixel intersections, native Sentinel-2 bands',
                  'scene_selection': 'one lowest scene cloud product per half-month; no temporal filling',
                  'period': ['2026-03-01', AS_OF], 'landcover': 'ESA WorldCover 2021 v200 cropland class 40, historical prior only',
                  'vegetation': 'NDVI>=0.4 on >=3 clear dates spanning >=30d; >=4 valid dates with spring/summer support',
                  'irrigation_proxy': 'cropland activity plus NDVI>=0.4 and NDMI>=0 on >=3 dry-context summer dates spanning>=30d',
                  'dry_context': '14 antecedent days complete, ET0>=35mm and rain<=0.35*ET0, ECMWF IFS estimates',
                  'limitations': ['not cadastral or official hectares', 'not a complete 2026 cropland inventory',
                    '2021 cropland mask may omit orchards and land-use changes', 'vegetation does not prove cultivation',
                    'irrigation proxy is not confirmed irrigation; groundwater and rainfed growth remain possible',
                    '20m mixed pixels and selected-date coverage limit precision', 'OSM boundary is not a certified legal survey']},
              'sources': ['https://www.openstreetmap.org/relation/15030684',
                          'https://earth-search.aws.element84.com/v1', WC,
                          'https://open-meteo.com/en/docs/historical-weather-api'],
              'code_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest()}
    atomic_json(OUT/'result.json', result)
    pd.DataFrame(rows).to_csv(OUT/'dates.csv', index=False)
    print(json.dumps(metrics, indent=2), flush=True)


if __name__ == '__main__':
    main()
