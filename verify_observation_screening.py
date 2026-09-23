"""Independent structural checks for the private control experiment, not accuracy."""
import argparse
import json
from pathlib import Path

import numpy as np
import pandas as pd

from release_tools import local_path, sha256
from run_observation_analysis import write_json
from run_observation_screening import ROOT, read_json


def require(condition, message):
    if not condition:
        raise ValueError(message)


def semantic_changes(frame):
    def meaningful_cycle(kind, cycle):
        return cycle.replace('', 'undetermined').where(kind.eq('annual'), '')
    old_cycle = meaningful_cycle(frame.baseline_type, frame.baseline_cycle)
    new_cycle = meaningful_cycle(frame.crop_type_candidate, frame.annual_cycle_candidate)
    return frame.baseline_type.ne(frame.crop_type_candidate) | old_cycle.ne(new_cycle)


def check_source_agreement(root, out, sample, manifest):
    catalog = read_json(local_path(root, manifest['config']['baseline']+'/manifest.json'))['eo_scenes']
    chosen = np.linspace(0, len(catalog)-1, 20, dtype=int)
    checked = 0; max_difference = 0.
    for position in chosen:
        job = catalog[position]
        path = local_path(root, job['output'])
        require(sha256(path) == job['sha256'], 'Source EO checksum changed')
        old = pd.read_parquet(path, filters=[('internal_parcel_id', 'in', sample.internal_parcel_id.tolist())],
                              columns=['internal_parcel_id', 'ndvi_mean', 'ndvi_median', 'ndvi_valid_fraction'])
        new = pd.read_parquet(out/'scenes'/(job['scene_id']+'.parquet'))
        joined = new.merge(old, on='internal_parcel_id', suffixes=('', '_source'), validate='one_to_one')
        equal_mask = (joined.scl7_fraction_10m.eq(0) & joined.water_fraction_10m.eq(0)
                      & np.isclose(joined.ndvi_valid_fraction, joined.ndvi_valid_fraction_source)
                      & joined.ndvi_mean_source.notna())
        for name in ('ndvi_mean', 'ndvi_median'):
            a, b = joined.loc[equal_mask, name], joined.loc[equal_mask, name+'_source']
            require(np.allclose(a, b, rtol=1e-5, atol=1e-5), 'Unexplained NDVI computation mismatch')
            checked += len(a)
            max_difference = max(max_difference, float(abs(a-b).max()) if len(a) else 0.)
    require(checked > 0, 'No source agreement checks possible')
    return {'scenes_checked': len(chosen), 'matched_index_statistics': checked, 'maximum_absolute_difference': max_difference}


def verify(root, relative):
    out = local_path(root, relative)
    manifest = read_json(out/'manifest.json')
    complete = read_json(out/'complete.json')
    for path, digest in complete['outputs'].items():
        require(sha256(local_path(out, path)) == digest, 'Output checksum mismatch: '+path)
    for group in ('code_sha256', 'input_sha256'):
        for path, digest in manifest[group].items():
            require(sha256(local_path(root, path)) == digest, 'Protected input/code changed: '+path)
    sample = pd.read_parquet(out/'selection.parquet')
    final = pd.read_parquet(out/'parcels.parquet')
    seasons = pd.read_parquet(out/'seasons.parquet')
    static = pd.read_parquet(out/'spatial.parquet')
    daily = pd.read_parquet(out/'daily.parquet')
    scope = pd.read_parquet(local_path(root, 'data/observations/'+manifest['config']['input_version']+'/scope.parquet'))
    ids = set(sample.internal_parcel_id)
    require(len(ids) == 120 and sample.cadastre_code.is_unique, 'Control identity problem')
    require(set(final.internal_parcel_id) == ids and len(final) == 120, 'Missing/duplicated final parcel')
    require(set(static.internal_parcel_id) == ids and len(static) == 120, 'Missing spatial diagnostics')
    require(len(seasons) == 600 and not seasons.duplicated(['internal_parcel_id', 'year']).any(), 'Missing/duplicated season')
    require(seasons.groupby('internal_parcel_id').year.apply(lambda y: sorted(y) == list(range(2021, 2026))).all(), 'Invalid season years')
    require(not daily.duplicated(['internal_parcel_id', 'observation_date']).any(), 'Incoherent dates')
    require(daily.groupby('observation_date').internal_parcel_id.nunique().eq(120).all(), 'Date population incomplete')
    require(not sample[['household', 'road_excluded']].any().any(), 'Excluded ground entered control')
    require(not final[['accepted', 'training_eligible']].any().any(), 'Unapproved labels')
    require((scope.included.sum(), len(scope)) == (22802, 43984), 'Changed full source scope')
    require(scope.internal_parcel_id.is_unique and scope.cadastre_code.is_unique, 'Duplicate source identity')
    require(ids.issubset(scope.loc[scope.included].internal_parcel_id), 'Control outside eligible scope')
    for frame in (sample, final, static):
        joined = frame.merge(scope[['internal_parcel_id', 'cadastre_code', 'area_official_m2']],
                             on='internal_parcel_id', validate='one_to_one', suffixes=('', '_source'))
        require(joined.cadastre_code.eq(joined.cadastre_code_source).all(), 'Code mutation')
        require(np.array_equal(joined.area_official_m2, joined.area_official_m2_source), 'Area mutation')
    fractions = [c for c in daily if c.endswith('_fraction') or '_fraction_' in c]
    require(not ((daily[fractions] < -1e-9) | (daily[fractions] > 1+1e-9)).any().any(), 'Fraction outside [0,1]')
    require(len(list((out/'scenes').glob('*.parquet'))) == 762, 'Missing supplemental scene')
    scene_rows = sum(pd.read_parquet(p, columns=['internal_parcel_id']).shape[0] for p in (out/'scenes').glob('*.parquet'))
    require(scene_rows == 91440, 'Unexpected source rows')
    for res in (10, 20):
        with np.load(out/f'weights_{res}m.npz') as w:
            areas = np.bincount(w['parcel'], weights=w['area'], minlength=120)
        require(np.allclose(areas, static.geometry_area_m2, rtol=1e-7, atol=.01), 'Geometry sampling coverage differs')
    baseline = local_path(root, manifest['config']['baseline'])
    old = pd.concat([pd.read_parquet(baseline/f'seasons/{year}.parquet', filters=[('internal_parcel_id', 'in', list(ids))])
                     for year in range(2021, 2026)], ignore_index=True)
    joined = seasons.merge(old[['internal_parcel_id', 'year', 'covered', 'usable_dates', 'type_code']],
                           on=['internal_parcel_id', 'year'], suffixes=('', '_old'), validate='one_to_one')
    result = {'structural_checks': 'passed', 'accuracy_checked': False, 'owner_approved': False,
              'source_parcels': len(scope), 'eligible': int(scope.included.sum()), 'control': 120,
              'scene_rows': scene_rows, 'parcel_years': len(seasons), 'no_missing_control_ids': True,
              'geometry_code_official_area_unchanged': True, 'old_output_and_release_pins_unchanged': True,
              'old_covered_seasons': int(joined.covered_old.sum()), 'new_covered_seasons': int(joined.covered.sum()),
              'quality_lost': int((joined.covered_old & ~joined.covered).sum()),
              'quality_gained': int((~joined.covered_old & joined.covered).sum()),
              'mean_usable_date_change': round(float((joined.usable_dates-joined.usable_dates_old).mean()), 2),
              'new_quality_reasons': joined.loc[~joined.covered, 'quality_reason'].value_counts().to_dict(),
              'spatially_limited_parcels': int(seasons.loc[seasons.spatial_reason.eq('spatial_resolution_limit'), 'internal_parcel_id'].nunique()),
              'heterogeneous_parcels': int(seasons.loc[seasons.heterogeneous, 'internal_parcel_id'].nunique()),
              'edge_sensitive_parcels': int(seasons.loc[seasons.edge_sensitive, 'internal_parcel_id'].nunique()),
              'isolated_excursions': int(seasons.isolated_excursions.sum()),
              'source_missing_EO_scene_rows': 0, 'legacy_250m_used': manifest['legacy_250m_used'],
              'semantic_class_changes': int(semantic_changes(final).sum()),
              'raw_payload_differences': int((final.baseline_type.ne(final.crop_type_candidate) |
                                             final.baseline_cycle.ne(final.annual_cycle_candidate)).sum()),
              'comparison_note': 'Raw report changed_parcels also counts empty vs undetermined cycle encoding; use semantic_class_changes for class changes'}
    result['source_agreement'] = check_source_agreement(root, out, sample, manifest)
    return result


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--version', default='screening_20260906_v2')
    args = parser.parse_args()
    result = verify(ROOT, 'data/analysis/observation_screening/'+args.version)
    target = local_path(ROOT, 'server_data/review/'+args.version+'/verification.json')
    if target.exists():
        require(read_json(target) == result, 'Verification changed; do not overwrite')
    else:
        write_json(target, result)
    print(json.dumps(result, ensure_ascii=False, indent=2))
