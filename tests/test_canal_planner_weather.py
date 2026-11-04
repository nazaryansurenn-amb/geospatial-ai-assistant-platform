"""Water delivery planner, step 3a (owner approval 2026-11-02): weather as known on the planning date. ERA5 to D-6,
forecasts issued on or before D, then optimistic / normal / pessimistic bands; nothing later than D is ever used."""
import sys
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from wp_core import canal_planner_weather as W  # noqa: E402

pytestmark = pytest.mark.skipif(not (W.FORECAST.exists() and W.ERA5.exists()), reason="weather sources not present")
D = date(2025, 7, 1)
CELL = [W.MAIN_CELL]


@pytest.fixture(scope="module")
def src():
    return W.load()


def test_sources_follow_the_planning_date(src):
    w = W.window(D, D - timedelta(days=10), D + timedelta(days=12), "pessimistic", CELL, src).set_index("day")
    assert (w.loc[: D - timedelta(days=6), "source"] == "actual").all()
    fc = w.loc[D - timedelta(days=5): D + timedelta(days=7)]
    assert (fc.source == "forecast").all()
    assert (fc.issued <= D).all()                                         # never issued after the planning date
    assert w.loc[D + timedelta(days=3), "lead"] == 3 and w.loc[D - timedelta(days=2), "lead"] == 1
    assert (w.loc[D + timedelta(days=8):, "source"] == "pessimistic").all()
    assert set(w.loc[D + timedelta(days=8):, "band_year"]) == {W.band_years(src)["pessimistic"]}
    assert w[["et0", "rain"]].notna().all().all()


def test_no_hindsight_scramble(src):
    """Changing every value the planning date could not know leaves the window unchanged."""
    base = W.window(D, D - timedelta(days=20), D + timedelta(days=20), "optimistic", CELL, src)
    era5 = src.era5.copy()
    late = era5.date > D - timedelta(days=W.ERA5_LAG)
    era5.loc[late, ["et0", "rain"]] = 99.0
    fc = src.forecast.copy()
    issued = fc.day - fc.lead.map(lambda n: timedelta(days=int(n)))
    fc.loc[issued > D, ["et0", "rain"]] = 99.0
    other = W.Sources(era5, src.normals, fc)
    again = W.window(D, D - timedelta(days=20), D + timedelta(days=20), "optimistic", CELL, other)
    assert np.allclose(base.et0, again.et0) and np.allclose(base.rain, again.rain)


def test_missing_forecast_falls_back_to_an_older_one(src):
    fc = src.forecast[~((src.forecast.lead == 2) & (src.forecast.day == D + timedelta(days=2)))]
    w = W.window(D, D + timedelta(days=2), D + timedelta(days=2), "normal", CELL, W.Sources(src.era5, src.normals, fc))
    assert w.iloc[0].source == "forecast" and w.iloc[0].lead == 3 and w.iloc[0].issued == D - timedelta(days=1)


def test_years_without_forecast_archive_use_the_band(src):
    w = W.window(date(2023, 7, 1), date(2023, 7, 1), date(2023, 7, 5), "normal", CELL, src)
    assert (w.source == "normal").all() and w.issued.isna().all()


def test_band_years_and_forecast_agreement(src):
    b = W.band_years(src)
    assert b["years_ranked"] == 30 and b["need_mm"]["optimistic"] < b["need_mm"]["pessimistic"]
    assert W.BAND_YEARS[0] <= b["optimistic"] <= W.BAND_YEARS[1]
    for y, c in W.forecast_check(src).items():                            # archived forecasts are close to ERA5
        assert 0.9 <= c["et0_ratio"] <= 1.15, (y, c)


def test_fao56_matches_the_fao_example():
    """FAO-56 example 18 (Uccle, 6 July): ET0 about 3.9 mm/day."""
    et0 = W.fao56_daily(np.array(12.3), np.array(21.5), np.array(63.0), np.array(84.0), np.array(2.78 * np.log(67.8 * 10 - 5.42) / 4.87),
                        np.array(22.07), 50.8, 187, 100.0)
    assert float(et0) == pytest.approx(3.9, abs=0.15)
