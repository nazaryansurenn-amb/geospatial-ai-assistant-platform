"""Canal ops demand v1: no hindsight over the whole dekad pipeline, data delays, dekads, tags, zero rules,
pre-sowing caps, alfalfa cuts, cloud-leak detector and the soil-water step."""
import copy
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import canal_ops_demand_v1 as m  # noqa: E402

CELL = "40.00_44.25"
CFG = copy.deepcopy(m.CFG)
CFG["cloud_leak"]["min_parcels"] = 5                     # small synthetic area: the detector still acts on scrambled data
PLAN = ["kc_plan", "kc_source", "view_age_days", "et0_mm_exp", "rain_mm_exp", "etc_mm_exp", "rain_eff_mm_exp",
        "depletion_start_mm_exp", "raw_mm_exp", "net_mm_full_exp", "net_mm_typical_exp", "charging_mm_exp",
        "presow_mm", "alfalfa_after_cut", "ripening", "stage_weight"]
ZERO = ["NO_CROP", "NON_FARM", "GREENHOUSE", "NOT_CLASSIFIABLE"]


def parcels():
    rows = [("f1", "FIELD_CROP", "ANN", 40, 1.0, "A"), ("f2", "FIELD_CROP", "ANN", 8, 0.5, "A"),
            ("f3", "FIELD_CROP", "ANN", 50, 1.0, "B"), ("a1", "FIELD_CROP", "ALF", 35, 1.2, "A"),
            ("o1", "ORCHARD", "ORC", 30, 1.5, "B"), ("v1", "VINEYARD", "VIN", 10, 0.8, "B"),
            ("u1", "CROPLAND_UNCERTAIN", "DEFER", 25, 0.7, "B"), ("n1", "NO_CROP", "NCU", 30, 2.0, "A"),
            ("g1", "GREENHOUSE", "GRH", 30, 0.3, "B"), ("x1", "NON_FARM", "NAT", 30, 0.4, "A"),
            ("s1", "NOT_CLASSIFIABLE", "NCI", 3, 0.005, "A")]
    p = pd.DataFrame(rows, columns=["internal_parcel_id", "land_class", "type", "mean_width_m", "area_ha", "community"])
    return m.classify(p.set_index("internal_parcel_id").assign(cell=CELL))


def profile(pid, t, year):
    field = 0.2 + 0.6 * np.exp(-((t - 25) / 7.0) ** 2) * (t >= 12)
    if pid in ("f1", "f2", "u1") or (pid == "f3" and year == 2025):
        return field
    if pid == "a1":                                       # alfalfa: green, cut every 7 views (two low views each)
        return np.where((t >= 10) & (t % 7 >= 5), 0.25, np.where(t >= 6, 0.75, 0.3))
    if pid == "o1":
        return np.clip(0.3 + 0.02 * t, 0.3, 0.7)
    if pid == "v1":
        return np.clip(0.2 + 0.015 * t, 0.2, 0.55)
    return np.full(len(t), 0.16)                          # f3 in 2024 (not cropped), no crop, greenhouse, ...


def views(year, seed=1):
    rng = np.random.default_rng(seed + year)
    dates = pd.date_range(f"{year}-02-03", f"{year}-10-30", freq="5D")
    t = np.arange(len(dates))
    rows = []
    for pid in parcels().index:
        ndvi = profile(pid, t, year) + rng.normal(0, 0.005, len(t))
        bsi = np.where(ndvi < 0.3, 0.12, 0.0)
        for d, n, b in zip(dates, ndvi, bsi):
            if pid == "f3" and year == 2025 and pd.Timestamp(f"{year}-06-01") <= d <= pd.Timestamp(f"{year}-06-30"):
                continue                                  # no clear view in June -> fallback Kc
            rows.append({"pid": pid, "date": d, "ndvi": float(n), "ndmi": float(0.3 * n - 0.05), "bsi": float(b),
                         "inner_ok": pid not in ("f2", "v1")})
    return pd.DataFrame(rows).sort_values(["pid", "date"]).reset_index(drop=True)


def weather(seed=2):
    rng = np.random.default_rng(seed)
    days = pd.date_range("2023-11-01", "2025-10-31")
    doy = days.dayofyear.to_numpy()
    w = pd.DataFrame({"cell": CELL, "date": days, "et0": 1 + 5 * np.clip(np.sin(np.pi * (doy - 45) / 280), 0, None)
                      + rng.normal(0, .3, len(days)).clip(-0.9, None), "rain": rng.exponential(0.8, len(days)), "tmax": 25.0})
    nd = np.arange(1, 366)
    n = pd.DataFrame({"cell": CELL, "doy": nd, "et0_fao_evapotranspiration": 1 + 5 * np.clip(np.sin(np.pi * (nd - 45) / 280), 0, None),
                      "precipitation_sum": 0.8, "temperature_2m_max": 25.0})
    return w, n


def pipeline(vs, w, n, cfg=CFG):
    """The whole dekad pipeline as built: leak detection per year -> mask -> add_causal -> daily Kc -> buckets."""
    leak = {y: [c["date"] for c in m.leak_candidates(v, cfg)] for y, v in vs.items()}
    return m.run_year(2025, parcels(), vs, w, n, leak, cfg).set_index(["dekad_id", "pid"]).sort_index()


def inputs():
    w, n = weather()
    return {2024: views(2024), 2025: views(2025)}, w, n


def scramble_after(df, cutoff, cols, seed=9):
    rng = np.random.default_rng(seed)
    df = df.copy()
    later = df.date > pd.Timestamp(cutoff)
    for c in cols:
        if c == "inner_ok":
            df.loc[later, c] = rng.uniform(size=later.sum()) > 0.5
        elif c in ("et0", "rain"):
            df.loc[later, c] = rng.uniform(0, 30, later.sum())
        else:
            df.loc[later, c] = rng.uniform(-0.5, 1.0, later.sum())
    return df


# ---------------------------------------------------------------- no hindsight (G1.1, G1.2, G1.7)
def test_plan_does_not_depend_on_data_after_as_of():
    vs, w, n = inputs()
    base = pipeline(vs, w, n)
    for dk in ("2025-03-D1", "2025-04-D2", "2025-06-D1", "2025-06-D3", "2025-07-D3", "2025-09-D2", "2025-10-D3"):
        as_of = base.loc[dk].as_of.iloc[0]
        vs2 = {2024: vs[2024], 2025: scramble_after(vs[2025], as_of - pd.Timedelta(days=1), ["ndvi", "ndmi", "bsi", "inner_ok"])}
        w2 = scramble_after(w, as_of - pd.Timedelta(days=6), ["et0", "rain"])
        alt = pipeline(vs2, w2, n)
        done = base.as_of <= as_of                        # this dekad and every dekad planned before it
        pd.testing.assert_frame_equal(base.loc[done, PLAN], alt.loc[done, PLAN], check_exact=False, rtol=1e-12)
        if dk != "2025-10-D3":                            # the scramble does reach later and actual values
            assert not np.allclose(base.loc[dk].rain_mm_act, alt.loc[dk].rain_mm_act)
            assert not np.allclose(base.loc[dk].etc_mm_act, alt.loc[dk].etc_mm_act)


def test_scene_usable_from_next_day_only():
    vs, w, n = inputs()
    base = pipeline(vs, w, n)
    as_of = base.loc["2025-07-D2"].as_of.iloc[0]
    for day, changes in ((as_of, False), (as_of - pd.Timedelta(days=1), True)):
        v = vs[2025][~((vs[2025].pid == "o1") & (vs[2025].date == day))]
        extra = pd.DataFrame([{"pid": "o1", "date": day, "ndvi": 0.31, "ndmi": 0.05, "bsi": 0.0, "inner_ok": True}])
        v = pd.concat([v, extra]).sort_values(["pid", "date"]).reset_index(drop=True)
        alt = pipeline({2024: vs[2024], 2025: v}, w, n)
        moved = not np.isclose(base.loc[("2025-07-D2", "o1"), "kc_plan"], alt.loc[("2025-07-D2", "o1"), "kc_plan"])
        assert moved == changes


def test_weather_complete_only_to_as_of_minus_6():
    vs, w, n = inputs()
    base = pipeline(vs, w, n)
    as_of = base.loc["2025-06-D2"].as_of.iloc[0]
    for lag, changes in ((5, False), (6, True)):
        w2 = w.copy()
        w2.loc[w2.date == as_of - pd.Timedelta(days=lag), "rain"] = 60.0
        alt = pipeline(vs, w2, n)
        moved = not np.allclose(base.loc["2025-06-D2"].depletion_start_mm_exp.fillna(0),
                                alt.loc["2025-06-D2"].depletion_start_mm_exp.fillna(0))
        assert moved == changes


# ---------------------------------------------------------------- dekads, tags, rules
def test_dekad_lengths_and_plan_dates():
    ds = m.dekads(2025)
    assert len(ds) == 24 and {d["days"] for d in ds} <= {8, 9, 10, 11}
    for mo in range(3, 11):
        assert sum(d["days"] for d in ds if d["month"] == mo) == pd.Timestamp(2025, mo, 1).days_in_month
    assert [d["days"] for d in ds if d["month"] in (3, 4)] == [10, 10, 11, 10, 10, 10]
    assert all(d["as_of"] == d["start"] - pd.Timedelta(days=2) for d in ds)
    assert [d["days"] for d in m.dekads(2024, [2])] == [10, 10, 9]


def test_tags_and_zero_rules():
    vs, w, n = inputs()
    d = pipeline(vs, w, n).reset_index()
    assert len(d) == 24 * len(parcels())
    assert set(d.kc_source) <= {"observed", "type-based", "fallback", "not applicable"}
    assert set(d.weather_tag) == {"expected"}
    z = d[d.land_class.isin(ZERO)]
    assert (z.kc_source == "not applicable").all() and z.kc_plan.isna().all()
    for c in ("net_mm_full_exp", "net_mm_typical_exp", "net_mm_full_act", "net_mm_typical_act", "net_m3_full_exp", "presow_mm"):
        assert (z[c] == 0).all()
    assert d[d.pid == "g1"].greenhouse_possibly_wells.all() and not d[d.pid != "g1"].greenhouse_possibly_wells.any()
    summer = d.month.between(5, 9)
    assert (d[summer & (d.pid == "f2")].kc_source == "type-based").all()          # narrow strip, no inner pixels
    assert (d[summer & (d.pid == "v1")].kc_source == "type-based").all()
    assert (d[summer & d.pid.isin(["f1", "o1"])].kc_source == "observed").all()
    assert (d[(d.dekad_id == "2025-06-D3") & (d.pid == "f3")].kc_source == "fallback").all()
    v = d[(d.pid == "v1") & (d.dekad_id == "2025-07-D2")].iloc[0]
    assert v.kc_plan == m.sr.CFG["land_types"]["kc_calendar"]["vineyard"][7 - 3]   # type calendar
    u = d[d.pid == "u1"]                                                            # use not specified: typical only
    assert (u.basis_only == "typical").all() and np.allclose(u.net_mm_full_exp, u.net_mm_typical_exp)
    crop = d[d.rule == "crop"]
    assert (crop.net_mm_typical_exp <= crop.net_mm_full_exp + 1e-9).all() and crop.net_mm_full_exp.sum() > 0
    assert (d.net_mm_full_exp >= 0).all() and (d.charging_mm_exp >= 0).all()
    assert not [c for c in d.columns if "gross" in c]                              # net only; efficiency in the core
    assert np.allclose(d.net_m3_full_exp, d.net_mm_full_exp * 10 * d.area_ha)


def test_presowing_window_cap_and_previous_crop():
    vs, w, n = inputs()
    d = pipeline(vs, w, n).reset_index()
    ps = d.groupby("pid").presow_mm.sum()
    assert ps["f1"] > 0                                   # cropped in 2024, bare before green-up
    assert ps["f3"] == 0                                  # not cropped in 2024
    assert ps[["a1", "o1", "v1"]].sum() == 0              # alfalfa and perennials never
    assert (ps <= CFG["presowing"]["season_cap_mm"] + 1e-9).all()
    assert (d[d.month >= 7].presow_mm == 0).all()
    cfg = copy.deepcopy(CFG)
    cfg["presowing"]["season_cap_mm"] = 10
    cfg["presowing"]["window_lead_days"] = 60
    d2 = pipeline(vs, w, n, cfg).reset_index()
    ps2 = d2.groupby("pid").presow_mm.sum()
    assert np.isclose(ps2["f1"], 10) and (ps2 <= 10 + 1e-9).all()


def test_alfalfa_floor_only_after_confirmed_cut():
    vs, w, n = inputs()
    d = pipeline(vs, w, n).reset_index()
    a = d[d.pid == "a1"]
    assert a.alfalfa_after_cut.any() and not d[d.pid != "a1"].alfalfa_after_cut.any()
    f = a[a.alfalfa_after_cut]
    assert (f.net_mm_full_exp >= 0.5 * f.etc_mm_exp - 1e-9).all() and (f.net_mm_full_exp > 0).all()
    v = vs[2025].copy()                                   # a single low view is no cut
    v.loc[v.pid == "a1", "ndvi"] = np.where(v[v.pid == "a1"].date == pd.Timestamp("2025-06-12"), 0.25, 0.75)
    v["bsi"] = np.where(v.ndvi < 0.3, 0.12, 0.0)
    d2 = pipeline({2024: vs[2024], 2025: v}, w, n).reset_index()
    assert not d2[d2.pid == "a1"].alfalfa_after_cut.any()


# ---------------------------------------------------------------- cloud-leak detector (G1.3)
def leak_views(dip_date="2025-06-11", share=0.8, n=60):
    rng = np.random.default_rng(3)
    dates = pd.date_range("2025-05-02", "2025-07-01", freq="5D")
    rows = []
    for i in range(n):
        for d in dates:
            nd = 0.7 + rng.normal(0, 0.01)
            if d == pd.Timestamp(dip_date) and i < share * n:
                nd -= 0.3
            rows.append({"pid": f"p{i}", "date": d, "ndvi": nd, "ndmi": 0.1, "bsi": 0.0, "inner_ok": True})
    return pd.DataFrame(rows).sort_values(["pid", "date"]).reset_index(drop=True)


def test_leak_detector_flags_area_wide_dip_only():
    hit = m.leak_candidates(leak_views())
    assert [h["date"] for h in hit] == ["2025-06-11"] and hit[0]["share_dipped"] >= 0.75
    assert m.leak_candidates(leak_views(share=0.05)) == []
    assert m.leak_candidates(leak_views(n=30)) == []      # too few parcels for an area-wide call


def test_leak_detector_is_causal_and_mask_precedes_causal_columns():
    v = leak_views()
    later = scramble_after(v, "2025-06-11", ["ndvi"])
    a = [h for h in m.leak_candidates(v) if h["date"] <= "2025-06-11"]
    b = [h for h in m.leak_candidates(later) if h["date"] <= "2025-06-11"]
    assert a == b
    o = m.masked_obs(v, ["2025-06-11"])
    assert pd.Timestamp("2025-06-11") not in set(o.date) and {"is_drop", "peak"} <= set(o.columns)


# ---------------------------------------------------------------- soil water
def test_bucket_step_balance():
    rng = np.random.default_rng(5)
    k = 5000
    taw = rng.choice([80.0, 140.0, 200.0], k)
    p = rng.choice([0.45, 0.5, 0.55], k)
    dr = rng.uniform(0, 1, k) * taw
    etc, rain, app = rng.uniform(0, 12, k), rng.exponential(3, k), rng.uniform(0, 20, k) * (rng.uniform(size=k) > 0.5)
    new, eta, ks, drain = m.bucket_step(dr, etc, rain, app, taw, p)
    assert (eta <= etc + 1e-12).all() and (eta >= 0).all() and ((ks >= 0) & (ks <= 1)).all()
    assert ((new >= 0) & (new <= taw + 1e-9)).all() and (drain >= 0).all()
    assert np.allclose(new - dr, eta - rain - app + drain, atol=1e-9)              # conservation
    x = m.bucket_step(10.0, 5.0, 0.0, 0.0, 80.0, 0.45)
    assert isinstance(x[0], float) and x[2] == 1.0 and np.isclose(x[0], 15.0)       # below RAW: no stress
    y = m.bucket_step(79.0, 8.0, 0.0, 0.0, 80.0, 0.45)
    assert y[1] < 8.0 and y[0] <= 80.0                                              # stressed near wilting point
    z = m.bucket_step(5.0, 2.0, 30.0, 0.0, 80.0, 0.45)
    assert z[0] == 0.0 and np.isclose(z[3], 23.0)                                   # surplus drains: 30 - 5 - 2
