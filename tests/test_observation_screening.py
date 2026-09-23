from dataclasses import replace

import numpy as np
import pandas as pd
import pytest

from wp_core.observation_screening import Policy, classify_year, isolated_excursions, predominant, weighted_overlap
from run_observation_screening import pack_masks, ratio


def packed(values):
    return np.packbits(values).tobytes()


def series(intervals=((90, 145),)):
    dates = pd.date_range('2021-01-01', '2021-12-31', freq='5D')
    d = dates.dayofyear.to_numpy()
    high = np.zeros(len(d), bool)
    for lo, hi in intervals:
        high |= (d >= lo) & (d <= hi)
    f = pd.DataFrame({'observation_date': dates.strftime('%Y-%m-%d')})
    for name, up, low in [('ndvi', .75, .2), ('evi2', .6, .1), ('ndmi', .35, -.1), ('bsi', -.2, .2), ('ndre', .25, .02)]:
        f[name+'_median'] = np.where(high, up, low)
        f[name+'_mean'] = f[name+'_median']
        f[name+'_valid_fraction'] = 1.
    f['vegetation_fraction'] = np.where(high, .9, .1)
    f['bare_fraction'] = np.where(high, .05, .8)
    f['ndvi_p10'] = f.ndvi_median-.05
    f['ndvi_p90'] = f.ndvi_median+.05
    f['ndvi_inner5_mean'] = f.ndvi_mean
    f['ndvi_inner5_valid_fraction'] = 1.
    for name in ('valid_mask_10m', 'valid_mask_20m'):
        f[name] = [packed([1]*5)]*len(f)
    f['vegetated_mask_10m'] = [packed([1]*5) if h else packed([0]*5) for h in high]
    return f


def classify(f, **kwargs):
    spatial = kwargs.pop('spatial', {'minimum_width_m': 25., 'area_pixel_equivalents_10m': 20.})
    return classify_year(f, spatial, np.ones(5), np.ones(5), **kwargs)


def test_one_and_two_episodes():
    single, events = classify(series())
    assert single['covered'] and single['annual_cycle_candidate'] == 'single_cycle'
    assert len(events) == 1 and not events[0]['left_censored']
    double, events = classify(series(((90, 145), (220, 270))))
    assert double['annual_cycle_candidate'] == 'two_cycles' and len(events) == 2


def test_persistent_profile():
    result, _ = classify(series(((90, 300),)))
    assert result['crop_type_candidate'] == 'perennial'


def test_left_censored_does_not_silently_drop_winter_growth():
    result, events = classify(series(((1, 105),)))
    assert result['annual_cycle_candidate'] == 'single_cycle'
    assert len(events) == 1 and events[0]['left_censored']


def test_unfinished_second_growth_is_not_single_cycle():
    result, _ = classify(series(((90, 145), (275, 365))))
    assert result['pending_growth']
    assert result['reason'] == 'unresolved_late_growth'
    assert result['annual_cycle_candidate'] != 'single_cycle'


def test_rapid_regrowth_not_automatically_double_crop():
    result, _ = classify(series(((90, 145), (166, 230))))
    assert result['possible_regrowth'] and result['reason'] == 'possible_cut_and_regrowth'


def test_different_visible_ground_is_not_comparable():
    f = series()
    high = f.ndvi_mean > .5
    for name in ('valid_mask_10m', 'valid_mask_20m'):
        f[name] = [packed([1, 1, 1, 0, 0]) if h else packed([0, 0, 1, 1, 1]) for h in high]
    result, events = classify(f)
    assert result['common_support_failures'] > 0 and not events
    assert result['crop_type_candidate'] == 'undetermined'


def test_two_different_vegetated_halves_not_two_cycles():
    f = series(((90, 145), (220, 270)))
    days = pd.to_datetime(f.observation_date).dt.dayofyear
    f['vegetated_mask_10m'] = [packed([1, 1, 1, 0, 0]) if d < 180 else packed([0, 0, 1, 1, 1]) for d in days]
    result, _ = classify(f)
    assert result['complete_episodes'] == 2 and result['peak_spatial_overlap'] == .2
    assert result['reason'] == 'multiple_or_spatially_mixed_episodes'


@pytest.mark.parametrize('spatial', [{'minimum_width_m': 4., 'area_pixel_equivalents_10m': 30.},
                                   {'minimum_width_m': 12., 'area_pixel_equivalents_10m': .15}])
def test_resolution_limit_preserves_record_and_unfiltered_candidate(spatial):
    result, _ = classify(series(), spatial=spatial)
    assert result['reason'] == 'spatial_resolution_limit'
    assert result['unfiltered_type'] == 'annual' and result['crop_type_candidate'] == 'undetermined'


def test_heterogeneous_and_edge_sensitive_are_separate_states():
    f = series(); f['ndvi_p10'] = 0.; f['ndvi_p90'] = .9
    result, _ = classify(f)
    assert result['reason'] == 'spatial_heterogeneity'
    f = series(); f['ndvi_inner5_mean'] = f.ndvi_mean+.2
    result, _ = classify(f)
    assert result['reason'] == 'edge_sensitive'


def test_duplicate_dates_and_unknown_phase_rejected():
    f = series()
    with pytest.raises(ValueError):
        classify(pd.concat([f, f.iloc[:1]]))
    with pytest.raises(ValueError):
        classify(f, drop_alternate=2)


def test_sparse_and_null_core_support_not_classified():
    f = series(); f['ndre_valid_fraction'] = np.nan
    result, _ = classify(f)
    assert not result['covered'] and result['reason'] == 'insufficient_dates'
    result, _ = classify(series().iloc[::10])
    assert not result['covered']


def test_outlier_not_step_change_or_disagreeing_indices():
    days = np.array([1, 6, 11]); good = np.ones(3, bool)
    assert isolated_excursions(days, np.array([.2, .8, .2]), np.array([.1, .6, .1]), good, Policy()).tolist() == [False, True, False]
    assert not isolated_excursions(days, np.array([.2, .8, .8]), np.array([.1, .6, .6]), good, Policy()).any()
    assert not isolated_excursions(days, np.array([.2, .8, .2]), np.array([.6, .1, .6]), good, Policy()).any()


def test_actual_weighted_overlap_and_invalid_masks():
    assert weighted_overlap(packed([1, 1, 0]), packed([0, 1, 1]), [2, 5, 3]) == .5
    with pytest.raises(ValueError):
        weighted_overlap(b'', b'', [1])
    with pytest.raises(ValueError):
        weighted_overlap(b'\xff', b'\xff', [float('nan')])


def seasons(types, cycles, covered=None):
    return pd.DataFrame({'year': range(2021, 2026), 'crop_type_candidate': types,
                         'annual_cycle_candidate': cycles, 'covered': covered or [True]*5})


def test_double_majority_is_not_two_of_three_selected_years():
    f = seasons(['annual']*3+['undetermined']*2, ['two_cycles']*2+['single_cycle']+['undetermined']*2)
    result = predominant(f)
    assert result['crop_type_candidate'] == 'annual' and result['annual_cycle_candidate'] == 'undetermined'
    f.loc[2, 'annual_cycle_candidate'] = 'two_cycles'
    assert predominant(f)['annual_cycle_candidate'] == 'two_cycles'
    assert not result['accepted'] and not result['training_eligible']


def test_tie_missing_year_and_profile_change():
    f = seasons(['annual']*2+['perennial']*2+['undetermined'], ['single_cycle']*2+['']*2+['undetermined'])
    result = predominant(f)
    assert result['crop_type_candidate'] == 'undetermined' and result['possible_profile_change']
    with pytest.raises(ValueError):
        predominant(f.iloc[:-1])


def test_pack_masks_and_finite_ratio():
    assert pack_masks(np.array([1, 0, 1], bool), [np.array([0, 1]), np.array([2])]) == [packed([1, 0]), packed([1])]
    assert np.isnan(ratio(np.array([1.]), np.array([0.]))[0])


def test_repeat_is_deterministic_and_input_unchanged():
    f = series(((90, 145), (220, 270))); before = f.copy(deep=True)
    assert classify(f) == classify(f)
    pd.testing.assert_frame_equal(f, before)


def test_source_runner_has_no_download_or_publishing_calls():
    import inspect
    import run_observation_screening as runner
    source = inspect.getsource(runner)
    assert 'process_scene(' not in source and 'cache_band(' not in source
    assert 'requests.' not in source and 'setFeatureState' not in source
    assert "'external_requests': False" in source


def test_cached_supplement_masks_scl7_and_keeps_native_radiometry(tmp_path, monkeypatch):
    import json
    import rasterio
    from rasterio.transform import from_origin
    import run_observation_screening as r
    sample = pd.DataFrame({'internal_parcel_id': ['a', 'b'], 'cadastre_code': ['code-a', 'code-b']})
    names = ('red', 'nir', 'scl', 'blue', 'nir08', 'rededge1', 'swir16')
    values = {'red': .1, 'nir': .5, 'blue': .05, 'nir08': .45, 'rededge1': .2, 'swir16': .2}
    assets = {n: {'href': 'https://example.invalid/'+n, 'scale': 1., 'offset': 0.,
                  'resolution_m': 10 if n in ('red', 'nir', 'blue') else 20} for n in names}
    bounds = [44., 40., 45., 41.]
    scene = {'id': 'fixture', 'collection': 'sentinel-2-l2a', 'assets': assets}
    base = tmp_path/f'data/cache/copernicus/{r.json_hash(bounds)[:16]}/sentinel-2-l2a/fixture'
    base.mkdir(parents=True)
    for name, asset in assets.items():
        path = base/(name+'.tif'); res = asset['resolution_m']
        with rasterio.open(path, 'w', driver='GTiff', width=3, height=2, count=1, dtype='float32',
                           crs='EPSG:32638', transform=from_origin(400000, 4400000, res, res)) as dst:
            dst.write(np.ones((2, 3), dtype='float32'), 1)
            dst.update_tags(scale='1', offset='0')
        path.with_suffix('.json').write_text(json.dumps({'identity': r.json_hash([asset, bounds]), 'sha256': r.sha256(path)}))
    def fake_grid(path, asset, weights, crs, categorical):
        return np.array([4., 5., 7., 4., 8., 7.]) if categorical else np.full(6, values[path.stem])
    monkeypatch.setattr(r, 'read_grid', fake_grid)
    w = {'parcel': np.repeat([0, 1], 3), 'pixel': np.arange(6), 'area': np.ones(6), 'count': np.array(2)}
    group = {'grid': {}, 'gather': np.arange(6), 'weights': w, 'groups': [np.arange(3), np.arange(3, 6)],
             'interiors': {5: w, 10: w}}
    f, pins = r.supplement(tmp_path, scene, {'date': '2021-06-01'}, bounds, {10: group, 20: group}, sample, 'frozen')
    assert len(f) == 2 and len(pins) == 7
    np.testing.assert_allclose(f.ndvi_valid_fraction, [2/3, 1/3])
    np.testing.assert_allclose(f.scl7_fraction_10m, [1/3, 1/3])
    np.testing.assert_allclose(f.ndvi_median, (values['nir']-values['red'])/(values['nir']+values['red']))
    np.testing.assert_allclose(f.ndre_median, (.45-.2)/(.45+.2))
    assert f.valid_mask_10m.iloc[0] == packed([1, 1, 0])
    assert f.geometry_version.eq('frozen').all()
    # Corrupt cache metadata must fail; the reader cannot repair it by downloading.
    side = base/'red.json'; data = json.loads(side.read_text()); data['sha256'] = 'wrong'
    side.write_text(json.dumps(data))
    with pytest.raises(ValueError, match='Cached raster changed'):
        r.supplement(tmp_path, scene, {'date': '2021-06-01'}, bounds, {10: group, 20: group}, sample, 'frozen')


def test_semantic_comparison_does_not_count_empty_unknown_encoding_as_change():
    from verify_observation_screening import semantic_changes
    frame = pd.DataFrame({'baseline_type': ['undetermined', 'annual', 'annual'],
                          'baseline_cycle': ['', 'two_cycles', ''],
                          'crop_type_candidate': ['undetermined', 'annual', 'annual'],
                          'annual_cycle_candidate': ['undetermined', 'single_cycle', 'undetermined']})
    assert semantic_changes(frame).tolist() == [False, True, False]
