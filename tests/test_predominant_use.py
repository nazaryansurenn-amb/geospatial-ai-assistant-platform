import numpy as np
import pytest
from wp_core.predominant_use import aggregate_years, classify_season


def test_majority_not_last_year():
    result, cycle, *_ = aggregate_years([[2, 2, 2, 1, 1]], [[0, 0, 0, 1, 1]], [[True] * 5])
    assert result[0] == "perennial"
    assert cycle[0] == ""


def test_no_unsigned_vote_underflow():
    result, *_ = aggregate_years(np.array([[1, 2, 2, 2, 2]], dtype="uint8"), [[1, 0, 0, 0, 0]], [[True]*5])
    assert result[0] == "perennial"


def test_missing_years_never_vote_single():
    result, cycle, *_ = aggregate_years([[1, 1, 1, 0, 0]], [[2, 2, 1, 0, 0]], [[1, 1, 1, 0, 0]])
    assert result[0] == "annual"
    assert cycle[0] == "two_cycle_recurring"


def test_tie_and_two_years_are_undetermined():
    result, *_ = aggregate_years([[1, 1, 2, 2, 0], [1, 1, 0, 0, 0]],
        [[1, 1, 0, 0, 0]]*2, [[1, 1, 1, 1, 0], [1, 1, 0, 0, 0]])
    assert result.tolist() == ["undetermined", "undetermined"]


def test_double_minority_defaults_single():
    _, cycle, *_ = aggregate_years([[1]*5], [[2, 2, 1, 1, 1]], [[True]*5])
    assert cycle[0] == "single_cycle"


def series(ndvi, days=None, vegetation=None):
    ndvi = np.array([ndvi], dtype=float)
    days = np.arange(65, 326, 10) if days is None else np.asarray(days)
    veg = np.where(ndvi >= .48, .92, .1) if vegetation is None else np.array([vegetation])
    return classify_season(days, ndvi, ndvi*.7-.25, .25-ndvi*.6, veg,
        np.where(ndvi < .30, .8, .05), np.zeros_like(ndvi), np.ones_like(ndvi), np.ones_like(ndvi)*20)


def test_bare_is_not_annual():
    r = series([.18]*27)
    assert r['covered'][0]
    assert r['type_code'][0] == 3


def test_full_single_cycle():
    r = series([.15]*4 + [.55,.65,.7,.75,.7,.65,.6] + [.15]*16)
    assert r['type_code'][0] == 1
    assert r['cycle_code'][0] == 1


def test_two_complete_cycles():
    r = series([.15]*3 + [.55,.7,.75,.7] + [.15]*4 + [.55,.7,.75,.7] + [.15]*12)
    assert r['type_code'][0] == 1
    assert r['cycle_code'][0] == 2


def test_different_half_field_peaks_not_double():
    ndvi = [.15]*3 + [.55,.7,.75,.7] + [.15]*4 + [.55,.7,.75,.7] + [.15]*12
    r = series(ndvi, vegetation=[.5 if n>.4 else .1 for n in ndvi])
    assert r['cycle_code'][0] != 2


def test_regrowth_not_double():
    r = series([.15]*3 + [.55,.7,.75,.7] + [.15]*2 + [.55,.7,.75,.7] + [.15]*2 + [.55,.7,.75,.7] + [.15]*8)
    assert r['type_code'][0] == 2
    assert r['cycle_code'][0] == 0


def test_missing_late_season_not_single():
    r = series([.2]*4 + [.6]*10 + [.2]*5, days=np.arange(90,280,10))
    assert not r['covered'][0]
    assert r['cycle_code'][0] == 0


def test_sixth_year_rejected():
    with pytest.raises(ValueError):
        aggregate_years([[1]*6], [[1]*6], [[True]*6])


def test_persistent_cover_with_interrow_peaks_not_double():
    r = series([.42,.55,.7,.65,.43,.55,.68,.6,.43]*3,
               vegetation=[.65]*27)
    assert r['type_code'][0] == 2
    assert r['cycle_code'][0] == 0


def test_cloud_gap_cannot_make_a_cycle():
    days = [65,75,85,95,105,115,125,135,145,155,215,225,235,245,255,265,275,285,295,305,315,325]
    r = series([.2]*3+[.65]*6+[.2]*3+[.65]*5+[.2]*5, days=days)
    assert not r['covered'][0]
    assert r['cycle_code'][0] == 0


def test_classifier_has_no_data_loading_or_coarse_grid_dependency():
    import ast
    import inspect
    from wp_core import predominant_use
    tree = ast.parse(inspect.getsource(predominant_use))
    imports = [node.module for node in ast.walk(tree) if isinstance(node,ast.ImportFrom)]
    imports += [alias.name for node in ast.walk(tree) if isinstance(node,ast.Import) for alias in node.names]
    assert set(imports) == {'__future__','dataclasses','numpy'}


def test_gradual_maturity_then_long_bare_period_is_annual():
    r = series([.15]*4+[.55,.65,.7,.75,.6,.44,.38,.32]+[.15]*15)
    assert r['type_code'][0] == 1


def test_dormant_winter_not_against_seasonal_perennial_canopy():
    r = series([.15]*4+[.55]*18+[.3,.2,.15,.15,.15])
    assert r['type_code'][0] == 2
