"""Offline control-120 experiment. Does not publish, download or change releases."""
from __future__ import annotations

import argparse
from dataclasses import asdict
import html
import importlib.metadata
import json
from pathlib import Path
import platform
import re
import time

import geopandas as gpd
import numpy as np
import pandas as pd
import rasterio
import shapely

from release_tools import local_path, sha256
from run_observation_analysis import run_lock, write_json, write_table, now
from wp_core.data_collector.eo import read_grid
from wp_core.data_collector.spatial import summarize
from wp_core.data_collector.storage import json_hash
from wp_core.observation_screening import Policy, classify_year, predominant

ROOT = Path(__file__).resolve().parent
CORE = ('ndvi', 'evi2', 'ndmi', 'bsi', 'ndre')
BASIS = 'data/analysis/parcel_eo/current_halo_v1/current_parcels.parquet'


def read_json(path):
    return json.loads(path.read_text(encoding='utf-8'))


def environment():
    return {'python': platform.python_version(), 'platform': platform.platform(),
            'gdal': rasterio.__gdal_version__,
            **{name: importlib.metadata.version(name) for name in
               ('numpy', 'pandas', 'pyarrow', 'rasterio', 'geopandas', 'shapely')}}


def pin(root, paths):
    return {p: sha256(local_path(root, p)) for p in sorted(set(paths))}


def prepare(root, config_path):
    config = read_json(local_path(root, config_path))
    if not re.fullmatch(r'[A-Za-z0-9_-]+', config['version']):
        raise ValueError('Invalid version')
    if config['years'] != list(range(2021, 2026)) or config['scope'] != 'control_120':
        raise ValueError('Only the completed five-year control experiment is authorized')
    if config['publication'] != 'internal_draft_only':
        raise ValueError('No publication from this runner')
    policy = Policy(**config['policy'])
    source = local_path(root, 'data/observations/'+config['input_version'])
    baseline = local_path(root, config['baseline'])
    manifest = read_json(baseline/'manifest.json')
    for path, digest in {**manifest['code_sha256'], **manifest['input_pins']}.items():
        if sha256(local_path(root, path)) != digest:
            raise ValueError('Pinned baseline changed: '+path)
    spec = read_json(source/'specification.json')
    if sha256(local_path(root, BASIS)) != spec['scope']['basis_sha256']:
        raise ValueError('Immutable geometry changed')
    for path, digest in spec['code_sha256'].items():
        if sha256(local_path(root, 'wp_core/data_collector/'+path)) != digest:
            raise ValueError('Pinned collector changed: '+path)
    population = pd.read_parquet(source/'parcels.parquet').sort_values('internal_parcel_id').reset_index(drop=True)
    sample = pd.read_parquet(baseline/'review_sample.parquet').sort_values('internal_parcel_id').reset_index(drop=True)
    known = pd.read_parquet(local_path(root, config['calibration_sample']))
    if len(sample) != 120 or not sample.internal_parcel_id.is_unique or not sample.cadastre_code.is_unique:
        raise ValueError('Invalid control population')
    if len(known) != 25 or not set(known.internal_parcel_id).issubset(sample.internal_parcel_id):
        raise ValueError('Invalid known25 split')
    if len(population) != 22802 or not population.internal_parcel_id.is_unique:
        raise ValueError('Unexpected source population')
    if sample[['household', 'road_excluded', 'accepted', 'training_eligible']].any().any():
        raise ValueError('Excluded parcel or accepted label entered the experiment')
    sample['control_split'] = np.where(sample.internal_parcel_id.isin(known.internal_parcel_id), 'known25', 'remaining95')
    geo = gpd.read_parquet(local_path(root, BASIS)).set_index('internal_parcel_id').loc[sample.internal_parcel_id]
    if geo.crs.to_epsg() != 4326 or not geo.is_valid.all() or geo.geometry.is_empty.any():
        raise ValueError('Invalid immutable cadastral basis')
    if not np.array_equal(geo.cadastre_code, sample.cadastre_code) or not np.array_equal(geo.area_official_m2, sample.area_official_m2):
        raise ValueError('Code/official area changed')
    geo = geo.to_crs(32638)
    scenes = read_json(source/'scene_manifest.json')['scenes']
    scene_map = {s['id']: s for s in scenes}
    jobs = manifest['eo_scenes']
    if len(jobs) != 762 or len(scene_map) != len(scenes) or set(j['scene_id'] for j in jobs) != set(scene_map):
        raise ValueError('Incomplete scene catalog')
    code_paths = [p.relative_to(root).as_posix() for p in (root/'wp_core/data_collector').glob('*.py')]
    code_paths += ['run_observation_screening.py', 'wp_core/observation_screening.py',
                   'wp_core/observation_rules.py', 'run_observation_analysis.py', 'release_tools.py']
    inputs = [BASIS, config_path, config['calibration_sample'], config['baseline']+'/manifest.json',
              config['baseline']+'/review_sample.parquet', config['baseline']+'/parcel_results.parquet',
              'config/releases.json', 'wp_core/use_type_release.py']
    inputs += [(source/name).relative_to(root).as_posix() for name in
               ('scope.parquet', 'parcels.parquet', 'specification.json', 'scene_manifest.json',
                'sampling/exact_area_10m.npz', 'sampling/exact_area_20m.npz')]
    for p in sorted(baseline.rglob('*.parquet')):
        inputs.append(p.relative_to(root).as_posix())
    pinned = {'config': config, 'effective_policy': asdict(policy), 'environment': environment(),
              'code_sha256': pin(root, code_paths), 'input_sha256': pin(root, inputs),
              'scope': spec['scope'], 'legacy_250m_used': False, 'old_labels_used_as_truth': False,
              'external_requests': False, 'approval': 'not_approved', 'accuracy': 'not_measured'}
    out = local_path(root, 'data/analysis/observation_screening/'+config['version'])
    out.mkdir(parents=True, exist_ok=True)
    return config, policy, source, sample, population, geo, jobs, scene_map, spec, pinned, out


def spatial_weights(source, population, sample, geo):
    positions = pd.Index(population.internal_parcel_id).get_indexer(sample.internal_parcel_id)
    if (positions < 0).any():
        raise ValueError('Sample absent from input EO population')
    lookup = np.full(len(population), -1, dtype=np.int32)
    lookup[positions] = np.arange(len(sample))
    static = sample[['internal_parcel_id', 'cadastre_code', 'activity_stage', 'area_official_m2', 'control_split']].copy()
    static['geometry_area_m2'] = geo.area.to_numpy()
    def minimum_width(g):
        xy = np.array(g.minimum_rotated_rectangle.exterior.coords)
        return float(np.linalg.norm(np.diff(xy, axis=0), axis=1).min())
    static['minimum_width_m'] = geo.geometry.map(minimum_width).to_numpy()
    static['area_pixel_equivalents_10m'] = static.geometry_area_m2/100
    grids = {}
    for res in (10, 20):
        path = source/f'sampling/exact_area_{res}m.npz'
        if sha256(path) != read_json(path.with_suffix('.json'))['sha256']:
            raise ValueError('Sampling checksum mismatch')
        with np.load(path) as z:
            original = dict(z)
        if int(original['count']) != len(population):
            raise ValueError('Sampling population mismatch')
        keep = lookup[original['parcel']] >= 0
        p = lookup[original['parcel'][keep]]
        pixels, area = original['pixel'][keep], original['area'][keep]
        total = np.bincount(p, weights=area, minlength=len(sample))
        if not np.allclose(total, geo.area, rtol=1e-7, atol=.01):
            raise ValueError('Sampling does not cover entire cadastral polygon')
        weights = {'parcel': p, 'pixel': np.arange(len(p)), 'area': area, 'count': np.array(len(sample))}
        t = original['transform']; rows, cols = np.divmod(pixels, int(original['width']))
        x, y = t[2]+cols*res, t[5]-rows*res
        cells = shapely.box(x, y-res, x+res, y)
        interiors = {}
        groups = [np.flatnonzero(p == i) for i in range(len(sample))]
        for distance in (5, 10):
            areas = np.zeros(len(area))
            for i, indices in enumerate(groups):
                inner = geo.geometry.iloc[i].buffer(-distance)
                if not inner.is_empty:
                    areas[indices] = shapely.area(shapely.intersection(cells[indices], inner))
            interiors[distance] = {**weights, 'area': areas}
            static[f'inner{distance}_area_fraction_{res}m'] = np.bincount(p, weights=areas, minlength=len(sample))/total
        static[f'pure_pixels_{res}m'] = np.bincount(p[area >= res*res*.999], minlength=len(sample))
        grids[res] = {'grid': original, 'gather': pixels, 'weights': weights, 'groups': groups, 'interiors': interiors}
    return grids, static


def ratio(numerator, denominator):
    return np.divide(numerator, denominator, out=np.full_like(numerator, np.nan),
                     where=np.isfinite(numerator) & np.isfinite(denominator) & (abs(denominator) > 1e-6))


def pack_masks(mask, groups):
    return [np.packbits(mask[g]).tobytes() for g in groups]


def supplement(root, scene, job, bounds, grids, sample, geometry_hash):
    base = local_path(root, f"data/cache/copernicus/{json_hash(bounds)[:16]}/{scene['collection']}/{scene['id']}")
    pins = {}
    required = ('red', 'nir', 'scl', 'blue', 'nir08', 'rededge1', 'swir16')
    for name in required:
        path = base/(name+'.tif'); record = read_json(path.with_suffix('.json'))
        if record['identity'] != json_hash([scene['assets'][name], bounds]) or sha256(path) != record['sha256']:
            raise ValueError('Cached raster changed: '+path.name)
        with rasterio.open(path) as src:
            asset = scene['assets'][name]
            if src.crs.to_epsg() != 32638 or not np.allclose(src.res, (asset['resolution_m'],)*2):
                raise ValueError('Unexpected cached raster grid')
            tags = src.tags()
            if not all(np.isclose(float(tags[k]), asset[k]) for k in ('scale', 'offset')):
                raise ValueError('Radiometry mismatch')
        pins[path.relative_to(root).as_posix()] = record['sha256']
    frame = sample[['internal_parcel_id', 'cadastre_code']].copy()
    frame['observation_date'] = job['date']; frame['scene_id'] = scene['id']
    frame['geometry_version'] = geometry_hash
    frame['data_version'] = 'strict_control_supplement_v2'
    for res in (10, 20):
        group = grids[res]; w = group['weights']; gather = group['gather']
        names = ('red', 'nir', 'scl') if res == 10 else required
        a = {name: read_grid(base/(name+'.tif'), scene['assets'][name], group['grid'], 'EPSG:32638', name == 'scl').ravel()[gather]
             for name in names}
        scl = a['scl']; clear = np.isin(scl, [4, 5])
        if res == 10:
            values = {'ndvi': ratio(a['nir']-a['red'], a['nir']+a['red']),
                      'evi2': 2.5*ratio(a['nir']-a['red'], a['nir']+2.4*a['red']+1)}
        else:
            values = {'ndmi': ratio(a['nir']-a['swir16'], a['nir']+a['swir16']),
                      'bsi': ratio(a['swir16']+a['red']-a['nir']-a['blue'], a['swir16']+a['red']+a['nir']+a['blue']),
                      'ndre': ratio(a['nir08']-a['rededge1'], a['nir08']+a['rededge1'])}
        valid = clear & np.isfinite(np.column_stack(list(values.values()))).all(axis=1)
        frame[f'valid_mask_{res}m'] = pack_masks(valid, group['groups'])
        for label, mask in [('scl7_fraction', scl == 7), ('water_fraction', scl == 6),
                            ('cloud_fraction', np.isin(scl, [8, 9, 10])), ('shadow_fraction', scl == 3), ('snow_fraction', scl == 11)]:
            frame[f'{label}_{res}m'] = summarize(mask.astype(float), np.ones(len(mask), bool), w)['mean']
        for index, v in values.items():
            for stat, vals in summarize(v, valid, w).items():
                frame[f'{index}_{stat}'] = vals
            if index == 'ndvi':
                for distance, inner in group['interiors'].items():
                    for stat in ('mean', 'valid_fraction'):
                        frame[f'ndvi_inner{distance}_{stat}'] = summarize(v, valid, inner)[stat]
        if res == 10:
            vegetation = valid & (scl == 4) & (values['ndvi'] >= .30)
            bare = valid & (scl == 5) & (values['ndvi'] <= .25)
            frame['vegetated_mask_10m'] = pack_masks(vegetation, group['groups'])
            for name, mask in [('vegetation_fraction', vegetation), ('bare_fraction', bare)]:
                frame[name] = summarize(mask.astype(float), valid, w)['mean']
    return frame, pins


def calculate(daily, sample, static, grids, policy):
    rows, events, stability = [], [], []
    by_id = {identifier: (i, static.iloc[i].to_dict()) for i, identifier in enumerate(sample.internal_parcel_id)}
    for (identifier, year), frame in daily.groupby(['internal_parcel_id', 'year'], sort=True):
        i, spatial = by_id[identifier]
        weights = [grids[r]['weights']['area'][grids[r]['groups'][i]] for r in (10, 20)]
        result, found = classify_year(frame, spatial, *weights, policy)
        rows.append({'internal_parcel_id': identifier, 'cadastre_code': sample.cadastre_code.iloc[i],
                     'control_split': sample.control_split.iloc[i], **result})
        events.extend({'internal_parcel_id': identifier, 'year': int(year), **e} for e in found)
        for phase in (0, 1):
            probe, _ = classify_year(frame, spatial, *weights, policy, drop_alternate=phase)
            stability.append({'internal_parcel_id': identifier, 'year': int(year), 'phase': phase,
                              'covered': probe['covered'], 'baseline_covered': result['covered'],
                              'crop_type': probe['crop_type_candidate'], 'cycle': probe['annual_cycle_candidate'],
                              'comparable': probe['covered'] and result['covered'],
                              'changed': (probe['crop_type_candidate'], probe['annual_cycle_candidate']) !=
                                         (result['crop_type_candidate'], result['annual_cycle_candidate'])})
    seasons = pd.DataFrame(rows)
    if len(seasons) != 600 or seasons.duplicated(['internal_parcel_id', 'year']).any():
        raise ValueError('Incomplete control-season population')
    final = []
    for i, record in sample.iterrows():
        part = seasons.loc[seasons.internal_parcel_id.eq(record.internal_parcel_id)]
        final.append({**record[['internal_parcel_id', 'cadastre_code', 'area_official_m2', 'activity_stage', 'control_split', 'review_group']].to_dict(),
                      **predominant(part, policy.minimum_years),
                      'baseline_type': record.crop_type_candidate, 'baseline_cycle': record.annual_cycle_candidate})
    return seasons, pd.DataFrame(final), pd.DataFrame(events), pd.DataFrame(stability)


def build_report(out, sample, static, daily, seasons, parcels, stability, seconds):
    comparable = stability.loc[stability.comparable]
    changes = parcels.baseline_type.ne(parcels.crop_type_candidate) | parcels.baseline_cycle.ne(parcels.annual_cycle_candidate)
    report = {'status': 'control_experiment_complete_not_accepted', 'parcels': len(parcels), 'parcel_years': len(seasons),
              'source_scenes': 762, 'scene_parcel_rows': 762*len(parcels), 'coherent_daily_rows': len(daily),
              'dates': int(daily.observation_date.nunique()), 'changed_parcels': int(changes.sum()),
              'reasons': seasons.reason.value_counts().to_dict(), 'types': parcels.crop_type_candidate.value_counts().to_dict(),
              'cycles': parcels.annual_cycle_candidate.value_counts().to_dict(),
              'covered_seasons': int(seasons.covered.sum()), 'temporal_stability_comparable': len(comparable),
              'temporal_stability_changed': int(comparable.changed.sum()),
              'temporal_thinning_quality_loss': int((stability.baseline_covered & ~stability.covered).sum()),
              'splits': {}, 'elapsed_seconds': round(seconds, 2), 'accuracy': 'not_measured',
              'full_run_ready': False, 'publication_ready': False,
              'limits': ['No independent verified reference labels', 'Draft thresholds, no per-parcel overrides',
                         'Spatial screening can identify ambiguity, not prove crop identity',
                         'Known25 and remaining95 are a split of the existing model-stratified sample, not a random accuracy sample']}
    for split in ('known25', 'remaining95'):
        part = parcels.loc[parcels.control_split.eq(split)]
        report['splits'][split] = {'parcels': len(part), 'types': part.crop_type_candidate.value_counts().to_dict(),
                                  'cycles': part.annual_cycle_candidate.value_counts().to_dict()}
    write_json(out/'report.json', report)
    table = parcels.merge(static[['internal_parcel_id', 'minimum_width_m', 'pure_pixels_10m']], on='internal_parcel_id', validate='one_to_one')
    table.minimum_width_m = table.minimum_width_m.round(1)
    columns = ['cadastre_code', 'control_split', 'baseline_type', 'baseline_cycle', 'crop_type_candidate',
               'annual_cycle_candidate', 'assessable_years', 'minimum_width_m', 'pure_pixels_10m']
    markup = '<!doctype html><html lang="ru"><meta charset="utf-8"><title>EO screening: control 120</title><style>body{font:14px system-ui;margin:28px;color:#172921}table{border-collapse:collapse}td,th{padding:6px 9px;border:1px solid #ccd8d1}th{position:sticky;top:0;background:#edf3ef}h1{font-size:24px}pre{white-space:pre-wrap}</style>'
    markup += '<h1>Контрольная проверка EO: 120 участков</h1><p>Внутренний эксперимент. Новые классы не утверждены; рабочая карта не изменена. Изменение класса не означает исправление ошибки.</p>'
    markup += '<pre>'+html.escape(json.dumps(report, ensure_ascii=False, indent=2))+'</pre>'+table[columns].to_html(index=False, escape=True)+'</html>'
    (out/'review.html').write_text(markup, encoding='utf-8')
    return report


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--config', default='config/observation_screening.json')
    args = ap.parse_args()
    start = time.monotonic()
    config, policy, source, sample, population, geo, jobs, scenes, spec, pinned, out = prepare(ROOT, args.config)
    with run_lock(out/'run.lock'):
        manifest_path = out/'manifest.json'
        if manifest_path.exists() and read_json(manifest_path) != pinned:
            raise ValueError('Experiment pins changed; create a new version, never overwrite')
        if not manifest_path.exists():
            write_json(manifest_path, pinned)
        if (out/'complete.json').exists():
            complete = read_json(out/'complete.json')
            for p, digest in complete['outputs'].items():
                if sha256(local_path(out, p)) != digest:
                    raise ValueError('Completed output changed')
            print('Existing completed experiment verified; no rewrite', flush=True)
            return
        grids, static = spatial_weights(source, population, sample, geo)
        write_table(out/'selection.parquet', sample)
        write_table(out/'spatial.parquet', static)
        for res, group in grids.items():
            np.savez_compressed(out/f'weights_{res}m.npz', **group['weights'])
        blocks, cached_pins = [], {}
        for number, job in enumerate(jobs, 1):
            path = out/'scenes'/(job['scene_id']+'.parquet')
            side = path.with_suffix('.json')
            if path.exists() and side.exists():
                checkpoint = read_json(side)
                if sha256(path) != checkpoint['sha256']:
                    raise ValueError('Scene checkpoint changed')
                for p, digest in checkpoint['raster_sha256'].items():
                    if sha256(local_path(ROOT, p)) != digest:
                        raise ValueError('Cached input changed during resume')
                frame = pd.read_parquet(path)
                pins = checkpoint['raster_sha256']
            else:
                frame, pins = supplement(ROOT, scenes[job['scene_id']], job, spec['scope']['bounds_wgs84'], grids,
                                         sample, spec['scope']['basis_sha256'])
                write_table(path, frame)
                write_json(side, {'sha256': sha256(path), 'raster_sha256': pins})
            if len(frame) != 120 or not frame.internal_parcel_id.is_unique or set(frame.internal_parcel_id) != set(sample.internal_parcel_id):
                raise ValueError('Incomplete supplemental scene')
            blocks.append(frame); cached_pins.update(pins)
            if number % 25 == 0 or number == len(jobs):
                elapsed = time.monotonic()-start
                write_json(out/'progress.json', {'state': 'cached_raster_screening', 'scenes': number, 'total': len(jobs),
                                                'elapsed_seconds': round(elapsed, 1), 'updated_at': now()})
                print(f'{number}/{len(jobs)} cached scenes; {elapsed:.1f}s', flush=True)
        raw = pd.concat(blocks, ignore_index=True)
        raw['support'] = raw[[k+'_valid_fraction' for k in CORE]].min(axis=1, skipna=False)
        raw['finite'] = np.isfinite(raw[[k+'_median' for k in CORE]]).all(axis=1)
        daily = raw.sort_values(['internal_parcel_id', 'observation_date', 'finite', 'support', 'scene_id'],
                                ascending=[True, True, False, False, True]).drop_duplicates(['internal_parcel_id', 'observation_date']).copy()
        daily['year'] = pd.to_datetime(daily.observation_date).dt.year
        write_table(out/'daily.parquet', daily)
        write_json(out/'raster_lineage.json', cached_pins)
        print('Classifying 600 parcel-seasons and testing temporal thinning', flush=True)
        seasons, parcels, events, stability = calculate(daily, sample, static, grids, policy)
        for name, frame in [('seasons', seasons), ('parcels', parcels), ('events', events), ('stability', stability)]:
            write_table(out/(name+'.parquet'), frame)
        if parcels.accepted.any() or parcels.training_eligible.any():
            raise ValueError('Unapproved output promoted')
        if pin(ROOT, pinned['input_sha256']) != pinned['input_sha256'] or pin(ROOT, pinned['code_sha256']) != pinned['code_sha256']:
            raise ValueError('Inputs/code changed during run')
        report = build_report(out, sample, static, daily, seasons, parcels, stability, time.monotonic()-start)
        outputs = {p.relative_to(out).as_posix(): sha256(p) for p in out.rglob('*')
                   if p.is_file() and p.name not in ('run.lock', 'complete.json', 'progress.json')}
        write_json(out/'complete.json', {'completed_at': now(), 'outputs': outputs, 'approval': 'not_approved'})
        write_json(out/'progress.json', {'state': 'control_complete_not_accepted', 'updated_at': now()})
        print(json.dumps(report, indent=2), flush=True)


if __name__ == '__main__':
    main()
