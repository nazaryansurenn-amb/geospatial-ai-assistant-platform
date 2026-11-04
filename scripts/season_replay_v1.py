"""Season replay v1 (owner 2026-11-01: "simulation like it's 2026 starting, and time by time the WUA sees where the
priority is to deliver irrigation water, compared with last year").

Each ISO week W (weeks 10-43) is computed ONLY from satellite views dated up to Sunday of W, weather up to that Sunday
and NDMI references from EARLIER years. Every per-view quantity used below (harvest/cut drops, season peak, drop
events) is cumulative and causal, so the result for W cannot change when later data changes
(tests/test_season_replay_v1.py scrambles all later data to prove it). Rules and thresholds:
config/season_replay_v1.json (frozen before the first run). Land types v1.4 are a static map made in late 2026: a
stated hindsight leak shown on every page.

  season_replay_v1.py build   -> data/analysis/season_replay_v1/
     community_weeks.parquet   year x ISO week x community (+ "ALL"): state areas, score, level, demand, weather
     parcel_weeks_{year}.parquet  per parcel and week (internal only; never served)
     summary.json, manifest.json (sha256 of every input)
Output is draft screening ("where to look first"), never delivery records, orders or a hydraulic plan.
"""
from __future__ import annotations
import hashlib
import json
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
CFG_PATH = ROOT / "config/season_replay_v1.json"
CFG = json.loads(CFG_PATH.read_text(encoding="utf-8"))
OUT = ROOT / "data/analysis/season_replay_v1"
LANDTYPES = ROOT / "server_data/review/alphaearth_photo_review_20261030_v1/releases/landtypes_v1_4/parcels.parquet"
COMMUNITIES = ROOT / "data/source/wua_areas.geojson"
WEATHER = ROOT / "data/observations/season_weather_era5_v1"
DAILY = {y: ROOT / f"data/analysis/observation_screening/transitions_area_20260906_v1/daily/{y}.parquet" for y in range(2021, 2026)}
DAILY[2026] = ROOT / "data/analysis/observation_screening/transitions_area_2026_v1/daily/2026.parquet"
CELLS = {"40.00_44.00": (40.00, 44.00), "40.00_44.25": (40.00, 44.25), "40.00_44.50": (40.00, 44.50),
         "40.25_44.00": (40.25, 44.00), "40.25_44.25": (40.25, 44.25), "40.25_44.50": (40.25, 44.50)}
OBS_COLS = ["internal_parcel_id", "observation_date", "ndvi_median", "ndmi_median", "bsi_median", "ndvi_valid_fraction",
            "ndmi_valid_fraction", "ndvi_inner5_valid_fraction"]


def sha(p):
    return hashlib.sha256(Path(p).read_bytes()).hexdigest()


def week_cutoff(year, week):
    """Sunday of ISO week `week`."""
    return date.fromisocalendar(year, week, 7)


# ---------------------------------------------------------------- inputs
def load_parcels(cfg=CFG):
    import shapely
    from shapely.geometry import shape
    d = pd.read_parquet(LANDTYPES, columns=["internal_parcel_id", "community", "area_ha", "mean_width_m", "lon", "lat", "type6"])
    d["ltype"] = d.type6.map(cfg["land_types"]["map"])
    d = d[d.ltype.notna()].copy()
    feats = json.loads(COMMUNITIES.read_text(encoding="utf-8"))["features"]
    names = [f["properties"]["name_hy"] for f in feats]
    geoms = [shape(f["geometry"]) for f in feats]
    miss = d.community.isna()
    if miss.any():                                      # parcels without a community (e.g. owner-labelled slivers)
        pts = shapely.points(d.loc[miss, "lon"].to_numpy(), d.loc[miss, "lat"].to_numpy())
        found = np.full(len(pts), None, dtype=object)
        for n, g in zip(names, geoms):
            found[shapely.contains(g, pts) & (found == None)] = n  # noqa: E711
        d.loc[miss, "community"] = found
    d["community"] = d.community.fillna("(outside community outlines)")
    lat = np.array([c[0] for c in CELLS.values()]); lon = np.array([c[1] for c in CELLS.values()])
    dist = (d.lat.to_numpy()[:, None] - lat[None, :]) ** 2 + (d.lon.to_numpy()[:, None] - lon[None, :]) ** 2
    d["cell"] = np.array(list(CELLS))[dist.argmin(1)]
    return d.set_index("internal_parcel_id")[["community", "area_ha", "mean_width_m", "ltype", "cell"]]


def load_obs(year, parcels, path=None, cfg=CFG):
    """Clear views of one year from the season start, with causal cumulative columns."""
    o = pd.read_parquet(path or DAILY[year], columns=OBS_COLS)
    o = o[o.internal_parcel_id.isin(parcels.index)]
    o["date"] = pd.to_datetime(o.observation_date)
    cv = cfg["clear_view"]
    lo, hi = cv["ndvi_range"]
    o = o[(o.date >= pd.Timestamp(f"{year}-{cfg['season_start']}")) & (o.date <= pd.Timestamp(f"{year}-10-31"))
          & o.ndvi_median.between(lo, hi) & o.ndmi_median.between(lo, hi) & o.bsi_median.notna()
          & (o.ndvi_valid_fraction >= cv["min_valid_fraction"]) & (o.ndmi_valid_fraction >= cv["min_valid_fraction"])]
    o = o.rename(columns={"internal_parcel_id": "pid", "ndvi_median": "ndvi", "ndmi_median": "ndmi", "bsi_median": "bsi"})
    o["inner_ok"] = o.ndvi_inner5_valid_fraction.fillna(0) >= cfg["score"]["inner_valid_min"]
    o = o[["pid", "date", "ndvi", "ndmi", "bsi", "inner_ok"]].sort_values(["pid", "date"]).reset_index(drop=True)
    return add_causal(o, cfg)


def add_causal(o, cfg=CFG):
    """Per view: harvest/cut drop flag from the previous 16 days, drop-event count, season peak so far.
    Each value depends only on this and earlier views of the same parcel."""
    g = cfg["guards"]
    win = f"{g['drop_window_days']}D"
    byp = o.set_index("date").groupby("pid")                          # o is sorted by pid, date: order matches
    prior_max = byp.ndvi.rolling(win, closed="left").max().to_numpy()
    prior_bsi = byp.bsi.rolling(win, closed="left").min().to_numpy()
    raw = pd.Series(((prior_max - o.ndvi.to_numpy()) >= g["drop_ndvi"]) & ((o.bsi.to_numpy() - prior_bsi) >= g["drop_bsi_rise"]), index=o.index)
    # one date never makes a sign: a harvest/cut drop must hold on two consecutive clear views (single cloud artefacts)
    o["is_drop"] = raw & raw.groupby(o.pid).shift(fill_value=False)
    o["drop_date"] = o.date.where(o.is_drop)
    o["last_drop"] = o.groupby("pid").drop_date.ffill()
    prev = o.groupby("pid").last_drop.shift()
    new_event = o.is_drop & (prev.isna() | ((o.date - prev).dt.days > g["drop_window_days"]))
    o["drop_events"] = new_event.astype(int).groupby(o.pid).cumsum()
    o["peak"] = o.groupby("pid").ndvi.cummax()
    o["peak_date"] = o.date.where(o.ndvi >= o.peak).groupby(o.pid).ffill()
    return o.drop(columns=["drop_date"])


def ndmi_refs(ref_obs, parcels):
    """Median NDMI per parcel and ISO week over weeks W-1..W+1 of the reference years; fallback per community x type."""
    if ref_obs is None or ref_obs.empty:
        return {}, {}
    r = ref_obs[["pid", "date", "ndmi"]].copy()
    r["wk"] = r.date.dt.isocalendar().week.astype(int)
    r = pd.concat([r.assign(wk=r.wk + k) for k in (-1, 0, 1)])
    own = r.groupby(["pid", "wk"]).ndmi.median()
    r = r.join(parcels[["community", "ltype"]], on="pid")
    grp = r.groupby(["community", "ltype", "wk"]).ndmi.median()
    return own.to_dict(), grp.to_dict()


def load_weather():
    w = pd.read_parquet(WEATHER / "daily.parquet", columns=["cell", "date", "et0_fao_evapotranspiration", "precipitation_sum", "temperature_2m_max"])
    w["date"] = pd.to_datetime(w.date)
    n = pd.read_parquet(WEATHER / "normals.parquet")
    return w.rename(columns={"et0_fao_evapotranspiration": "et0", "precipitation_sum": "rain", "temperature_2m_max": "tmax"}), n


def week_weather(w, normals, cutoff):
    """Past-7-day ET0, rain and mean Tmax per cell ending at the cutoff; normal ET0 for the same days (1991-2020)."""
    c = pd.Timestamp(cutoff)
    x = w[(w.date > c - pd.Timedelta(days=7)) & (w.date <= c)]
    g = x.groupby("cell").agg(et0=("et0", "sum"), rain=("rain", "sum"), tmax=("tmax", "mean"), days=("et0", "count"))
    doys = [min((c - pd.Timedelta(days=k)).dayofyear, 365) for k in range(7)]
    g["et0_normal"] = normals[normals.doy.isin(doys)].groupby("cell").et0_fao_evapotranspiration.sum()
    g.loc[g.days < 7, ["et0", "rain", "tmax"]] = np.nan          # incomplete week: no weather, nothing is scored
    return g


# ---------------------------------------------------------------- one week
def kc_from_ndvi(ndvi, kc_full, cfg=CFG):
    k = cfg["kc_from_ndvi"]
    fc = np.clip((ndvi - k["ndvi_bare"]) / (k["ndvi_full"] - k["ndvi_bare"]), 0, 1)
    return k["kc_min"] + (kc_full - k["kc_min"]) * np.minimum(1, k["cover_gain"] * fc)


def stage_weight(ltype, cutoff, cfg=CFG):
    sw = cfg["stage_weight"][ltype]
    m = cutoff.month
    if ltype == "vineyard" and m == 8:
        return sw["8a"] if cutoff.day <= 15 else sw["8b"]
    return sw.get(str(m), 0.0)


def week_parcels(obs, cutoff, parcels, refs, wx, cfg=CFG):
    """State, crop coefficient, score parts and demand of every parcel for the week ending `cutoff`.
    Uses obs rows dated <= cutoff only."""
    c = pd.Timestamp(cutoff)
    month = cutoff.month
    window = cfg["clear_view"]["max_age_days_mar_may" if month <= 5 else "max_age_days_jun_oct"]
    last = obs[obs.date <= c].groupby("pid").tail(1).set_index("pid")
    p = parcels.join(last, how="left")
    p["age"] = (c - p.date).dt.days
    p["fresh"] = p.age <= window
    kfull = p.ltype.map(cfg["land_types"]["kc_full"])
    p["kc"] = np.where(p.fresh, kc_from_ndvi(p.ndvi.to_numpy(), kfull.to_numpy(), cfg), np.nan)
    cal = cfg["land_types"]["kc_calendar"]
    per = p.ltype != "field"
    p.loc[per & ~p.fresh, "kc"] = p.loc[per & ~p.fresh, "ltype"].map(lambda t: cal[t][month - 3])
    fld = (p.ltype == "field")
    med = p[fld & p.fresh].groupby("community").kc.median()           # same-week neighbours, causal
    fill = p.loc[fld & ~p.fresh, "community"].map(med)
    p.loc[fld & ~p.fresh, "kc"] = fill.fillna(p.loc[fld & p.fresh, "kc"].median() if (fld & p.fresh).any() else 0.4)
    p["kc"] = p.kc.astype(float)
    p["kc_source"] = np.where(p.fresh, "observed", "estimated")
    # weather
    p = p.join(wx[["et0", "rain", "tmax", "et0_normal"]], on="cell")
    s = cfg["score"]
    p["d"] = np.minimum(1, p.kc * p.et0 / s["demand_mm_full"])
    p["w"] = np.clip((p.et0 - s["weather_et0_low_mm"]) / s["weather_et0_span_mm"], 0, 1)
    # dryness: own reference of earlier years, else community x type reference
    own, grp = refs
    wk = cutoff.isocalendar()[1]
    ref = pd.Series([own.get((i, wk), np.nan) for i in p.index], index=p.index, dtype=float)
    alt = pd.Series([grp.get((cm, lt, wk), np.nan) for cm, lt in zip(p.community, p.ltype)], index=p.index, dtype=float)
    ref = ref.fillna(alt)
    wide = (p.mean_width_m >= s["narrow_width_m"]) | p.inner_ok.fillna(False).astype(bool)
    p["s_assessed"] = p.fresh & wide & ref.notna()
    p["s"] = np.where(p.s_assessed, np.clip((ref - p.ndmi) / s["dryness_ndmi_span"], 0, 1), 0.0)
    p["ndmi_ref"] = ref
    # stage weight and guards
    p["g"] = np.array([stage_weight(t, cutoff, cfg) for t in p.ltype], dtype=float)
    ep = cfg["stage_weight"]["early_peak"]
    early = ((p.ltype == "field") & (p.peak_date <= pd.Timestamp(f"{cutoff.year}-{ep['peak_before']}"))
             & (c > pd.Timestamp(f"{cutoff.year}-{ep['after']}")) & (p.ndvi < ep["below_share_of_peak"] * p.peak))
    p.loc[early, "g"] = np.minimum(p.loc[early, "g"], ep["weight"])
    gd = cfg["guards"]
    days_since_drop = (c - p.last_drop).dt.days
    guard_len = np.where(p.drop_events.fillna(0) >= 2, gd["guard_days_after_regrow"], gd["guard_days"])
    guarded = days_since_drop.notna() & (days_since_drop <= guard_len)
    state = np.select([~p.fresh, p.et0.isna(), guarded, p.ndvi < gd["bare_ndvi"]],
                      ["no_view", "no_weather", "guarded", "bare"], "scored")
    p["state"] = state
    wts = s["weights"]
    p["score"] = np.where(p.state == "scored", 100 * p.g * (wts["demand"] * p.d + wts["dryness"] * p.s + wts["weather"] * p.w), np.nan)
    # demand: full crop requirement for the week, m3 (1 mm on 1 ha = 10 m3); net of effective rain
    etc = p.kc * p.et0
    p["etc_m3"] = etc * 10 * p.area_ha
    p["net_m3"] = np.maximum(0, etc - cfg["effective_rain_share"] * p.rain) * 10 * p.area_ha
    return p


# ---------------------------------------------------------------- aggregation
def wmedian(v, w):
    v = np.asarray(v, float); w = np.asarray(w, float)
    ok = np.isfinite(v) & (w > 0)
    if not ok.any():
        return np.nan
    v, w = v[ok], w[ok]
    o = np.argsort(v); v, w = v[o], w[o]
    cw = np.cumsum(w)
    return float(v[np.searchsorted(cw, cw[-1] / 2)])


def level(score, scored_share, cfg=CFG):
    cm = cfg["community"]
    if not np.isfinite(score) or scored_share < cm["min_scored_share"]:
        return "unassessed"
    for lv in cfg["levels"]:
        if score >= lv["min_score"]:
            if lv["key"] == "attention" and scored_share < cm["attention_min_scored_share"]:
                return "watch"
            return lv["key"]
    return "none"


def aggregate(p, cfg=CFG):
    rows = []
    for name, g in list(p.groupby("community")) + [("ALL", p)]:
        a = g.area_ha
        tot = a.sum()
        share = {st: float(a[g.state == st].sum() / tot) for st in ("scored", "guarded", "bare", "no_view", "no_weather")}
        sc = g.state == "scored"
        score = wmedian(g.score[sc], a[sc])
        dry = sc & g.s_assessed & (g.s >= 0.5)
        rows.append({"community": name, "area_ha": round(tot, 2), "parcels": int(len(g)),
                     **{f"{k}_share": round(v, 4) for k, v in share.items()},
                     "score": round(score, 1) if np.isfinite(score) else None,
                     "level": level(score, share["scored"], cfg),
                     "dry_ha": round(float(a[dry].sum()), 1), "dry_parcels": int(dry.sum()),
                     "ndvi_median": round(float(g.ndvi[g.fresh].median()), 3) if g.fresh.any() else None,
                     "ndmi_median": round(float(g.ndmi[g.fresh].median()), 3) if g.fresh.any() else None,
                     "kc_mean": round(float(np.average(g.kc, weights=a)), 3),
                     "etc_m3": round(float(g.etc_m3.sum()), 0), "net_m3": round(float(g.net_m3.sum()), 0),
                     "et0_mm": round(float(np.average(g.et0, weights=a)), 1) if g.et0.notna().all() else None,
                     "et0_normal_mm": round(float(np.average(g.et0_normal, weights=a)), 1),
                     "rain_mm": round(float(np.average(g.rain, weights=a)), 1) if g.rain.notna().all() else None,
                     "tmax_c": round(float(np.average(g.tmax, weights=a)), 1) if g.tmax.notna().all() else None})
    return rows


def paired_change(p_now, p_prev):
    """Median NDVI / NDMI difference over parcels with a fresh view in both years, per community and ALL."""
    j = p_now[["community", "fresh", "ndvi", "ndmi"]].join(p_prev[["fresh", "ndvi", "ndmi"]], rsuffix="_prev")
    j = j[j.fresh & j.fresh_prev.fillna(False).astype(bool)]
    out = {}
    for name, g in list(j.groupby("community")) + [("ALL", j)]:
        out[name] = {"pairs": int(len(g)),
                     "ndvi_diff": round(float((g.ndvi - g.ndvi_prev).median()), 3) if len(g) else None,
                     "ndmi_diff": round(float((g.ndmi - g.ndmi_prev).median()), 3) if len(g) else None}
    return out


# ---------------------------------------------------------------- build
def run_year(year, parcels, w, normals, cfg=CFG, obs=None, ref_obs=None):
    weeks = range(cfg["weeks"]["first_iso_week"], cfg["weeks"]["last_iso_week"] + 1)
    obs = load_obs(year, parcels, cfg=cfg) if obs is None else obs
    refs = ndmi_refs(ref_obs, parcels)
    frames, rows = {}, []
    for wk in weeks:
        cut = week_cutoff(year, wk)
        wx = week_weather(w, normals, cut)
        p = week_parcels(obs, cut, parcels, refs, wx, cfg)
        frames[wk] = p
        for r in aggregate(p, cfg):
            rows.append({"year": year, "week": wk, "week_start": str(cut - timedelta(days=6)), "week_end": str(cut), **r})
    return frames, pd.DataFrame(rows)


def build():
    OUT.mkdir(parents=True, exist_ok=True)
    parcels = load_parcels()
    w, normals = load_weather()
    obs = {y: load_obs(y, parcels) for y in DAILY}
    tables, frames = [], {}
    for y in DAILY:
        refy = CFG["reference_years"][str(y)]
        ref_obs = pd.concat([obs[r] for r in refy]) if refy else None
        fr, t = run_year(y, parcels, w, normals, obs=obs[y], ref_obs=ref_obs)
        tables.append(t)
        if y in (CFG["years"]["replay"], CFG["years"]["compare"]):
            frames[y] = fr
            keep = ["community", "ltype", "area_ha", "state", "fresh", "kc", "kc_source", "ndvi", "ndmi", "ndmi_ref", "d", "s", "w", "g", "score", "net_m3"]
            pd.concat([f[keep].assign(week=k) for k, f in fr.items()]).reset_index().to_parquet(OUT / f"parcel_weeks_{y}.parquet", index=False)
        print(y, "done", flush=True)
    cw = pd.concat(tables, ignore_index=True)
    ry, cy = CFG["years"]["replay"], CFG["years"]["compare"]
    pc = []
    for wk in frames[ry]:
        for name, v in paired_change(frames[ry][wk], frames[cy][wk]).items():
            pc.append({"year": ry, "week": wk, "community": name, **v})
    cw = cw.merge(pd.DataFrame(pc), on=["year", "week", "community"], how="left")
    cw.to_parquet(OUT / "community_weeks.parquet", index=False)
    a = cw[cw.community == "ALL"]
    summary = {"version": CFG["version"], "built": datetime.now(timezone.utc).isoformat(),
               "parcels": int(len(parcels)), "area_ha": round(float(parcels.area_ha.sum()), 1),
               "by_type": parcels.groupby("ltype").area_ha.sum().round(1).to_dict(),
               "communities": int(parcels.community.nunique()),
               "season_net_requirement_thousand_m3": {str(y): round(float(g.net_m3.sum()) / 1000, 0) for y, g in a.groupby("year")},
               "scored_share_median": {str(y): round(float(g.scored_share.median()), 3) for y, g in a.groupby("year")},
               "attention_weeks_2026": int(((cw.year == ry) & (cw.level == "attention") & (cw.community != "ALL")).sum()),
               "leak_note": "Land types v1.4 were made in late 2026 (static map); stated on every replay page."}
    (OUT / "summary.json").write_text(json.dumps(summary, indent=1, ensure_ascii=False), encoding="utf-8")
    inputs = {str(p.relative_to(ROOT)): sha(p) for p in [CFG_PATH, LANDTYPES, COMMUNITIES, WEATHER / "daily.parquet",
                                                           WEATHER / "normals.parquet", Path(__file__), *DAILY.values()]}
    (OUT / "manifest.json").write_text(json.dumps({"version": CFG["version"], "created": summary["built"], "inputs": inputs,
                                                   "outputs": {p.name: sha(p) for p in sorted(OUT.glob("*.parquet"))}}, indent=1), encoding="utf-8")
    print(json.dumps(summary, indent=1, ensure_ascii=False))


if __name__ == "__main__":
    if sys.argv[1:2] == ["build"]:
        build()
    else:
        sys.exit(__doc__)
