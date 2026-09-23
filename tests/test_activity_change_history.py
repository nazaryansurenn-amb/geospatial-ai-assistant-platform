import itertools
import json
from pathlib import Path
from types import SimpleNamespace
import pytest
from wp_core.activity_change_history import classify, transition, CLASSES

ROOT = Path(__file__).resolve().parents[1]
REVIEW = ROOT / "server_data/review/activity_change_history_review_20260906_v1"


@pytest.mark.parametrize("codes, expected", [
    ([3,3,3,3,1], "became_active"), ([3,3,3,3,2], "became_active"),
    ([1,1,1,1,3], "became_inactive"), ([2,1,2,1,3], "became_inactive"),
    ([1,3,3,3,3], "became_inactive"),
    ([3,1,2,1,2], "became_active"), ([3,3,1,2,1], "became_active"),
    ([1,2,3,3,3], "became_inactive"), ([1,3,1,3,3], "not_selected"),
    ([1,3,1,3,1], "not_selected"), ([1,1,1,1,2], "not_selected"),
    ([1,1,1,1,1], "not_selected"), ([3,3,3,3,3], "not_selected"),
    ([0,3,3,3,1], "insufficient"), ([1,1,1,1,0], "insufficient"),
    ([3,3,3,3], "insufficient"), ([3,3,3,3,True], "insufficient"),
    (None, "insufficient"), ("invalid", "insufficient"),
])
def test_strict_temporal_rule(codes, expected):
    assert classify(codes, 5) == expected


def test_all_possible_five_year_patterns():
    for values in itertools.product(range(4), repeat=5):
        actual = classify(values, sum(v != 0 for v in values))
        expected = "insufficient" if 0 in values else "not_selected"
        binary = [v in (1, 2) for v in values]
        changes = [i for i in range(1, 5) if binary[i] != binary[i-1]]
        if 0 not in values and len(changes) == 1:
            expected = "became_active" if binary[-1] else "became_inactive"
        assert actual == expected
        assert transition(values, sum(v != 0 for v in values))[1] == (2021 + changes[0] if expected in CLASSES else None)


def test_exclusions_and_coverage_cannot_be_bypassed():
    assert classify([3,3,3,3,1], 4) == "insufficient"
    assert classify([3,3,3,3,1], 5, household=True) == "excluded"
    assert classify([1,1,1,1,3], 5, road=True) == "excluded"


def test_agent_context_addition_preserves_other_modes(monkeypatch):
    from wp_core.activity_change_history_agent import install, MODE
    from wp_core import agent_service_v5 as service, agent_presentation
    monkeypatch.setattr(agent_presentation, "TAB_NAMES", {k: dict(v) for k,v in agent_presentation.TAB_NAMES.items()})
    fake = SimpleNamespace(clean_context=service.clean_context, reader_context=service.reader_context, instructions=lambda: "existing")
    payload = json.loads((REVIEW / "summary.json").read_text())
    lookup = json.loads((REVIEW / "parcel_lookup.json").read_text())
    original = {"scope":"stage_1", "mode":"consolidation", "include_expansion":False, "selected_code":None, "language":"en"}
    before = fake.reader_context(original)
    install(fake, payload, lookup)
    assert fake.clean_context(original) == original
    assert fake.reader_context(original) == before
    new = fake.clean_context({**original, "mode":MODE})
    text = fake.reader_context(new)
    assert "Cultivation changes 2021-2025" in text
    assert "cannot filter this five-year transition cohort" in text
    assert "existing" in fake.instructions()
    with pytest.raises(service.QueryError):
        fake.clean_context({**original, "mode":"arbitrary"})


def test_ui_has_sixth_tab_and_vector_only_delivery():
    source = REVIEW / "frontend_source/src"
    app = (source / "App.jsx").read_text(encoding="utf-8")
    view = (source / "ActivityChangeView.jsx").read_text(encoding="utf-8")
    schema = (source / "landResourcesSchema.js").read_text(encoding="utf-8")
    assert schema.index('id: "activity_change"') > schema.index('id: "consolidation"')
    assert 'type: "vector"' in view and 'setFeatureState' not in view
    assert 'became_active: "#318cf0"' in view and 'became_inactive: "#ef454f"' in view
    assert '${window.location.origin}${delivery.url}' in view
    assert 'useActivityChange' in app and '<AgentPanel' in app


def test_register_covers_all_immutable_parcels():
    import geopandas as gpd
    path = ROOT / "data/analysis/activity_change/activity_change_history_review_20260906_v1"
    register = gpd.read_parquet(path / "register.parquet")
    source = gpd.read_parquet(ROOT / "data/analysis/parcel_eo/current_halo_v1/current_parcels.parquet")
    r = register.set_index("cadastre_code").sort_index()
    s = source.set_index("cadastre_code").sort_index()
    assert len(r) == len(s) == 43984 and r.index.is_unique
    assert r.area_official_m2.equals(s.area_official_m2)
    assert (r.geometry.to_wkb() == s.geometry.to_wkb()).all()
    winners = register[register.change_class.isin(CLASSES)]
    assert not winners.household.any() and not winners.road_excluded.any()
    assert winners.change_year.between(2022, 2025).all()
    assert winners.profile_year_count.eq(5).all()
    manifest = json.loads((path / "manifest.json").read_text())
    assert len(manifest["inputs"]) == 7
    assert all("250" not in name and not name.endswith(".npz") for name in manifest["inputs"])
