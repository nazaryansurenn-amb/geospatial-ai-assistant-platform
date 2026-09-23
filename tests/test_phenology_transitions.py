import inspect
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from test_observation_screening import series, packed
from wp_core.observation_screening import classify_year as old_classify, predominant
from wp_core.phenology_transitions import classify_year, TransitionPolicy

SPATIAL = {'minimum_width_m': 25., 'area_pixel_equivalents_10m': 20.}


def classify(frame, **kwargs):
    return classify_year(frame, kwargs.pop('spatial', SPATIAL), np.ones(5), np.ones(5), **kwargs)


@pytest.mark.parametrize('intervals,kind,cycle', [(((90, 145),), 'annual', 'single_cycle'),
                                                (((90, 145), (220, 270)), 'annual', 'two_cycles'),
                                                (((90, 300),), 'perennial', '')])
def test_normal_profiles_preserved(intervals, kind, cycle):
    for phase in (None, 0, 1):
        r, _ = classify(series(intervals), drop_alternate=phase)
        assert (r['crop_type_candidate'], r['annual_cycle_candidate']) == (kind, cycle)


def valley_series(end_day=216):
    f = series(((90, 300),))
    days = pd.to_datetime(f.observation_date).dt.dayofyear
    low = days.between(180, end_day)
    for index, value in [('ndvi', .3), ('evi2', .2), ('ndmi', .05), ('bsi', .08)]:
        f.loc[low, index+'_median'] = value
        f.loc[low, index+'_mean'] = value
    f.loc[low, 'vegetation_fraction'] = .1
    f.loc[low, 'bare_fraction'] = .1
    f['ndvi_inner5_mean'] = f.ndvi_mean
    f['ndvi_p10'] = f.ndvi_median-.05
    f['ndvi_p90'] = f.ndvi_median+.05
    return f


def test_repeated_dip_not_continuous_canopy_and_not_confirmed_second_crop():
    f = valley_series()
    old, _ = old_classify(f, SPATIAL, np.ones(5), np.ones(5))
    assert old['crop_type_candidate'] == 'perennial'
    for phase in (None, 0, 1):
        result, _ = classify(f, drop_alternate=phase)
        assert result['crop_type_candidate'] == 'undetermined'
        assert result['reason'] == 'interrupted_canopy_review'
        assert all(not t['confirmed_second_cycle'] for t in result['transition_checks'])
        assert all(not t['bare_soil_anchor'] for t in result['transition_checks'])


def test_interior_gap_is_not_proof_of_uninterrupted_canopy():
    f = series(((90, 300),))
    days = pd.to_datetime(f.observation_date).dt.dayofyear
    f = f.loc[~days.between(171, 196)]
    result, _ = classify(f)
    assert result['covered']
    assert result['reason'] == 'canopy_continuity_not_observed'


def test_short_regrowth_does_not_automatically_reject_perennial_profile():
    r, _ = classify(valley_series(end_day=196))
    assert r['interior_interruptions'] > 0 and r['blocking_interruptions'] == 0
    assert r['crop_type_candidate'] == 'perennial'
    assert not r['transition_rule_changed_result']


def test_short_early_growth_cannot_be_glued_to_late_growth_across_soil():
    f = series(((65, 75), (215, 290)))
    old, _ = old_classify(f, SPATIAL, np.ones(5), np.ones(5))
    assert old['annual_cycle_candidate'] == 'single_cycle'
    r, events = classify(f)
    assert r['bridged_soil_intervals'] > 0
    assert r['annual_cycle_candidate'] == 'undetermined'
    assert r['crop_type_candidate'] == 'annual'
    assert r['reason'] == 'bridged_growth_episodes_review'
    assert not events[0]['bridged_soil_intervals'][0]['confirmed_additional_cycle']


def test_long_gap_at_decline_does_not_confirm_cycle():
    f = series(((90, 145),))
    days = pd.to_datetime(f.observation_date).dt.dayofyear
    f = f.loc[~days.between(146, 166)]
    result, checks = classify(f)
    assert result['covered'] and result['reason'] == 'event_transition_not_observed'
    assert checks and not checks[0]['transition_supported']
    assert result['annual_cycle_candidate'] == 'undetermined'


def test_spatial_exclusions_are_not_overridden_by_event_logic():
    spatial = {'minimum_width_m': 4., 'area_pixel_equivalents_10m': 10.}
    result, _ = classify(valley_series(), spatial=spatial)
    assert result['reason'] == 'spatial_resolution_limit'
    assert not result['transition_rule_changed_result']


def test_mowing_hypothesis_is_not_promoted_to_annual():
    result, _ = classify(series(((90, 145), (166, 230))))
    assert result['reason'] == 'possible_cut_and_regrowth'
    assert result['crop_type_candidate'] == 'undetermined'


def test_no_per_parcel_rules_or_training_labels():
    import wp_core.phenology_transitions as module
    source = inspect.getsource(module)
    for forbidden in ('cadastre_code', '04-014', 'verified_label', 'baseline_type', 'requests.', 'rasterio'):
        assert forbidden not in source


def test_repeat_is_deterministic_and_input_immutable():
    f = valley_series(); before = f.copy(deep=True)
    assert classify(f) == classify(f)
    pd.testing.assert_frame_equal(f, before)


def test_cycle_majority_still_includes_ambiguous_covered_years():
    f = pd.DataFrame({'year': range(2021, 2026), 'covered': True, 'crop_type_candidate': ['annual']*3+['undetermined']*2,
                      'annual_cycle_candidate': ['two_cycles']*2+['single_cycle']+['undetermined']*2})
    r = predominant(f)
    assert r['crop_type_candidate'] == 'annual' and r['annual_cycle_candidate'] == 'undetermined'


def test_real_regression_both_thinning_phases_do_not_become_perennial():
    root = Path(__file__).resolve().parents[1]
    source = root/'data/analysis/observation_screening/screening_20260906_v2'
    if not (source/'complete.json').exists():
        pytest.skip('Private cached control dataset is not installed')
    spatial = pd.read_parquet(source/'spatial.parquet')
    i = spatial.index[spatial.cadastre_code.eq('04-014-0228-0011')][0]
    frame = pd.read_parquet(source/'daily.parquet', filters=[('internal_parcel_id', '=', spatial.internal_parcel_id.iloc[i]), ('year', '=', 2024)])
    weights = []
    for res in (10, 20):
        with np.load(source/f'weights_{res}m.npz') as w:
            weights.append(w['area'][w['parcel'] == i])
    for phase in (0, 1):
        old, _ = old_classify(frame, spatial.iloc[i].to_dict(), *weights, drop_alternate=phase)
        new, _ = classify_year(frame, spatial.iloc[i].to_dict(), *weights, drop_alternate=phase)
        assert old['crop_type_candidate'] == 'perennial'
        assert new['crop_type_candidate'] == 'undetermined' and new['interior_interruptions'] > 0
    full, _ = classify_year(frame, spatial.iloc[i].to_dict(), *weights)
    assert full['annual_cycle_candidate'] == 'two_cycles'
