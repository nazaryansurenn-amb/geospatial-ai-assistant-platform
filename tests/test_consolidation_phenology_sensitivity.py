import json
from pathlib import Path

import numpy as np
import pytest

from wp_core.consolidation_phenology_sensitivity import Policy, connected_components, rescore_pair
from run_consolidation_phenology_sensitivity import STRICT


def record(screen="additional_10m", passing_years=(2021, 2022)):
    details = []
    for year in range(2021, 2026):
        moderate = year in passing_years
        details.append({"year": year, "comparable": True, "similar": False, "reason": "different_profile",
            "rmse": ([.12, .10] if moderate else [.3, .3]) + ([.1, .12] if screen == "four_index" else []),
            "correlations": [.7, .75], "phase_agreement": .75})
    return {"screen": screen, "reason": "not_repeatedly_similar", "support_year_mask": 0, "year_results": json.dumps(details)}


@pytest.mark.parametrize("screen", ["four_index", "additional_10m"])
def test_controlled_relaxation_keeps_period_and_accepts_two_years(screen):
    r = record(screen)
    assert not rescore_pair(r, STRICT)["similar"]
    relaxed = rescore_pair(r, Policy())
    assert relaxed["similar"]
    assert relaxed["support_year_mask"] == 0b00011


def test_one_season_is_not_repeated_evidence():
    assert not rescore_pair(record(passing_years=(2021,)), Policy())["similar"]


def test_shared_pixel_rejection_is_not_overridden():
    r = {"screen": "additional_10m", "reason": "shared_10m_pixel_review", "support_year_mask": 0, "year_results": "[]"}
    assert rescore_pair(r, Policy())["state"] == "shared_pixel_review"
    r["year_results"] = record()["year_results"]
    with pytest.raises(ValueError, match="Contradictory"):
        rescore_pair(r, Policy())


@pytest.mark.parametrize("reason", ["weak_seasonal_contrast", "insufficient_common_dates"])
def test_quality_and_flat_profile_failures_stay_unknown_or_rejected(reason):
    r = record()
    details = json.loads(r["year_results"])
    for d in details:
        d.update(comparable=False, reason=reason)
    r["year_results"] = json.dumps(details)
    assert rescore_pair(r, Policy())["support_year_mask"] == 0


def test_native_index_dimensions_and_period_are_checked():
    r = record("four_index")
    details = json.loads(r["year_results"])
    details[0]["rmse"] = [.1, .1]
    r["year_results"] = json.dumps(details)
    with pytest.raises(ValueError, match="Incomplete"):
        rescore_pair(r, Policy())
    details[0]["year"] = 2026
    r["year_results"] = json.dumps(details)
    with pytest.raises(ValueError, match="Five unique"):
        rescore_pair(r, Policy())


def test_missing_metrics_fail_closed():
    r = record()
    details = json.loads(r["year_results"])
    details[0]["correlations"] = [float("nan"), .9]
    r["year_results"] = json.dumps(details)
    with pytest.raises(ValueError, match="Incomplete"):
        rescore_pair(r, Policy())


def test_empty_graph_does_not_invent_groups():
    assert connected_components(["a", "b"], [], {"a": 5000., "b": 5000.}) == []


def test_components_preserve_exact_area_size_and_connectivity():
    areas = {f"p{i:02d}": 5000. for i in range(10)}
    ids = list(areas)
    edges = list(zip(ids[:-1], ids[1:]))
    result = connected_components(ids, edges, areas)
    assert len(result) == 1 and result[0]["parcel_count"] == 10 and result[0]["area_ha"] == 5.
    assert result == connected_components(ids[::-1], edges[::-1], areas)
    limited = connected_components(ids, edges[:4] + edges[5:], areas)
    assert all(g["area_ha"] < 5. for g in limited)
    areas[ids[0]] = 5000.01
    with pytest.raises(ValueError, match="oversized"):
        connected_components(ids, edges, areas)


def test_connectivity_is_not_claimed_as_complete_link_similarity():
    # A-B, B-C and C-D only give a spatial upper bound, not A-C/A-D/B-D agreement.
    result = connected_components(list("abcd"), [("a", "b"), ("b", "c"), ("c", "d")], dict.fromkeys("abcd", 4000.))
    assert result[0]["area_ha"] == 1.6
    assert "similar" not in result[0] and "candidate" not in result[0]


@pytest.mark.parametrize("kwargs", [{"minimum_years": 1}, {"minimum_correlation": 0.}, {"maximum_rmse": (.1, np.nan, .1, .1)}])
def test_invalid_policy_rejected(kwargs):
    with pytest.raises(ValueError):
        Policy(**kwargs)


def test_changes_do_not_reload_eo_or_write_public_maps():
    root = Path(__file__).resolve().parents[1]
    source = (root / "run_consolidation_phenology_sensitivity.py").read_text()
    for forbidden in ("requests.", "httpx.", "rasterio.", "grid_250", "openai.", "load_series(", "to_crs(", "setFeatureState"):
        assert forbidden not in source
    assert "strict.verify()" in source
    assert 'data/analysis/land_consolidation/' in source
