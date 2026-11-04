"""Season replay v1: no hindsight (later data cannot change a week), harvest guard, levels, crop coefficient."""
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import season_replay_v1 as r  # noqa: E402

CELL = "40.00_44.25"


def parcels():
    return pd.DataFrame({"community": ["A", "A", "B", "B"], "area_ha": [1.0, 2.0, 1.5, 0.5],
                         "mean_width_m": [40, 30, 25, 8], "ltype": ["field", "field", "orchard", "vineyard"],
                         "cell": [CELL] * 4}, index=pd.Index(["p1", "p2", "p3", "p4"], name="internal_parcel_id"))


def views(year=2026, seed=1):
    rng = np.random.default_rng(seed)
    dates = pd.date_range(f"{year}-02-03", f"{year}-10-30", freq="5D")
    rows = []
    for i, pid in enumerate(["p1", "p2", "p3", "p4"]):
        t = np.arange(len(dates))
        ndvi = 0.2 + 0.6 * np.exp(-((t - 25 - 3 * i) / 9.0) ** 2) + rng.normal(0, 0.01, len(t))
        if pid == "p1":                                   # harvest in early July: drop and bare soil, two views
            cut = dates >= pd.Timestamp(f"{year}-07-05")
            ndvi = np.where(cut, 0.22, ndvi)
        bsi = np.where(ndvi < 0.3, 0.12, 0.0)
        rows += [{"pid": pid, "date": d, "ndvi": float(n), "ndmi": float(0.3 * n - 0.05), "bsi": float(b), "inner_ok": pid != "p4"}
                 for d, n, b in zip(dates, ndvi, bsi)]
    return pd.DataFrame(rows).sort_values(["pid", "date"]).reset_index(drop=True)


def weather(year=2026, seed=2):
    rng = np.random.default_rng(seed)
    days = pd.date_range(f"{year}-01-01", f"{year}-10-31")
    w = pd.DataFrame({"cell": CELL, "date": days, "et0": 3 + 3 * np.clip(np.sin(np.pi * (days.dayofyear.to_numpy() - 60) / 240), 0, None) + rng.normal(0, .2, len(days)),
                      "rain": rng.exponential(0.5, len(days)), "tmax": 25.0})
    n = pd.DataFrame({"cell": CELL, "doy": range(1, 366), "et0_fao_evapotranspiration": 4.0, "precipitation_sum": 0.5, "temperature_2m_max": 25.0})
    return w, n


def run_week(o, w, n, wk, refs=({}, {})):
    cut = r.week_cutoff(2026, wk)
    return r.week_parcels(r.add_causal(o.copy()), cut, parcels(), refs, r.week_weather(w, n, cut))


def scramble_after(df, cutoff, cols, seed=9):
    rng = np.random.default_rng(seed)
    df = df.copy()
    later = df.date > pd.Timestamp(cutoff)
    for c in cols:
        df.loc[later, c] = rng.uniform(-0.5, 1.0, later.sum()) if c != "et0" else rng.uniform(0, 15, later.sum())
    return df


def test_week_does_not_depend_on_later_data():
    o, (w, n) = views(), weather()
    for wk in (14, 22, 28, 33):
        cut = r.week_cutoff(2026, wk)
        a = run_week(o, w, n, wk)
        b = run_week(scramble_after(o, cut, ["ndvi", "ndmi", "bsi"]), scramble_after(w, cut, ["et0", "rain"]), n, wk)
        cols = ["state", "kc", "d", "s", "w", "g", "score", "net_m3"]
        pd.testing.assert_frame_equal(a[cols], b[cols])


def test_harvest_is_guarded_not_scored():
    o, (w, n) = views(), weather()
    p = run_week(o, w, n, 28)                             # week ending 12 July, one week after the harvest drop
    assert p.loc["p1", "state"] in ("guarded", "bare")
    assert np.isnan(p.loc["p1", "score"])


def test_single_view_dip_is_not_a_harvest():
    o = views()
    i = o[(o.pid == "p2") & (o.date == pd.Timestamp("2026-06-17"))].index
    o.loc[i, ["ndvi", "bsi"]] = [0.2, 0.2]                # one cloudy view
    c = r.add_causal(o)
    assert not c.loc[c.pid == "p2", "is_drop"].any()


def test_dryness_needs_wide_parcel_and_reference():
    o, (w, n) = views(), weather()
    wk = 26
    refs = ({(pid, wk): 0.5 for pid in ["p1", "p2", "p3", "p4"]}, {})
    p = run_week(o, w, n, wk, refs)
    assert p.loc["p4", "s_assessed"] is np.False_ or not p.loc["p4", "s_assessed"] or p.loc["p4", "mean_width_m"] >= 20 \
        or bool(p.loc["p4", "inner_ok"])
    assert p.loc["p3", "s"] > 0                          # NDMI well below the reference -> dryness signal


def test_incomplete_weather_week_scores_nothing():
    o, (w, n) = views(), weather()
    cut = r.week_cutoff(2026, 30)
    w = w[w.date != pd.Timestamp(cut)]
    p = r.week_parcels(r.add_causal(o.copy()), cut, parcels(), ({}, {}), r.week_weather(w, n, cut))
    assert set(p.state) <= {"no_weather", "no_view"}


def test_levels_and_coverage():
    assert r.level(65, 0.5) == "attention"
    assert r.level(65, 0.35) == "watch"                   # high score but thin coverage
    assert r.level(45, 0.9) == "watch"
    assert r.level(10, 0.9) == "none"
    assert r.level(80, 0.2) == "unassessed"
    assert r.level(float("nan"), 0.9) == "unassessed"


def test_kc_from_ndvi_bounds():
    k = r.kc_from_ndvi(np.array([0.0, 0.15, 0.6, 0.95]), 1.1)
    assert k[0] == k[1] == 0.15 and k[-1] == 1.1 and 0.15 < k[2] <= 1.1


def test_week_cutoff_is_sunday():
    c = r.week_cutoff(2026, 28)
    assert c.isoweekday() == 7 and c.isocalendar()[1] == 28
