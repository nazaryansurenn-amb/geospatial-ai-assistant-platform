"""Data-mistake auditor, fixed-code part (plan v5 section 3 'Data-fix loop', agent #2; rules in
config/audit_detectors_v1.json).

Detectors run over land types v1.4 (read-only) and the 2021-2026 Sentinel-2 daily series and return hits. A hit is a
question to the owner, never a claimed error. Each hit carries a rule id, an opaque parcel handle (never the cadastral
code or the internal parcel id), hectares, a width class and evidence fields. Geometry, code, area and hygiene checks
are report-only; the other checks may become proposals that change one field, type6.

Proposals go to an append-only, hash-chained JSONL ledger. A rejection suppresses re-proposal of the same fingerprint
(rule, handle, field, current value). The owner decides one proposal at a time on the loopback review page; the apply
job (owner only) writes land types v1.5 to a new folder and never touches v1.4.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import shutil
import stat
import tempfile
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = ROOT / "config/audit_detectors_v1.json"
GENESIS = "0" * 64
DAILY_COLUMNS = ["internal_parcel_id", "observation_date", "ndvi_median", "ndmi_median", "bsi_median",
                 "ndvi_valid_fraction", "ndvi_p10", "ndvi_p90", "ndvi_inner5_mean", "ndvi_inner5_valid_fraction"]
YEARLY_COLUMNS = ["internal_parcel_id", "state", "peak_ndvi", "n_green", "green_span_days", "max_ndvi_jun_oct",
                  "pipeline_cycles"]
PARCEL_COLUMNS = ["internal_parcel_id", "item_id", "community", "area_ha", "mean_width_m", "photo_date", "photo_current",
                  "type6", "main_class", "owner_label", "source_v11"]
CURVE_INDICES = ("ndvi", "ndmi", "bsi")


# ---------------------------------------------------------------- helpers
def load_config(path=CONFIG_PATH) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def canonical(obj) -> bytes:
    return json.dumps(obj, sort_keys=True, ensure_ascii=False, separators=(",", ":"), default=_plain).encode("utf-8")


def _plain(v):
    if isinstance(v, (np.integer,)):
        return int(v)
    if isinstance(v, (np.floating,)):
        return None if np.isnan(v) else float(v)
    if isinstance(v, (np.bool_,)):
        return bool(v)
    if isinstance(v, (date, datetime, pd.Timestamp)):
        return v.isoformat()[:10]
    raise TypeError(f"not JSON: {type(v)}")


def sha256_bytes(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def sha256_file(path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def handle_of(pid: str, salt: str) -> str:
    """Opaque, stable parcel handle; the cadastral code and the internal id never leave the PC through a hit."""
    return "p" + sha256_bytes(f"{salt}|{pid}".encode())[:12]


def width_class(width_m, cfg) -> str:
    w = cfg["width_classes"]
    if width_m is None or pd.isna(width_m):
        return "unknown"
    return "wide" if width_m >= w["wide_min_m"] else ("sliver" if width_m < w["sliver_below_m"] else "strip")


def type_source(source) -> str:
    """Coarse origin of the current type, for the owner and the auditor (never public)."""
    s = str(source or "")
    if s.startswith("owner label"):
        return "owner label"
    if s == "fixed":
        return "fixed: not classifiable"
    if s == "v1":
        return "AI photo review"
    if s.startswith("unclassified"):
        return "AI photo review, low confidence"
    if "rule" in s or s.startswith("thin strip"):
        return "deterministic rule"
    if s.startswith("owner review"):
        return "owner review"
    return "other"


def _f(v, nd=3):
    return None if v is None or pd.isna(v) else round(float(v), nd)


def _day(dates) -> np.ndarray:
    return pd.to_datetime(dates).values.astype("datetime64[D]").astype(np.int64)


# ---------------------------------------------------------------- series aggregation
@dataclass
class SeriesAgg:
    """Per-parcel aggregates of the daily series; built one year at a time so 25 M rows never sit in memory."""
    oor_parcel: list = field(default_factory=list)      # frames: pid, year, ndvi_rows, bsi_rows, clear_rows, dates, max_abs
    oor_year: dict = field(default_factory=dict)        # year -> counts
    dips: list = field(default_factory=list)            # frames: pid, year, date
    edge: list = field(default_factory=list)            # frames: pid, year, dates_over
    monthly: list = field(default_factory=list)         # frames: pid, year, month, ndvi, ndmi, bsi
    clear: pd.DataFrame | None = None                   # latest-year clear stats, index pid
    series_end: dict = field(default_factory=dict)      # year -> last observation date

    def frame(self, name) -> pd.DataFrame:
        parts = [p for p in getattr(self, name) if len(p)]
        return pd.concat(parts, ignore_index=True) if parts else pd.DataFrame()


def aggregate_year(series: SeriesAgg, daily: pd.DataFrame, year: int, cfg: dict) -> None:
    lo, hi = cfg["series"]["valid_range"]
    gate = cfg["series"]["clear_valid_fraction_min"]
    c1, c2, c12 = (cfg["checks"][k] for k in ("AUD01_OUT_OF_RANGE", "AUD02_CLOUD_LEAK_DATE", "AUD12_EDGE_INTERIOR"))
    d = daily
    pid = d.internal_parcel_id.astype(str)
    series.series_end[year] = str(d.observation_date.max())

    if year in c1["years"]:
        n, b = d.ndvi_median, d.bsi_median
        bad_n = n.notna() & ((n < lo) | (n > hi))
        bad_b = b.notna() & ((b < lo) | (b > hi))
        bad_p = ((d.ndvi_p10 < lo) | (d.ndvi_p10 > hi) | (d.ndvi_p90 < lo) | (d.ndvi_p90 > hi)).fillna(False)
        series.oor_year[year] = {"ndvi_rows": int(bad_n.sum()), "ndvi_parcels": int(pid[bad_n].nunique()),
                                 "ndvi_dates": int(d.observation_date[bad_n].nunique()),
                                 "ndvi_rows_passing_clear_gate": int((bad_n & (d.ndvi_valid_fraction >= gate)).sum()),
                                 "bsi_rows": int(bad_b.sum()), "p10_p90_rows": int(bad_p.sum())}
        bad = bad_n | bad_b
        if bad.any():
            o = pd.DataFrame({"pid": pid[bad], "date": d.observation_date[bad], "ndvi": bad_n[bad], "bsi": bad_b[bad],
                              "clear": (d.ndvi_valid_fraction[bad] >= gate),
                              "absmax": np.fmax(n[bad].abs(), b[bad].abs())})
            series.oor_parcel.append(o.groupby("pid").agg(ndvi_rows=("ndvi", "sum"), bsi_rows=("bsi", "sum"),
                                                          clear_rows=("clear", "sum"), dates=("date", "nunique"),
                                                          max_abs=("absmax", "max")).reset_index().assign(year=year))

    clear = d.ndvi_valid_fraction.ge(gate) & d.ndvi_median.between(lo, hi)
    v = pd.DataFrame({"pid": pid[clear], "date": d.observation_date[clear].astype(str),
                      "ndvi": d.ndvi_median[clear], "ndmi": d.ndmi_median[clear].where(d.ndmi_median[clear].between(lo, hi)),
                      "bsi": d.bsi_median[clear].where(d.bsi_median[clear].between(lo, hi)),
                      "inner": d.ndvi_inner5_mean[clear], "inner_valid": d.ndvi_inner5_valid_fraction[clear]})
    v = v.sort_values(["pid", "date"], kind="stable").reset_index(drop=True)
    v["day"] = _day(v.date)

    # cloud leak candidates: a drop from the previous clear date that recovers soon after
    if year in c2["years"] and len(v):
        same = lambda k: v.pid.shift(k).eq(v.pid)                      # noqa: E731
        prev = v.ndvi.shift(1).where(same(1))
        drop = v.ndvi <= prev - c2["drop_min"]
        rec = pd.Series(False, index=v.index)
        for k in range(1, 9):
            nk, dk = v.ndvi.shift(-k), v.day.shift(-k)
            rec |= same(-k) & (dk - v.day <= c2["recover_days"]) & (nk >= prev - c2["recover_within"])
        dip = drop & rec
        series.dips.append(pd.DataFrame({"pid": v.pid[dip], "year": year, "date": v.date[dip]}))

    if year in c12["years"] and len(v):
        e = v[(v.inner_valid >= c12["inner_valid_min"]) & v.inner.between(lo, hi)]
        over = (e.inner - e.ndvi).abs() > c12["max_diff"]
        cnt = e[over].groupby("pid").size()
        series.edge.append(pd.DataFrame({"pid": cnt.index, "year": year, "dates_over": cnt.values}))

    if len(v):
        m = v.assign(month=pd.to_datetime(v.date).dt.month).groupby(["pid", "month"])[list(CURVE_INDICES)].median()
        series.monthly.append(m.reset_index().assign(year=year))

    c15 = cfg["checks"]["AUD15_STALE_THIN_SERIES"]
    if year == c15["year"]:
        end = _day([series.series_end[year]])[0]
        w0, w1 = (_day([f"{year}-{md}"])[0] for md in c15["window"])
        win = v[(v.day >= w0) & (v.day <= w1)]
        last = v.groupby("pid").day.max()
        n_win = win.groupby("pid").size()
        bounds = pd.concat([win[["pid", "day"]],                      # window edges count as views for the gap
                            pd.DataFrame({"pid": last.index, "day": w0}),
                            pd.DataFrame({"pid": last.index, "day": w1})]).drop_duplicates()
        bounds = bounds.sort_values(["pid", "day"])
        gap = bounds.day.diff().where(bounds.pid.eq(bounds.pid.shift(1)))   # days between consecutive clear views
        max_gap = gap.groupby(bounds.pid).max()
        series.clear = pd.DataFrame({"last_clear_day": last, "days_since_clear": end - last}).join(
            n_win.rename("clear_dates_window")).join(max_gap.rename("max_gap_days"))
        series.clear["clear_dates_window"] = series.clear.clear_dates_window.fillna(0).astype(int)


def daily_path(cfg, root, year) -> Path:
    key = "daily_2026" if year == 2026 else "daily_2021_2025"
    return Path(root) / cfg["inputs"][key].format(year=year)


def aggregate_series(cfg, root=ROOT, years=None, loader=None) -> SeriesAgg:
    series = SeriesAgg()
    for y in years or cfg["years"]:
        d = loader(y) if loader else pd.read_parquet(daily_path(cfg, root, y), columns=DAILY_COLUMNS)
        aggregate_year(series, d, y, cfg)
        del d
    return series


# ---------------------------------------------------------------- context
@dataclass
class Context:
    parcels: pd.DataFrame                   # index internal_parcel_id; PARCEL_COLUMNS
    years: dict                             # year -> DataFrame (index pid) of the yearly state columns
    series: SeriesAgg
    spatial: pd.DataFrame                   # index pid: area_official_m2, geometry_area_m2
    geometry: object = None                 # GeoDataFrame (pid column, metric CRS) or None
    stress: pd.DataFrame | None = None      # index pid: state
    v3: pd.DataFrame | None = None          # index pid: crop_type_candidate
    weather: pd.DataFrame | None = None


def load_context(cfg, root=ROOT, series: SeriesAgg | None = None) -> Context:
    import geopandas as gpd
    root = Path(root)
    i = cfg["inputs"]
    parcels = pd.read_parquet(root / i["landtypes_v1_4"], columns=PARCEL_COLUMNS).set_index("internal_parcel_id")
    years = {y: pd.read_parquet(root / i["yearly_state"].format(year=y), columns=YEARLY_COLUMNS)
             .set_index("internal_parcel_id") for y in cfg["years"]}
    sp = pd.read_parquet(root / i["spatial"], columns=["internal_parcel_id", "cadastre_code", "area_official_m2",
                                                       "geometry_area_m2"])
    code_to_pid = sp.set_index("cadastre_code").internal_parcel_id      # stays inside this function
    spatial = sp.drop(columns="cadastre_code").set_index("internal_parcel_id")
    st = pd.read_parquet(root / i["stress"], columns=["cadastre_code", "state"])
    stress = pd.DataFrame({"state": st.state.values}, index=st.cadastre_code.map(code_to_pid).values)
    stress = stress[stress.index.notna()]
    v3 = pd.read_parquet(root / i["transitions_v3"], columns=["internal_parcel_id", "crop_type_candidate"]) \
        .set_index("internal_parcel_id")
    geom = gpd.read_parquet(root / i["geometry"], columns=["internal_parcel_id", "geometry"])
    geom = geom[geom.internal_parcel_id.isin(parcels.index)].reset_index(drop=True)
    weather = pd.read_parquet(root / i["weather"], columns=["date", "cell", "et0_fao_evapotranspiration",
                                                            "precipitation_sum", "complete", "year"])
    return Context(parcels=parcels, years=years, series=series if series is not None else aggregate_series(cfg, root),
                   spatial=spatial, geometry=geom, stress=stress, v3=v3, weather=weather)


# ---------------------------------------------------------------- hits
QUESTIONS = {
    "AUD03_NO_CROP_CULTIVATED": "Typed 'no crop in 2025', but the 2025 satellite series shows a full crop cycle. "
                                "Should this parcel be '{suggest}'?",
    "AUD04_NON_FARM_CULTIVATED": "Typed 'not farmland', but the satellite series shows a cultivated season in {n} of "
                                 "2024-2026. Should this parcel be '{suggest}'?",
    "AUD05_FIELD_CROP_NEVER_CULTIVATED": "Typed 'field crops', but no year 2021-2026 shows a cultivated season "
                                         "({idle} years look not cultivated). Should this parcel be '{suggest}'?",
    "AUD06_PERENNIAL_NO_GREEN": "Typed '{type}', but the June-October NDVI stayed under {thr} in 2025 and 2026. Is the "
                                "type right? (Review only: this test misses many real perennials.)",
    "AUD07_PERENNIAL_CROP_CYCLES": "Typed '{type}', but the series shows an annual crop cycle in {n} of 2024-2026 (a "
                                   "young planting or intercrop is possible). Should this parcel be '{suggest}'?",
    "AUD08_GREENHOUSE_FIELD_CURVE": "Typed 'greenhouse', but the {years} series looks like an open-field crop cycle. "
                                    "Should this parcel be '{suggest}'?",
    "AUD12_EDGE_INTERIOR": "The parcel's interior differs from its whole-parcel NDVI by more than {thr} on {n} dates in "
                           "{years} (a mixed parcel or an edge effect is possible). Is the type right?",
    "AUD13_STRESS_ON_NON_FARM": "Typed '{type}', but the 2026 irrigation stress rule picked it as a stress candidate. "
                                "Is the type right?",
    "AUD14_TYPE_VS_TRANSITIONS_V3": "Typed '{type}', but the older Transitions v3 pipeline calls it an annual crop. Is "
                                    "the type right?",
}
TYPE_NAMES = {"FIELD_CROP": "Field crops", "NO_CROP": "No crop in 2025", "VINEYARD": "Vineyard", "ORCHARD": "Orchard",
              "PERENNIAL": "Perennial, type unknown", "NON_FARM": "Not farmland", "GREENHOUSE": "Greenhouse",
              "CROPLAND_UNCERTAIN": "Field land, 2025 use unknown", "UNCLASSIFIED": "Unclassified", None: "not classifiable"}


def _hit(cfg, ctx, rule_id, pid, evidence, report_reason=None, **qargs):
    c = cfg["checks"][rule_id]
    p = ctx.parcels.loc[pid]
    t = p.type6 if isinstance(p.type6, str) else None
    reason = report_reason
    if c["report_only"]:
        reason = reason or f"{c.get('kind', 'report')} check: report-only"
    elif isinstance(p.owner_label, str):
        reason = "owner label wins: reported, never proposed"
    elif t is None:
        reason = "not classifiable parcel: reported, never proposed"
    suggest = c.get("suggest")
    hit = {"rule_id": rule_id, "check_no": c["no"], "rule_version": cfg["version"], "kind": "parcel",
           "handle": handle_of(pid, cfg["handle_salt"]), "ha": _f(p.area_ha, 4),
           "width_class": width_class(p.mean_width_m, cfg), "type": t, "evidence": evidence,
           "report_only": reason is not None, "report_reason": reason, "_pid": pid}
    if reason is None:
        q = QUESTIONS[rule_id].format(type=TYPE_NAMES.get(t, t), suggest=TYPE_NAMES.get(suggest, suggest), **qargs)
        hit["proposal"] = {"field": cfg["proposal_field"], "from": t, "suggested": suggest, "question": q}
    return hit


def _years_where(ctx, years, pred) -> dict:
    """pid -> list of years where pred(yearly frame) holds."""
    out = {}
    for y in years:
        yf = ctx.years.get(y)
        if yf is None:
            continue
        for pid in yf.index[pred(yf).fillna(False).to_numpy()]:
            out.setdefault(pid, []).append(y)
    return out


def detect_out_of_range(cfg, ctx):
    o = ctx.series.frame("oor_parcel")
    hits = []
    if len(o):
        for pid, g in o.groupby("pid"):
            if pid not in ctx.parcels.index:
                continue
            ev = {"ndvi_rows": int(g.ndvi_rows.sum()), "bsi_rows": int(g.bsi_rows.sum()),
                  "rows_passing_clear_gate": int(g.clear_rows.sum()), "dates": int(g.dates.sum()),
                  "years": sorted(int(y) for y in g.year), "max_abs_value": _f(g.max_abs.max())}
            hits.append(_hit(cfg, ctx, "AUD01_OUT_OF_RANGE", pid, ev))
    return hits, {"by_year": {str(y): v for y, v in sorted(ctx.series.oor_year.items())}}


def detect_cloud_leak_dates(cfg, ctx):
    c = cfg["checks"]["AUD02_CLOUD_LEAK_DATE"]
    dp = ctx.series.frame("dips")
    hits, by_year = [], {}
    if len(dp):
        cnt = dp.groupby(["year", "date"]).size()
        for (y, d), n in cnt[cnt >= c["min_parcels"]].sort_index().items():
            hits.append({"rule_id": "AUD02_CLOUD_LEAK_DATE", "check_no": c["no"], "rule_version": cfg["version"],
                         "kind": "date", "date": d, "evidence": {"parcels_dipping": int(n)}, "report_only": True,
                         "report_reason": "hygiene check: mask the date in a new data version"})
        for y, g in cnt.groupby(level=0):
            fl = g[g >= c["min_parcels"]]
            by_year[str(y)] = {"dip_rows": int(g.sum()), "flagged_dates": int(len(fl)), "rows_on_flagged_dates": int(fl.sum()),
                               "largest": [{"date": d, "parcels": int(n)} for (_, d), n in fl.nlargest(3).items()]}
    return hits, {"by_year": by_year}


def detect_no_crop_cultivated(cfg, ctx):
    c = cfg["checks"]["AUD03_NO_CROP_CULTIVATED"]
    yf = ctx.years[c["year"]]
    p = ctx.parcels
    ids = p.index[(p.type6 == "NO_CROP") & p.index.isin(yf.index[yf.state == "cultivated"])]
    hits = []
    for pid in ids:
        r = yf.loc[pid]
        ev = {"year": c["year"], "state": "cultivated", "peak_ndvi": _f(r.peak_ndvi), "green_dates": int(r.n_green),
              "green_span_days": int(r.green_span_days),
              "state_2026": _state(ctx, 2026, pid)}
        hits.append(_hit(cfg, ctx, "AUD03_NO_CROP_CULTIVATED", pid, ev))
    return hits, {}


def _state(ctx, year, pid):
    yf = ctx.years.get(year)
    return None if yf is None or pid not in yf.index else str(yf.at[pid, "state"])


def detect_non_farm_cultivated(cfg, ctx):
    c = cfg["checks"]["AUD04_NON_FARM_CULTIVATED"]
    cult = _years_where(ctx, c["years"], lambda yf: yf.state == "cultivated")
    p = ctx.parcels
    hits = []
    for pid in p.index[p.type6 == "NON_FARM"]:
        ys = cult.get(pid, [])
        if len(ys) >= c["min_years"]:
            w = p.at[pid, "mean_width_m"]
            strip = not (w >= c["proposal_min_width_m"])
            hits.append(_hit(cfg, ctx, "AUD04_NON_FARM_CULTIVATED", pid, {"cultivated_years": ys},
                             report_reason="strip under 20 m: road-edge mixing, report-only" if strip else None,
                             n=len(ys)))
    return hits, {}


def detect_field_crop_never_cultivated(cfg, ctx):
    c = cfg["checks"]["AUD05_FIELD_CROP_NEVER_CULTIVATED"]
    cult = _years_where(ctx, c["years"], lambda yf: yf.state == "cultivated")
    idle = _years_where(ctx, c["years"], lambda yf: yf.state == "idle")
    p = ctx.parcels
    hits = []
    for pid in p.index[p.type6 == "FIELD_CROP"]:
        if not cult.get(pid) and len(idle.get(pid, [])) >= c["min_idle_years"]:
            hits.append(_hit(cfg, ctx, "AUD05_FIELD_CROP_NEVER_CULTIVATED", pid,
                             {"cultivated_years": [], "not_cultivated_years": idle[pid]}, idle=len(idle[pid])))
    return hits, {}


def detect_perennial_no_green(cfg, ctx):
    c = cfg["checks"]["AUD06_PERENNIAL_NO_GREEN"]
    p = ctx.parcels
    hits = []
    for pid in p.index[p.type6.isin(c["types"])]:
        mx = {y: ctx.years[y].at[pid, "max_ndvi_jun_oct"] if pid in ctx.years[y].index else np.nan for y in c["years"]}
        if all(pd.notna(v) and v < c["max_ndvi"] for v in mx.values()):
            hits.append(_hit(cfg, ctx, "AUD06_PERENNIAL_NO_GREEN", pid,
                             {"max_ndvi_jun_oct": {str(y): _f(v) for y, v in mx.items()}}, thr=c["max_ndvi"]))
    return hits, {}


def detect_perennial_crop_cycles(cfg, ctx):
    c = cfg["checks"]["AUD07_PERENNIAL_CROP_CYCLES"]
    cyc = _years_where(ctx, c["years"], lambda yf: yf.pipeline_cycles >= 1)
    p = ctx.parcels
    hits = []
    for pid in p.index[p.type6.isin(c["types"])]:
        ys = cyc.get(pid, [])
        if len(ys) >= c["min_years"]:
            hits.append(_hit(cfg, ctx, "AUD07_PERENNIAL_CROP_CYCLES", pid, {"crop_cycle_years": ys}, n=len(ys)))
    return hits, {}


def detect_greenhouse_field_curve(cfg, ctx):
    c = cfg["checks"]["AUD08_GREENHOUSE_FIELD_CURVE"]
    cyc = _years_where(ctx, c["years"], lambda yf: yf.pipeline_cycles >= 1)
    p = ctx.parcels
    hits = []
    for pid in p.index[p.type6 == "GREENHOUSE"]:
        ys = cyc.get(pid, [])
        if ys:
            hits.append(_hit(cfg, ctx, "AUD08_GREENHOUSE_FIELD_CURVE", pid, {"crop_cycle_years": ys},
                             years=" and ".join(map(str, ys))))
    return hits, {}


def detect_slivers(cfg, ctx):
    c = cfg["checks"]["AUD09_SLIVER"]
    p = ctx.parcels
    m = (p.area_ha < c["min_area_ha"]) | (p.mean_width_m < c["min_width_m"])
    hits = [_hit(cfg, ctx, "AUD09_SLIVER", pid, {"area_ha": _f(p.at[pid, "area_ha"], 4),
                                                  "mean_width_m": _f(p.at[pid, "mean_width_m"], 1)})
            for pid in p.index[m]]
    return hits, {"already_not_classifiable": int((m & p.type6.isna()).sum())}


def detect_duplicate_nested(cfg, ctx):
    import geopandas as gpd
    import shapely
    c = cfg["checks"]["AUD10_DUPLICATE_NESTED"]
    if ctx.geometry is None or not len(ctx.geometry):
        return [], {"skipped": "no geometry"}
    g = ctx.geometry
    g = g.to_crs(c["metric_crs"]) if g.crs is not None and str(g.crs) != c["metric_crs"] else g
    g = g[["internal_parcel_id", "geometry"]].reset_index(drop=True)
    j = gpd.sjoin(g, g, predicate="intersects")
    j = j[j.index < j.index_right]
    a, b = g.geometry.values[j.index.to_numpy()], g.geometry.values[j.index_right.to_numpy()]
    inter = shapely.area(shapely.intersection(a, b))
    small = np.minimum(shapely.area(a), shapely.area(b))
    same = shapely.equals(a, b)
    nested = inter > c["nested_share"] * small
    hits = []
    salt = cfg["handle_salt"]
    for k in np.flatnonzero(nested):
        pa, pb = j.internal_parcel_id_left.iat[k], j.internal_parcel_id_right.iat[k]
        ev = {"relation": "identical" if same[k] else "nested", "other_handle": handle_of(pb, salt),
              "overlap_m2": _f(inter[k], 1), "overlap_share_of_smaller": _f(inter[k] / small[k] if small[k] else 1.0)}
        hits.append(_hit(cfg, ctx, "AUD10_DUPLICATE_NESTED", pa, ev) | {"kind": "pair"})
    return hits, {"identical_pairs": int(same.sum()), "nested_pairs": int((nested & ~same).sum()),
                  "pairs_overlap_over_report_m2": int((inter > c["overlap_report_m2"]).sum())}


def detect_area_mismatch(cfg, ctx):
    c = cfg["checks"]["AUD11_AREA_MISMATCH"]
    s = ctx.spatial[ctx.spatial.index.isin(ctx.parcels.index)]
    rel = (s.geometry_area_m2 - s.area_official_m2).abs() / s.area_official_m2
    hits = [_hit(cfg, ctx, "AUD11_AREA_MISMATCH", pid, {"geometry_m2": _f(s.at[pid, "geometry_area_m2"], 0),
                                                         "official_m2": _f(s.at[pid, "area_official_m2"], 0),
                                                         "rel_diff": _f(rel[pid])})
            for pid in rel.index[rel > c["max_rel_diff"]]]
    return hits, {"over_large_rel_diff": int((rel > c["large_rel_diff"]).sum())}


def detect_edge_interior(cfg, ctx):
    c = cfg["checks"]["AUD12_EDGE_INTERIOR"]
    e = ctx.series.frame("edge")
    hits = []
    if len(e):
        e = e[e.year.isin(c["years"])].groupby("pid").dates_over.sum()
        for pid, n in e[e >= c["min_dates"]].items():
            if pid in ctx.parcels.index:
                hits.append(_hit(cfg, ctx, "AUD12_EDGE_INTERIOR", pid, {"dates_over": int(n), "years": c["years"]},
                                 thr=c["max_diff"], n=int(n), years=", ".join(map(str, c["years"]))))
    return hits, {}


def detect_stress_on_non_farm(cfg, ctx):
    c = cfg["checks"]["AUD13_STRESS_ON_NON_FARM"]
    if ctx.stress is None:
        return [], {"skipped": "no stress layer"}
    cand = ctx.stress.index[ctx.stress.state == "candidate"]
    p = ctx.parcels
    types = [t for t in c["types"] if t is not None]
    want = p.type6.isin(types) | (p.type6.isna() if None in c["types"] else False)
    hits = [_hit(cfg, ctx, "AUD13_STRESS_ON_NON_FARM", pid, {"stress_state": "candidate"})
            for pid in p.index[want & p.index.isin(cand)]]
    return hits, {"stress_candidates": int(len(cand))}


def detect_type_vs_v3(cfg, ctx):
    c = cfg["checks"]["AUD14_TYPE_VS_TRANSITIONS_V3"]
    if ctx.v3 is None:
        return [], {"skipped": "no transitions v3"}
    ann = ctx.v3.index[ctx.v3.crop_type_candidate == "annual"]
    p = ctx.parcels
    hits = [_hit(cfg, ctx, "AUD14_TYPE_VS_TRANSITIONS_V3", pid, {"transitions_v3": "annual"})
            for pid in p.index[p.type6.isin(c["types"]) & p.index.isin(ann)]]
    return hits, {}


def detect_stale_thin(cfg, ctx):
    c = cfg["checks"]["AUD15_STALE_THIN_SERIES"]
    cl = ctx.series.clear
    hits, counts = [], {"stale": 0, "thin": 0, "gap": 0, "no_clear_date": 0}
    if cl is None:
        return hits, {"skipped": "latest year not aggregated"}
    cl = cl.reindex(ctx.parcels.index)
    for pid, r in cl.iterrows():
        flags = []
        if pd.isna(r.days_since_clear):
            flags.append("no_clear_date")
        else:
            if r.days_since_clear > c["stale_days"]:
                flags.append("stale")
            if r.clear_dates_window < c["thin_min_dates"]:
                flags.append("thin")
            if r.max_gap_days > c["max_gap_days"]:
                flags.append("gap")
        for f_ in flags:
            counts[f_] += 1
        if flags:
            hits.append(_hit(cfg, ctx, "AUD15_STALE_THIN_SERIES", pid, {
                "flags": flags, "days_since_clear": _f(r.days_since_clear, 0),
                "clear_dates_window": None if pd.isna(r.clear_dates_window) else int(r.clear_dates_window),
                "max_gap_days": _f(r.max_gap_days, 0)}))
    return hits, counts | {"series_end": ctx.series.series_end.get(c["year"])}


def detect_weather_gaps(cfg, ctx):
    c = cfg["checks"]["AUD16_WEATHER_GAPS"]
    w = ctx.weather
    if w is None:
        return [], {"skipped": "no weather store"}
    w = w.assign(date=pd.to_datetime(w.date))
    hits, counts = [], {"missing_days": 0, "negative_rain": 0, "et0_over_max": 0}
    last = {}
    for y in c["years"]:
        lo, hi = pd.Timestamp(f"{y}-{c['window'][0]}"), pd.Timestamp(f"{y}-{c['window'][1]}")
        wy = w[(w.date >= lo) & (w.date <= hi)]
        stored = wy[wy.et0_fao_evapotranspiration.notna() | wy.precipitation_sum.notna()]
        if not len(stored):
            continue
        end = min(hi, stored.date.max())
        last[str(y)] = end.date().isoformat()
        want = pd.date_range(lo, end, freq="D")
        for cell in sorted(w.cell.unique()):
            wc = wy[(wy.cell == cell) & (wy.date <= end)].set_index("date")
            bad = {}
            for d_ in want.difference(wc.index):
                bad[d_] = "missing day"
            ok = wc[~wc.index.isin(bad)]
            for d_ in ok.index[ok.et0_fao_evapotranspiration.isna() | ok.precipitation_sum.isna() | ~ok.complete.astype(bool)]:
                bad[d_] = "missing values"
            for d_ in ok.index[ok.precipitation_sum < 0]:
                bad[d_] = "negative rain"
            for d_ in ok.index[ok.et0_fao_evapotranspiration > c["max_et0"]]:
                bad[d_] = "ET0 above max"
            for d_, why in sorted(bad.items()):
                counts[{"missing day": "missing_days", "missing values": "missing_days", "negative rain": "negative_rain",
                        "ET0 above max": "et0_over_max"}[why]] += 1
                hits.append({"rule_id": "AUD16_WEATHER_GAPS", "check_no": c["no"], "rule_version": cfg["version"],
                             "kind": "weather", "date": d_.date().isoformat(), "evidence": {"cell": cell, "problem": why},
                             "report_only": True, "report_reason": "hygiene check: fix in a new weather version"})
    return hits, counts | {"last_day_checked": last}


DETECTORS = {
    "AUD01_OUT_OF_RANGE": detect_out_of_range,
    "AUD02_CLOUD_LEAK_DATE": detect_cloud_leak_dates,
    "AUD03_NO_CROP_CULTIVATED": detect_no_crop_cultivated,
    "AUD04_NON_FARM_CULTIVATED": detect_non_farm_cultivated,
    "AUD05_FIELD_CROP_NEVER_CULTIVATED": detect_field_crop_never_cultivated,
    "AUD06_PERENNIAL_NO_GREEN": detect_perennial_no_green,
    "AUD07_PERENNIAL_CROP_CYCLES": detect_perennial_crop_cycles,
    "AUD08_GREENHOUSE_FIELD_CURVE": detect_greenhouse_field_curve,
    "AUD09_SLIVER": detect_slivers,
    "AUD10_DUPLICATE_NESTED": detect_duplicate_nested,
    "AUD11_AREA_MISMATCH": detect_area_mismatch,
    "AUD12_EDGE_INTERIOR": detect_edge_interior,
    "AUD13_STRESS_ON_NON_FARM": detect_stress_on_non_farm,
    "AUD14_TYPE_VS_TRANSITIONS_V3": detect_type_vs_v3,
    "AUD15_STALE_THIN_SERIES": detect_stale_thin,
    "AUD16_WEATHER_GAPS": detect_weather_gaps,
}


def run_detectors(cfg, ctx):
    """All 16 checks -> (hits, summary). Hits still carry the private `_pid`; strip it with public_hit()."""
    if set(DETECTORS) != set(cfg["checks"]):
        raise ValueError("config checks and detectors differ")
    hits, summary = [], {"version": cfg["version"], "parcels": int(len(ctx.parcels)),
                         "parcels_reconcile": int(len(ctx.parcels)) == cfg["expected_parcels"], "checks": {}}
    for rule_id, fn in DETECTORS.items():
        h, extra = fn(cfg, ctx)
        parcel_hits = [x for x in h if x.get("_pid") is not None]
        pids = {x["_pid"] for x in parcel_hits}
        prop = [x for x in parcel_hits if not x["report_only"]]
        summary["checks"][rule_id] = {
            "no": cfg["checks"][rule_id]["no"], "title": cfg["checks"][rule_id]["title"],
            "report_only": cfg["checks"][rule_id]["report_only"], "hits": len(h), "parcels": len(pids),
            "ha": round(float(ctx.parcels.loc[list(pids), "area_ha"].sum()), 1) if pids else 0.0,
            "wide_parcels": sum(1 for x in {x["_pid"]: x for x in parcel_hits}.values() if x["width_class"] == "wide"),
            "proposable": len(prop), "proposable_ha": round(sum(x["ha"] for x in prop), 1)} | extra
        hits.extend(h)
    return hits, summary


def public_hit(hit) -> dict:
    return {k: v for k, v in hit.items() if not k.startswith("_")}


# ---------------------------------------------------------------- evidence
def evidence_packet(cfg, ctx, hit) -> dict:
    """What the owner and the auditor see for one parcel hit: type and its source, photo date, 2021-2026 curves."""
    pid = hit["_pid"]
    p = ctx.parcels.loc[pid]
    t = p.type6 if isinstance(p.type6, str) else None
    mon = getattr(ctx, "_monthly_by_pid", None)
    if mon is None:
        m = ctx.series.frame("monthly")
        mon = {k: g for k, g in m.groupby("pid")} if len(m) else {}
        ctx._monthly_by_pid = mon
    g = mon.get(pid)
    curve = {}
    for y in cfg["years"]:
        row = {ix: [None] * 12 for ix in CURVE_INDICES}
        if g is not None:
            for r in g[g.year == y].itertuples():
                for ix in CURVE_INDICES:
                    row[ix][int(r.month) - 1] = _f(getattr(r, ix))
        curve[str(y)] = row
    years = {}
    for y in cfg["years"]:
        yf = ctx.years.get(y)
        if yf is not None and pid in yf.index:
            r = yf.loc[pid]
            years[str(y)] = {"state": str(r.state), "peak_ndvi": _f(r.peak_ndvi), "max_ndvi_jun_oct": _f(r.max_ndvi_jun_oct),
                             "crop_cycles": int(r.pipeline_cycles), "green_dates": int(r.n_green)}
    packet = {"handle": hit["handle"], "rule_id": hit["rule_id"], "rule_version": hit["rule_version"],
              "ha": hit["ha"], "width_class": hit["width_class"],
              "community": p.community if isinstance(p.community, str) else None,
              "type": {"value": t, "name": TYPE_NAMES.get(t, t), "main_class": p.main_class if isinstance(p.main_class, str) else None,
                       "source": type_source(p.source_v11), "owner_label": isinstance(p.owner_label, str)},
              "photo": {"date": p.photo_date if isinstance(p.photo_date, str) else None,
                        "current": bool(p.photo_current) if isinstance(p.photo_current, (bool, np.bool_)) else None},
              "curve_monthly_median": curve, "years": years, "hit_evidence": hit["evidence"]}
    packet["evidence_sha"] = sha256_bytes(canonical(packet))
    return packet


# ---------------------------------------------------------------- ledger
class LedgerError(RuntimeError):
    pass


def fingerprint(rule_id, handle, field_, current) -> str:
    return sha256_bytes(f"{rule_id}|{handle}|{field_}|{current}".encode())[:24]


class Ledger:
    """Append-only, hash-chained JSONL. Every line stores the previous line's hash; any edit breaks verify()."""

    def __init__(self, path):
        self.path = Path(path)

    def read(self) -> list:
        if not self.path.exists():
            return []
        events, prev = [], GENESIS
        for n, line in enumerate(self.path.read_text(encoding="utf-8").splitlines(), 1):
            if not line.strip():
                continue
            ev = json.loads(line)
            body = {k: v for k, v in ev.items() if k != "hash"}
            if ev.get("prev") != prev or ev.get("seq") != len(events) + 1 or sha256_bytes(canonical(body)) != ev.get("hash"):
                raise LedgerError(f"ledger chain broken at line {n}")
            events.append(ev)
            prev = ev["hash"]
        return events

    def append(self, event: dict, events=None) -> dict:
        events = self.read() if events is None else events
        ev = {"seq": len(events) + 1, "at": datetime.now(timezone.utc).isoformat(), **event,
              "prev": events[-1]["hash"] if events else GENESIS}
        ev["hash"] = sha256_bytes(canonical(ev))
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.path, "a", encoding="utf-8", newline="\n") as f:
            f.write(canonical(ev).decode("utf-8") + "\n")
            f.flush()
            os.fsync(f.fileno())
        events.append(ev)
        return ev


def ledger_state(events) -> dict:
    """proposal_id -> {'proposal', 'decision' (latest accept/reject or None), 'comments'}."""
    out = {}
    for ev in events:
        if ev["event"] == "proposed":
            out[ev["proposal_id"]] = {"proposal": ev, "decision": None, "comments": []}
        elif ev["event"] == "decided" and ev["proposal_id"] in out:
            out[ev["proposal_id"]]["decision"] = ev
        elif ev["event"] == "comment" and ev["proposal_id"] in out:
            out[ev["proposal_id"]]["comments"].append(ev)
    return out


def check_proposal(cfg, hit, seen: dict, rejected: set) -> str | None:
    """The data-fix Checker: None when the hit may become a proposal, else the reason it may not."""
    c = cfg["checks"].get(hit.get("rule_id"))
    if c is None:
        return "unknown rule"
    if c["report_only"] or hit.get("report_only") or "proposal" not in hit:
        return "report-only"
    pr = hit["proposal"]
    if pr["field"] != cfg["proposal_field"]:
        return "field not allowed"
    if pr["suggested"] is not None and pr["suggested"] not in cfg["allowed_types"]:
        return "suggested type not allowed"
    fp = fingerprint(hit["rule_id"], hit["handle"], pr["field"], pr["from"])
    if fp in rejected:
        return "rejected before"
    if fp in seen:
        return "already proposed"
    return None


def propose(cfg, ledger: Ledger, hits, packets: dict, run_id: str, inputs_sha: str) -> dict:
    """Append every proposable hit not proposed before. `packets` maps (rule_id, handle) -> evidence packet."""
    events = ledger.read()
    state = ledger_state(events)
    seen = {s["proposal"]["fingerprint"]: pid for pid, s in state.items()}
    rejected = {s["proposal"]["fingerprint"] for s in state.values()
                if s["decision"] and s["decision"]["decision"] == "reject"}
    counts = {"new": 0, "report-only": 0, "rejected before": 0, "already proposed": 0, "other": 0}
    n = len(state)
    order = sorted((h for h in hits if h.get("handle")), key=lambda h: (h["check_no"], -(h.get("ha") or 0), h["handle"]))
    for h in order:
        why = check_proposal(cfg, h, seen, rejected)
        if why:
            counts[why if why in counts else "other"] += 1
            continue
        pr = h["proposal"]
        pk = packets[(h["rule_id"], h["handle"])]
        n += 1
        fp = fingerprint(h["rule_id"], h["handle"], pr["field"], pr["from"])
        ev = ledger.append({"event": "proposed", "proposal_id": f"AP{n:05d}", "fingerprint": fp, "rule_id": h["rule_id"],
                            "rule_version": h["rule_version"], "handle": h["handle"], "field": pr["field"],
                            "from": pr["from"], "suggested": pr["suggested"], "question": pr["question"],
                            "ha": h["ha"], "width_class": h["width_class"], "evidence_sha": pk["evidence_sha"],
                            "run_id": run_id, "inputs_sha": inputs_sha}, events)
        seen[fp] = ev["proposal_id"]
        counts["new"] += 1
    return counts


DECISIONS = ("accept", "reject", "comment")


def decide(cfg, ledger: Ledger, proposal_id: str, decision: str, value=None, comment: str = "", via="review page"):
    """One owner decision on one proposal. accept needs an allowed type different from the current one."""
    if decision not in DECISIONS:
        raise ValueError("decision must be accept, reject or comment")
    events = ledger.read()
    st = ledger_state(events).get(proposal_id)
    if st is None:
        raise ValueError("unknown proposal")
    comment = str(comment or "")[:500]
    if decision == "comment":
        if not comment.strip():
            raise ValueError("empty comment")
        return ledger.append({"event": "comment", "proposal_id": proposal_id, "comment": comment, "via": via}, events)
    ev = {"event": "decided", "proposal_id": proposal_id, "decision": decision, "comment": comment, "via": via}
    if decision == "accept":
        if value not in cfg["allowed_types"]:
            raise ValueError("choose an allowed type")
        if value == st["proposal"]["from"]:
            raise ValueError("the chosen type equals the current type")
        ev["value"] = value
    return ledger.append(ev, events)


# ---------------------------------------------------------------- apply (owner only; never called by a model)
class ApplyRefused(RuntimeError):
    pass


SOURCE_BY = {"owner": "owner review (audit v1)",
             "owner bulk accept": "audit fix, bulk-accepted by owner, not individually reviewed",
             "AI expert panel": "audit fix, decided by AI expert panel (irrigation + remote sensing agree), not owner-reviewed",
             "other": "audit fix, decided outside the review page, not owner-reviewed"}
HOW_BY = {"owner": "reviewed by the owner one at a time", "owner bulk accept": "bulk-accepted by the owner without "
          "individual review", "AI expert panel": "decided by an AI expert panel (irrigation + remote sensing agree), "
          "not owner-reviewed", "other": "decided outside the review page"}


def decided_by(dec) -> str:
    via = str(dec.get("via", ""))
    if via.startswith("review page"):
        return "owner"
    if via.startswith("bulk"):
        return "owner bulk accept"
    if via.startswith("AI expert"):
        return "AI expert panel"
    return "other"


def _readonly(folder: Path):
    for f in folder.rglob("*"):
        if f.is_file():
            f.chmod(stat.S_IREAD)


def build_release(cfg, base_dir, target_dir, ledger_path, frozen_files=()) -> dict:
    """Write land types v1.5 = v1.4 + the owner's accepted proposals. Refuses if the target exists, the ledger chain is
    broken, the base changed since the proposals were made, or nothing was accepted. Owner labels win. v1.4 is only
    read. The release is written to a temporary folder and renamed, so a failure leaves nothing behind."""
    base_dir, target_dir = Path(base_dir), Path(target_dir)
    if target_dir.exists():
        raise ApplyRefused(f"{target_dir} exists; versions are never overwritten")
    events = Ledger(ledger_path).read()
    state = ledger_state(events)
    base_file = base_dir / "parcels.parquet"
    base_sha = sha256_file(base_file)
    accepted = [s for s in state.values() if s["decision"] and s["decision"]["decision"] == "accept"]
    if not accepted:
        raise ApplyRefused("no accepted proposals: nothing to apply")
    lapsed = [s["proposal"]["proposal_id"] for s in accepted if s["proposal"]["inputs_sha"] != base_sha]
    if lapsed:
        raise ApplyRefused(f"base release changed since {len(lapsed)} proposals were made (approval lapsed)")
    d = pd.read_parquet(base_file)
    before = d.copy()
    handles = d.internal_parcel_id.map(lambda p: handle_of(p, cfg["handle_salt"]))
    row_of = pd.Series(d.index, index=handles.values)
    d["type6_v14"] = d.type6
    d["audit_rule"] = None
    d["audit_proposal_id"] = None
    d["audit_bulk"] = False
    d["audit_decided_by"] = None
    applied, skipped = [], []
    for s in sorted(accepted, key=lambda s: s["proposal"]["proposal_id"]):
        p, dec = s["proposal"], s["decision"]
        k = row_of.get(p["handle"])
        if k is None:
            skipped.append({"proposal_id": p["proposal_id"], "why": "handle not in base"})
            continue
        cur = d.at[k, "type6"] if isinstance(d.at[k, "type6"], str) else None
        if isinstance(before.at[k, "owner_label"], str):
            skipped.append({"proposal_id": p["proposal_id"], "why": "owner label wins"})
            continue
        if cur != p["from"]:
            skipped.append({"proposal_id": p["proposal_id"], "why": "type changed since proposal (lapsed)"})
            continue
        d.at[k, "type6"] = dec["value"]
        d.at[k, "main_class"] = cfg["main_class"].get(dec["value"], dec["value"])
        by = decided_by(dec)
        if by == "owner":                                       # only the owner's own review becomes an owner label
            d.at[k, "owner_label"] = dec["value"]
        d.at[k, "source_v11"] = SOURCE_BY[by]
        d.at[k, "audit_bulk"] = by != "owner"                   # True = not individually reviewed by the owner
        d.at[k, "audit_decided_by"] = by
        d.at[k, "audit_rule"] = p["rule_id"]
        d.at[k, "audit_proposal_id"] = p["proposal_id"]
        applied.append({"proposal_id": p["proposal_id"], "item_id": d.at[k, "item_id"], "handle": p["handle"],
                        "rule_id": p["rule_id"], "from": p["from"], "to": dec["value"], "ha": float(d.at[k, "area_ha"]),
                        "decided_by": by})
    if not applied:
        raise ApplyRefused("no accepted proposal could be applied: " + json.dumps(skipped))
    recon = reconcile(cfg, before, d, applied)
    counts = lambda frame, col: {str(k): {"parcels": int(len(g)), "ha": round(g.area_ha.sum(), 1)}   # noqa: E731
                                 for k, g in frame.groupby(frame[col].fillna("not classifiable"))}
    tmp = Path(tempfile.mkdtemp(prefix=".landtypes_v1_5_", dir=target_dir.parent))
    try:
        (tmp / "frozen").mkdir()
        d.to_parquet(tmp / "parcels.parquet", index=False)
        d.to_csv(tmp / "parcels.csv", index=False, encoding="utf-8-sig")
        pd.DataFrame(applied).to_csv(tmp / "changes.csv", index=False, encoding="utf-8-sig")
        shutil.copyfile(ledger_path, tmp / "frozen" / "proposals.jsonl")
        for f in frozen_files:
            shutil.copyfile(f, tmp / "frozen" / Path(f).name)
        summary = {"version": "v1.5", "base": base_dir.name, "applied": len(applied), "skipped": skipped,
                   "bulk_accepted_not_reviewed": int(d.audit_bulk.sum()),
                   "decided_by": pd.Series([a["decided_by"] for a in applied]).value_counts().astype(int).to_dict(),
                   "applied_by_rule": pd.DataFrame(applied).groupby("rule_id").size().astype(int).to_dict(),
                   "types": counts(d, "type6"), "main_classes": counts(d, "main_class"), "reconcile": recon,
                   "note": "owner-accepted auditor proposals on top of v1.4; owner labels win; geometry, codes and "
                           "area unchanged; v1.4 stays read-only as rollback"}
        (tmp / "summary.json").write_text(json.dumps(summary, indent=1, default=_plain), encoding="utf-8")
        by_n = pd.Series([a["decided_by"] for a in applied]).value_counts()
        how = ", ".join(f"{int(by_n[b])} {HOW_BY[b]}" for b in SOURCE_BY if b in by_n)
        (tmp / "README.md").write_text(
            f"# Land types v1.5 (frozen {datetime.now(timezone.utc).date()})\n\nv1.4 plus {len(applied)} auditor "
            f"proposals: {how} (ledger snapshot in frozen/; column audit_decided_by says who decided; only the "
            "owner's own one-at-a-time decisions become owner labels). Owner labels win. Geometry, cadastral codes "
            "and area are unchanged. Read-only; v1.4 stays as rollback; a new version goes to a new folder.\n",
            encoding="utf-8")
        files = {f.relative_to(tmp).as_posix(): sha256_file(f) for f in sorted(tmp.rglob("*")) if f.is_file()}
        manifest = {"version": "v1.5", "created": datetime.now(timezone.utc).isoformat(),
                    "label": "AI-interpreted from imagery + owner labels + owner-accepted auditor fixes "
                             f"({how}); not field-verified",
                    "inputs": {f"{base_dir.name}/parcels.parquet": base_sha,
                               "ledger_last_hash": events[-1]["hash"], "ledger_events": len(events)},
                    "parcels_sha256": files["parcels.parquet"], "files_sha256": files}
        (tmp / "manifest.json").write_text(json.dumps(manifest, indent=1), encoding="utf-8")
        if sha256_file(base_file) != base_sha:
            raise ApplyRefused("base release changed during apply")
        if target_dir.exists():
            raise ApplyRefused(f"{target_dir} exists; versions are never overwritten")
        tmp.rename(target_dir)
    except BaseException:
        shutil.rmtree(tmp, ignore_errors=True)
        raise
    _readonly(target_dir)
    return summary


def reconcile(cfg, before: pd.DataFrame, after: pd.DataFrame, applied) -> dict:
    """Totals must reconcile: same parcels, same area, same geometry fields; type counts move only by the changes."""
    if len(after) != cfg["expected_parcels"] or len(before) != cfg["expected_parcels"]:
        raise ApplyRefused(f"parcel count {len(after)} != {cfg['expected_parcels']}")
    if not before.internal_parcel_id.equals(after.internal_parcel_id):
        raise ApplyRefused("parcel ids changed")
    for col in ("area_ha", "mean_width_m", "lon", "lat", "item_id"):
        if col in before and not before[col].equals(after[col]):
            raise ApplyRefused(f"{col} changed: geometry, codes and area are never edited")
    tb, ta = before.type6.fillna("-").value_counts(), after.type6.fillna("-").value_counts()
    delta = ta.sub(tb, fill_value=0)
    want = pd.Series(0.0, index=delta.index)
    for a in applied:
        want[a["to"]] = want.get(a["to"], 0) + 1
        want[a["from"]] = want.get(a["from"], 0) - 1
    if not delta.reindex(want.index, fill_value=0).eq(want).all() or delta.abs().sum() != want.reindex(delta.index, fill_value=0).abs().sum():
        raise ApplyRefused("type counts do not match the applied changes")
    changed = int((before.type6.fillna("-") != after.type6.fillna("-")).sum())
    if changed != len(applied):
        raise ApplyRefused("changed rows do not match the applied changes")
    return {"parcels": int(len(after)), "area_ha": round(float(after.area_ha.sum()), 4),
            "area_ha_base": round(float(before.area_ha.sum()), 4), "changed_rows": changed}


# ---------------------------------------------------------------- review page tokens
def item_sig(secret: bytes, proposal_id: str, evidence_sha: str) -> str:
    return hmac.new(secret, f"{proposal_id}|{evidence_sha}".encode(), hashlib.sha256).hexdigest()[:32]
