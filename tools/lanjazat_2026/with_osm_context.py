"""Add mapped orchard/vineyard context without changing the saved first screen."""
import hashlib
import json
from pathlib import Path
import numpy as np
import geopandas as gpd
import shapely
from shapely.geometry import Polygon, shape, mapping
from shapely.ops import unary_union
from pyproj import Transformer
from shapely.ops import transform
from analysis import OUT, build_weights, summarize, atomic_json


def agricultural_areas(boundary, weights, lc, source):
    nodes = {e['id']: (e['lon'], e['lat']) for e in source['elements'] if e['type'] == 'node'}
    geometries, audit, skipped = [], [], []
    for e in source['elements']:
        kind = e.get('tags', {}).get('landuse')
        if e['type'] != 'way' or kind not in ['farmland', 'orchard', 'vineyard']:
            continue
        try:
            coords = [nodes[n] for n in e['nodes']]
            if coords[0] != coords[-1]:
                raise ValueError('open_way')
            poly = Polygon(coords)
            if not poly.is_valid:
                raise ValueError('invalid_way')
            clipped = poly.intersection(boundary)
            if not clipped.is_empty and clipped.area > 0:
                geometries.append(clipped)
                audit.append({'osm_way': e['id'], 'landuse': kind})
        except (KeyError, ValueError) as exc:
            skipped.append({'osm_way': e['id'], 'reason': str(exc)})
    union = unary_union(geometries)
    project = Transformer.from_crs(4326, 32638, always_xy=True).transform
    # Clip again in the measurement CRS: projection changes straight-edge shapes.
    metric = transform(project, union).intersection(transform(project, boundary))
    pixel = weights['pixel']
    row, col = pixel // int(weights['width']), pixel % int(weights['width'])
    a, b, c, d, e, f = weights['transform'][:6]
    x, y = c + col*a, f + row*e
    cells = shapely.box(x, y+e, x+a, y)
    osm_area = shapely.area(shapely.intersection(cells, metric))
    area = weights['area']
    assert np.all(osm_area <= area+.01), 'OSM context escapes administrative boundary'
    osm_area = np.minimum(osm_area, area)
    ag = np.maximum(osm_area, np.where(lc == 40, area, 0))
    removed = float(ag[np.isin(lc, [50, 80])].sum()/10000)
    ag[np.isin(lc, [50, 80])] = 0
    return ag, {'included_osm_ways': audit, 'skipped_ways': skipped,
                'osm_context_ha_before_landcover_exclusion': metric.area/10000,
                'excluded_historical_built_water_ha': removed}, union


def main():
    first = json.loads((OUT/'result.json').read_text())
    feature = json.loads((OUT/'boundary.geojson').read_text(encoding='utf-8'))
    geom = shape(feature['geometry'])
    weights = build_weights(gpd.GeoDataFrame({'geometry': [geom]}, crs=4326), 20, 'EPSG:32638')
    with np.load(OUT/'worldcover_2021.npz') as data:
        lc = data['values']
    source = json.loads((OUT/'osm_landuse_source.json').read_text())
    ag, audit, context = agricultural_areas(geom, weights, lc, source)
    selected = json.loads((OUT/'scenes.json').read_text())['selected']
    samples = []
    for s in selected:
        with np.load(OUT/'samples'/f"{s['id']}.npz") as z:
            samples.append({k: z[k] for k in z.files})
    dates = [s['properties']['datetime'][:10] for s in selected]
    meteo = json.loads((OUT/'weather.json').read_text()) if (OUT/'weather.json').exists() else None
    metrics, rows, sensitivity = summarize(samples, dates, ag, np.full(len(ag), 40), meteo)
    result = {'version': 'lanjazat_admin_2026_screen_v2', 'as_of': first['as_of'],
              'observation_dates': dates,
              'boundary_ha': first['metrics']['boundary_ha'],
              'repeated_vegetation_ha_all_land': first['metrics']['repeated_vegetation_ha'],
              'recent_vegetation_ha_all_land': first['metrics']['recent_vegetation_ha'],
              'area_coverage_ha': first['metrics']['covered_ha'],
              'agricultural_context_ha': metrics['boundary_ha'],
              'active_vegetation_in_agricultural_context_ha': metrics['active_in_historical_cropland_ha'],
              'recent_vegetation_in_agricultural_context_ha': metrics['recent_vegetation_ha'],
              'irrigation_compatible_agricultural_vegetation_ha': metrics['summer_irrigation_compatible_proxy_ha'],
              'confirmed_irrigated_ha': None,
              'ag_context_insufficient_observations_ha': metrics['insufficient_observations_ha'],
              'activity_sensitivity_not_confidence_interval_ha': sensitivity,
              'context_method': 'Exact union of OSM farmland/orchard/vineyard areas with historical ESA WorldCover2021 class40 pixel areas, clipped to the administrative boundary. Historical class50 and class80 excluded.',
              'audit': audit, 'dates': rows,
              'limits': ['Mapped landuse is context, not a complete or current agricultural inventory.',
                        'Green vegetation in mapped agriculture can include weeds or abandoned orchards.',
                        'Irrigation-compatible growth can have other causes; actual irrigation is unconfirmed.',
                        'Sampling 13 dates can miss short cycles; hectares are approximate, not cadastral.',
                        'Public OSM tags and ESA WorldCover2021 can be outdated.',
                        'Hectare categories overlap; irrigation proxy is a subset of active agricultural vegetation.'],
              'source_hashes': {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in
                                [OUT/'boundary.geojson', OUT/'worldcover_2021.npz', OUT/'osm_landuse_source.json',
                                 OUT/'result.json', Path(__file__), Path(__file__).with_name('analysis.py')]}}
    assert result['active_vegetation_in_agricultural_context_ha'] <= result['agricultural_context_ha']+.001
    if result['irrigation_compatible_agricultural_vegetation_ha'] is not None:
        assert result['irrigation_compatible_agricultural_vegetation_ha'] <= result['active_vegetation_in_agricultural_context_ha']+.001
    atomic_json(OUT/'result_v2.json', result)
    atomic_json(OUT/'osm_agricultural_context.geojson', {'type': 'Feature', 'properties': {'source': 'OpenStreetMap, ODbL'}, 'geometry': mapping(context)})
    print(json.dumps({k:v for k,v in result.items() if k.endswith('_ha') or k == 'activity_sensitivity_not_confidence_interval_ha'}, indent=2))
    print('OSM ways: '+str(len(audit['included_osm_ways']))+'; skipped: '+str(len(audit['skipped_ways'])))


if __name__ == '__main__':
    main()
