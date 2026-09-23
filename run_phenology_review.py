"""Recheck transition logic using existing private tables, without raster I/O."""
from dataclasses import asdict
import json
from pathlib import Path
import shutil
import time

import numpy as np
import pandas as pd

from release_tools import local_path, sha256
from run_observation_analysis import now, run_lock, write_json, write_table
from run_observation_screening import environment, pin
from wp_core.observation_screening import Policy, predominant
from wp_core.phenology_transitions import TransitionPolicy, classify_year

ROOT = Path(__file__).resolve().parent
SOURCE = 'data/analysis/observation_screening/screening_20260906_v2'
OUTPUT = 'data/analysis/observation_screening/transitions_20260906_v3'


def read_json(path):
    return json.loads(path.read_text(encoding='utf-8'))


def validate_source(root, source):
    manifest, complete = read_json(source/'manifest.json'), read_json(source/'complete.json')
    for relative, digest in complete['outputs'].items():
        if sha256(local_path(source, relative)) != digest:
            raise ValueError('Control source changed: '+relative)
    for group in ('code_sha256', 'input_sha256'):
        for relative, digest in manifest[group].items():
            if sha256(local_path(root, relative)) != digest:
                raise ValueError('Preserved input/code changed: '+relative)
    return manifest


def results(daily, sample, static, weights, previous_seasons, previous_probes, policy, transition_policy):
    indexed = static.set_index('internal_parcel_id')
    positions = {identifier: i for i, identifier in enumerate(static.internal_parcel_id)}
    old_seasons = previous_seasons.set_index(['internal_parcel_id', 'year'])
    old_probes = previous_probes.set_index(['internal_parcel_id', 'year', 'phase'])
    seasons, probes, events = [], [], []
    for (identifier, year), frame in daily.groupby(['internal_parcel_id', 'year'], sort=True):
        spatial = indexed.loc[identifier].to_dict(); i = positions[identifier]
        w = [weights[r]['area'][weights[r]['parcel'] == i] for r in (10, 20)]
        full, found = classify_year(frame, spatial, *w, policy, transition_policy)
        old = old_seasons.loc[(identifier, year)]
        if (full['previous_type'], full['previous_cycle'], full['covered']) != (old.crop_type_candidate, old.annual_cycle_candidate, old.covered):
            raise ValueError('Full baseline replay differs')
        seasons.append({'internal_parcel_id': identifier, 'cadastre_code': spatial['cadastre_code'],
                        'control_split': spatial['control_split'], **full})
        events.extend({'internal_parcel_id': identifier, 'year': int(year), **e} for e in found)
        for phase in (0, 1):
            probe, _ = classify_year(frame, spatial, *w, policy, transition_policy, phase)
            old_probe = old_probes.loc[(identifier, year, phase)]
            if (probe['previous_type'], probe['previous_cycle'], probe['covered']) != (old_probe.crop_type, old_probe.cycle, old_probe.covered):
                raise ValueError('Thinned baseline replay differs')
            probes.append({'internal_parcel_id': identifier, 'year': int(year), 'phase': phase,
                           'covered': probe['covered'], 'comparable': probe['covered'] and full['covered'],
                           'old_type': probe['previous_type'], 'old_cycle': probe['previous_cycle'],
                           'old_reason': probe['previous_reason'], 'crop_type': probe['crop_type_candidate'],
                           'cycle': probe['annual_cycle_candidate'], 'reason': probe['reason'],
                           'full_type': full['crop_type_candidate'], 'full_cycle': full['annual_cycle_candidate'],
                           'changed': (probe['crop_type_candidate'], probe['annual_cycle_candidate']) !=
                                      (full['crop_type_candidate'], full['annual_cycle_candidate']),
                           'old_changed': bool(old_probe.changed),
                           'old_cross_type_switch': {old.crop_type_candidate, old_probe.crop_type} == {'annual', 'perennial'},
                           'cross_type_switch': {full['crop_type_candidate'], probe['crop_type_candidate']} == {'annual', 'perennial'},
                           'bridged_soil_intervals': probe['bridged_soil_intervals'],
                           'transition_checks': probe['transition_checks']})
    seasons = pd.DataFrame(seasons)
    if len(seasons) != 600 or seasons.duplicated(['internal_parcel_id', 'year']).any():
        raise ValueError('Incomplete parcel-year result')
    final = []
    for record in sample.itertuples():
        selected = seasons.loc[seasons.internal_parcel_id.eq(record.internal_parcel_id)]
        final.append({'internal_parcel_id': record.internal_parcel_id, 'cadastre_code': record.cadastre_code,
                      'area_official_m2': record.area_official_m2, 'activity_stage': record.activity_stage,
                      'control_split': record.control_split, 'review_group': record.review_group,
                      **predominant(selected, policy.minimum_years)})
    return seasons, pd.DataFrame(final), pd.DataFrame(probes), pd.DataFrame(events)


def report(seasons, parcels, probes, old, seconds):
    comparable = probes.loc[probes.comparable]
    joined = parcels.merge(old, on='internal_parcel_id', suffixes=('', '_old'), validate='one_to_one')
    changed = joined.crop_type_candidate.ne(joined.crop_type_candidate_old) | joined.annual_cycle_candidate.ne(joined.annual_cycle_candidate_old)
    specific = {'single_cycle', 'two_cycles'}
    specific_full = comparable.full_type.eq('perennial') | (comparable.full_type.eq('annual') & comparable.full_cycle.isin(specific))
    specific_probe = comparable.crop_type.eq('perennial') | (comparable.crop_type.eq('annual') & comparable.cycle.isin(specific))
    r = {'status': 'control_complete_not_accepted', 'parcels': len(parcels), 'parcel_years': len(seasons),
         'covered_seasons': int(seasons.covered.sum()), 'changed_parcels': int(changed.sum()),
         'types': parcels.crop_type_candidate.value_counts().to_dict(),
         'cycles': parcels.annual_cycle_candidate.value_counts().to_dict(),
         'changed_seasons': int(seasons.transition_rule_changed_result.sum()),
         'new_reasons': seasons.loc[seasons.transition_rule_changed_result, 'reason'].value_counts().to_dict(),
         'comparable_probes': len(comparable), 'old_unstable_probes': int(comparable.old_changed.sum()),
         'new_unstable_probes': int(comparable.changed.sum()),
         'old_direct_annual_perennial_switches': int(comparable.old_cross_type_switch.sum()),
         'new_direct_annual_perennial_switches': int(comparable.cross_type_switch.sum()),
         'jointly_specific_probes': int((specific_full & specific_probe).sum()),
         'specific_disagreements': int((specific_full & specific_probe & comparable.changed).sum()),
         'scope_ids_preserved': True, 'legacy_250m_used': False, 'raster_reads': 0, 'network_requests': 0,
         'public_release_changed': False, 'accuracy': 'not_measured', 'accepted': False,
         'mass_run_status': 'not_run_control_only', 'elapsed_seconds': round(seconds, 2), 'splits': {}}
    for split in ('known25', 'remaining95'):
        p = parcels.loc[parcels.control_split.eq(split)]
        r['splits'][split] = {'parcels': len(p), 'types': p.crop_type_candidate.value_counts().to_dict()}
    if parcels[['accepted', 'training_eligible']].any().any() or len(parcels) != 120 or not parcels.internal_parcel_id.is_unique:
        raise ValueError('Invalid identity or approval')
    if not joined.cadastre_code.eq(joined.cadastre_code_old).all() or not np.array_equal(joined.area_official_m2, joined.area_official_m2_old):
        raise ValueError('Parcel code/area changed')
    return r, joined.loc[changed]


def write_report(path, r, changed):
    lines = ['# Проверка переходов между циклами', '', 'Статус: внутренний контроль, не принятая классификация.', '',
             f"Проверены {r['parcels']} участков и {r['parcel_years']} сезонов. Расчёт: {r['elapsed_seconds']} с.",
             'Повторного чтения растров, скачиваний и изменения рабочей карты нет.', '',
             '## Что изменено', '',
             '- Низкий растительный сигнал, подтверждённый соседней датой и последующим ростом, прерывает гипотезу непрерывного покрова.',
             '- Не требуется выдавать этот промежуток за доказанную уборку, открытую почву или второй посев.',
             '- Короткие спады с быстрым отрастанием остаются отдельной диагностикой и сами по себе не отменяют многолетний профиль.',
             '- Качество проверяется непосредственно вокруг спада; большой пробел не становится подтверждённым циклом.',
             '- Нельзя объединять ранний и поздний рост в один эпизод через повторно наблюдавшуюся открытую почву.',
             '- Дороги, приусадебные, кадастр, исходные индексы, пространственные ограничения и правило большинства лет не менялись.', '',
             '## Итоги', '', '| Показатель | До | После |', '|---|---:|---:|',
             f"| Изменения при прореживании, из {r['comparable_probes']} сопоставимых проб | {r['old_unstable_probes']} | {r['new_unstable_probes']} |",
             f"| Прямые переходы однолетние ↔ многолетние | {r['old_direct_annual_perennial_switches']} | {r['new_direct_annual_perennial_switches']} |", '',
             f"Изменено {r['changed_seasons']} сезонных результатов и {r['changed_parcels']} пятилетних итогов.", '',
             'Текущие кандидаты: '+', '.join(f'{k}: {v}' for k,v in r['types'].items())+'.', '',
             '## Изменившиеся участки', '', '| Кадастровый код | Прежний тип / цикл | Новый тип / цикл |', '|---|---|---|']
    for row in changed.itertuples():
        lines.append(f'| {row.cadastre_code} | {row.crop_type_candidate_old} / {row.annual_cycle_candidate_old} | {row.crop_type_candidate} / {row.annual_cycle_candidate} |')
    lines += ['', '## Ограничения', '',
              'Уменьшение неустойчивости не является измерением точности. Неопределённость не заменяется искусственным классом.',
              'Проверка специально удаляет половину пригодных дат; это не означает потерю исходных снимков.',
              'Большинство оставшихся изменений связано с потерей подтверждения отдельного цикла, а не с переносом в противоположный тип.',
              'Массовый пересчёт и публикация не выполнены. Результаты не являются обучающими метками.']
    path.write_text('\n'.join(lines)+'\n', encoding='utf-8')


def main():
    start = time.monotonic()
    source, out = local_path(ROOT, SOURCE), local_path(ROOT, OUTPUT)
    old_manifest = validate_source(ROOT, source)
    policy = Policy(**old_manifest['effective_policy']); transition_policy = TransitionPolicy()
    inputs = [SOURCE+'/'+name for name in ('manifest.json', 'complete.json', 'daily.parquet', 'selection.parquet', 'spatial.parquet',
                                           'seasons.parquet', 'stability.parquet', 'parcels.parquet', 'weights_10m.npz', 'weights_20m.npz')]
    code = ['run_phenology_review.py', 'wp_core/phenology_transitions.py', 'wp_core/observation_screening.py',
            'wp_core/observation_rules.py', 'run_observation_analysis.py', 'run_observation_screening.py', 'release_tools.py']
    manifest = {'source': SOURCE, 'scope': 'control_120', 'policy': asdict(policy), 'transition_policy': asdict(transition_policy),
                'input_sha256': pin(ROOT, inputs), 'code_sha256': pin(ROOT, code), 'environment': environment(),
                'previous_class_as_ground_truth': False, 'publication': 'internal_draft_only'}
    out.mkdir(parents=True, exist_ok=True)
    with run_lock(out/'run.lock'):
        if (out/'manifest.json').exists() and read_json(out/'manifest.json') != manifest:
            raise ValueError('Changed experiment pins; no overwrite permitted')
        if (out/'complete.json').exists():
            for p, digest in read_json(out/'complete.json')['outputs'].items():
                if sha256(local_path(out, p)) != digest:
                    raise ValueError('Completed output checksum mismatch')
            print('Completed transition review verified, no rewrite')
            return
        write_json(out/'manifest.json', manifest)
        for relative in code:
            snapshot = out/'source'/relative
            snapshot.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(local_path(ROOT, relative), snapshot)
        sample = pd.read_parquet(source/'selection.parquet')
        static = pd.read_parquet(source/'spatial.parquet')
        daily = pd.read_parquet(source/'daily.parquet')
        old = pd.read_parquet(source/'parcels.parquet')
        if sample[['household', 'road_excluded', 'accepted', 'training_eligible']].any().any():
            raise ValueError('Excluded or accepted input label')
        if set(sample.internal_parcel_id) != set(static.internal_parcel_id) or set(sample.internal_parcel_id) != set(daily.internal_parcel_id):
            raise ValueError('Missing control ID')
        weights = {}
        for res in (10, 20):
            with np.load(source/f'weights_{res}m.npz') as z:
                weights[res] = dict(z)
        print('Rechecking 600 seasons and 1200 temporal probes from existing tables', flush=True)
        seasons, parcels, probes, events = results(daily, sample, static, weights,
                                                  pd.read_parquet(source/'seasons.parquet'), pd.read_parquet(source/'stability.parquet'),
                                                  policy, transition_policy)
        r, changed = report(seasons, parcels, probes, old, time.monotonic()-start)
        for name, frame in [('seasons', seasons), ('parcels', parcels), ('probes', probes), ('events', events), ('changes', changed)]:
            write_table(out/(name+'.parquet'), frame)
        write_json(out/'report.json', r)
        write_report(out/'RESULT_RU.md', r, changed)
        validate_source(ROOT, source)
        if pin(ROOT, inputs) != manifest['input_sha256'] or pin(ROOT, code) != manifest['code_sha256']:
            raise ValueError('Input or code changed during calculation')
        hashes = {p.relative_to(out).as_posix(): sha256(p) for p in out.rglob('*') if p.is_file() and p.name not in ('run.lock', 'complete.json')}
        write_json(out/'complete.json', {'created_at': now(), 'outputs': hashes, 'accepted': False})
        print(json.dumps(r, ensure_ascii=False, indent=2), flush=True)


if __name__ == '__main__':
    main()
