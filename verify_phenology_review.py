"""Verify the control-only transition experiment and preserved predecessor files."""
import json
from pathlib import Path

import numpy as np
import pandas as pd

from release_tools import local_path, sha256
from run_observation_analysis import write_json
from run_phenology_review import ROOT, SOURCE, OUTPUT, read_json, validate_source


def require(value, message):
    if not value:
        raise ValueError(message)


def concrete(kind, cycle):
    label = kind.where(kind.ne('annual'), cycle)
    return label.where(kind.eq('perennial') | (kind.eq('annual') & cycle.isin(['single_cycle', 'two_cycles'])), 'undetermined')


def verify():
    source, out = local_path(ROOT, SOURCE), local_path(ROOT, OUTPUT)
    validate_source(ROOT, source)
    manifest, complete = read_json(out/'manifest.json'), read_json(out/'complete.json')
    for p, digest in complete['outputs'].items():
        require(sha256(local_path(out, p)) == digest, 'Output checksum changed')
    for p, digest in manifest['code_sha256'].items():
        require(sha256(local_path(ROOT, p)) == digest, 'Current algorithm differs')
        require(sha256(local_path(out/'source', p)) == digest, 'Source snapshot differs')
    for p, digest in manifest['input_sha256'].items():
        require(sha256(local_path(ROOT, p)) == digest, 'Input changed')
    preserved = []
    for version in ('transitions_20260906_v1', 'transitions_20260906_v2'):
        previous = local_path(ROOT, 'data/analysis/observation_screening/'+version)
        for p, digest in read_json(previous/'complete.json')['outputs'].items():
            require(sha256(local_path(previous, p)) == digest, 'Prior output changed')
        for p, digest in read_json(previous/'manifest.json')['code_sha256'].items():
            candidate = local_path(previous/'source', p)
            if not candidate.exists():
                candidate = ROOT/'server_data/review'/version/'source'/Path(p).name
            if not candidate.exists():
                candidate = local_path(ROOT, p)
            require(sha256(candidate) == digest, 'Prior algorithm unavailable')
        preserved.append(version)
    sample = pd.read_parquet(source/'selection.parquet')
    old_seasons = pd.read_parquet(source/'seasons.parquet')
    seasons = pd.read_parquet(out/'seasons.parquet')
    parcels = pd.read_parquet(out/'parcels.parquet')
    probes = pd.read_parquet(out/'probes.parquet')
    require(len(parcels) == 120 and parcels.internal_parcel_id.is_unique and parcels.cadastre_code.is_unique, 'Parcel identity problem')
    require(set(parcels.internal_parcel_id) == set(sample.internal_parcel_id), 'Control lost parcels')
    require(len(seasons) == 600 and not seasons.duplicated(['internal_parcel_id','year']).any(), 'Season coverage problem')
    require(seasons.groupby('internal_parcel_id').year.apply(lambda y: sorted(y) == list(range(2021,2026))).all(), 'Wrong years')
    require(len(probes) == 1200 and not probes.duplicated(['internal_parcel_id','year','phase']).any(), 'Probe coverage problem')
    require(not sample[['household','road_excluded']].any().any(), 'Excluded parcels entered analysis')
    joined = parcels.merge(sample[['internal_parcel_id','cadastre_code','area_official_m2']], on='internal_parcel_id', suffixes=('','_old'), validate='one_to_one')
    require(joined.cadastre_code.eq(joined.cadastre_code_old).all(), 'Code changed')
    require(np.array_equal(joined.area_official_m2, joined.area_official_m2_old), 'Official area changed')
    for frame, fields in ((parcels, ['crop_type_candidate','annual_cycle_candidate','accepted','training_eligible']),
                          (seasons, ['crop_type_candidate','annual_cycle_candidate','covered','reason'])):
        require(not frame[fields].isna().any().any(), 'Missing classification state')
    require(not parcels[['accepted','training_eligible']].any().any() and not seasons.accepted.any(), 'Unapproved labels promoted')
    paired = seasons.merge(old_seasons[['internal_parcel_id','year','covered']], on=['internal_parcel_id','year'], suffixes=('','_old'), validate='one_to_one')
    require(paired.covered.eq(paired.covered_old).all(), 'Observation quality policy changed')
    old_probes = pd.read_parquet(source/'stability.parquet').merge(old_seasons[['internal_parcel_id','year','crop_type_candidate','annual_cycle_candidate']], on=['internal_parcel_id','year'], validate='many_to_one')
    stats = {}
    for name, data, kind, cycle in [('old', old_probes, 'crop_type_candidate','annual_cycle_candidate'), ('new', probes, 'full_type','full_cycle')]:
        one, two = concrete(data[kind], data[cycle]), concrete(data.crop_type, data.cycle)
        specific = data.comparable & one.ne('undetermined') & two.ne('undetermined')
        stats[name] = {'comparable': int(data.comparable.sum()), 'jointly_specific': int(specific.sum()),
                       'specific_disagreements': int((specific & one.ne(two)).sum()), 'all_state_changes': int((data.comparable & data.changed).sum())}
    r = read_json(out/'report.json')
    require(r['specific_disagreements'] == stats['new']['specific_disagreements'], 'Report count mismatch')
    require(r['new_unstable_probes'] == stats['new']['all_state_changes'], 'Report stability mismatch')
    return {'status': 'structural_checks_passed', 'control': 120, 'seasons': 600, 'probes': 1200,
            'covered_seasons': int(seasons.covered.sum()), 'geometry_code_area_and_masks_unchanged': True,
            'prior_outputs_and_code_preserved': preserved, 'all_source_checksums_passed': True,
            'temporal_checks': stats, 'accuracy_verified': False, 'accepted': False,
            'class_counts': parcels.crop_type_candidate.value_counts().to_dict(),
            'annual_cycle_counts': parcels.loc[parcels.crop_type_candidate.eq('annual'), 'annual_cycle_candidate'].value_counts().to_dict()}


if __name__ == '__main__':
    result = verify()
    target = ROOT/'server_data/review/transitions_20260906_v3/verification.json'
    if target.exists():
        require(read_json(target) == result, 'Verification differs; no overwrite')
    else:
        write_json(target, result)
    print(json.dumps(result, indent=2, ensure_ascii=False))
