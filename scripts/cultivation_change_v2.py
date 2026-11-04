"""Started / stopped cultivation and two crops per year, v2 (owner-approved rebuild, 2026-11-01).

ONE frozen rule for every year 2021-2026 from the daily Sentinel-2 parcel series (same window 1 Jan - 1 Nov in every
year; 2026 observed through 2026-11-01). Rules and every limit live in config/cultivation_change_v2.json; the rule
sources are the owner decisions and the two "second review" sections of
docs/CULTIVATION_CHANGE_DOUBLE_CROP_STRATEGY_2026_11_01_EN.md. Replaces v1's year_state() and its autumn-sowing class.
A NEW separate review layer: the approved area50 layer, land types v1.4 and the v1 outputs are read, never changed.

  cultivation_change_v2.py calibrate     tune the declared free limits on 2021 + 2023 vs the approved area50 codes,
                                         freeze them in the config (refuses once frozen)
  cultivation_change_v2.py build [OUT]   apply the frozen rule to 2021-2026 -> data/analysis/cultivation_change_v2/
                                         (refuses to overwrite)

Yearly state: cultivated / unclear / idle / unusable (config yearly_state.rule).
Started in Y: Y-3..Y-1 idle, Y cultivated. Stopped in Y: Y-3..Y-1 cultivated, Y idle; confirmed only when Y+1 is idle.
Two crops in Y: two sowings AND two harvests in Y (two closed cycles, no winter carry-over, no autumn sowing).
Each class has two tiers, strong and review. Seen from satellite images; not checked on the ground.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import itertools
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "config/cultivation_change_v2.json"
OUT = ROOT / "data/analysis/cultivation_change_v2"
STATE_CODE = {"cultivated": "C", "idle": "I", "unclear": "U", "unusable": "X"}
DAILY_COLS = ["internal_parcel_id", "observation_date", "ndvi_median", "ndvi_valid_fraction", "vegetation_fraction",
              "bsi_median", "ndvi_inner5_mean", "ndvi_inner5_valid_fraction"]


# ----------------------------------------------------------------------------------------------------------- config
def load_config(path=CONFIG):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def doy(year, mmdd):
    """Days since 1 January of `year` for 'MM-DD'."""
    m, d = (int(x) for x in mmdd.split("-"))
    return (dt.date(year, m, d) - dt.date(year, 1, 1)).days


def series_dir(cfg, year):
    return ROOT / cfg["inputs"]["series_2026" if year == 2026 else "series_2021_2025"]


# ----------------------------------------------------------------------------------------------- series primitives
def clean_daily(d, cfg):
    """Drop EVERY row with NDVI outside the valid range (or missing), before the cloud gate, the same way in every
    year; then valid rows only (ndvi_valid_fraction >= min), one row per parcel and date.
    Returns (clean frame, {"rows_ndvi_out_of_range": all rows dropped, "of_which_pass_cloud_gate": n, ...})."""
    s = cfg["series"]
    lo, hi = s["ndvi_valid_range"]
    bad = (d.ndvi_median < lo) | (d.ndvi_median > hi)
    missing = d.ndvi_median.isna()
    gate = d.ndvi_valid_fraction >= s["valid_fraction_min"]
    counts = {"rows_ndvi_out_of_range": int(bad.sum()), "of_which_pass_cloud_gate": int((bad & gate).sum()),
              "parcels_affected": int(d.internal_parcel_id[bad].nunique()),
              "dates_affected": int(d.observation_date[bad].nunique()),
              "max_ndvi_dropped": round(float(d.ndvi_median[bad].max()), 3) if bad.any() else None,
              "min_ndvi_dropped": round(float(d.ndvi_median[bad].min()), 3) if bad.any() else None,
              "rows_ndvi_missing": int(missing.sum())}
    d = d[~bad & ~missing & gate]
    d = d.sort_values(["internal_parcel_id", "observation_date", "ndvi_valid_fraction"])
    d = d.drop_duplicates(["internal_parcel_id", "observation_date"], keep="last")
    return d, counts


def smooth3(v):
    """3-date centred rolling median with the edges as the mean of two (= pandas rolling(3, center, min_periods=1))."""
    v = np.asarray(v, float)
    n = len(v)
    if n < 3:
        return v.copy() if n < 2 else np.full(n, v.mean())
    out = np.median(np.vstack([v[:-2], v[1:-1], v[2:]]), axis=0)
    return np.concatenate([[(v[0] + v[1]) / 2], out, [(v[-2] + v[-1]) / 2]])


def find_cycles(vs, bare, green):
    """Runs of non-bare dates (vs > bare) that contain vs >= green. Returns [(a, b)]: a = index of the bare date
    before the run (-1 when open at the start), b = index of the bare date after it (len(vs) when open at the end)."""
    n = len(vs)
    is_bare = vs <= bare
    out, i = [], 0
    while i < n:
        if vs[i] >= green and not is_bare[i]:
            j = i
            while j < n and not is_bare[j]:
                j += 1
            k = i
            while k > 0 and not is_bare[k - 1]:
                k -= 1
            out.append((k - 1, j))
            i = j + 1
        else:
            i += 1
    return out


def max_gap(days, start, end):
    """Longest gap (days) between valid dates covering [start, end]; the period edges count when no date lies
    outside them (no date at all = end - start)."""
    days = np.asarray(days)
    before = days[days < start]
    after = days[days > end]
    inside = days[(days >= start) & (days <= end)]
    pts = np.concatenate([[before.max() if len(before) else start], inside, [after.min() if len(after) else end]])
    return int(np.diff(pts).max())


# ------------------------------------------------------------------------------------------------- yearly features
def year_features(days, ndvi, vf, year, cfg):
    """Threshold-free features of one parcel-year (days = day of year inside the window, sorted)."""
    sig, fx = cfg["signal"], cfg["yearly_state"]["fixed"]
    days, ndvi, vf = np.asarray(days), np.asarray(ndvi, float), np.asarray(vf, float)
    f = {"n_valid": len(days), "max_gap_may_aug": max_gap(days, doy(year, fx["gap_period"][0]), doy(year, fx["gap_period"][1])),
         "n_green": 0, "green_span_days": 0, "first_green": np.nan, "last_green": np.nan, "peak": np.nan,
         "peak_doy": np.nan, "bare_start": False, "harvest_fall": 0.0, "spring_bare_before_july": False,
         "max_ndvi_jun_oct": np.nan, "max_ndvi": np.nan, "max_vegetation_share": np.nan}
    if not len(days):
        return f
    f["max_ndvi"] = float(ndvi.max())
    f["max_vegetation_share"] = float(np.nanmax(vf)) if np.isfinite(vf).any() else np.nan
    ip = int(np.argmax(ndvi))
    f["peak"], f["peak_doy"] = float(ndvi[ip]), int(days[ip])
    jo = (days >= doy(year, fx["idle_window_jun_oct"][0])) & (days <= doy(year, fx["idle_window_jun_oct"][1]))
    if jo.any():
        f["max_ndvi_jun_oct"] = float(ndvi[jo].max())
    g = (ndvi >= sig["green_ndvi"]) & (vf >= sig["green_vegetation_fraction"])
    if g.any():
        gi = np.flatnonzero(g)
        f["n_green"] = int(g.sum())
        f["first_green"], f["last_green"] = int(days[gi[0]]), int(days[gi[-1]])
        f["green_span_days"] = int(days[gi[-1]] - days[gi[0]])
        f["bare_start"] = bool((ndvi[:gi[0]] <= fx["bare_start_ndvi"]).any())
        after = ndvi[gi[-1] + 1:]
        f["harvest_fall"] = float(ndvi.max() - after.min()) if len(after) else 0.0
        late = days[gi[-1] + 1:]
        f["spring_bare_before_july"] = bool(((after <= sig["bare_ndvi"]) & (late < doy(year, fx["spring_only_bare_before"]))).any())
    return f


def classify_states(feat, limits, cfg, year):
    """Vectorised yearly state from a features frame (one row per parcel) and the (frozen or candidate) limits."""
    fx, sig = cfg["yearly_state"]["fixed"], cfg["signal"]
    n = feat.n_valid.to_numpy()
    usable = (n >= fx["min_valid_dates"]) & (feat.max_gap_may_aug.to_numpy() <= fx["max_gap_days_may_aug"])
    spring_only = feat.last_green.fillna(-1).to_numpy() < doy(year, fx["spring_only_last_green_before"])
    cultivated = (usable & (feat.n_green.to_numpy() >= limits["green_dates_min"])
                  & (feat.green_span_days.to_numpy() >= limits["green_span_days_min"])
                  & (feat.peak.fillna(0).to_numpy() >= fx["peak_min"]) & feat.bare_start.to_numpy(bool)
                  & (feat.harvest_fall.to_numpy() >= limits["harvest_fall_min"])
                  & (~spring_only | feat.spring_bare_before_july.to_numpy(bool)))
    idle = (usable & ~cultivated & (feat.n_green.to_numpy() == 0) & (feat.max_ndvi.fillna(9).to_numpy() < sig["green_ndvi"])
            & (feat.max_ndvi_jun_oct.fillna(9).to_numpy() < limits["idle_max_ndvi_jun_oct"])
            & (feat.max_vegetation_share.fillna(9).to_numpy() < limits["idle_max_vegetation_share"]))
    return np.where(~usable, "unusable", np.where(cultivated, "cultivated", np.where(idle, "idle", "unclear")))


# ---------------------------------------------------------------------------------------------------- two crops
def two_crops(days, ndvi, vf, bsi, inner, year, prev_winter_max, cfg, cut_count=None):
    """Two sowings and two harvests in `year`. Returns {"status", "fail", evidence...}.
    status: None (fewer than two cycles), "multi_cycle" (3+), "winter_carryover" (cycle 1 carried over or green in
    winter: a winter sowing, never counted), "autumn_sowing" (cycle 2 still green on 1 Nov, green-up after the
    window: never counted), "second_harvest_not_seen" (cycle 2 still green on 1 Nov, green-up in the window),
    "two_cycles" (two closed cycles; fail = first strong check not met, None = all signal checks met).
    cut_count: callable() -> cut events of the year's series (lazy, only called when needed)."""
    tc, sig = cfg["two_crops"], cfg["signal"]
    st = tc["strong"]
    days, ndvi = np.asarray(days), np.asarray(ndvi, float)
    out = {"status": None, "fail": None, "n_cycles": 0}
    if len(days) < 3:
        return out
    vs = smooth3(ndvi)
    cyc = find_cycles(vs, sig["bare_ndvi"], sig["green_ndvi"])
    out["n_cycles"] = len(cyc)
    if len(cyc) < 2:
        return out
    if len(cyc) >= 3:
        out["status"] = "multi_cycle"
        return out
    (a1, b1), (a2, b2) = cyc
    n = len(days)
    D = lambda mmdd: doy(year, mmdd)
    jf = days < D("03-01")
    winter = max(float(ndvi[jf].max()) if jf.any() else -1.0, prev_winter_max if prev_winter_max is not None and
                 np.isfinite(prev_winter_max) else -1.0)
    out["winter_max_ndvi"] = round(winter, 3)
    if a1 < 0 or winter >= st["winter_max_ndvi"]:
        out["status"] = "winter_carryover"
        return out
    onset2 = int(days[a2 + 1])
    out["green_up_2"] = onset2
    if b2 >= n:
        in_window = D(st["green_up_2"][0]) <= onset2 <= D(st["green_up_2"][1])
        out["status"] = "second_harvest_not_seen" if in_window else "autumn_sowing"
        return out
    out["status"] = "two_cycles"
    vf, bsi = np.asarray(vf, float), np.asarray(bsi, float)
    inner = np.asarray(inner, float)
    pk1 = a1 + int(np.argmax(ndvi[a1:b1 + 1]))
    pk2 = a2 + int(np.argmax(ndvi[a2:b2 + 1]))
    gap_idx = np.arange(b1, a2 + 1)
    gap_bare = gap_idx[ndvi[gap_idx] <= sig["bare_ndvi"]]
    onset1 = int(days[a1 + 1])
    first_bare_after1 = int(days[gap_bare[0]]) if len(gap_bare) else int(days[b1])
    out.update({"green_up_1": onset1, "peak_1_doy": int(days[pk1]), "peak_1": round(float(ndvi[pk1]), 3),
                "peak_2_doy": int(days[pk2]), "peak_2": round(float(ndvi[pk2]), 3),
                "harvest_1_doy": int(days[b1]), "harvest_2_doy": int(days[b2]),
                "gap_days": int(days[gap_bare[-1]] - days[gap_bare[0]]) if len(gap_bare) else 0})

    def green_dates(a, b):
        w = slice(a, b + 1)
        return int(np.sum((ndvi[w] >= sig["green_ndvi"]) & (vf[w] >= sig["green_vegetation_fraction"])))

    def fall(pk, b):
        return float(ndvi[pk] - ndvi[pk:b + 1].min())

    def nanmax(x):
        return float(np.nanmax(x)) if np.isfinite(x).any() else np.nan

    def nanmin(x):
        return float(np.nanmin(x)) if np.isfinite(x).any() else np.nan

    gap_bsi = bsi[gap_bare] if len(gap_bare) else np.array([np.nan])
    checks = [
        ("first_bare_start_before_15_feb", days[a1] >= D(st["first_bare_start_not_before"])),
        ("green_up_1_window", D(st["green_up_1"][0]) <= onset1 <= D(st["green_up_1"][1])),
        ("peak_1", ndvi[pk1] >= st["cycle_peak_min"] and D(st["peak_1_window"][0]) <= days[pk1] <= D(st["peak_1_window"][1])),
        ("harvest_1_bare_by_20_jul", days[b1] <= D(st["harvest_1_bare_by"])),
        ("gap_bare_dates", len(gap_bare) >= st["gap_bare_dates_min"]),
        ("gap_days", st["gap_days"][0] <= out["gap_days"] <= st["gap_days"][1]),
        ("gap_bsi", bool(np.isfinite(gap_bsi).any() and np.isfinite(bsi[[pk1, pk2]]).all()
                         and np.nanmedian(gap_bsi) > np.max(bsi[[pk1, pk2]]))),
        ("green_up_2_window", D(st["green_up_2"][0]) <= onset2 <= D(st["green_up_2"][1])),
        ("green_up_2_after_bare", onset2 - first_bare_after1 >= st["green_up_2_after_bare_days_min"]),
        ("peak_2", ndvi[pk2] >= st["cycle_peak_min"] and D(st["peak_2_window"][0]) <= days[pk2] <= D(st["peak_2_window"][1])),
        ("harvest_2_bare_by_1_nov", days[b2] <= D(st["harvest_2_bare_by"])),
        ("cycle_days", days[b1] - days[a1] >= st["cycle_days_min"] and days[b2] - days[a2] >= st["cycle_days_min"]),
        ("cycle_green_dates", green_dates(a1, b1) >= st["cycle_green_dates_min"] and green_dates(a2, b2) >= st["cycle_green_dates_min"]),
        ("harvest_fall", fall(pk1, b1) >= st["harvest_fall_min"] and fall(pk2, b2) >= st["harvest_fall_min"]),
        ("valid_date_gap", int(np.diff(days[a1:b2 + 1]).max()) <= st["max_valid_gap_days"]),
        ("inner_pixels", bool(nanmax(inner[a1:b1 + 1]) >= st["inner_peak_min"] and nanmax(inner[a2:b2 + 1]) >= st["inner_peak_min"]
                              and nanmin(inner[b1:a2 + 1]) <= st["inner_dip_max"])),
        ("cut_events", cut_count is None or cut_count() <= st["max_cut_events"]),
    ]
    out["fail"] = next((name for name, ok in checks if not ok), None)
    return out


# ------------------------------------------------------------------------------------------------- land / tiers
def land_code(type6, type_, attrs):
    """v1.4 land code (config land_codes)."""
    t6 = None if type6 is None or (isinstance(type6, float) and np.isnan(type6)) else str(type6)
    attrs = str(attrs or "")
    if t6 == "FIELD_CROP":
        return "ALF" if type_ == "ALF" or "alfalfa" in attrs else "ANN"
    if t6 == "CROPLAND_UNCERTAIN":
        return "CUN"
    if t6 == "NO_CROP":
        return "NAT" if type_ in ("NAT", "GRS") else "NCU"
    if t6 is None:
        return "NCI"
    return {"ORCHARD": "PER", "VINEYARD": "PER", "PERENNIAL": "PER", "GREENHOUSE": "GRH", "NON_FARM": "NON"}.get(t6, "UNC")


def alfalfa_flag(code, attrs, cuts):
    """Alfalfa: code ALF, 'alfalfa' in attrs_v11, or any year with >= 2 cut events in the v1.4 cut history."""
    c = json.loads(cuts) if isinstance(cuts, str) else (cuts or {})
    return code == "ALF" or "alfalfa" in str(attrs or "") or any((v or 0) >= 2 for v in c.values())


def start_stop(states, year, history=3):
    """states: {year: state}. -> ('started' | 'stopped' | None, status). The window needs `history` earlier years."""
    get = lambda y: states.get(y, "unusable")
    prev = [get(year - k) for k in range(1, history + 1)]
    nxt = states.get(year + 1)
    if get(year) == "cultivated" and all(s == "idle" for s in prev):
        status = ("confirmed_by_next_year" if nxt == "cultivated" else "not_continued_next_year" if nxt == "idle"
                  else "first_season_only" if nxt is None else "next_year_unclear")
        return "started", status
    if get(year) == "idle" and all(s == "cultivated" for s in prev):
        status = "confirmed" if nxt == "idle" else "resumed_next_year" if nxt == "cultivated" else "provisional"
        return "stopped", status
    return None, None


def tier_start_stop(kind, code, width, alfalfa, n_green, excluded, cfg, green_span=0):
    """-> (class, tier, reason). kind: 'started' / 'stopped'."""
    ss = cfg["start_stop"]
    strong = ss["strong"]
    if excluded or code not in ss["review_land_codes"] + ["ALF"]:
        return None, None, f"excluded land ({'household/road' if excluded else code})"
    if kind == "stopped" and alfalfa:
        return ss["alfalfa_stopped_class"], "review", "alfalfa not cut"
    reasons = []
    if alfalfa:
        reasons.append("alfalfa")
    if code not in strong["land_codes"]:
        reasons.append(f"land code {code}")
    if (width or 0) < strong["min_width_m"]:
        reasons.append("strip < 20 m")
    if kind == "started" and n_green < strong["green_dates_min_started"]:
        reasons.append("fewer than 6 green dates")
    if kind == "started" and green_span < strong["green_span_days_min_started"]:
        reasons.append("green < 60 days")
    return kind, ("review" if reasons else "strong"), "; ".join(reasons) or None


def tier_two_crops(status, fail, code, width, alfalfa, excluded, cfg):
    """-> (class, tier, reason)."""
    tc = cfg["two_crops"]
    if status in (None, "winter_carryover", "autumn_sowing"):
        return None, None, {"winter_carryover": "winter sowing (not counted)", "autumn_sowing": "autumn sowing (not counted)"}.get(status)
    if excluded or alfalfa or code not in tc["review_land_codes"]:
        return None, None, "excluded: " + ("household/road" if excluded else "alfalfa" if alfalfa else f"land code {code}")
    if status == "multi_cycle":
        return "multi_cycle_review", "review", "3+ cycles"
    if status == "second_harvest_not_seen":
        return "second_harvest_not_seen", "review", "second crop still green on 1 Nov (harvest not observed)"
    reasons = [fail] if fail else []
    if code not in tc["strong"]["land_codes"]:
        reasons.append(f"land code {code}")
    if (width or 0) < tc["strong"]["min_width_m"]:
        reasons.append("strip < 20 m")
    return "two_crops", ("review" if reasons else "strong"), "; ".join(reasons) or None


# ----------------------------------------------------------------------------------------------- data assembly
def prev_winter(cfg, year):
    """Max valid NDVI 1 Nov - 31 Dec of year-1 per parcel (None when year-1 has no series)."""
    p = series_dir(cfg, year - 1) / f"daily/{year - 1}.parquet"
    if year - 1 not in cfg["years"] or not p.exists():
        return None
    d = pd.read_parquet(p, columns=["internal_parcel_id", "observation_date", "ndvi_median", "ndvi_valid_fraction"])
    d = d[d.observation_date >= f"{year - 1}-11-01"]
    d, _ = clean_daily(d, cfg)
    return d.groupby("internal_parcel_id").ndvi_median.max().to_dict()


def analyse_year(cfg, year, parcels, with_two_crops=True):
    """Features (+ two-crop results) for every parcel in `parcels` for one year. -> (frame, data notes)."""
    from wp_core.land_types import cut_events
    raw = pd.read_parquet(series_dir(cfg, year) / f"daily/{year}.parquet", columns=DAILY_COLS)
    n_rows = len(raw)
    d, bad_counts = clean_daily(raw, cfg)
    del raw
    t = pd.to_datetime(d.observation_date)
    d = d.assign(doy=(t - pd.Timestamp(f"{year}-01-01")).dt.days.to_numpy())
    end = doy(year, cfg["series"]["window_end_inclusive"])
    d = d[(d.doy >= 0) & (d.doy <= end)].sort_values(["internal_parcel_id", "doy"])
    inner_ok = d.ndvi_inner5_valid_fraction.to_numpy(float) >= cfg["two_crops"]["strong"]["inner_valid_fraction_min"]
    inner = np.where(inner_ok, d.ndvi_inner5_mean.to_numpy(float), np.nan)
    ids = d.internal_parcel_id.to_numpy()
    cuts_ = np.r_[0, np.flatnonzero(ids[1:] != ids[:-1]) + 1, len(ids)]
    days, ndvi, vf, bsi = d.doy.to_numpy(), d.ndvi_median.to_numpy(float), d.vegetation_fraction.to_numpy(float), d.bsi_median.to_numpy(float)
    pw = prev_winter(cfg, year) if with_two_crops else None
    rows = {}
    for s, e in zip(cuts_[:-1], cuts_[1:]):
        pid = ids[s]
        f = year_features(days[s:e], ndvi[s:e], vf[s:e], year, cfg)
        if with_two_crops:
            def cut_count(s=s, e=e):
                base = dt.datetime(year, 1, 1)
                return cut_events([(base + dt.timedelta(days=int(x)), float(v)) for x, v in zip(days[s:e], ndvi[s:e])])
            r = two_crops(days[s:e], ndvi[s:e], vf[s:e], bsi[s:e], inner[s:e], year,
                          None if pw is None else pw.get(pid), cfg, cut_count)
            f.update({"tc_" + k: v for k, v in r.items()})
        rows[pid] = f
    feat = pd.DataFrame.from_dict(rows, orient="index").reindex(parcels)
    feat.index.name = "internal_parcel_id"
    feat["n_valid"] = feat.n_valid.fillna(0).astype(int)
    feat["max_gap_may_aug"] = feat.max_gap_may_aug.fillna(999)
    for c in ("n_green", "green_span_days", "harvest_fall"):
        feat[c] = feat[c].fillna(0)
    for c in ("bare_start", "spring_bare_before_july"):
        feat[c] = feat[c].fillna(False).astype(bool)
    notes = {"daily_rows": n_rows, **bad_counts, "previous_nov_dec_observed": pw is not None}
    return feat, notes


def approved_reference(cfg, parcels):
    """year -> Series(parcel -> 'active' / 'partial_not_passing' / 'idle' / 'unusable')."""
    a = ROOT / cfg["inputs"]["approved_area50"]
    reg = pd.read_parquet(a / "register.parquet", columns=["internal_parcel_id", "cadastre_code", "annual_state_codes"])
    reg = reg[reg.internal_parcel_id.isin(parcels)].dropna(subset=["annual_state_codes"])
    gate = pd.read_parquet(a / "annual_area_evidence.parquet").groupby(["cadastre_code", "analysis_year"]).passes_area50.max()
    out = {}
    codes = reg.annual_state_codes.map(json.loads)
    for i, y in enumerate(range(2021, 2026)):
        vals = []
        for cc, c in zip(reg.cadastre_code, codes):
            k = c[i]
            vals.append("active" if k == 1 or (k == 2 and bool(gate.get((cc, y), False))) else
                        "partial_not_passing" if k == 2 else "idle" if k == 3 else "unusable")
        out[y] = pd.Series(vals, index=reg.internal_parcel_id.to_numpy())
    return out


def agreement(states, ref, crosstab=True):
    """Shares for one year: states = Series(parcel -> state), ref = Series(parcel -> approved).
    Primary (strict) shares: approved 'unusable' (code 0) left out, approved 'partial_not_passing' counted as
    disagreement. The *_excl_partial shares also leave out approved partial_not_passing (the approved layer's own
    'unclear'); reported for information, never used to choose limits."""
    j = pd.DataFrame({"state": states}).join(ref.rename("approved"), how="inner")
    share = lambda num, den: round(num / den, 4) if den else None
    out = {}
    for suffix, judged in (("", j[j.approved != "unusable"]),
                           ("_excl_partial", j[~j.approved.isin(["unusable", "partial_not_passing"])])):
        c, i = judged[judged.state == "cultivated"], judged[judged.state == "idle"]
        out["cultivated_in_approved_active" + suffix] = share(int((c.approved == "active").sum()), len(c))
        out["idle_in_approved_idle" + suffix] = share(int((i.approved == "idle").sum()), len(i))
    judged = j[j.approved != "unusable"]
    act, idl = judged[judged.approved == "active"], judged[judged.approved == "idle"]
    out.update({"approved_active_called_cultivated": share(int((act.state == "cultivated").sum()), len(act)),
                "approved_idle_called_idle": share(int((idl.state == "idle").sum()), len(idl)),
                "n_cultivated": int((judged.state == "cultivated").sum()), "n_idle": int((judged.state == "idle").sum())})
    if crosstab:
        out["crosstab"] = pd.crosstab(j.state, j.approved).to_dict()
    return out


def landtypes(cfg):
    lt = pd.read_parquet(ROOT / cfg["inputs"]["land_types_v1_4"],
                         columns=["internal_parcel_id", "type6", "type", "attrs_v11", "cuts", "mean_width_m"])
    lt["land_code"] = [land_code(a, b, c) for a, b, c in zip(lt.type6, lt.type, lt.attrs_v11)]
    lt["alfalfa"] = [alfalfa_flag(a, b, c) for a, b, c in zip(lt.land_code, lt.attrs_v11, lt.cuts)]
    return lt.set_index("internal_parcel_id")


# -------------------------------------------------------------------------------------------------- calibrate
def calibrate(config_path=CONFIG):
    cfg = load_config(config_path)
    if cfg["frozen"]:
        raise SystemExit("config is frozen; limits are never re-tuned after freezing (make a new version).")
    cal = cfg["calibration"]
    parcels = landtypes(cfg).index
    ref = approved_reference(cfg, parcels)
    feats = {y: analyse_year(cfg, y, parcels, with_two_crops=False)[0] for y in cal["calibration_years"] + cal["held_out_years"]}
    free = cfg["yearly_state"]["free_limits"]
    expert = cfg["yearly_state"]["expert_values"]
    tg = cal["targets"]
    grid = []
    for combo in itertools.product(*free.values()):
        lim = dict(zip(free.keys(), combo))
        per = {y: agreement(pd.Series(classify_states(feats[y], lim, cfg, y), index=feats[y].index), ref[y], crosstab=False)
               for y in cal["calibration_years"]}
        ok = all((m["cultivated_in_approved_active"] or 0) >= tg["cultivated_in_approved_active_min"]
                 and (m["idle_in_approved_idle"] or 0) >= tg["idle_in_approved_idle_min"] for m in per.values())
        cover = float(np.mean([(m["approved_active_called_cultivated"] + m["approved_idle_called_idle"]) / 2 for m in per.values()]))
        dist = sum(abs(lim[k] - expert[k]) / (max(free[k]) - min(free[k])) for k in free)
        margin = min(min(m["cultivated_in_approved_active"] - tg["cultivated_in_approved_active_min"],
                         m["idle_in_approved_idle"] - tg["idle_in_approved_idle_min"]) for m in per.values())
        grid.append({"limits": lim, "targets_met": ok, "coverage": round(cover, 4), "distance_from_expert": round(dist, 3),
                     "margin": round(margin, 4), "per_year": {y: {k: v for k, v in m.items() if k != "crosstab"} for y, m in per.items()}})
    feasible = [g for g in grid if g["targets_met"]]
    if feasible:
        best = sorted(feasible, key=lambda g: (-g["coverage"], g["distance_from_expert"]))[0]
    else:   # no limit set reaches the targets: take the one closest to them and say so
        best = sorted(grid, key=lambda g: (-g["margin"], -g["coverage"], g["distance_from_expert"]))[0]
    lim = best["limits"]
    expert_row = next(g for g in grid if g["limits"] == {k: expert[k] for k in free})
    table = {}
    for y in cal["calibration_years"] + cal["held_out_years"]:
        m = agreement(pd.Series(classify_states(feats[y], lim, cfg, y), index=feats[y].index), ref[y])
        m["role"] = "calibration" if y in cal["calibration_years"] else "held_out"
        table[str(y)] = m
    cfg["yearly_state"]["limits"] = lim
    cfg["frozen"] = True
    cfg["calibration"]["record"] = {
        "frozen_at": dt.datetime.now().isoformat(timespec="seconds"), "grid_size": len(grid), "feasible": len(feasible),
        "targets_met": bool(feasible), "chosen": best, "expert_values_result": expert_row,
        "agreement": table}
    Path(config_path).write_text(json.dumps(cfg, indent=1, default=_json_default), encoding="utf-8")
    print(json.dumps({"chosen": lim, "feasible": len(feasible), "grid": len(grid),
                      "agreement": {y: {k: v for k, v in m.items() if k != "crosstab"} for y, m in table.items()}}, indent=1))


def _json_default(o):
    if isinstance(o, (np.integer,)):
        return int(o)
    if isinstance(o, (np.floating,)):
        return float(o)
    if isinstance(o, (np.bool_,)):
        return bool(o)
    return str(o)


# ------------------------------------------------------------------------------------------------------ build
def sha256(p):
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 22), b""):
            h.update(chunk)
    return h.hexdigest()


def build(out=OUT, config_path=CONFIG):
    out = Path(out)
    cfg = load_config(config_path)
    if not cfg["frozen"] or not cfg["yearly_state"]["limits"]:
        raise SystemExit("config is not frozen: run calibrate first.")
    if out.exists():
        raise SystemExit(f"{out} exists; results are never overwritten.")
    lim = cfg["yearly_state"]["limits"]
    years = cfg["years"]
    lt = landtypes(cfg)
    parcels = lt.index
    a = ROOT / cfg["inputs"]["approved_area50"]
    reg = pd.read_parquet(a / "register.parquet", columns=["internal_parcel_id", "household", "road_excluded", "change_class", "change_year"])
    reg = reg.set_index("internal_parcel_id").reindex(parcels)
    excluded = (reg.household.fillna(0).astype(bool) | reg.road_excluded.fillna(0).astype(bool)) | lt.land_code.isin(["PER", "GRH", "NON", "UNC"])
    ref = approved_reference(cfg, parcels)
    feats, notes = {}, {}
    for y in years:
        feats[y], notes[y] = analyse_year(cfg, y, parcels)
        feats[y]["state"] = classify_states(feats[y], lim, cfg, y)
        print(y, pd.Series(feats[y].state).value_counts().to_dict(), notes[y], flush=True)
    states = pd.DataFrame({y: feats[y].state for y in years})
    ss_first = cfg["start_stop"]["first_assessable_year"]
    pipeline = {}
    for y in years:
        e = pd.read_parquet(series_dir(cfg, y) / f"events/{y}.parquet", columns=["internal_parcel_id", "left_censored", "soil_first_date"])
        e = e[~e.left_censored & e.soil_first_date.notna() & (e.soil_first_date <= f"{y}-11-01")]
        pipeline[y] = e.groupby("internal_parcel_id").size()
    out.mkdir(parents=True)
    (out / "parcels").mkdir()
    summary = {"version": cfg["version"], "built_at": dt.datetime.now().isoformat(timespec="seconds"),
               "observed_through": {str(y): ("2026-11-01" if y == 2026 else f"{y}-11-01 (window cut-off)") for y in years},
               "limits": lim, "parcels": len(parcels), "data_notes": {str(y): notes[y] for y in years},
               "states": {}, "classes": {}, "approved_agreement": {}, "start_stop_vs_approved": {},
               "calibration": cfg["calibration"]["record"], "wording": cfg["wording"],
               "note": "candidate review layer; not validated; strong classes go to the owner's type-first check before any publication"}
    st_rows = states.to_dict("index")
    lt_rows = {k: pd.Series(v) for k, v in lt.to_dict("index").items()}
    excl = excluded.to_dict()
    for y in years:
        f = feats[y].to_dict("index")
        rows = []
        for pid in parcels:
            st = st_rows[pid]
            land = lt_rows[pid]
            r = pd.Series(f[pid])
            ss = (None, None)
            if y >= ss_first:
                ss = start_stop(st, y, cfg["start_stop"]["history_years"])
            ss_cls, ss_tier, ss_reason = (None, None, None)
            if ss[0]:
                ss_cls, ss_tier, ss_reason = tier_start_stop(ss[0], land.land_code, land.mean_width_m, land.alfalfa,
                                                             int(r.n_green), bool(excl[pid]), cfg, int(r.green_span_days))
            tc_status = r.get("tc_status")
            tc_status = None if (tc_status is None or (isinstance(tc_status, float) and np.isnan(tc_status))) else tc_status
            tc_fail = r.get("tc_fail")
            tc_fail = None if (tc_fail is None or (isinstance(tc_fail, float) and np.isnan(tc_fail))) else tc_fail
            tc_cls, tc_tier, tc_reason = tier_two_crops(tc_status, tc_fail, land.land_code, land.mean_width_m,
                                                        land.alfalfa, bool(excl[pid]), cfg)
            ev = {k[3:]: _plain(r[k]) for k in r.index if k.startswith("tc_") and k not in ("tc_status", "tc_fail")
                  and _plain(r[k]) is not None}
            for k in ("green_up_1", "green_up_2", "peak_1_doy", "peak_2_doy", "harvest_1_doy", "harvest_2_doy"):
                if k in ev:
                    ev[k] = str(dt.date(y, 1, 1) + dt.timedelta(days=int(ev[k])))
            rows.append({
                "internal_parcel_id": pid, "year": y, "state": r.state,
                "states_window": "".join(STATE_CODE[st[k]] if k in st else "-" for k in range(y - 3, y + 2)),
                "n_valid": int(r.n_valid), "n_green": int(r.n_green), "green_span_days": int(r.green_span_days),
                "peak_ndvi": _round(r.peak), "peak_date": _date(y, r.peak_doy), "first_green": _date(y, r.first_green),
                "last_green": _date(y, r.last_green), "harvest_fall": _round(r.harvest_fall),
                "max_ndvi_jun_oct": _round(r.max_ndvi_jun_oct), "max_gap_may_aug": int(min(r.max_gap_may_aug, 999)),
                "land_code_v14": land.land_code, "type6_v14": land.type6, "width_m": _round(land.mean_width_m, 1),
                "alfalfa": bool(land.alfalfa), "excluded": bool(excl[pid]),
                "start_stop_class": ss_cls, "start_stop_tier": ss_tier, "start_stop_status": ss[1] if ss_cls else None,
                "start_stop_reason": ss_reason if ss[0] else None,
                "two_crops_class": tc_cls, "two_crops_tier": tc_tier, "two_crops_reason": tc_reason,
                "two_crops_signal": tc_status, "two_crops_first_fail": tc_fail,
                "n_cycles": int(r.get("tc_n_cycles") or 0) if pd.notna(r.get("tc_n_cycles")) else 0,
                "two_crops_evidence": json.dumps(ev) if ev else None,
                "pipeline_cycles": int(pipeline[y].get(pid, 0)),
                "approved_state": ref[y].get(pid) if y in ref else None})
        t = pd.DataFrame(rows)
        t.to_parquet(out / "parcels" / f"{y}.parquet", index=False)
        summary["states"][str(y)] = t.state.value_counts().to_dict()
        cls = {}
        for col, tier in (("start_stop_class", "start_stop_tier"), ("two_crops_class", "two_crops_tier")):
            sub = t[t[col].notna()]
            for (c, ti), n in sub.groupby([col, tier]).size().items():
                cls.setdefault(c, {})[ti] = int(n)
        for c in ("started", "stopped"):
            sub = t[t.start_stop_class == c]
            if len(sub):
                cls[c]["by_status"] = {f"{ti}/{s}": int(n) for (ti, s), n in sub.groupby(["start_stop_tier", "start_stop_status"]).size().items()}
        summary["classes"][str(y)] = cls
        if y in ref:
            summary["approved_agreement"][str(y)] = {**agreement(t.set_index("internal_parcel_id").state, ref[y]),
                                                      "role": "calibration" if y in cfg["calibration"]["calibration_years"] else "held_out"}
            ap = reg.change_class.where(reg.change_year == y, "other").rename("approved_change")
            sub = t[t.start_stop_class.notna()].set_index("internal_parcel_id")
            if len(sub):
                x = sub.join(ap)
                summary["start_stop_vs_approved"][str(y)] = {f"{a_}/{b_}": int(n) for (a_, b_), n in
                                                             x.groupby(["start_stop_class", "approved_change"]).size().items()}
    (out / "summary.json").write_text(json.dumps(summary, indent=1, default=_json_default), encoding="utf-8")
    inputs = [config_path, Path(__file__), ROOT / cfg["inputs"]["land_types_v1_4"], a / "register.parquet",
              a / "annual_area_evidence.parquet", ROOT / "wp_core/land_types.py"]
    for y in years:
        inputs += [series_dir(cfg, y) / f"daily/{y}.parquet", series_dir(cfg, y) / f"events/{y}.parquet"]
    manifest = {"version": cfg["version"], "built_at": summary["built_at"], "command": "scripts/cultivation_change_v2.py build",
                "inputs": {_rel(p): sha256(p) for p in inputs},
                "outputs": {str(p.relative_to(out)).replace("\\", "/"): sha256(p) for p in sorted(out.rglob("*.parquet"))
                            } | {"summary.json": sha256(out / "summary.json")},
                "ndvi_out_of_range_dropped": {str(y): notes[y] for y in years},
                "never_changed": ["approved area50 layer", "land types v1.4", "cultivation_change_v1 outputs"]}
    (out / "manifest.json").write_text(json.dumps(manifest, indent=1), encoding="utf-8")
    print(json.dumps({"classes": summary["classes"]}, indent=1))


def _rel(p):
    p = Path(p).resolve()
    return str(p.relative_to(ROOT) if p.is_relative_to(ROOT) else p).replace("\\", "/")


def _plain(v):
    if v is None or (isinstance(v, float) and np.isnan(v)):
        return None
    if isinstance(v, (np.integer,)):
        return int(v)
    if isinstance(v, (np.floating,)):
        return float(v)
    return v


def _round(v, k=3):
    return None if v is None or pd.isna(v) else round(float(v), k)


def _date(year, d):
    return None if d is None or pd.isna(d) else str(dt.date(year, 1, 1) + dt.timedelta(days=int(d)))


if __name__ == "__main__":
    sys.path.insert(0, str(ROOT))
    if sys.argv[1:2] == ["calibrate"]:
        calibrate()
    elif sys.argv[1:2] == ["build"]:
        build(*sys.argv[2:3])
    else:
        sys.exit(__doc__)
