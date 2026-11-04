"""Water delivery planner, step 3b and 4: the state of every parcel on the planning date (owner approval 2026-11-02,
docs/OWNER_APPROVALS_2026_11_02_EN.md B4; config/canal_planner_v1.json "activity"). Fixed code, never a model.

Activity on the planning date D uses only Sentinel-2 views usable by D (a scene dated t is usable from t + 1, owner-
approved or candidate cloud-leak dates masked), with the cultivation-change v2 green definition:
  active        two or more green views this season (from 1 March) up to D;
  expected      no green view yet, D before the type's green-up date, and active last season (labelled);
  idle_so_far   no green view this season, and past the green-up date with enough clear views, or idle last season;
  unclear       anything else; shown separately and given no water;
  no_farm_water land classes without canal need (no crop, not farmland, greenhouse, not classifiable).
Only active and expected land gets water. Last season's activity is this same rule run on 31 October of the year
before, so no other layer is needed and nothing later than D is read. The approved cultivation-change layers and the
land types are not changed.
"""
from __future__ import annotations

import json
import sys
from datetime import date, timedelta
from functools import lru_cache
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import canal_ops_demand_v1 as DM  # noqa: E402
import season_replay_v1 as SR  # noqa: E402

CFG = json.loads((ROOT / "config/canal_planner_v1.json").read_text(encoding="utf-8"))
ACT = CFG["activity"]
COLS = ["internal_parcel_id", "observation_date", "ndvi_median", "ndmi_median", "ndvi_valid_fraction",
        "vegetation_fraction", "ndvi_inner5_valid_fraction"]
STATES = ("active", "expected", "idle_so_far", "unclear", "no_farm_water")
PERENNIAL = {"orchard", "vineyard", "perennial"}


@lru_cache(maxsize=1)
def parcels() -> pd.DataFrame:
    """Every v1.4 parcel with its demand land class, Kc land type and bucket parameters (demand v1 rules)."""
    return DM.load_parcels()


@lru_cache(maxsize=1)
def leak_dates() -> dict:
    """Masked scene dates per year: the owner-approved list where it exists, else the detector's candidates."""
    d = ROOT / "data/analysis/canal_ops_sim_v1/demand"
    ok = json.loads((d / "cloud_leak_dates_approved.json").read_text(encoding="utf-8"))["dates"]
    cand = json.loads((d / "cloud_leak_dates_candidate.json").read_text(encoding="utf-8"))["candidates"]
    out = {int(y): [c["date"] for c in v] for y, v in cand.items()}
    out.update({int(y): list(v) for y, v in ok.items()})
    return out


@lru_cache(maxsize=8)
def views(year: int) -> pd.DataFrame:
    """Clear views of one year (pid, date, ndvi, ndmi, vf, green, inner_ok), cloud-leak dates removed."""
    path = SR.DAILY.get(year)
    if path is None or not path.exists():
        return pd.DataFrame(columns=["pid", "date", "ndvi", "ndmi", "vf", "green", "inner_ok"])
    o = pd.read_parquet(path, columns=COLS)
    o = o[o.ndvi_median.between(-1, 1) & (o.ndvi_valid_fraction >= ACT["valid_fraction_min"])]
    o = o.rename(columns={"internal_parcel_id": "pid", "ndvi_median": "ndvi", "ndmi_median": "ndmi",
                          "vegetation_fraction": "vf"})
    o["date"] = pd.to_datetime(o.observation_date).dt.date
    o = o[~o.date.isin({date.fromisoformat(x) for x in leak_dates().get(year, [])})]
    o["green"] = (o.ndvi >= ACT["green_ndvi"]) & (o.vf >= ACT["green_vegetation_fraction"])
    o["inner_ok"] = o.ndvi_inner5_valid_fraction.fillna(0) >= DM.CFG["kc"]["inner_valid_min"]
    return o[["pid", "date", "ndvi", "ndmi", "vf", "green", "inner_ok"]].drop_duplicates(["pid", "date"])


def _counts(d: date) -> pd.DataFrame:
    """Per parcel: green and clear views from 1 March to the last date usable on d."""
    m0, d0 = map(int, ACT["season_from"].split("-"))
    o = views(d.year)
    o = o[(o.date >= date(d.year, m0, d0)) & (o.date <= d - timedelta(days=ACT["s2_delay_days"]))]
    g = o.groupby("pid")
    return pd.DataFrame({"n_green": g.green.sum(), "n_views": g.size(), "last_view": g.date.max()})


def activity(d: date, _prev: bool = True) -> pd.DataFrame:
    """Activity of every parcel on the planning date d (see the module text). Columns: activity, basis, n_green,
    n_views, last_view, last_season."""
    p = parcels()
    c = _counts(d).reindex(p.index)
    n_green, n_views = c.n_green.fillna(0).to_numpy(int), c.n_views.fillna(0).to_numpy(int)
    perennial = p.ltype.isin(PERENNIAL).to_numpy()
    until = {k: date(d.year, *map(int, v.split("-"))) for k, v in ACT["expect_until"].items()}
    before_greenup = np.where(perennial, d <= until["perennial"], d <= until["field"])
    if _prev:
        last = activity(date(d.year - 1, 10, 31), _prev=False).activity.reindex(p.index).to_numpy()
    else:
        last = np.full(len(p), "unknown", dtype=object)
    last_active = last == "active"
    farm = (p.rule != "zero").to_numpy()
    state = np.select(
        [~farm, n_green >= ACT["active_min_green"], (n_green == 0) & before_greenup & last_active,
         (n_green == 0) & ((~before_greenup & (n_views >= ACT["idle_min_views"])) | (last == "idle_so_far"))],
        ["no_farm_water", "active", "expected", "idle_so_far"], "unclear")
    basis = np.select([state == "expected", state == "active"], ["last season's state (no green view yet this season)",
                                                                "green views this season"], "")
    return pd.DataFrame({"activity": state, "basis": basis, "n_green": n_green, "n_views": n_views,
                         "last_view": c.last_view.to_numpy(), "last_season": last}, index=p.index)


def check_against_yearly(year: int) -> dict:
    """Activity on 31 October against the cultivation-change v2 yearly state of the same year (approved layers are
    read, never changed): share of v2 'cultivated' called active, of v2 'idle' called idle_so_far, and the spread."""
    y = pd.read_parquet(ROOT / f"data/analysis/cultivation_change_v2/parcels/{year}.parquet",
                        columns=["internal_parcel_id", "state"]).set_index("internal_parcel_id").state
    a = activity(date(year, 10, 31))
    farm = a[a.activity != "no_farm_water"]
    j = farm.join(y, how="inner")
    tab = pd.crosstab(j.state, j.activity)
    share = lambda s, col: round(float((j[j.state == s].activity == col).mean()), 4) if (j.state == s).any() else None
    return {"year": year, "farm_parcels": int(len(j)), "cultivated_called_active": share("cultivated", "active"),
            "idle_called_idle_so_far": share("idle", "idle_so_far"),
            "table": {k: {kk: int(vv) for kk, vv in v.items()} for k, v in tab.to_dict("index").items()}}


# ---------------------------------------------------------------- satellite snapshot and soil water on the planning date
REF_TURN_MM = (CFG["turn"]["min_m3_per_ha"] + CFG["turn"]["max_m3_per_ha"]) / 2 / 10     # m3/ha -> mm


@lru_cache(maxsize=4)
def _kc_views(year: int):
    """Clear views of the demand model with the causal columns, cloud-leak dates masked (demand v1 rules)."""
    return DM.masked_obs(DM.clear_views(year, parcels()), leak_dates().get(year, []))


def snapshot(d: date, band: str = "normal", horizon_days: int = 10) -> pd.DataFrame:
    """Every parcel on the planning date d. Satellite: last usable view, its age, NDVI, Kc and Kc source (demand v1:
    observed / type-based / fallback). Soil water: a reference bucket from 1 February with the weather known on d
    (canal_planner_weather) and the daily Kc; until d, active land is assumed to get a turn of the owner's typical
    size (750 m3/ha, at the field inlet) whenever it reaches the stress point. Recorded deliveries replace this
    assumption once the ledger holds them. From d on, no water: days_to_stress is the first day the depletion passes
    the stress point (RAW), within horizon_days."""
    from wp_core import canal_planner_weather as W
    p = parcels()
    act = activity(d)
    first = date(d.year, 2, 1)
    days = pd.date_range(first, d + timedelta(days=horizon_days))
    t0 = (d - first).days                                          # index of the planning date
    V = DM.daily_views(_kc_views(d.year), pd.Index(p.index), days, DM.CFG["no_hindsight"]["s2_delay_days"])
    kc, src = DM.daily_kc(p, V, days)
    k0 = np.nan_to_num(kc)
    k0[:, t0 + 1:] = k0[:, [t0]]                                    # Kc of the planning date held after it
    cells = sorted(p.cell.unique())
    wx = W.window(d, first, days[-1].date(), band, cells)
    et0 = wx.pivot(index="cell", columns="day", values="et0").reindex(cells).to_numpy(float)
    rain = wx.pivot(index="cell", columns="day", values="rain").reindex(cells).to_numpy(float)
    ci = pd.Index(cells).get_indexer(p.cell)
    w, normals = SR.load_weather()
    taw = p.taw_mm.fillna(1.0).to_numpy(float)
    p0 = p.p.fillna(0.5).to_numpy(float)
    share = DM.CFG["bucket"]["effective_rain_share"]
    water = act.activity.isin(ACT["water"]).to_numpy()
    dr = DM.winter_depletion(p, w, normals, d.year)
    stress_day = np.full(len(p), np.nan)
    for t in range(len(days)):
        etc = k0[:, t] * et0[ci, t]
        raw = DM.p_adjusted(p0, etc) * taw
        if t < t0:
            app = np.where(water & (dr > raw), REF_TURN_MM, 0.0)
        else:
            app = 0.0
            if t == t0:
                dr_d, raw_d = dr.copy(), raw.copy()
            hit = np.isnan(stress_day) & (dr > raw)
            stress_day[hit] = t - t0
        dr = DM.bucket_step(dr, etc, share * rain[ci, t], app, taw, p0)[0]
    farm = (p.rule != "zero").to_numpy()
    with np.errstate(invalid="ignore"):
        age = (DM.day_num(pd.DatetimeIndex([d]))[0] - V["vdate"][:, t0])
    out = act.copy()
    out["area_ha"] = p.area_ha
    out["community"], out["cell"], out["ltype"], out["land_class"] = p.community, p.cell, p.ltype, p.land_class
    out["kc"] = np.where(farm, kc[:, t0], np.nan)
    out["kc_source"] = DM.SRC[src[:, t0]]
    out["view_age_days"] = np.where(farm, age, np.nan)
    out["ndvi_last"] = np.where(farm, V["ndvi"][:, t0], np.nan)
    rc = DM.CFG["ripening"]                                         # demand v1 rule, from views usable by d
    with np.errstate(invalid="ignore"):
        out["ripening"] = ((p.ltype == "field").to_numpy() & farm & (src[:, t0] == DM.OBSERVED)
                           & (V["peak"][:, t0] >= rc["min_peak_ndvi"])
                           & (V["ndvi"][:, t0] <= rc["below_share_of_peak"] * V["peak"][:, t0])
                           & (DM.day_num(pd.DatetimeIndex([d]))[0] - V["peak_date"][:, t0] >= rc["min_days_after_peak"]))
    out["depletion_mm"] = np.where(farm, dr_d, np.nan)
    out["raw_mm"] = np.where(farm, raw_d, np.nan)
    out["taw_mm"] = np.where(farm, taw, np.nan)
    out["days_to_stress"] = np.where(water, stress_day, np.nan)
    out["etc_mm_day"] = np.where(farm, (k0[:, t0:t0 + 7] * et0[ci, t0:t0 + 7]).mean(1), np.nan)
    out.attrs["weather"] = wx[wx.day >= d].groupby("source").size().to_dict()
    return out


def summary(s: pd.DataFrame) -> dict:
    """Hectares by activity and, for land that gets water, by Kc source and by days to stress."""
    ha = lambda m: round(float(s.area_ha[m].sum()), 2)
    water = s.activity.isin(ACT["water"])
    dts = s.days_to_stress
    return {"ha_by_activity": {k: ha(s.activity == k) for k in STATES},
            "water_ha_by_kc_source": {k: ha(water & (s.kc_source == k)) for k in DM.SRC[:3]},
            "water_ha_by_days_to_stress": {"now": ha(water & (dts == 0)), "1-3 days": ha(water & dts.between(1, 3)),
                                           "4-7 days": ha(water & dts.between(4, 7)),
                                           "later": ha(water & (dts > 7) | water & dts.isna())},
            "weather_days_after_planning_date": s.attrs.get("weather", {})}
