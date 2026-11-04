"""Started / stopped cultivation and two crops per year, v1 (owner decisions 2026-11-01; strategy and decisions in
docs/CULTIVATION_CHANGE_DOUBLE_CROP_STRATEGY_2026_11_01_EN.md). A NEW separate review layer: the approved 8526
cultivation-change layer (activity_change_area50_20260907_v1) and the land types releases are read, never changed.

  cultivation_change_v1.py build [YEAR]   -> data/analysis/cultivation_change_v1/{YEAR}/ (default 2025)

Rules (owner):
- Yearly state from the approved yearly codes (1 active; 2 partial = active only when it passes the >=50% area gate,
  otherwise unclear; 3 idle; 0 unusable).
- STARTED in Y: Y-2 and Y-1 idle, Y active; "strong" when Y-3 is also idle. Any unusable / unclear year in the window:
  no claim. STOPPED in Y: Y-3, Y-2, Y-1 active, Y idle; provisional until Y+1 is also idle.
- TWO CROPS of Y, counted BY SOWING YEAR: crop cycles that start in Y (not carried over from the previous autumn), each
  peaking at NDVI >= 0.5 and lasting >= 45 days, separated by bare soil (NDVI <= 0.25 between them); a winter crop sown in
  autumn Y-1 belongs to Y-1. An autumn-Y sowing is confirmed by a carried-over cycle in Y+1 peaking in March-May >= 0.5
  (needs Y+1 data). Three or more cycles -> "multi-cycle, review". Alfalfa (cut history) is excluded.
- Strips under 20 m mean width: not assessable unless the inner pixels show the same pattern.
- Perennials, greenhouses, not-farmland (land types v1.3), households and roads: own review states, never start/stop.
"""
from __future__ import annotations
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
AREA50 = ROOT / "data/analysis/activity_change/activity_change_area50_20260907_v1"
SERIES = {y: ROOT / "data/analysis/observation_screening/transitions_area_20260906_v1" for y in range(2021, 2026)}
SERIES[2026] = ROOT / "data/analysis/observation_screening/transitions_area_2026_v1"
LANDTYPES = ROOT / "server_data/review/alphaearth_photo_review_20261030_v1/releases/landtypes_v1_3/parcels.parquet"
OUT = ROOT / "data/analysis/cultivation_change_v1"
YEARS = [2021, 2022, 2023, 2024, 2025]
NOT_FIELD = {"VINEYARD", "ORCHARD", "PERENNIAL", "GREENHOUSE", "NON_FARM"}
PEAK, BARE, MIN_DAYS, THIN_M = .5, .25, 45, 20


def effective_states(codes, gates):
    """codes: approved yearly codes 2021-2025; gates: year -> passes the >=50% area gate. -> year -> state."""
    out = {}
    for y, c in zip(YEARS, codes):
        out[y] = ("active" if c == 1 or (c == 2 and gates.get(y)) else "unclear" if c == 2
                  else "idle" if c == 3 else "unusable")
    return out


def start_stop(states, year):
    """'started' / 'started_strong' / 'stopped_provisional' / None from the effective yearly states."""
    get = lambda y: states.get(y, "unusable")
    if get(year) == "active" and get(year - 1) == "idle" and get(year - 2) == "idle":
        return "started_strong" if get(year - 3) == "idle" else "started"
    if get(year) == "idle" and all(get(year - k) == "active" for k in (1, 2, 3)):
        return "stopped_provisional"
    return None


def crop_cycles(events, series, inner=False):
    """Cycles sown in the year (not carried over), each with peak >= PEAK and >= MIN_DAYS long.
    events: rows of one parcel; series: (dates, ndvi) of the same parcel. Returns [(start, peak_date, end, peak)]."""
    dates, vals = series
    out = []
    for e in events.itertuples():
        if e.left_censored:
            continue
        s, en = pd.Timestamp(e.start_date), pd.Timestamp(e.end_date)
        window = (dates >= s) & (dates <= en)
        if not window.any() or (en - s).days < MIN_DAYS:
            continue
        peak = float(np.nanmax(vals[window]))
        if peak >= PEAK:
            out.append((s, pd.Timestamp(e.peak_date), en, peak))
    return sorted(out)


def bare_between(a, b, series):
    """True when NDVI drops to bare soil (<= BARE) on 2+ dates between the peaks of cycles a and b."""
    dates, vals = series
    window = (dates > a[1]) & (dates < b[1])
    return int(np.sum(vals[window] <= BARE)) >= 2


def two_crops(cycles, series):
    """'two_crops' / 'multi_cycle_review' / None."""
    if len(cycles) >= 3:
        return "multi_cycle_review"
    if len(cycles) == 2 and bare_between(cycles[0], cycles[1], series):
        return "two_crops"
    return None


def build(year=2025):
    year = int(year)
    target = OUT / str(year)
    if target.exists():
        raise SystemExit(f"{target} exists; results are never overwritten.")
    reg = pd.read_parquet(AREA50 / "register.parquet").drop(columns="geometry")
    gate = pd.read_parquet(AREA50 / "annual_area_evidence.parquet")
    gate = gate.groupby(["cadastre_code", "analysis_year"]).passes_area50.max()
    lt = pd.read_parquet(LANDTYPES)[["internal_parcel_id", "type6", "mean_width_m", "attrs_v11", "cuts", "area_ha"]]
    d = reg.merge(lt, on="internal_parcel_id", how="inner")            # the 22,802 eligible parcels
    daily = pd.read_parquet(SERIES[year] / f"daily/{year}.parquet",
                            columns=["internal_parcel_id", "observation_date", "ndvi_median", "ndvi_valid_fraction",
                                     "ndvi_inner5_mean", "ndvi_inner5_valid_fraction"])
    daily = daily[daily.ndvi_valid_fraction >= .8]
    daily["observation_date"] = pd.to_datetime(daily.observation_date)
    ev = pd.read_parquet(SERIES[year] / f"events/{year}.parquet")
    by_ev = dict(tuple(ev.groupby("internal_parcel_id")))
    by_day = {p: (g.observation_date.to_numpy(), g.ndvi_median.to_numpy(float),
                  np.where(g.ndvi_inner5_valid_fraction.to_numpy(float) >= .5, g.ndvi_inner5_mean.to_numpy(float), np.nan))
              for p, g in daily.sort_values("observation_date").groupby("internal_parcel_id")}
    # thin-strip exception for started/stopped: the inner pixels must show the same active / idle years
    thin_ids = set(d[d.mean_width_m.fillna(0) < THIN_M].internal_parcel_id)
    inner_max = {}
    for y in range(year - 3, year + 1):
        if not (SERIES[y] / f"daily/{y}.parquet").exists():
            continue
        g = pd.read_parquet(SERIES[y] / f"daily/{y}.parquet", columns=["internal_parcel_id", "ndvi_inner5_mean", "ndvi_inner5_valid_fraction"])
        g = g[g.internal_parcel_id.isin(thin_ids) & (g.ndvi_inner5_valid_fraction >= .5)]
        inner_max[y] = g.groupby("internal_parcel_id").ndvi_inner5_mean.max().to_dict()

    def inner_agrees(pid, states):
        """Every year of the start/stop window: inner max >= PEAK when active, < 0.35 when idle."""
        for y in range(year - 3, year + 1):
            v, s = inner_max.get(y, {}).get(pid), states.get(y)
            if s in ("active", "idle") and (v is None or (s == "active") != (v >= PEAK) or (s == "idle" and v >= .35)):
                return False
        return True

    rows = []
    for r in d.itertuples():
        codes = json.loads(r.annual_state_codes) if isinstance(r.annual_state_codes, str) else None
        cuts = json.loads(r.cuts) if isinstance(r.cuts, str) else {}
        alfalfa = "alfalfa" in str(r.attrs_v11 or "") or sum((cuts.get(str(y)) or 0) >= 2 for y in range(year - 3, year + 1)) >= 2
        thin = (r.mean_width_m or 0) < THIN_M
        review = ("household or road" if r.household or r.road_excluded else
                  "perennial / greenhouse / not farmland" if r.type6 in NOT_FIELD else None)
        # started / stopped
        ss, ss_inner_ok = None, None
        if codes and len(codes) == 5 and year <= 2025:
            states = effective_states(codes, {y: bool(gate.get((r.cadastre_code, y), False)) for y in YEARS})
            ss = start_stop(states, year)
            if ss and thin:
                ss_inner_ok = inner_agrees(r.internal_parcel_id, states)
        # two crops (by sowing year)
        tc, cycles, inner_ok = None, [], None
        if r.internal_parcel_id in by_ev and r.internal_parcel_id in by_day:
            dates, ndvi, inner = by_day[r.internal_parcel_id]
            cycles = crop_cycles(by_ev[r.internal_parcel_id], (dates, ndvi))
            tc = two_crops(cycles, (dates, ndvi))
            if thin and tc:
                inner_cycles = crop_cycles(by_ev[r.internal_parcel_id], (dates, np.nan_to_num(inner, nan=-1)))
                inner_ok = two_crops(inner_cycles, (dates, np.nan_to_num(inner, nan=1))) == tc
        ss_state = (None if not ss else f"review: {review}" if review else
                    "alfalfa rotation, review" if alfalfa else "not assessable (strip < 20 m)" if thin and not ss_inner_ok else ss)
        tc_state = (None if not tc else f"review: {review}" if review else "alfalfa, excluded" if alfalfa else
                    "not assessable (strip < 20 m)" if thin and not inner_ok else tc)
        rows.append({"internal_parcel_id": r.internal_parcel_id, "area_ha": r.area_ha, "width_m": r.mean_width_m,
                     "land_type_v13": r.type6, "approved_change": r.change_class, "start_stop_raw": ss,
                     "start_stop": ss_state, "two_crops_raw": tc, "two_crops": tc_state, "cycles_sown_in_year": len(cycles),
                     "cycle_peaks": json.dumps([[str(c[1].date()), round(c[3], 2)] for c in cycles])})
    t = pd.DataFrame(rows)
    target.mkdir(parents=True)
    t.to_parquet(target / "parcels.parquet", index=False)
    cnt = lambda col: t[col].value_counts().to_dict()
    cross = pd.crosstab(t.start_stop.fillna("none"), t.approved_change.fillna("none")).to_dict()
    summary = {"year": year, "rules": __doc__.split("Rules (owner):")[1].strip(), "parcels": len(t),
               "start_stop": cnt("start_stop"), "two_crops": cnt("two_crops"),
               "start_stop_vs_approved": cross, "note": "candidate review layer; not validated; 2025 stops stay provisional "
               "until 2026 is assessed; autumn-2025 sowings need the 2026 data"}
    (target / "summary.json").write_text(json.dumps(summary, indent=1, default=str), encoding="utf-8")
    print(json.dumps({k: summary[k] for k in ("start_stop", "two_crops")}, indent=1, default=str))


def year_state(year, strict=False):
    """2026 has no approved yearly code: a simple rule, checked against the approved 2025 codes (2026-11-01):
    active = >= 2 valid March-October dates with NDVI >= 0.5 over >= 50% of the parcel (96% of approved-active 2025
    parcels; only 2 approved-active parcels called idle); idle = >= 10 valid dates and NDVI never >= 0.35; else unclear."""
    d = pd.read_parquet(SERIES[year] / f"daily/{year}.parquet",
                        columns=["internal_parcel_id", "observation_date", "ndvi_median", "ndvi_valid_fraction", "vegetation_fraction"])
    d = d[d.ndvi_valid_fraction >= .8]
    m = pd.to_datetime(d.observation_date).dt.month
    d = d[(m >= 3) & (m <= 10)]
    g = d.groupby("internal_parcel_id")
    green = d[(d.ndvi_median >= PEAK) & (d.vegetation_fraction >= .5)].groupby("internal_parcel_id").size()
    t = pd.DataFrame({"n": g.size(), "mx": g.ndvi_median.max()}).join(green.rename("green")).fillna({"green": 0})
    need = 4 if strict else 2                       # strict: a sustained green season, not a short weed flush
    return pd.Series(np.where(t.green >= need, "active", np.where((t.n >= 10) & (t.mx < .35), "idle", "unclear")), index=t.index)


def build2026():
    """2026 additions, observed through 2026-11-01: confirm or reject the 2025 stops; started / stopped in 2026; autumn-2025
    sowings added to the 2025 two-crop count; two crops sown in 2026. Writes cultivation_change_v1/2026/."""
    target = OUT / "2026"
    if target.exists():
        raise SystemExit(f"{target} exists; results are never overwritten.")
    base = pd.read_parquet(OUT / "2025/parcels.parquet")
    s26 = year_state(2026)
    s26_strict = year_state(2026, strict=True)
    lt = pd.read_parquet(LANDTYPES)[["internal_parcel_id", "attrs_v11", "cuts"]].set_index("internal_parcel_id")
    flags = pd.read_parquet(AREA50 / "register.parquet", columns=["internal_parcel_id", "household", "road_excluded"]).set_index("internal_parcel_id")

    def exclusion(pid, land_type, width):
        """The same exclusions as 2025: households/roads, perennials etc., alfalfa history, strips under 20 m."""
        if pid in flags.index and (flags.at[pid, "household"] or flags.at[pid, "road_excluded"]):
            return "review: household or road"
        if land_type in NOT_FIELD:
            return "review: perennial / greenhouse / not farmland"
        cuts = json.loads(lt.at[pid, "cuts"]) if pid in lt.index and isinstance(lt.at[pid, "cuts"], str) else {}
        if "alfalfa" in str(lt.at[pid, "attrs_v11"] if pid in lt.index else "") or sum((cuts.get(str(y)) or 0) >= 2 for y in range(2022, 2026)) >= 2:
            return "alfalfa, review"
        if (width or 0) < THIN_M:
            return "not assessable (strip < 20 m)"
        return None
    reg = pd.read_parquet(AREA50 / "register.parquet", columns=["internal_parcel_id", "cadastre_code", "annual_state_codes"])
    gate = pd.read_parquet(AREA50 / "annual_area_evidence.parquet").groupby(["cadastre_code", "analysis_year"]).passes_area50.max()
    states = {}
    for r in reg.dropna(subset=["annual_state_codes"]).itertuples():
        st = effective_states(json.loads(r.annual_state_codes), {y: bool(gate.get((r.cadastre_code, y), False)) for y in YEARS})
        st[2026] = s26.get(r.internal_parcel_id, "unusable")
        states[r.internal_parcel_id] = st
    ev26 = pd.read_parquet(SERIES[2026] / "events/2026.parquet")
    d26 = pd.read_parquet(SERIES[2026] / "daily/2026.parquet", columns=["internal_parcel_id", "observation_date", "ndvi_median", "ndvi_valid_fraction"])
    d26 = d26[d26.ndvi_valid_fraction >= .8]
    d26["observation_date"] = pd.to_datetime(d26.observation_date)
    peak26 = d26.groupby("internal_parcel_id")
    by_day = {p: (g.observation_date.to_numpy(), g.ndvi_median.to_numpy(float)) for p, g in peak26}
    by_ev = dict(tuple(ev26.groupby("internal_parcel_id")))

    def autumn_sown(pid):
        """A cycle carried into 2026 (sown in autumn 2025) that peaks in March-May at NDVI >= PEAK."""
        e = by_ev.get(pid)
        if e is None or pid not in by_day:
            return False
        dates, vals = by_day[pid]
        for x in e[e.left_censored].itertuples():
            pk = pd.Timestamp(x.peak_date)
            win = (dates >= pd.Timestamp(x.start_date)) & (dates <= pd.Timestamp(x.end_date))
            if 3 <= pk.month <= 5 and win.any() and np.nanmax(vals[win]) >= PEAK:
                return True
        return False

    rows = []
    for r in base.itertuples():
        st = states.get(r.internal_parcel_id, {})
        s26_ = st.get(2026, "unusable")
        # 2025 stops: confirmed when 2026 is also idle, resumed when 2026 is active, else still provisional
        stop25 = None
        if r.start_stop == "stopped_provisional":
            stop25 = {"idle": "stopped_confirmed", "active": "resumed_2026"}.get(s26_, "stopped_provisional")
        ss26 = start_stop(st, 2026) if st else None
        if ss26 == "stopped_provisional":
            ss26 = "stopped_2026_provisional"
        elif ss26 in ("started", "started_strong"):
            ss26 = ss26.replace("started", "started_2026") + "_observed_so_far"
        excl = exclusion(r.internal_parcel_id, r.land_type_v13, r.width_m)
        if ss26 and ss26.startswith("started") and s26_strict.get(r.internal_parcel_id) != "active":
            ss26 = "started_2026_weak_signal"           # active by the simple rule only, not by the strict one
        # two crops of 2025 by sowing year: add an autumn-2025 sowing seen in spring 2026
        n25 = int(r.cycles_sown_in_year) + (1 if r.cycles_sown_in_year >= 1 and autumn_sown(r.internal_parcel_id) else 0)
        tc25 = r.two_crops
        if r.two_crops_raw is None and n25 >= 2 and not isinstance(r.two_crops, str):
            tc25 = excl or "two_crops_with_autumn_sowing"
        # two crops sown in 2026 (autumn-2026 sowings not observable yet)
        tc26 = None
        if r.internal_parcel_id in by_ev and r.internal_parcel_id in by_day:
            c = crop_cycles(by_ev[r.internal_parcel_id], by_day[r.internal_parcel_id])
            tc26 = two_crops(c, by_day[r.internal_parcel_id])
        rows.append({"internal_parcel_id": r.internal_parcel_id, "area_ha": r.area_ha, "width_m": r.width_m,
                     "land_type_v13": r.land_type_v13, "state_2026": s26_, "stop_2025": stop25,
                     "start_stop_2026": None if not ss26 else (excl or ss26),
                     "two_crops_2025": tc25, "two_crops_2026_raw": tc26})
    t = pd.DataFrame(rows)
    excl = t.land_type_v13.isin(NOT_FIELD) | (t.width_m.fillna(0) < THIN_M)
    t["two_crops_2026"] = np.where(t.two_crops_2026_raw.notna() & excl, "review / not assessable", t.two_crops_2026_raw)
    target.mkdir(parents=True)
    t.to_parquet(target / "parcels.parquet", index=False)
    cnt = lambda col: t[col].value_counts().to_dict()
    summary = {"observed_through": "2026-11-01", "state_2026": cnt("state_2026"), "stop_2025": cnt("stop_2025"),
               "start_stop_2026": cnt("start_stop_2026"), "two_crops_2025": cnt("two_crops_2025"),
               "two_crops_2026": cnt("two_crops_2026"), "rule_2026": year_state.__doc__.strip()}
    (target / "summary.json").write_text(json.dumps(summary, indent=1, default=str), encoding="utf-8")
    print(json.dumps({k: v for k, v in summary.items() if k != "rule_2026"}, indent=1, default=str))


if __name__ == "__main__":
    if sys.argv[1:2] == ["build"]:
        build(*sys.argv[2:3])
    elif sys.argv[1:2] == ["build2026"]:
        build2026()
    else:
        sys.exit(__doc__)
