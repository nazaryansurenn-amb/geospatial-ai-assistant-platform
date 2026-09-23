"""Independent arithmetic checks over saved observations; no downloads."""
import json
import hashlib
from datetime import date, timedelta
import numpy as np
import geopandas as gpd
from shapely.geometry import shape
from analysis import OUT, build_weights, atomic_json
from with_osm_context import agricultural_areas

feature = json.loads((OUT/'boundary.geojson').read_text(encoding='utf-8'))
boundary = shape(feature['geometry'])
frame = gpd.GeoDataFrame({'geometry': [boundary]}, crs=4326)
weights = build_weights(frame, 20, 32638)
areas = weights['area']
result = json.loads((OUT/'result_v2.json').read_text())
with np.load(OUT/'worldcover_2021.npz') as f:
    lc = f['values']
ag, audit, _ = agricultural_areas(boundary, weights, lc, json.loads((OUT/'osm_landuse_source.json').read_text()))
scenes = json.loads((OUT/'scenes.json').read_text())['selected']
dated = [(s['properties']['datetime'][:10], s['id']) for s in scenes]
assert len(dated) == len(set(d for d, _ in dated)) == 13
assert all('2026-03-01' <= d <= result['as_of'] for d, _ in dated)
count = np.zeros(len(ag), int); spring = count.copy(); summer = count.copy()
hits = count.copy(); wet_hits = count.copy()
lo = np.full(len(ag), 999999); hi = np.zeros(len(ag), int)
wlo = lo.copy(); whi = hi.copy()
weather = json.loads((OUT/'weather.json').read_text())['daily']
rain = dict(zip(weather['time'], weather['precipitation_sum']))
eto = dict(zip(weather['time'], weather['et0_fao_evapotranspiration']))
for d, sid in dated:
    with np.load(OUT/'samples'/f'{sid}.npz') as f:
        clear = f['clear']; ndvi = f['ndvi']; ndmi = f['ndmi']
    t = date.fromisoformat(d).toordinal()
    count += clear
    if d < '2026-06-01': spring += clear
    else: summer += clear
    green = clear & (ndvi >= .4)
    hits += green; lo = np.where(green, np.minimum(lo, t), lo); hi = np.where(green, t, hi)
    days = [(date.fromisoformat(d)-timedelta(days=i)).isoformat() for i in range(1,15)]
    good = all(rain.get(k) is not None and eto.get(k) is not None for k in days)
    dry = good and sum(eto[k] for k in days) >= 35 and sum(rain[k] for k in days) <= .35*sum(eto[k] for k in days)
    matched = green & (ndmi >= 0) & dry & (d >= '2026-06-01')
    wet_hits += matched; wlo = np.where(matched, np.minimum(wlo,t),wlo); whi = np.where(matched,t,whi)
covered = (count >= 4) & (spring >= 1) & (summer >= 2)
active = covered & (hits >= 3) & (hi-lo >= 30)
proxy = active & (wet_hits >= 3) & (whi-wlo >= 30)
checks = {
    'boundary_ha': areas.sum()/10000,
    'agricultural_context_ha': ag.sum()/10000,
    'active_vegetation_in_agricultural_context_ha': ag[active].sum()/10000,
    'irrigation_compatible_agricultural_vegetation_ha': ag[proxy].sum()/10000,
    'repeated_vegetation_ha_all_land': areas[active].sum()/10000,
}
for key, value in checks.items():
    assert abs(result[key]-value) <= .00051, (key, result[key], value)
assert np.isclose(areas.sum(), frame.to_crs(32638).area.iloc[0], atol=.01)
assert np.all((ag >= 0) & (ag <= areas+.0001))
assert result['confirmed_irrigated_ha'] is None
for filename, expected in result['source_hashes'].items():
    p = OUT/filename
    if not p.exists(): p = OUT.parent/filename
    assert hashlib.sha256(p.read_bytes()).hexdigest() == expected, filename
atomic_json(OUT/'verification.json', {'passed': True, 'checks': checks, 'scenes': len(dated),
    'geometry_valid': boundary.is_valid, 'administrative_relation': 15030684,
    'source_hashes_match': True, 'no_scope_escape_or_double_count': True,
    'classification_accuracy_verified': False, 'actual_irrigation_verified': False})
print(json.dumps({'passed': True, 'checks': checks}, indent=2))
