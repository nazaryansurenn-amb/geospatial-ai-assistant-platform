"""Canal operations demand v1 (canal_ops_sim_v1, plan v5 step 4b): daily water balance per parcel, summed into calendar
dekads (1-10, 11-20, 21-month end), NET demand only. Field efficiency (gross) is applied once, in the core
(wp_core/canal_allocation.py).

No hindsight (G1.1, G1.2): dekad D is planned on as_of = D start - 2 days. A Sentinel-2 scene dated t is usable from
t + 1; ERA5 is complete only to as_of - 6, the days after it and the planned dekad use 1991-2020 normals ('expected').
Actual dekad values are stored separately for the ledger, usable only after D end + 10 days.
tests/test_canal_ops_demand_v1.py scrambles every later input to prove the plan values cannot change.

Cloud-leak mask (G1.3): a causal per-date detector (area-wide NDVI dip vs each parcel's previous unmasked view) runs
on the clear views before season_replay_v1.add_causal. The candidate list goes to cloud_leak_dates_candidate.json for
the owner; until an approved list exists the build uses the candidates and is marked 'mask pending owner approval'.

Kc (G1.4, G1.5): latest usable clear view; strips under 20 m without valid inner pixels take the type calendar or the
community median of wide fresh fields ('type-based'); no fresh view gets the same estimate tagged 'fallback'.

Demand per v1.4 land class (G0.3): NO_CROP / NON_FARM / GREENHOUSE / not classifiable = 0 (greenhouses flagged
'possibly wells'); field and perennial land from Kc on two bases (full requirement, typical practice); unclassified and
'use not specified' land on the typical basis only; pre-sowing 15 mm/week in the parcel's earlier-year sowing window on
land cropped the year before (season cap); alfalfa never 0 after a cut confirmed by two clear views.
Demand refills the root zone to RAW (p-adjusted, FAO-56) on a full-supply reference bucket; refill below RAW is
'charging' (surplus-only) and never part of net demand. bucket_step() is the soil-water step the core reuses.

  canal_ops_demand_v1.py build   -> data/analysis/canal_ops_sim_v1/demand/
     parcel_dekad_2025.parquet              per parcel and dekad (internal only; never served)
     capacity_reference_2021_2024.parquet   per parcel and dekad, actual net demand 2021-2024 (P90 for capacities)
     cloud_leak_dates_candidate.json, dekad_summary.json, manifest.json (sha256 of every input and output)
Rules and parameters: config/canal_ops_demand_v1.json (frozen). Units: mm, m3 (1 mm on 1 ha = 10 m3).
"""
from __future__ import annotations

import calendar
import hashlib
import json
import sys
import warnings
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import season_replay_v1 as sr  # noqa: E402

CFG_PATH = ROOT / "config/canal_ops_demand_v1.json"
CFG = json.loads(CFG_PATH.read_text(encoding="utf-8"))
OUT = ROOT / "data/analysis/canal_ops_sim_v1/demand"
BASE = ["pid", "date", "ndvi", "ndmi", "bsi", "inner_ok"]
EPOCH = pd.Timestamp("1970-01-01")
SRC = np.array(["observed", "type-based", "fallback", "not applicable"])
OBSERVED, TYPE_BASED, FALLBACK, NA = range(4)


def sha(p):
    return hashlib.sha256(Path(p).read_bytes()).hexdigest()


def day_num(d):
    """Days since 1970-01-01 (float, NaN for NaT)."""
    if isinstance(d, pd.Series):
        return ((d - EPOCH) / pd.Timedelta(days=1)).to_numpy(float)
    return np.asarray((pd.DatetimeIndex(d) - EPOCH) / pd.Timedelta(days=1), float)


def doy_noleap(days):
    d = pd.DatetimeIndex(days)
    return np.asarray(d.dayofyear - (d.is_leap_year & (d.month > 2)), int)


def dekads(year, months=CFG["season"]["dekad_months"]):
    out = []
    for m in months:
        last = calendar.monthrange(year, m)[1]
        for k, (a, b) in enumerate(((1, 10), (11, 20), (21, last)), 1):
            s, e = pd.Timestamp(year, m, a), pd.Timestamp(year, m, b)
            out.append({"dekad_id": f"{year}-{m:02d}-D{k}", "month": m, "dekad": k, "start": s, "end": e,
                        "days": (e - s).days + 1, "as_of": s - pd.Timedelta(days=2)})
    return out


# ---------------------------------------------------------------- soil water (reused by the core)
def bucket_step(state, etc_mm, rain_mm, applied_net_mm, taw, p, p_clip=tuple(CFG["bucket"]["p_clip"])):
    """One daily root-zone step (FAO-56 ch. 8). state = depletion Dr in mm (0 = field capacity, taw = wilting point);
    etc_mm = crop water use, rain_mm = effective rain, applied_net_mm = net water reaching the root zone.
    p is adjusted p + 0.04 (5 - ETc). Returns (depletion, eta, ks, drainage) with
    depletion_new - state = eta - rain - applied + drainage exactly, 0 <= depletion <= taw and eta <= etc.
    Works on scalars or numpy arrays."""
    dr, etc, rain, app = (np.asarray(x, float) for x in (state, etc_mm, rain_mm, applied_net_mm))
    taw, p = np.asarray(taw, float), np.asarray(p, float)
    p_adj = np.clip(p + 0.04 * (5 - etc), *p_clip)
    raw = p_adj * taw
    ks = np.where(dr <= raw, 1.0, np.clip((taw - dr) / ((1 - p_adj) * taw), 0, 1))
    eta = np.minimum(ks * etc, np.maximum(taw - dr + rain + app, 0))    # never dries past the wilting point
    x = dr + eta - rain - app
    new, drain = np.maximum(x, 0), np.maximum(-x, 0)
    if np.ndim(new) == 0:
        return float(new), float(eta), float(ks), float(drain)
    return new, eta, ks, drain


def p_adjusted(p, etc_per_day, cfg=CFG):
    return np.clip(p + 0.04 * (5 - etc_per_day), *cfg["bucket"]["p_clip"])


# ---------------------------------------------------------------- inputs
class _AnyType(dict):
    """type6 map for season_replay_v1.load_parcels that keeps every v1.4 parcel: pandas Series.map uses __missing__
    of a dict subclass, so 'not classifiable' (type6 None) is kept with ltype 'none'."""
    def __missing__(self, key):
        return "none"


def classify(p, cfg=CFG):
    """Demand rule, Kc land type, bucket group, TAW/p and typical factor per parcel from its v1.4 land class."""
    rules = cfg["land_classes"]["rules"]
    p = p.copy()
    p["rule"] = p.land_class.map(lambda c: rules[c]["rule"])
    p["ltype"] = p.land_class.map(lambda c: rules[c]["ltype"])
    alf = p["type"].isin(cfg["land_classes"]["alfalfa_types"]) & (p.ltype == "field") & (p.rule != "zero")
    p["bucket_group"] = np.where(p.rule == "zero", "none", np.where(alf, "alfalfa", p.ltype))
    grp = cfg["bucket"]["groups"]
    p["taw_mm"] = p.bucket_group.map(lambda g: grp[g]["taw_mm"] if g in grp else np.nan)
    p["p"] = p.bucket_group.map(lambda g: grp[g]["p"] if g in grp else np.nan)
    p["greenhouse_possibly_wells"] = p.land_class == "GREENHOUSE"
    return p


def load_parcels(cfg=CFG):
    rcfg = dict(sr.CFG)
    rcfg["land_types"] = {**sr.CFG["land_types"], "map": _AnyType({k: v["ltype"] for k, v in cfg["land_classes"]["rules"].items()})}
    p = sr.load_parcels(rcfg)
    t = pd.read_parquet(sr.LANDTYPES, columns=["internal_parcel_id", "type6", "type"]).set_index("internal_parcel_id")
    p = p.drop(columns="ltype").join(t)
    p["land_class"] = p.type6.fillna("NOT_CLASSIFIABLE")
    return classify(p.drop(columns="type6"), cfg)


def clear_views(year, parcels, path=None):
    """Clear views of one year (season_replay_v1 filter) without the causal columns: the mask goes in before add_causal."""
    return sr.load_obs(year, parcels, path=path)[BASE]


# ---------------------------------------------------------------- cloud-leak mask (G1.3)
def leak_candidates(views, cfg=CFG):
    """Causal per-date detector. For each scene date in order, compare every parcel with its previous UNMASKED clear
    view; a date is a candidate when an area-wide share of vegetated parcels dips at once. Uses views up to the date
    only; later recovery is never used. Returns [{date, parcels_compared, share_dipped}]."""
    lk = cfg["cloud_leak"]
    codes, pids = pd.factorize(views.pid)
    last_v = np.full(len(pids), np.nan)
    last_d = np.full(len(pids), np.nan)
    tn = day_num(views.date)
    nd = views.ndvi.to_numpy(float)
    out = []
    for d in np.unique(tn):
        i = np.flatnonzero(tn == d)
        c = codes[i]
        prev = last_v[c]
        with np.errstate(invalid="ignore"):
            ok = (d - last_d[c] <= lk["max_gap_days"]) & (prev >= lk["veg_ndvi"])
        n = int(ok.sum())
        share = float((nd[i][ok] - prev[ok] <= -lk["dip_ndvi"]).mean()) if n else 0.0
        if n >= lk["min_parcels"] and share >= lk["min_share"]:
            out.append({"date": (EPOCH + pd.Timedelta(days=d)).date().isoformat(), "parcels_compared": n,
                        "share_dipped": round(share, 3)})
            continue
        last_v[c], last_d[c] = nd[i], d
    return out


def masked_obs(views, dates, cfg=CFG):
    """Drop masked scene dates, then the season_replay_v1 causal columns (harvest/cut drops, peak so far)."""
    keep = ~views.date.isin(pd.to_datetime(list(dates)))
    o = views[keep].sort_values(["pid", "date"]).reset_index(drop=True)
    return sr.add_causal(o.copy())


# ---------------------------------------------------------------- daily Kc
def daily_views(obs, pid_index, days, delay):
    """Latest view usable on each day (scene date + delay <= day) per parcel, as [P, T] float arrays."""
    P, T = len(pid_index), len(days)
    code = pid_index.get_indexer(obs.pid)
    o = obs[code >= 0]
    right = pd.DataFrame({"code": code[code >= 0].astype(np.int64), "avail": (day_num(o.date) + delay).astype(np.int64),
                          "vdate": day_num(o.date), "ndvi": o.ndvi.to_numpy(float), "inner_ok": o.inner_ok.to_numpy(float),
                          "peak": o.peak.to_numpy(float), "peak_date": day_num(o.peak_date),
                          "last_drop": day_num(o.last_drop)}).sort_values("avail", kind="stable")
    left = pd.DataFrame({"avail": np.repeat(day_num(days).astype(np.int64), P), "code": np.tile(np.arange(P, dtype=np.int64), T)})
    m = pd.merge_asof(left, right, on="avail", by="code", direction="backward")
    return {c: m[c].to_numpy(float).reshape(T, P).T for c in ["vdate", "ndvi", "inner_ok", "peak", "peak_date", "last_drop"]}


def daily_kc(parcels, V, days, cfg=CFG):
    """Kc and source tag per parcel and day from the usable views V (G1.4, G1.5)."""
    rc, kc_cfg = sr.CFG, cfg["kc"]
    P, T = len(parcels), len(days)
    month = np.asarray(pd.DatetimeIndex(days).month)
    cv = rc["clear_view"]
    window = np.where(month <= 5, cv["max_age_days_mar_may"], cv["max_age_days_jun_oct"])
    with np.errstate(invalid="ignore"):
        fresh = (day_num(days)[None, :] - V["vdate"]) <= window[None, :]
    wide = (parcels.mean_width_m.to_numpy(float) >= kc_cfg["narrow_width_m"])[:, None] | (V["inner_ok"] >= 1)
    active = (parcels.rule != "zero").to_numpy()
    lt = parcels.ltype.to_numpy()
    kfull = parcels.ltype.map(rc["land_types"]["kc_full"]).to_numpy(float)
    kc_obs = sr.kc_from_ndvi(V["ndvi"], kfull[:, None], rc)
    observed = fresh & wide & active[:, None]
    est = np.full((P, T), np.nan)
    cal = rc["land_types"]["kc_calendar"]
    mi = np.clip(month, 3, 10) - 3
    for t_ in ("orchard", "vineyard", "perennial"):
        est[lt == t_] = np.asarray(cal[t_])[mi]
    fc = (parcels.land_class == "FIELD_CROP").to_numpy()
    ref = np.where(observed & fc[:, None], kc_obs, np.nan)
    comm = parcels.community.to_numpy()
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)
        area_med = np.nanmedian(ref, axis=0) if fc.any() else np.full(T, np.nan)
        area_med = np.where(np.isnan(area_med), kc_cfg["fallback_default"], area_med)
        for c in np.unique(comm):
            rows = (comm == c) & (lt == "field")
            if rows.any():
                cm = np.nanmedian(ref[fc & (comm == c)], axis=0) if (fc & (comm == c)).any() else np.full(T, np.nan)
                est[rows] = np.where(np.isnan(cm), area_med, cm)
    kc = np.where(observed, kc_obs, est)
    src = np.where(observed, OBSERVED, np.where(fresh, TYPE_BASED, FALLBACK)).astype(np.int8)
    kc[~active] = np.nan
    src[~active] = NA
    return kc, src


# ---------------------------------------------------------------- weather
def cell_arrays(w, normals, cells, days):
    """Actual (NaN if missing) and normal ET0 / rain per cell and day, [C, T]."""
    x = w[w.date.isin(days)]
    act = {v: x.pivot_table(index="date", columns="cell", values=v).reindex(index=days, columns=cells).to_numpy(float).T
           for v in ("et0", "rain")}
    n = normals.set_index(["doy", "cell"])
    doy = doy_noleap(days)
    nrm = {v: n[c].unstack("cell").reindex(index=doy, columns=cells).to_numpy(float).T
           for v, c in (("et0", "et0_fao_evapotranspiration"), ("rain", "precipitation_sum"))}
    return act, nrm


def winter_depletion(parcels, w, normals, year, cfg=CFG):
    """Depletion on 1 Feb from the Nov-Jan balance (actual ERA5, no irrigation), starting part-depleted on 1 Nov."""
    bw = cfg["bucket"]["winter"]
    days = pd.date_range(f"{year - 1}-{bw['start']}", f"{year}-01-31")
    cells = sorted(parcels.cell.unique())
    ci = pd.Index(cells).get_indexer(parcels.cell)
    act, nrm = cell_arrays(w, normals, cells, days)
    et0 = np.where(np.isnan(act["et0"]), nrm["et0"], act["et0"])
    rain = np.where(np.isnan(act["rain"]), nrm["rain"], act["rain"])
    taw = parcels.taw_mm.fillna(1.0).to_numpy(float)
    p = parcels.p.fillna(0.5).to_numpy(float)
    kc = parcels.bucket_group.map(bw["kc"]).fillna(0.0).to_numpy(float)
    dr = bw["start_depletion_share_of_taw"] * taw
    share = cfg["bucket"]["effective_rain_share"]
    for t in range(len(days)):
        dr = bucket_step(dr, kc * et0[ci, t], share * rain[ci, t], 0.0, taw, p)[0]
    return dr


# ---------------------------------------------------------------- pre-sowing
def greenup_dates(obs, cfg=CFG):
    """First clear view in the green-up months with NDVI >= greenup_ndvi after a bare view of the same year."""
    ps = cfg["presowing"]
    o = obs[obs.date.dt.month.between(*ps["greenup_months"])]
    bare_before = (o.ndvi < ps["bare_ndvi"]).groupby(o.pid).cummax().groupby(o.pid).shift(fill_value=False)
    return o[bare_before & (o.ndvi >= ps["greenup_ndvi"])].groupby("pid").date.min()


def presow_windows(parcels, earlier_obs, year, cfg=CFG):
    """Per parcel [start, end] day numbers in `year` from its own earlier-year sowing estimates (NaN if none)."""
    ps = cfg["presowing"]
    sow = []
    for o in earlier_obs:
        g = greenup_dates(o, cfg)
        sow.append((g - pd.Timedelta(days=ps["sowing_before_greenup_days"])).dt.dayofyear.rename(len(sow)))
    if not sow:
        return np.full(len(parcels), np.nan), np.full(len(parcels), np.nan)
    s = pd.concat(sow, axis=1).reindex(parcels.index)
    jan0 = day_num(pd.DatetimeIndex([f"{year}-01-01"]))[0] - 1
    return (jan0 + s.min(axis=1) - ps["window_lead_days"]).to_numpy(float), (jan0 + s.max(axis=1)).to_numpy(float)


# ---------------------------------------------------------------- one year
def run_year(year, parcels, views, w, normals, leak_dates, cfg=CFG):
    """Dekad table of one year. views: {year: clear views (BASE columns)} for `year` and earlier years;
    leak_dates: {year: [ISO dates]} masked before add_causal."""
    nh, rules = cfg["no_hindsight"], cfg["demand"]
    obs = {y: masked_obs(v, leak_dates.get(y, []), cfg) for y, v in views.items() if y <= year}
    days = pd.date_range(f"{year}-{cfg['season']['daily_start']}", f"{year}-10-31")
    tn = day_num(days)
    P, T = len(parcels), len(days)
    pidx = pd.Index(parcels.index)
    V = daily_views(obs[year], pidx, days, nh["s2_delay_days"])
    kc, src = daily_kc(parcels, V, days, cfg)
    cells = sorted(parcels.cell.unique())
    ci = pd.Index(cells).get_indexer(parcels.cell)
    act, nrm = cell_arrays(w, normals, cells, days)
    act = {k: np.where(np.isnan(v), nrm[k], v) for k, v in act.items()}
    share = cfg["bucket"]["effective_rain_share"]

    active = (parcels.rule != "zero").to_numpy()
    typical_only = (parcels.rule == "typical_only").to_numpy()
    grp = parcels.bucket_group.to_numpy()
    taw = parcels.taw_mm.fillna(1.0).to_numpy(float)
    p0 = parcels.p.fillna(0.5).to_numpy(float)
    f = parcels.bucket_group.map(rules["typical_factor"]).fillna(0.0).to_numpy(float)
    k0 = np.nan_to_num(kc)                              # zero-rule land: no crop water use in the bucket
    alfalfa = grp == "alfalfa"
    field_land = (parcels.ltype == "field").to_numpy() & active & ~alfalfa

    # pre-sowing eligibility: cropped the previous year, own earlier-year sowing window, not yet green this year
    ps = cfg["presowing"]
    prev = obs.get(year - 1)
    cropped_prev = np.zeros(P, bool)
    if prev is not None:
        pk = prev[prev.date.dt.month.between(3, 10)].groupby("pid").ndvi.max().reindex(pidx)
        cropped_prev = (pk >= ps["cropped_prev_year_peak_ndvi"]).to_numpy()
    w_lo, w_hi = presow_windows(parcels, [obs[y] for y in sorted(obs) if y < year], year, cfg)
    green_now = day_num(greenup_dates(obs[year], cfg).reindex(pidx))
    presow_ok = field_land & cropped_prev & ~np.isnan(w_lo)
    presow_used = np.zeros(P)

    dr = winter_depletion(parcels, w, normals, year, cfg)
    dr_hist = np.zeros((P, T + 1)); dr_hist[:, 0] = dr
    eta_hist = np.zeros((P, T))
    app = np.zeros((P, T))
    done = 0

    def advance(to):                                    # actual trajectory of the full-supply reference bucket
        nonlocal dr, done
        for t in range(done, to):
            dr, eta_hist[:, t] = bucket_step(dr, k0[:, t] * act["et0"][ci, t], share * act["rain"][ci, t], app[:, t], taw, p0)[:2]
            dr_hist[:, t + 1] = dr
        done = max(done, to)

    frames = []
    for D in dekads(year):
        a = int(np.searchsorted(tn, day_num([D["as_of"]])[0]))
        s = int(np.searchsorted(tn, day_num([D["start"]])[0]))
        e = s + D["days"]
        lag = nh["weather_lag_days"]
        advance(a - lag + 1)                            # actual weather complete to as_of - 6
        x = dr.copy()
        kp = k0[:, a]                                   # Kc as of as_of, held for the bridge and the dekad
        for t in range(a - lag + 1, s):                 # bridge as_of-5 .. start-1: expected (normals)
            k = k0[:, t] if t <= a else kp
            x = bucket_step(x, k * nrm["et0"][ci, t], share * nrm["rain"][ci, t], app[:, t], taw, p0)[0]
        n = e - s
        et0_exp = nrm["et0"][ci, s:e].sum(1)
        rain_exp = nrm["rain"][ci, s:e].sum(1)
        etc_exp = kp * et0_exp
        reff_exp = share * rain_exp
        raw_exp = p_adjusted(p0, etc_exp / n, cfg) * taw
        full = np.maximum(0, x + etc_exp - reff_exp - raw_exp)
        typ = np.maximum(0, x + f * etc_exp - reff_exp - raw_exp)
        charge = np.maximum(0, x + etc_exp - reff_exp) - full
        # alfalfa: never 0 after a cut confirmed by two clear views usable by as_of
        ac = cfg["alfalfa_after_cut"]
        with np.errstate(invalid="ignore"):
            cut = alfalfa & (tn[a] - V["last_drop"][:, a] <= ac["regrowth_days"])
        full = np.where(cut, np.maximum(full, ac["min_share_of_etc"] * etc_exp), full)
        typ = np.where(cut, np.maximum(typ, ac["min_share_of_etc"] * f * etc_exp), typ)
        # pre-sowing water in the earlier-year window, until this year's green-up is seen
        with np.errstate(invalid="ignore"):
            in_win = ((tn[s:e][None, :] >= w_lo[:, None]) & (tn[s:e][None, :] <= w_hi[:, None])).sum(1)
            seen_green = green_now + nh["s2_delay_days"] <= tn[a]
        presow = np.where(presow_ok & ~seen_green, np.minimum(ps["mm_per_week"] / 7 * in_win,
                                                              ps["season_cap_mm"] - presow_used), 0.0)
        presow_used += presow
        full, typ = full + presow, typ + presow
        full = np.where(typical_only, typ, full)
        full, typ, charge = (np.where(active, v, 0.0) for v in (full, typ, charge))
        app[:, s:e] = (full / n)[:, None]
        with np.errstate(invalid="ignore"):
            rc = cfg["ripening"]
            ripening = (field_land & (src[:, a] == OBSERVED) & (V["peak"][:, a] >= rc["min_peak_ndvi"])
                        & (V["ndvi"][:, a] <= rc["below_share_of_peak"] * V["peak"][:, a])
                        & (tn[a] - V["peak_date"][:, a] >= rc["min_days_after_peak"]))
        sw_date = D["start"].date()
        frames.append(pd.DataFrame({
            "pid": pidx, "year": year, "dekad_id": D["dekad_id"], "month": D["month"], "dekad": D["dekad"],
            "start": D["start"], "end": D["end"], "days": n, "as_of": D["as_of"],
            "weather_complete_to": D["as_of"] - pd.Timedelta(days=lag),
            "actual_usable_from": D["end"] + pd.Timedelta(days=nh["actual_usable_after_days"]),
            "kc_plan": np.where(active, kp, np.nan), "kc_source": SRC[src[:, a]],
            "view_age_days": np.where(active, tn[a] - V["vdate"][:, a], np.nan),
            "et0_mm_exp": et0_exp, "rain_mm_exp": rain_exp, "etc_mm_exp": np.where(active, etc_exp, 0.0),
            "rain_eff_mm_exp": reff_exp, "depletion_start_mm_exp": np.where(active, x, np.nan),
            "raw_mm_exp": np.where(active, raw_exp, np.nan), "weather_tag": "expected",
            "net_mm_full_exp": full, "net_mm_typical_exp": typ, "charging_mm_exp": charge, "presow_mm": presow,
            "alfalfa_after_cut": cut, "ripening": ripening,
            "stage_weight": parcels.ltype.map({t_: sr.stage_weight(t_, sw_date) if t_ in sr.CFG["stage_weight"] else 0.0
                                               for t_ in parcels.ltype.unique()}).to_numpy(float),
        }))
    advance(T)

    # actual dekad values for the ledger (same reference bucket, actual weather and daily Kc)
    for fr, D in zip(frames, dekads(year)):
        s = int(np.searchsorted(tn, day_num([D["start"]])[0]))
        e = s + D["days"]
        n = e - s
        et0_act = act["et0"][ci, s:e].sum(1)
        rain_act = act["rain"][ci, s:e].sum(1)
        etc_act = (k0[:, s:e] * act["et0"][ci, s:e]).sum(1)
        reff_act = share * rain_act
        x = dr_hist[:, s]
        raw_act = p_adjusted(p0, etc_act / n, cfg) * taw
        full = np.maximum(0, x + etc_act - reff_act - raw_act)
        typ = np.maximum(0, x + f * etc_act - reff_act - raw_act)
        cut = fr.alfalfa_after_cut.to_numpy()
        ac = cfg["alfalfa_after_cut"]
        full = np.where(cut, np.maximum(full, ac["min_share_of_etc"] * etc_act), full)
        typ = np.where(cut, np.maximum(typ, ac["min_share_of_etc"] * f * etc_act), typ)
        full, typ = full + fr.presow_mm.to_numpy(), typ + fr.presow_mm.to_numpy()
        full = np.where(typical_only, typ, full)
        fr["et0_mm_act"], fr["rain_mm_act"] = et0_act, rain_act
        fr["etc_mm_act"] = np.where(active, etc_act, 0.0)
        fr["eta_mm_ref_act"] = np.where(active, eta_hist[:, s:e].sum(1), 0.0)
        fr["rain_eff_mm_act"] = reff_act
        fr["depletion_start_mm_act"] = np.where(active, x, np.nan)
        fr["net_mm_full_act"] = np.where(active, full, 0.0)
        fr["net_mm_typical_act"] = np.where(active, typ, 0.0)
        fr["observed_days_act"] = (src[:, s:e] == OBSERVED).sum(1)
    d = pd.concat(frames, ignore_index=True)
    meta = parcels[["community", "cell", "area_ha", "mean_width_m", "land_class", "rule", "ltype", "bucket_group",
                    "taw_mm", "p", "greenhouse_possibly_wells"]]
    d = d.join(meta, on="pid")
    for b in ("full", "typical"):
        for k in ("exp", "act"):
            d[f"net_m3_{b}_{k}"] = d[f"net_mm_{b}_{k}"] * 10 * d.area_ha
    d["basis_only"] = np.where(d.rule == "typical_only", "typical", np.where(d.rule == "zero", "zero", "both"))
    return d


# ---------------------------------------------------------------- build
def leak_file_dates(cands):
    return {y: [c["date"] for c in v] for y, v in cands.items()}


def list_hash(dates):
    return hashlib.sha256(json.dumps({str(k): v for k, v in sorted(dates.items())}, sort_keys=True).encode()).hexdigest()


def summarise(d, cap, leak_status, n_candidates, parcels):
    ha = parcels.groupby("land_class").area_ha.sum()
    by = d.groupby("land_class")
    season = {}
    for c, g in by:
        season[c] = {"area_ha": round(float(ha[c]), 1),
                     **{f"net_{b}_{k}_thousand_m3": round(float(g[f"net_m3_{b}_{k}"].sum()) / 1000, 1)
                        for b in ("full", "typical") for k in ("exp", "act")},
                     "net_full_exp_mm": round(float(g.net_m3_full_exp.sum() / 10 / ha[c]), 1) if ha[c] else None}
    dem = d[d.rule != "zero"]
    a = dem.area_ha
    obs_share = float(a[dem.kc_source == "observed"].sum() / a.sum())
    src_share = {s: round(float(a[dem.kc_source == s].sum() / a.sum()), 3) for s in SRC[:3]}
    vol_share = {s: round(float(dem.net_m3_full_exp[dem.kc_source == s].sum() / dem.net_m3_full_exp.sum()), 3) for s in SRC[:3]}
    per_dekad = []
    for k, g in d.groupby("dekad_id", sort=False):
        gd = g[g.rule != "zero"]
        per_dekad.append({"dekad": k, "days": int(g.days.iloc[0]), "as_of": str(g.as_of.iloc[0].date()),
                          **{f"net_{b}_{x}_thousand_m3": round(float(g[f"net_m3_{b}_{x}"].sum()) / 1000, 1)
                             for b in ("full", "typical") for x in ("exp", "act")},
                          "charging_to_field_capacity_thousand_m3_state": round(float((g.charging_mm_exp * 10 * g.area_ha).sum()) / 1000, 1),
                          "presow_thousand_m3": round(float((g.presow_mm * 10 * g.area_ha).sum()) / 1000, 1),
                          "observed_area_share": round(float(gd.area_ha[gd.kc_source == "observed"].sum() / gd.area_ha.sum()), 3)})
    comm = (dem.area_ha.where(dem.kc_source == "observed", 0.0).groupby(dem.community).sum()
            / dem.groupby("community").area_ha.sum()).round(3).to_dict()
    return {
        "version": CFG["version"], "built": datetime.now(timezone.utc).isoformat(), "year": CFG["years"]["plan"],
        "mask_status": leak_status, "cloud_leak_candidate_dates": n_candidates,
        "parcels": int(len(parcels)), "area_ha": round(float(parcels.area_ha.sum()), 1),
        "season_2025_by_land_class": season,
        "season_2025_total_thousand_m3": {f"net_{b}_{k}": round(float(d[f"net_m3_{b}_{k}"].sum()) / 1000, 1)
                                          for b in ("full", "typical") for k in ("exp", "act")},
        "kc_source_area_share_at_plan": src_share, "observed_share": round(obs_share, 3),
        "estimated_share": round(1 - obs_share, 3), "kc_source_volume_share_full_exp": vol_share,
        "observed_area_share_by_community": comm, "per_dekad": per_dekad,
        "capacity_reference_p90_2021_2024": cap,
        "notes": ["NET demand only; field efficiency is applied once, in the core",
                  "plan values: Kc and weather as of as_of (ERA5 to as_of-6, normals after, tagged 'expected')",
                  "actual values are for the ledger, usable only after dekad end + 10 days",
                  "full-supply reference bucket; the core drives its own bucket with the simulated allocation",
                  "charging (refill below RAW to field capacity) is surplus-only, never part of net demand; it is a state at the dekad start, not additive across dekads",
                  "land types v1.4: static map made in late 2026 (stated hindsight leak), the same map for every year",
                  "greenhouses are 0 and flagged 'possibly wells'"]}


def capacity_p90(c):
    """P90 over 2021-2024 dekads of each group's total actual net demand, as mm/dekad-day and l/s/ha."""
    out = {}
    for gname, g in c[c.rule != "zero"].groupby("bucket_group"):
        t = g.groupby(["year", "dekad_id"]).agg(m3=("net_m3_full_act", "sum"), days=("days", "first"))
        area = float(g.drop_duplicates("pid").area_ha.sum())
        mm_day = t.m3 / 10 / area / t.days
        out[gname] = {"area_ha": round(area, 1), "dekads": int(len(t)),
                      "p90_mm_per_day": round(float(mm_day.quantile(0.9)), 2),
                      "p90_lps_per_ha": round(float(mm_day.quantile(0.9)) * 10000 / 86400, 3)}
    return out


def build():
    OUT.mkdir(parents=True, exist_ok=True)
    parcels = load_parcels()
    w, normals = sr.load_weather()
    years = list(range(CFG["years"]["first_obs_year"], CFG["years"]["plan"] + 1))
    views = {y: clear_views(y, parcels) for y in years}
    cands = {y: leak_candidates(views[y]) for y in years}
    cand_dates = leak_file_dates(cands)
    (OUT / "cloud_leak_dates_candidate.json").write_text(json.dumps({
        "version": CFG["version"], "status": "candidate - pending owner approval (not approved automatically)",
        "detector": CFG["cloud_leak"], "dates_sha256": list_hash(cand_dates),
        "candidates": {str(y): v for y, v in cands.items()}}, indent=1), encoding="utf-8")
    approved = ROOT / CFG["cloud_leak"]["approved_file"]
    # owner-approved years use the approved list; every other year keeps its candidates, labelled pending
    ok = {int(y): v for y, v in json.loads(approved.read_text(encoding="utf-8"))["dates"].items()} if approved.exists() else {}
    leak = {y: ok.get(y, cand_dates[y]) for y in years}
    status = {y: "owner-approved mask" if y in ok else CFG["cloud_leak"]["status_when_not_approved"] for y in years}
    caps = []
    for y in CFG["years"]["capacity_reference"]:
        c = run_year(y, parcels, {k: v for k, v in views.items() if k <= y}, w, normals, leak)
        caps.append(c[["pid", "year", "dekad_id", "month", "dekad", "days", "community", "land_class", "rule",
                       "bucket_group", "area_ha", "net_mm_full_act", "net_mm_typical_act", "net_m3_full_act",
                       "net_m3_typical_act", "net_m3_full_exp", "net_m3_typical_exp"]])
        print(y, "done", flush=True)
    cap = pd.concat(caps, ignore_index=True)
    cap["mask_status"] = cap.year.map(status)
    cap.to_parquet(OUT / "capacity_reference_2021_2024.parquet", index=False)
    y = CFG["years"]["plan"]
    d = run_year(y, parcels, views, w, normals, leak).assign(mask_status=status[y])
    d.to_parquet(OUT / f"parcel_dekad_{y}.parquet", index=False)
    print(y, "done", flush=True)
    summary = summarise(d, capacity_p90(cap), {str(k): v for k, v in status.items()}, {str(k): len(v) for k, v in cand_dates.items()}, parcels)
    (OUT / "dekad_summary.json").write_text(json.dumps(summary, indent=1, ensure_ascii=False), encoding="utf-8")
    inputs = [CFG_PATH, Path(__file__), Path(sr.__file__), sr.CFG_PATH, sr.LANDTYPES, sr.COMMUNITIES,
              sr.WEATHER / "daily.parquet", sr.WEATHER / "normals.parquet", *[sr.DAILY[k] for k in years]]
    if approved.exists():
        inputs.append(approved)
    (OUT / "manifest.json").write_text(json.dumps({
        "version": CFG["version"], "created": summary["built"], "mask_status": summary["mask_status"],
        "leak_dates_sha256": list_hash(leak),
        "inputs": {str(p.relative_to(ROOT)): sha(p) for p in inputs},
        "outputs": {p.name: sha(p) for p in sorted(OUT.glob("*")) if p.name != "manifest.json"}}, indent=1), encoding="utf-8")
    print(json.dumps({k: v for k, v in summary.items() if k not in ("per_dekad", "observed_area_share_by_community")},
                     indent=1, ensure_ascii=False))


if __name__ == "__main__":
    if sys.argv[1:2] == ["build"]:
        build()
    else:
        sys.exit(__doc__)
