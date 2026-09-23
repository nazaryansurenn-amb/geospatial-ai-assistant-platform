import ast
import inspect
import json
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from release_tools import sha256
from run_observation_analysis import load_year, review_sample, settings
from wp_core.observation_rules import (CORE, FRACTIONS, INDICES, Policy, coverage,
                                       predominant, season_signals, seasonal_features)


def curve(ndvi, days=None, fraction=None, valid=None):
    ndvi = np.array([ndvi], dtype=np.float32)
    days = np.arange(65, 326, 10) if days is None else np.array(days)
    values = {k: ndvi.copy() for k in INDICES}
    values.update(evi2=ndvi * .8, ndmi=ndvi * .7 - .25,
                  bsi=.25 - ndvi * .6, ndre=ndvi * .5,
                  vegetation_fraction=np.where(ndvi >= .48, .95, .1) if fraction is None else np.array([fraction]),
                  bare_fraction=np.where(ndvi <= .30, .8, .05), water_fraction=np.zeros_like(ndvi))
    support = {k: np.ones_like(ndvi) for k in INDICES}
    if valid is not None:
        support = {k: np.array([valid], dtype=np.float32) for k in INDICES}
    pixels = np.full(ndvi.shape, 20)
    return days, values, support, pixels


def classify(ndvi, **kwargs):
    return season_signals(*curve(ndvi, **kwargs), 2021)[0]


SINGLE = [.15] * 4 + [.55, .65, .70, .75, .65, .60, .55] + [.15] * 16
DOUBLE = [.15] * 3 + [.55, .70, .75, .70] + [.15] * 4 + [.55, .70, .75, .70] + [.15] * 12


def test_one_observed_complete_cycle():
    r = classify(SINGLE)
    assert r["covered"][0]
    assert r["type_code"][0] == 1
    assert r["cycle_code"][0] == 1


def test_two_complete_cycles():
    r = classify(DOUBLE)
    assert r["type_code"][0] == 1
    assert r["complete_cycles"][0] == 2
    assert r["cycle_code"][0] == 2


def test_bare_is_not_annual_or_proof_of_unused_land():
    r = classify([.15] * 27)
    assert r["covered"][0]
    assert r["type_code"][0] == 3
    assert r["cycle_code"][0] == 0


def test_perennial_with_winter_dormancy():
    r = classify([.15] * 4 + [.55] * 18 + [.3, .2, .15, .15, .15])
    assert r["type_code"][0] == 2


def test_interrow_peaks_not_double():
    r = classify([.42, .55, .7, .65, .43, .55, .68, .6, .43] * 3, fraction=[.65] * 27)
    assert r["type_code"][0] == 2
    assert r["cycle_code"][0] == 0


def test_different_halves_not_two_sowings():
    r = classify(DOUBLE, fraction=[.5 if v > .4 else .1 for v in DOUBLE])
    assert r["complete_cycles"][0] == 2
    assert r["type_code"][0] == 4
    assert r["cycle_code"][0] == 0


def test_cutting_is_review_not_automatic_perennial_or_double():
    r = classify([.15] * 3 + [.55, .7, .75, .7] + [.15] * 2 + [.55, .7, .75, .7] + [.15] * 14)
    assert r["possible_regrowth"][0]
    assert r["type_code"][0] == 4
    assert r["cycle_code"][0] == 0


def test_real_gap_not_one_cycle():
    days = np.arange(65, 326, 10)
    keep = ~((days >= 170) & (days <= 220))
    r = classify(np.array(DOUBLE)[keep], days=days[keep])
    assert not r["covered"][0]
    assert "long_gap" in r["quality_reason"][0]
    assert r["cycle_code"][0] == 0


def test_missing_late_season_with_many_dates():
    days = np.arange(65, 266, 5)
    r = classify([.55] * len(days), days=days)
    assert not r["covered"][0]
    assert "missing_late_season" in r["quality_reason"][0]


def test_coverage_counts_season_edges():
    days = np.arange(110, 326, 5)
    q, _ = coverage(days, np.ones((1, len(days)), dtype=bool), 2021, Policy())
    assert not q["covered"][0]
    assert q["maximum_gap_days"][0] == 50


def test_second_incomplete_growth_not_single():
    r = classify([.15] * 3 + [.55, .7, .75, .7] + [.15] * 12 + [.55] * 8)
    assert r["type_code"][0] == 4
    assert r["cycle_code"][0] == 0


def test_no_soil_reset_not_annual():
    days, v, s, p = curve(SINGLE)
    v["bare_fraction"][:] = .05
    r, _ = season_signals(days, v, s, p, 2021)
    assert r["type_code"][0] == 4


def test_boundary_cycle_with_no_start_not_asserted_annual():
    r = classify([.6] * 9 + [.15] * 18)
    assert r["boundary_growth_events"][0] == 1
    assert r["type_code"][0] == 4


def test_low_spatial_support_not_a_crop_label():
    r = classify(SINGLE, valid=[.1] * 27)
    assert r["type_code"][0] == 0


def test_tiny_parcel_observations_are_retained_but_not_classified():
    days, v, s, p = curve(SINGLE)
    p[:] = 1
    r, _ = season_signals(days, v, s, p, 2021)
    assert np.isfinite(v["ndvi"]).all()
    assert r["usable_dates"][0] == 0
    assert r["type_code"][0] == 0
    assert r["cycle_code"][0] == 0


def test_one_index_alone_cannot_assign_type():
    days, v, s, p = curve(SINGLE)
    v["evi2"][:] = .05
    r, _ = season_signals(days, v, s, p, 2021)
    assert r["type_code"][0] == 3


def test_every_index_has_features_missing_remains_nan():
    days, v, s, p = curve(SINGLE)
    v["psri"][:] = np.nan
    s["psri"][:] = 0
    result = seasonal_features(days, v, s, p, 2021)
    assert all(k + "_median" in result for k in INDICES)
    assert result.psri_dates.iloc[0] == 0
    assert pd.isna(result.psri_median.iloc[0])
    assert not np.isinf(result.select_dtypes("number").to_numpy()).any()


def test_duplicate_days_must_be_resolved_before_rules():
    args = list(curve(SINGLE))
    args[0][1] = args[0][0]
    with pytest.raises(ValueError, match="increasing"):
        season_signals(*args, 2021)


@pytest.mark.parametrize("types,cycles,covered,label,cycle", [
    ([2, 2, 2, 1, 1], [0, 0, 0, 1, 1], [1] * 5, "perennial", ""),
    ([1] * 5, [2, 2, 1, 1, 1], [1] * 5, "annual", "single_cycle"),
    ([1] * 5, [2, 2, 2, 1, 1], [1] * 5, "annual", "two_cycles"),
    ([1, 1, 1, 0, 0], [2, 2, 1, 0, 0], [1, 1, 1, 0, 0], "annual", "two_cycles"),
    ([1, 1, 0, 0, 0], [1, 1, 0, 0, 0], [1, 1, 0, 0, 0], "undetermined", ""),
    ([1, 1, 2, 2, 4], [1, 1, 0, 0, 0], [1] * 5, "undetermined", ""),
    ([1, 1, 1, 4, 4], [0] * 5, [1] * 5, "undetermined", ""),
])
def test_majority_not_last_year_and_missing_never_votes(types, cycles, covered, label, cycle):
    r = predominant(np.array([types], dtype="uint8"), [cycles], [covered], [[True] * 5])
    assert r.crop_type_candidate.iloc[0] == label
    assert r.annual_cycle_candidate.iloc[0] == cycle
    assert not r.accepted.any()


def test_all_missing_stays_undetermined_not_low_vegetation_history():
    r = predominant([[0] * 5], [[0] * 5], [[False] * 5], [[False] * 5])
    assert r.history_signal.iloc[0] == "incomplete_or_mixed_history"


def test_six_years_rejected():
    with pytest.raises(ValueError):
        predominant([[1] * 6], [[1] * 6], [[True] * 6], [[True] * 6])


def test_same_day_source_is_coherent_and_best_support(tmp_path):
    parcels = pd.DataFrame({"internal_parcel_id": ["a", "b"], "cadastre_code": ["c1", "c2"]})
    jobs = []
    for name, support, value in (("first", [.9, .2], [.3, .4]), ("second", [.3, .9], [.7, .8])):
        frame = parcels.copy()
        frame["observation_date"] = "2021-04-01"
        frame["scene_id"] = name
        frame["geometry_version"] = "hash"
        frame["data_version"] = "test"
        frame["ndvi_count"] = 10
        for k in INDICES:
            frame[k + "_mean"] = value
            frame[k + "_valid_fraction"] = support
        for k in FRACTIONS:
            frame[k] = value
        path = tmp_path / "data/observations/test/eo" / (name + ".parquet")
        path.parent.mkdir(parents=True, exist_ok=True)
        frame.to_parquet(path, index=False)
        jobs.append({"date": "2021-04-01", "scene_id": name, "sha256": sha256(path),
                     "rows": 2, "output": path.relative_to(tmp_path).as_posix()})
    days, values, supports, _, info = load_year(tmp_path, list(reversed(jobs)), parcels, "hash", "test")
    assert len(days) == 1
    assert info["scenes"] == 2 and info["distinct_dates"] == 1
    assert np.allclose(values["ndvi"][:, 0], [.3, .8])
    assert np.allclose(values["evi"][:, 0], [.3, .8])
    assert np.allclose(supports["ndvi"][:, 0], [.9, .9])
    jobs[0]["sha256"] = "wrong"
    with pytest.raises(ValueError, match="checksum"):
        load_year(tmp_path, jobs, parcels, "hash", "test")


def test_sample_is_balanced_reproducible_and_not_training_labels():
    data = pd.DataFrame({"internal_parcel_id": [f"p{i}" for i in range(240)],
                         "activity_stage": ["stage_1", "stage_2"] * 120,
                         "area_group": ["small", "large", "medium"] * 80,
                         "spatial_group": [str(i % 11) for i in range(240)],
                         "crop_type_candidate": ["annual"] * 120 + ["perennial"] * 60 + ["undetermined"] * 60,
                         "annual_cycle_candidate": ["single_cycle"] * 60 + ["two_cycles"] * 60 + [""] * 120})
    sample, shortfalls = review_sample(data)
    second, _ = review_sample(data.sample(frac=1, random_state=4))
    assert len(sample) == 120 and sample.internal_parcel_id.is_unique
    assert not any(shortfalls.values()) and not sample.training_eligible.any()
    assert sample.verified_label.eq("").all()
    assert sample.internal_parcel_id.tolist() == second.internal_parcel_id.tolist()
    assert sample.groupby(["review_group", "activity_stage"]).size().eq(15).all()


def test_config_rejects_2026_and_path_escape(tmp_path):
    (tmp_path / "config").mkdir()
    p = tmp_path / "config/test.json"
    config = {"version": "v1", "input_version": "v1", "years": [2026], "publication": "internal_draft_only", "policy": {}}
    p.write_text(json.dumps(config))
    with pytest.raises(ValueError, match="completed"):
        settings(tmp_path, "config/test.json")
    config["version"] = "../public"
    p.write_text(json.dumps(config))
    with pytest.raises(ValueError, match="slug"):
        settings(tmp_path, "config/test.json")


def test_calculations_have_no_grid_weather_llm_or_old_classifier_import():
    from wp_core import observation_rules
    tree = ast.parse(inspect.getsource(observation_rules))
    modules = {node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)}
    modules |= {a.name for node in ast.walk(tree) if isinstance(node, ast.Import) for a in node.names}
    assert modules == {"__future__", "dataclasses", "datetime", "warnings", "numpy", "pandas"}
    import run_observation_analysis
    text = inspect.getsource(run_observation_analysis)
    assert "setFeatureState" not in text and "build_tiles" not in text and "import openai" not in text
    assert "import predominant_use" not in text and "import weather" not in text
