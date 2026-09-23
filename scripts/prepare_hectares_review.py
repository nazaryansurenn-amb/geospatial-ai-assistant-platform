"""Calculate displayed official areas and build on the current consolidation UI."""
from pathlib import Path
import hashlib
import json
import shutil
import sqlite3
import sys

ROOT = Path(__file__).resolve().parents[1]
SLUG = 'hectares_review_20260906_v1'
BASE = 'consolidation_review_20260906_v2'
REVIEW = ROOT / 'server_data/review' / SLUG
SOURCE = REVIEW / 'frontend_source'
FRONTEND = ROOT / 'output' / f'frontend_{SLUG}'


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False) + '\n', encoding='utf-8')


def verify_base():
    for name, record in json.loads((ROOT / 'config' / f'{BASE}.review.lock.json').read_text())['files'].items():
        assert sha(ROOT / name) == record['sha256'], name


def prepare():
    import pandas as pd
    verify_base()
    assert not SOURCE.exists() and not FRONTEND.exists(), 'Preserve existing preparation'
    basis = pd.read_parquet(ROOT / 'data/analysis/parcel_eo/current_halo_v1/current_parcels.parquet', columns=['cadastre_code', 'area_official_m2'])
    source_index = ROOT / 'server_data/land_analytics_land_potential_1km_review_20260906_v1.sqlite3'
    with sqlite3.connect(source_index.as_uri() + '?mode=ro', uri=True) as c:
        frame = pd.read_sql_query('SELECT * FROM parcel_analytics', c).merge(basis, on='cadastre_code', validate='one_to_one')
    outside = pd.read_parquet(ROOT / 'data/analysis/land_potential/land_potential_expansion_1km_20260906_v1/all_parcels.parquet',
                              columns=['stage', 'potential_class', 'household_agriculture', 'road_excluded', 'area_official_m2'])
    outside = outside.rename(columns={'stage': 'activity_stage', 'household_agriculture': 'household'})
    for f in [frame, outside]:
        f['eligible'] = ~(f.household.astype(bool) | f.road_excluded.astype(bool))
    delivery = json.loads((ROOT / 'server_data/land_analytics_summary_land_potential_1km_review_20260906_v1.json').read_text())
    result = {name: {} for name in ['activity', 'history', 'land_use_type', 'annual_cycles']}
    result['potential'] = {name: {} for name in ['inner', 'outside', 'combined']}
    receipts = []

    def area(f, mask, expected=None, label=''):
        if expected is not None:
            assert int(mask.sum()) == expected, (label, int(mask.sum()), expected)
        value = float(f.loc[mask, 'area_official_m2'].sum()) / 10000
        receipts.append({'label': label, 'count': int(mask.sum()), 'official_area_ha': value})
        return value

    for scope in ['lower_hrazdan', 'stage_1', 'stage_2']:
        f = frame if scope == 'lower_hrazdan' else frame[frame.activity_stage.eq(scope)]
        e = f.eligible
        ready = e & f.activity_state.eq('ready')
        a = delivery['activity']['summaries'][scope]
        result['activity'][scope] = {
            'open_field': area(f, ready, a['open_field_parcel_count'], f'{scope}:open_field'),
            'review': area(f, e & ~f.activity_state.eq('ready'), a['activity_review_parcel_count'], f'{scope}:activity_review'),
            'household': area(f, f.household.eq(1), a['household_parcel_count'], f'{scope}:household'),
            'classes': {k: area(f, ready & f.activity_class.eq(k), n, f'{scope}:activity:{k}') for k, n in a['activity_class_counts'].items()}}
        h = delivery['history']['summaries'][scope]
        result['history'][scope] = {'eligible': area(f, e, h['eligible_parcel_count'], f'{scope}:history_eligible'),
            'processed': area(f, e, h['processed_parcel_count'], f'{scope}:history_processed'),
            'automatic': area(f, e & f.history_class.isin(['stable_active', 'periodic', 'stable_no_activity']), h['automatic_classified_count'], f'{scope}:history_automatic'),
            'review': area(f, e & ~f.history_class.isin(['stable_active', 'periodic', 'stable_no_activity']), h['review_count'], f'{scope}:history_review'),
            'classes': {k: area(f, e & f.history_class.eq(k), n, f'{scope}:history:{k}') for k, n in h['class_counts'].items()}}
        for topic, column in [('land_use_type', 'crop_type'), ('annual_cycles', 'annual_cycle')]:
            s = delivery[topic]['summaries'][scope]
            result[topic][scope] = {'eligible': area(f, e, s['eligible_parcel_count'], f'{scope}:{topic}:eligible'),
                'household': result['activity'][scope]['household'],
                'classes': {k: area(f, e & f[column].eq(k), n, f'{scope}:{topic}:{k}') for k, n in s['class_counts'].items()}}
        for part, all_f, summaries in [('inner', frame, delivery['potential']['summaries']),
                ('outside', outside, delivery['potential']['expansion']['summaries']),
                ('combined', pd.concat([frame, outside], ignore_index=True), delivery['potential']['expansion']['combined_summaries'])]:
            pf = all_f if scope == 'lower_hrazdan' else all_f[all_f.activity_stage.eq(scope)]
            s = summaries[scope]
            result['potential'][part][scope] = {'eligible': area(pf, pf.eligible, s['eligible_parcel_count'], f'{scope}:{part}:eligible'),
                'candidate': area(pf, pf.potential_class.isin(['gravity_candidate', 'mechanical_candidate']), s['candidate_count'], f'{scope}:{part}:candidate'),
                'classes': {k: area(pf, pf.potential_class.eq(k), n, f'{scope}:{part}:{k}') for k, n in s['class_counts'].items()}}
    write(REVIEW / 'official_areas.json', result)
    write(REVIEW / 'numerical_verification.json', {'passed': True, 'checks': receipts,
        'source_index_sha256': sha(source_index), 'all_counts_match_existing_delivery': True})
    shutil.copytree(ROOT / 'server_data/review' / BASE / 'frontend_source', SOURCE)
    shutil.copytree(ROOT / 'output' / f'frontend_{BASE}' / 'data', FRONTEND / 'data')
    config = SOURCE / 'vite.config.mjs'
    config.write_text(config.read_text().replace(f'frontend_{BASE}', f'frontend_{SLUG}'), encoding='utf-8')
    print(json.dumps({'prepared': True, 'checked_count_area_pairs': len(receipts)}))


def finish():
    verify_base()
    assert (FRONTEND / 'index.html').exists()
    for p in (ROOT / 'output' / f'frontend_{BASE}' / 'data').rglob('*'):
        if p.is_file():
            assert sha(p) == sha(FRONTEND / 'data' / p.relative_to(ROOT / 'output' / f'frontend_{BASE}' / 'data'))
    files = [ROOT / 'run_hectares_review.py', ROOT / 'wp_core/review_hectares.py', REVIEW / 'official_areas.json',
             *[p for p in SOURCE.rglob('*') if p.is_file()], *[p for p in FRONTEND.rglob('*') if p.is_file()]]
    write(ROOT / 'config' / f'{SLUG}.review.lock.json', {'version': SLUG, 'base': BASE,
        'files': {str(p.relative_to(ROOT)): {'bytes': p.stat().st_size, 'sha256': sha(p)} for p in files}})
    print('Area display sealed; original map data unchanged')


if __name__ == '__main__':
    {'prepare': prepare, 'finish': finish}[sys.argv[1]]()
