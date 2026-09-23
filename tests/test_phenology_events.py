"""Independent, synthetic evidence checks for the private event experiment.

Fixtures contain observations, spatial support, and valid-pixel masks only.
They deliberately share no helpers, candidate labels, or classifier imports
with the preceding analytical experiments.
"""

from __future__ import annotations

import ast
import json
import subprocess
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from wp_core.phenology_events import EventPolicy, classify_year, predominant


ROOT = Path(__file__).resolve().parents[1]
INDICES = ("ndvi", "evi2", "ndmi", "bsi", "ndre")
SPATIAL = {
    "minimum_width_m": 80.0,
    "area_pixel_equivalents_10m": 100.0,
    "pure_pixels_10m": 64,
    "pure_pixels_20m": 16,
}
WEIGHTS10 = np.ones(100, dtype=float)
WEIGHTS20 = np.ones(25, dtype=float)


def packed_mask(size: int, start: int = 0, stop: int | None = None) -> bytes:
    mask = np.zeros(size, dtype=np.uint8)
    mask[start:stop] = 1
    return np.packbits(mask, bitorder="big").tobytes()


def observations(ndvi: list[float]) -> pd.DataFrame:
    """Neutral dense season with strongly agreeing, independently named indices."""
    rows = []
    for date, value in zip(pd.date_range("2023-03-01", periods=len(ndvi), freq="10D"), ndvi):
        # These are synthetic raw statistics, not inherited class thresholds.
        proportion = np.clip((value - 0.12) / 0.66, 0.0, 1.0)
        row = {
            "observation_date": date.strftime("%Y-%m-%d"),
            "ndvi_median": value,
            "evi2_median": 0.08 + 0.54 * proportion,
            "ndmi_median": -0.25 + 0.57 * proportion,
            "bsi_median": 0.28 - 0.46 * proportion,
            "ndre_median": 0.03 + 0.29 * proportion,
            "ndvi_p10": value - 0.035,
            "ndvi_p90": value + 0.035,
            "ndvi_mean": value,
            "ndvi_inner5_mean": value,
            "ndvi_inner5_valid_fraction": 1.0,
            "valid_mask_10m": packed_mask(100),
            "valid_mask_20m": packed_mask(25),
        }
        row.update({f"{name}_valid_fraction": 1.0 for name in INDICES})
        rows.append(row)
    return pd.DataFrame(rows)


def single_season() -> pd.DataFrame:
    return observations(
        [0.12] * 3 + [0.50, 0.62, 0.73, 0.78, 0.74, 0.66, 0.52, 0.36] + [0.12] * 16
    )


def double_season() -> pd.DataFrame:
    return observations(
        [0.12] * 3 + [0.54, 0.68, 0.78, 0.72, 0.56, 0.35] + [0.12] * 3
        + [0.54, 0.68, 0.78, 0.73, 0.61, 0.49, 0.34] + [0.12] * 8
    )


def analyze(frame: pd.DataFrame, spatial: dict | None = None, **kwargs):
    return classify_year(frame, SPATIAL if spatial is None else spatial, WEIGHTS10, WEIGHTS20, **kwargs)


def complete_events(events):
    return [event for event in events if event["complete_cycle"]]


def test_complete_single_sequence_has_one_observed_cycle():
    result, events = analyze(single_season())
    assert result["crop_type_candidate"] == "annual"
    assert result["annual_cycle_candidate"] == "single_cycle"
    assert len(complete_events(events)) == 1
    assert events[0]["soil_start_date"] < events[0]["soil_end_date"]


def test_two_separated_complete_sequences_have_two_observed_cycles():
    result, events = analyze(double_season())
    assert result["crop_type_candidate"] == "annual"
    assert result["annual_cycle_candidate"] == "two_cycles"
    assert len(complete_events(events)) == 2
    assert events[0]["soil_end_date"] < events[1]["start_date"]


def test_incomplete_early_growth_is_split_but_never_invented_as_second_cycle():
    frame = observations(
        [0.12] * 3 + [0.59, 0.73] + [0.12] * 4
        + [0.52, 0.64, 0.73, 0.78, 0.72, 0.62, 0.49, 0.35] + [0.12] * 10
    )
    result, events = analyze(frame)
    assert len(events) == 2
    assert events[0]["complete_cycle"] is False
    assert events[0]["soil_start_date"] == frame.iloc[5]["observation_date"]
    assert events[0]["soil_end_date"] < events[1]["start_date"]
    assert events[1]["complete_cycle"] is True
    assert result["annual_cycle_candidate"] == "undetermined"


def test_short_decline_and_regrowth_does_not_force_two_cycles():
    frame = observations(
        [0.12] * 3 + [0.54, 0.68, 0.78, 0.75, 0.69, 0.62] + [0.12]
        + [0.62, 0.70, 0.78, 0.76, 0.68, 0.61, 0.53, 0.38] + [0.12] * 9
    )
    result, events = analyze(frame)
    assert result["annual_cycle_candidate"] != "two_cycles"
    assert len(complete_events(events)) < 2


def test_quick_recovery_after_two_low_dates_retains_regrowth_alternative():
    frame = observations(
        [0.12] * 3 + [0.54, 0.68, 0.78, 0.75, 0.69, 0.62] + [0.12] * 2
        + [0.62, 0.70, 0.78, 0.76, 0.68, 0.61, 0.53, 0.38] + [0.12] * 8
    )
    result, _ = analyze(frame)
    assert result["short_regrowth_candidate_count"] >= 1
    assert result["annual_cycle_candidate"] == "undetermined"


def test_gap_at_an_event_boundary_cannot_be_interpolated_into_completed_cycle():
    frame = single_season()
    # Remove the decline and the earliest soil confirmations, retaining the
    # later soil observations. The inferred boundary now crosses a long gap.
    frame = frame.drop(index=range(8, 15)).reset_index(drop=True)
    result, events = analyze(frame)
    assert result["annual_cycle_candidate"] != "single_cycle"
    assert not complete_events(events)


def test_absent_observations_are_undetermined_not_inactivity():
    frame = single_season()
    frame[[f"{name}_median" for name in INDICES]] = np.nan
    result, events = analyze(frame)
    assert result["crop_type_candidate"] == "undetermined"
    assert result["annual_cycle_candidate"] in ("undetermined", "")
    assert events == []
    assert "inactive" not in json.dumps(result).lower()


def test_empty_frame_without_any_year_context_is_explicitly_rejected():
    with pytest.raises(ValueError):
        analyze(single_season().iloc[:0].copy())


def test_low_signal_without_growth_is_undetermined_not_a_specific_land_use():
    result, events = analyze(observations([0.12] * 27))
    assert result["crop_type_candidate"] == "undetermined"
    assert not complete_events(events)


def test_sustained_cover_yields_only_a_perennial_candidate():
    result, _ = analyze(observations([0.65, 0.70, 0.74] * 9))
    assert result["crop_type_candidate"] == "perennial"
    assert result["annual_cycle_candidate"] == ""
    assert result["accepted"] is False


def test_event_dates_come_from_observations_without_invented_boundaries():
    frame = double_season()
    _, events = analyze(frame)
    observed_dates = set(frame["observation_date"])
    assert events
    for event in events:
        for field in ("start_date", "peak_date", "end_date", "soil_start_date", "soil_end_date"):
            if event[field]:
                assert event[field] in observed_dates


def test_order_of_observations_does_not_change_the_inference():
    frame = double_season()
    assert analyze(frame.sample(frac=1.0, random_state=791).reset_index(drop=True)) == analyze(frame)


@pytest.mark.parametrize("grid", ["10m", "20m"])
def test_insufficient_common_ground_does_not_establish_a_complete_cycle(grid):
    frame = single_season()
    for index in frame.index:
        green = frame.loc[index, "ndvi_median"] >= 0.40
        if grid == "10m":
            frame.loc[index, "valid_mask_10m"] = packed_mask(100, 0, 60) if green else packed_mask(100, 40, 100)
        else:
            frame.loc[index, "valid_mask_20m"] = packed_mask(25, 0, 15) if green else packed_mask(25, 10, 25)
    for name in INDICES:
        frame[f"{name}_valid_fraction"] = 0.6
    result, events = analyze(frame)
    assert result["annual_cycle_candidate"] != "single_cycle"
    assert not complete_events(events)


@pytest.mark.parametrize("grid", ["10m", "20m"])
def test_two_events_on_different_observed_ground_do_not_establish_two_cycles(grid):
    frame = double_season()
    for index in frame.index:
        first_episode = index < 12
        if grid == "10m":
            mask = packed_mask(100, 0, 60) if first_episode else packed_mask(100, 40, 100)
        else:
            mask = packed_mask(25, 0, 15) if first_episode else packed_mask(25, 10, 25)
        frame.loc[index, f"valid_mask_{grid}"] = mask
    for name in INDICES:
        frame[f"{name}_valid_fraction"] = 0.6
    result, _ = analyze(frame)
    assert result["noncomparable_reset_count"] > 0
    assert result["annual_cycle_candidate"] == "undetermined"


def test_missing_core_index_cannot_create_a_complete_cycle():
    frame = single_season()
    frame["ndmi_median"] = np.nan
    result, events = analyze(frame)
    assert result["crop_type_candidate"] == "undetermined"
    assert not complete_events(events)


@pytest.mark.parametrize("grid", ["10m", "20m"])
def test_truncated_valid_pixel_mask_is_rejected(grid):
    frame = single_season()
    frame.loc[4, f"valid_mask_{grid}"] = b"\xff"
    with pytest.raises(ValueError):
        analyze(frame)


def test_duplicate_dates_cannot_increase_observed_event_evidence():
    frame = single_season()
    duplicated = pd.concat([frame, frame], ignore_index=True)
    try:
        result = analyze(duplicated)
    except ValueError:
        # Either a strict daily contract or deterministic deduplication is
        # acceptable; silently counting duplicates as extra evidence is not.
        return
    assert result == analyze(frame)


@pytest.mark.parametrize(
    "changes",
    [
        {"minimum_width_m": 5.0},
        {"area_pixel_equivalents_10m": 1.5},
    ],
)
def test_spatially_unresolved_parcels_abstain(changes):
    result, _ = analyze(single_season(), {**SPATIAL, **changes})
    assert result["crop_type_candidate"] == "undetermined"
    assert result["annual_cycle_candidate"] != "single_cycle"


def test_contradicting_independent_indices_do_not_inherit_ndvi_class():
    frame = single_season()
    frame["evi2_median"] = 0.04
    frame["ndmi_median"] = -0.30
    frame["ndre_median"] = 0.01
    frame["bsi_median"] = 0.30
    result, events = analyze(frame)
    assert result["crop_type_candidate"] == "undetermined"
    assert not complete_events(events)


def test_inference_is_unchanged_by_prior_labels_and_threshold_derived_columns():
    frame = single_season()
    expected = analyze(frame)
    contaminated = frame.assign(
        crop_type_candidate="perennial",
        annual_cycle_candidate="two_cycles",
        prior_class="household",
        accepted=True,
        vegetation_fraction=0.0,
        bare_fraction=1.0,
        vegetated_mask_10m=b"invalid old mask that must never be decoded",
        support=-100.0,
        finite=False,
    )
    assert analyze(contaminated) == expected
    contaminated["vegetation_fraction"] = 1.0
    contaminated["bare_fraction"] = 0.0
    contaminated["crop_type_candidate"] = "annual"
    contaminated["annual_cycle_candidate"] = "single_cycle"
    contaminated["vegetated_mask_10m"] = b""
    assert analyze(contaminated) == expected


def test_inference_does_not_mutate_observations_spatial_metadata_or_weights():
    frame = single_season()
    original = frame.copy(deep=True)
    spatial = SPATIAL.copy()
    weights10 = WEIGHTS10.copy()
    weights20 = WEIGHTS20.copy()
    classify_year(frame, spatial, weights10, weights20)
    pd.testing.assert_frame_equal(frame, original)
    assert spatial == SPATIAL
    np.testing.assert_array_equal(weights10, WEIGHTS10)
    np.testing.assert_array_equal(weights20, WEIGHTS20)


def season_votes(types, cycles, covered=None):
    return pd.DataFrame(
        {
            "year": list(range(2021, 2021 + len(types))),
            "crop_type_candidate": types,
            "annual_cycle_candidate": cycles,
            "covered": [True] * len(types) if covered is None else covered,
            "accepted": [False] * len(types),
        }
    )


def test_adequately_observed_undetermined_years_stay_in_type_denominator():
    seasons = season_votes(
        ["annual", "annual", "undetermined", "undetermined", "undetermined"],
        ["two_cycles", "two_cycles", "undetermined", "undetermined", "undetermined"],
    )
    result = predominant(seasons)
    assert result["covered_years"] == 5
    assert result["annual_years"] == 2
    assert result["undetermined_years"] == 3
    assert result["crop_type_candidate"] == "undetermined"
    assert result["annual_cycle_candidate"] != "two_cycles"


def test_unknown_cycle_years_cannot_manufacture_a_predominant_cycle_count():
    seasons = season_votes(
        ["annual"] * 5,
        ["two_cycles", "two_cycles", "undetermined", "undetermined", "undetermined"],
    )
    result = predominant(seasons)
    assert result["crop_type_candidate"] == "annual"
    assert result["two_cycle_years"] == 2
    assert result["annual_cycle_candidate"] == "undetermined"


def test_mixed_cycle_history_is_reported_separately_from_land_use():
    seasons = season_votes(
        ["annual", "annual", "annual", "annual", "undetermined"],
        ["single_cycle", "single_cycle", "two_cycles", "two_cycles", "undetermined"],
    )
    result = predominant(seasons)
    assert result["crop_type_candidate"] == "annual"
    assert result["annual_cycle_candidate"] == "undetermined"
    assert result["single_cycle_years"] == 2
    assert result["two_cycle_years"] == 2
    assert result["cycle_history_status"] == "variable_candidate_years"


def test_latest_year_does_not_override_predominant_completed_seasons():
    seasons = season_votes(
        ["annual", "annual", "annual", "annual", "perennial"],
        ["single_cycle"] * 4 + [""],
    )
    result = predominant(seasons)
    assert result["crop_type_candidate"] == "annual"
    assert result["annual_cycle_candidate"] == "single_cycle"
    assert predominant(seasons.iloc[::-1].reset_index(drop=True)) == result


def test_fewer_than_three_covered_years_cannot_establish_predominant_use():
    seasons = season_votes(
        ["annual"] * 5,
        ["single_cycle"] * 5,
        covered=[True, True, False, False, False],
    )
    result = predominant(seasons)
    assert result["covered_years"] == 2
    assert result["annual_years"] == 2
    assert result["assessable_years"] == 2
    assert result["crop_type_candidate"] == "undetermined"


def test_missing_years_cannot_silently_disappear_from_the_five_year_scope():
    seasons = season_votes(["annual"] * 5, ["single_cycle"] * 5)
    with pytest.raises(ValueError):
        predominant(seasons.iloc[:3])


def test_repeated_year_cannot_create_an_extra_multiyear_vote():
    seasons = season_votes(["annual"] * 5, ["single_cycle"] * 5)
    duplicated = pd.concat([seasons, seasons.iloc[[0]]], ignore_index=True)
    with pytest.raises(ValueError):
        predominant(duplicated)


def test_current_incomplete_season_cannot_enter_completed_season_inference():
    frame = single_season()
    frame["observation_date"] = (
        pd.to_datetime(frame["observation_date"]) + pd.DateOffset(years=3)
    ).dt.strftime("%Y-%m-%d")
    with pytest.raises(ValueError):
        analyze(frame)
    seasons = season_votes(["annual"] * 5, ["single_cycle"] * 5)
    seasons.loc[0, "year"] = 2026
    with pytest.raises(ValueError):
        predominant(seasons)


def test_candidates_are_never_accepted_or_training_truth():
    result, _ = analyze(single_season())
    assert result["accepted"] is False
    assert result["training_eligible"] is False
    multiyear = predominant(season_votes(["annual"] * 5, ["single_cycle"] * 5))
    assert multiyear["accepted"] is False
    assert multiyear["training_eligible"] is False


def test_engine_has_no_dependency_on_previous_analytical_modules():
    path = ROOT / "wp_core" / "phenology_events.py"
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.ImportFrom):
            assert node.level == 0, "Relative analytical imports defeat isolation"
            assert not (node.module or "").startswith(("wp_core.", "core."))
        if isinstance(node, ast.Import):
            assert not any(alias.name.startswith(("wp_core.", "core.")) for alias in node.names)
    probe = """
import importlib.abc
import sys
class BlockAnalyticalImports(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.startswith(('wp_core.', 'core.')) and fullname != 'wp_core.phenology_events':
            raise AssertionError('Unexpected analytical dependency: ' + fullname)
sys.meta_path.insert(0, BlockAnalyticalImports())
from wp_core.phenology_events import EventPolicy, classify_year, predominant
print('independent import passed')
"""
    completed = subprocess.run([sys.executable, "-B", "-c", probe], cwd=ROOT, capture_output=True, text=True, check=False)
    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == "independent import passed"

