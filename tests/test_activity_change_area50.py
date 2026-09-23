import itertools
import math
import pandas as pd
import pytest
from wp_core.activity_change_area50 import dated_area_evidence, annual_area_evidence, gated_transition
from wp_core.activity_change_history import transition, CLASSES


def observation(date="2022-06-01", total=100, valid=100, fraction=.5, scene="a", **kw):
    return {"cadastre_code": "04-002-0108-0037", "observation_date": date, "scene_id": scene,
            "sampling_method": "pixel_center_10m", "quality_status": "usable",
            "parcel_pixel_count": total, "valid_pixel_count": valid, "vegetation_fraction": fraction, **kw}


@pytest.mark.parametrize("share,expected", [(0.5,"became_inactive"),(.4999,"not_selected"),(.9,"became_inactive"),
    (None,"insufficient"),(float("nan"),"insufficient"),(True,"insufficient"),(-1,"insufficient"),(1.1,"insufficient")])
def test_cutoff(share, expected):
    assert gated_transition([2,3,3,3,3], 5, [share,None,None,None,None])[0] == expected


def test_every_partial_year_not_only_2025():
    assert gated_transition([3,2,1,2,2], 5, [None,.8,None,.49,.9])[0] == "not_selected"
    assert gated_transition([3,2,1,2,2], 5, [None,.5,None,.5,.5])[:2] == ("became_active",2022)
    assert gated_transition([1,1,3,3,3], 5, [None]*5)[:2] == ("became_inactive",2023)


def test_all_patterns_never_introduce_or_flip_a_class():
    for values in itertools.product(range(4), repeat=5):
        old = transition(values, 5)
        for share in (.49,.5,None):
            new = gated_transition(values, 5, [share]*5)
            if new[0] in CLASSES:
                assert new[:2] == old
            if old[0] not in CLASSES:
                assert new[:2] == old


def test_whole_sample_denominator_not_only_clear_part():
    f = dated_area_evidence(pd.DataFrame([observation(valid=40,fraction=1)]))
    assert f.area_share_lower.iloc[0] == .4


def test_exact_half_and_rounded_thirds_recover_integer_counts():
    f = dated_area_evidence(pd.DataFrame([observation(total=2,valid=2),observation(date="2022-07-01",total=6,valid=3,fraction=.6667)]))
    assert f.area_share_lower.tolist() == [.5,2/6]
    assert f.count_recovery_exact.all()


def test_one_green_date_or_duplicate_scene_is_not_corroboration():
    rows = [observation(fraction=.8), observation(fraction=.8,scene="b"), observation(date="2022-07-01",fraction=.49)]
    annual, daily = annual_area_evidence(pd.DataFrame(rows), 2022)
    assert len(daily)==2 and not annual.passes_area50.iloc[0]
    assert annual.repeated_area_share_lower.iloc[0] == .49


def test_two_distinct_half_area_dates_pass():
    annual, _ = annual_area_evidence(pd.DataFrame([observation(),observation(date="2022-07-01")]),2022)
    assert annual.passes_area50.iloc[0]


@pytest.mark.parametrize("kw", [{"quality_status":"not_observed"},{"fraction":float("nan")},{"total":0},
    {"valid":101},{"sampling_method":"fractional_10m_overlap"}])
def test_unsupported_evidence_is_missing_not_zero(kw):
    annual, daily = annual_area_evidence(pd.DataFrame([observation(**kw)]),2022)
    assert math.isnan(daily.area_share_lower.iloc[0])
    assert annual.area_evidence_state.iloc[0] == "insufficient_area_evidence"


def test_wrong_year_rejected():
    with pytest.raises(ValueError):
        annual_area_evidence(pd.DataFrame([observation(date="2026-06-01")]),2022)


def test_household_road_unknown_and_reversal_remain_excluded():
    assert gated_transition([2,3,3,3,3],5,[.8]*5,household=True)[0]=="excluded"
    assert gated_transition([2,3,3,3,3],5,[.8]*5,road=True)[0]=="excluded"
    assert gated_transition([2,0,3,3,3],4,[.8]*5)[0]=="insufficient"
    assert gated_transition([2,3,2,3,3],5,[.8]*5)[0]=="not_selected"


def test_saved_result_preserves_parcels_and_enforces_every_partial_season():
    import json
    from pathlib import Path
    import geopandas as gpd
    root = Path(__file__).resolve().parents[1]
    folder = root / "data/analysis/activity_change"
    old = gpd.read_parquet(folder / "activity_change_history_review_20260906_v1/register.parquet").set_index("cadastre_code")
    new = gpd.read_parquet(folder / "activity_change_area50_20260907_v1/register.parquet").set_index("cadastre_code")
    annual = pd.read_parquet(folder / "activity_change_area50_20260907_v1/annual_area_evidence.parquet")
    assert len(new) == 43984 and new.index.is_unique and new.internal_parcel_id.is_unique
    assert new.crs.to_epsg() == 4326 and new.geometry.is_valid.all()
    for column in ("internal_parcel_id", "public_parcel_id", "area_official_m2", "annual_state_codes"):
        assert new[column].equals(old[column])
    assert new.geometry.to_wkb().equals(old.geometry.to_wkb())
    assert len(annual) == 22802 * 5 and not annual.duplicated(["cadastre_code", "analysis_year"]).any()
    assert set(annual.analysis_year) == set(range(2021, 2026))
    evidence = annual.set_index(["cadastre_code", "analysis_year"])
    winners = new[new.change_class.isin(CLASSES)]
    assert winners.change_class.value_counts().to_dict() == {"became_inactive":448, "became_active":129}
    assert not winners.household.any() and not winners.road_excluded.any()
    assert winners.change_class.equals(old.loc[winners.index, "change_class"])
    assert winners.change_year.equals(old.loc[winners.index, "change_year"])
    for code, row in winners.iterrows():
        for year, state in zip(range(2021, 2026), json.loads(row.annual_state_codes)):
            if state == 2:
                item = evidence.loc[(code, year)]
                assert item.passes_area50 and item.repeated_area_share_lower >= .5
                assert item.qualifying_area_dates >= 2
    removed = new[old.change_class.isin(CLASSES) & ~new.change_class.isin(CLASSES)]
    assert len(removed) == 1391
    assert set(removed.change_class) <= {"not_selected", "insufficient"}


def test_agent_replaces_stale_totals_and_preserves_other_sections(monkeypatch):
    import json
    from pathlib import Path
    from types import SimpleNamespace
    from wp_core import agent_service_v6 as service, agent_presentation_v6 as presentation
    from wp_core.activity_change_history_agent_v6 import install as install_old
    from wp_core.activity_change_area50_agent import install
    root = Path(__file__).resolve().parents[1] / "server_data/review"
    monkeypatch.setattr(presentation, "TAB_NAMES", {k:dict(v) for k,v in presentation.TAB_NAMES.items()})
    fake = SimpleNamespace(clean_context=service.clean_context, reader_context=service.reader_context, instructions=service.instructions)
    old = root / "activity_change_history_review_20260906_v1"
    new = root / "activity_change_area50_20260907_v1"
    install_old(fake, json.loads((old / "summary.json").read_text()), json.loads((old / "parcel_lookup.json").read_text()))
    context = {"scope":"lower_hrazdan", "mode":"degradation", "include_expansion":False, "selected_code":None, "language":"en"}
    before = fake.reader_context(context)
    assert "311 parcels" in fake.reader_context({**context, "mode":"activity_change"})
    install(fake, json.loads((new / "summary.json").read_text()), json.loads((new / "parcel_lookup.json").read_text()))
    assert fake.reader_context(context) == before and fake.clean_context(context) == context
    for language in ("hy", "ru", "en"):
        current = fake.clean_context({**context, "mode":"activity_change", "language":language})
        text = fake.reader_context(current)
        assert "129 " in text and "448 " in text
        assert "311 " not in text and "1657 " not in text
    assert "Never treat the below-cutoff or unavailable cases as inactive" in fake.instructions()
