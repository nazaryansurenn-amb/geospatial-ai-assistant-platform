"""Independent checks for the bounded v2 low-interval segmentation correction."""

from __future__ import annotations

import ast
from pathlib import Path
import numpy as np
import pandas as pd
import pytest

from wp_core.phenology_events_v2 import classify_year


INDICES = ("ndvi", "evi2", "ndmi", "bsi", "ndre")
SPATIAL = {
    "minimum_width_m": 80.0,
    "area_pixel_equivalents_10m": 100.0,
    "pure_pixels_10m": 64,
    "pure_pixels_20m": 16,
}
WEIGHTS10 = np.ones(100)
WEIGHTS20 = np.ones(25)


def mask(size, start=0, end=None):
    bits = np.zeros(size, dtype=np.uint8)
    bits[start:end] = 1
    return np.packbits(bits, bitorder="big").tobytes()


def observations(values):
    rows = []
    for date, ndvi in zip(pd.date_range("2023-03-01", periods=len(values), freq="10D"), values):
        ratio = np.clip((ndvi - 0.12) / 0.66, 0, 1)
        row = {
            "observation_date": date.strftime("%Y-%m-%d"),
            "ndvi_median": ndvi,
            "evi2_median": 0.08 + ratio * 0.54,
            "ndmi_median": -0.25 + ratio * 0.57,
            "bsi_median": 0.28 - ratio * 0.46,
            "ndre_median": 0.03 + ratio * 0.29,
            "ndvi_p10": ndvi - 0.035,
            "ndvi_p90": ndvi + 0.035,
            "ndvi_mean": ndvi,
            "ndvi_inner5_mean": ndvi,
            "ndvi_inner5_valid_fraction": 1.0,
            "valid_mask_10m": mask(100),
            "valid_mask_20m": mask(25),
        }
        row.update({f"{name}_valid_fraction": 1.0 for name in INDICES})
        rows.append(row)
    return pd.DataFrame(rows)


def double_sequence():
    return observations(
        [0.12] * 3 + [0.54, 0.68, 0.78, 0.72, 0.56, 0.35] + [0.12] * 3
        + [0.54, 0.68, 0.78, 0.73, 0.61, 0.49, 0.34] + [0.12] * 8
    )


def analyze(frame):
    return classify_year(frame, SPATIAL, WEIGHTS10, WEIGHTS20)


def alternating_interval():
    frame = double_sequence()
    frame.loc[10, "bsi_median"] = -0.28
    return frame


@pytest.mark.parametrize("count", [1, 2])
def test_clear_complete_cycles_retain_supported_candidate_counts(count):
    frame = double_sequence() if count == 2 else observations(
        [0.12] * 3 + [0.50, 0.62, 0.73, 0.78, 0.74, 0.66, 0.52, 0.36] + [0.12] * 16
    )
    result, events = analyze(frame)
    assert result["crop_type_candidate"] == "annual"
    assert result["annual_cycle_candidate"] == ("single_cycle" if count == 1 else "two_cycles")
    assert sum(event["complete_cycle"] for event in events) == count
    assert result["accepted"] is False
    assert result["training_eligible"] is False


def test_missing_numeric_observations_remain_unknown():
    frame = double_sequence()
    frame[[f"{name}_median" for name in INDICES]] = np.nan
    result, events = analyze(frame)
    assert result["covered"] is False
    assert result["crop_type_candidate"] == "undetermined"
    assert events == []


def test_spatial_limit_remains_unknown_despite_clear_temporal_cycles():
    result, _ = classify_year(double_sequence(), {**SPATIAL, "minimum_width_m": 5.0}, WEIGHTS10, WEIGHTS20)
    assert result["crop_type_candidate"] == "undetermined"
    assert result["annual_cycle_candidate"] == "undetermined"


def test_alternating_bsi_does_not_break_continuously_observed_low_interval():
    frame = alternating_interval()
    result, events = analyze(frame)
    assert result["covered"] is True
    assert result["crop_type_candidate"] == "annual"
    assert result["annual_cycle_candidate"] == "two_cycles"
    assert len(events) == 2
    assert all(event["complete_cycle"] for event in events)
    assert events[0]["soil_start_date"] == frame.loc[9, "observation_date"]
    assert events[0]["soil_end_date"] == frame.loc[11, "observation_date"]


def test_negative_bsi_low_date_cannot_become_soil_end_confirmation():
    frame = alternating_interval()
    _, events = analyze(frame)
    assert len(events) == 2
    assert events[0]["end_date"] == frame.loc[9, "observation_date"]
    assert events[0]["end_confirmed_date"] == frame.loc[11, "observation_date"]
    assert events[0]["end_confirmed_date"] != frame.loc[10, "observation_date"]


@pytest.mark.parametrize("bsi_values", [[-0.28, -0.28, -0.28], [0.28, -0.28, -0.28]])
def test_low_interval_without_repeated_positive_bsi_cannot_establish_reset(bsi_values):
    frame = double_sequence()
    frame.loc[9:11, "bsi_median"] = bsi_values
    result, events = analyze(frame)
    assert len(events) == 1
    assert result["annual_cycle_candidate"] != "two_cycles"


def test_actual_nonlow_observation_breaks_the_low_envelope():
    frame = alternating_interval()
    frame.loc[10, ["ndvi_median", "ndvi_mean", "ndvi_inner5_mean"]] = 0.40
    frame.loc[10, "ndvi_p10"] = 0.365
    frame.loc[10, "ndvi_p90"] = 0.435
    frame.loc[10, "evi2_median"] = 0.28
    result, events = analyze(frame)
    assert len(events) == 1
    assert result["annual_cycle_candidate"] != "two_cycles"


def test_positive_witnesses_more_than_twenty_days_apart_do_not_create_reset():
    frame = alternating_interval()
    frame.loc[11, "observation_date"] = (
        pd.Timestamp(frame.loc[11, "observation_date"]) + pd.Timedelta(days=1)
    ).strftime("%Y-%m-%d")
    result, events = analyze(frame)
    assert len(events) == 1
    assert result["annual_cycle_candidate"] != "two_cycles"


def test_event_gap_is_not_repaired_by_the_new_low_envelope():
    frame = alternating_interval().drop(index=8)
    frame.loc[9, "observation_date"] = "2023-06-10"
    frame.loc[10, "observation_date"] = "2023-06-15"
    frame.loc[11, "observation_date"] = "2023-06-20"
    result, _ = analyze(frame)
    assert result["covered"] is True
    assert result["gap_interrupted_count"] >= 1
    assert result["annual_cycle_candidate"] != "two_cycles"


@pytest.mark.parametrize("grid", ["10m", "20m"])
def test_intermediate_negative_bsi_date_must_share_observed_ground(grid):
    frame = alternating_interval()
    if grid == "10m":
        frame["valid_mask_10m"] = mask(100, 0, 60)
        frame.loc[10, "valid_mask_10m"] = mask(100, 40, 100)
    else:
        frame["valid_mask_20m"] = mask(25, 0, 15)
        frame.loc[10, "valid_mask_20m"] = mask(25, 10, 25)
    frame[[f"{name}_valid_fraction" for name in INDICES]] = 0.6
    result, _ = analyze(frame)
    assert result["noncomparable_reset_count"] >= 1
    assert result["annual_cycle_candidate"] != "two_cycles"


def test_short_early_growth_remains_incomplete_after_alternating_bsi_reset():
    frame = observations(
        [0.12] * 3 + [0.59, 0.73] + [0.12] * 4
        + [0.52, 0.64, 0.73, 0.78, 0.72, 0.62, 0.49, 0.35] + [0.12] * 10
    )
    frame.loc[6, "bsi_median"] = -0.28
    frame.loc[8, "bsi_median"] = -0.28
    result, events = analyze(frame)
    assert len(events) == 2
    assert events[0]["complete_cycle"] is False
    assert events[1]["complete_cycle"] is True
    assert result["annual_cycle_candidate"] == "undetermined"


def test_alternating_bsi_correction_does_not_bypass_rapid_regrowth_caution():
    frame = alternating_interval()
    # Preserve three measured low dates within a 20-day low-to-green interval.
    frame.loc[9, "observation_date"] = "2023-06-09"
    frame.loc[10, "observation_date"] = "2023-06-14"
    frame.loc[11, "observation_date"] = "2023-06-19"
    result, _ = analyze(frame)
    assert result["short_regrowth_candidate_count"] >= 1
    assert result["annual_cycle_candidate"] == "undetermined"


def test_prior_labels_and_binary_features_do_not_affect_the_new_reset_logic():
    frame = alternating_interval()
    expected = analyze(frame)
    contaminated = frame.assign(
        crop_type_candidate="perennial",
        annual_cycle_candidate="single_cycle",
        vegetation_fraction=0.0,
        bare_fraction=0.0,
        vegetated_mask_10m=b"legacy mask must not be read",
        support=-10.0,
        finite=False,
        accepted=True,
    )
    assert analyze(contaminated) == expected


def test_new_reset_logic_preserves_input_observations():
    frame = alternating_interval()
    original = frame.copy(deep=True)
    analyze(frame)
    pd.testing.assert_frame_equal(frame, original)


def test_new_engine_does_not_import_previous_rule_engines():
    source = Path(__file__).resolve().parents[1] / "wp_core" / "phenology_events_v2.py"
    for node in ast.walk(ast.parse(source.read_text(encoding="utf-8"))):
        if isinstance(node, ast.ImportFrom):
            assert node.level == 0
            assert not (node.module or "").startswith(("wp_core.", "core."))
        elif isinstance(node, ast.Import):
            assert not any(alias.name.startswith(("wp_core.", "core.")) for alias in node.names)
