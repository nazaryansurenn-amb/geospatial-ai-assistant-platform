"""Deterministic optical canopy-moisture screening; never measured water demand."""
from __future__ import annotations
import json
import os
import hashlib
import sqlite3
import warnings
from pathlib import Path
from datetime import datetime, timezone
from contextlib import contextmanager
from concurrent.futures import ProcessPoolExecutor, as_completed

import numpy as np
import pandas as pd
import geopandas as gpd

ROOT = Path(__file__).resolve().parents[1]
VERSION = "optical_water_screening_20260906_v1"
CONFIG = ROOT / "config" / (VERSION + ".json")
OUT = ROOT / "data/analysis/rapid_water_loss" / VERSION
EO = ROOT / "data/analysis/observation_screening/transitions_area_20260906_v1"
BASE = ROOT / "data/observations/observations_2021_2025_v1"
GEOM = ROOT / "data/source/cadastre/parcels_wua.gpkg"


def read(p):
    return json.loads(Path(p).read_text(encoding="utf-8"))


def sha(p):
    with Path(p).open("rb") as f:
        return hashlib.file_digest(f, "sha256").hexdigest()


def now():
    return datetime.now(timezone.utc).isoformat()


def write(p, value):
    p = Path(p); p.parent.mkdir(parents=True, exist_ok=True)
    temp = p.with_suffix(p.suffix + ".tmp")
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
    os.replace(temp, p)


@contextmanager
def lock():
    import msvcrt
    OUT.mkdir(parents=True, exist_ok=True)
    with (OUT / "run.lock").open("a+b") as f:
        if f.tell() == 0:
            f.write(b"0"); f.flush()
        f.seek(0); msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
        try:
            yield
        finally:
            f.seek(0); msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)


def weather_snapshot():
    jobs = ROOT / "server_data/collector/observations_2021_2025_v1/jobs.sqlite3"
    with sqlite3.connect(jobs.resolve().as_uri()+"?mode=ro", uri=True) as db:
        records = {r[0]: r[1:] for r in db.execute("select id,state,output,sha256,rows from jobs where kind='weather'")}
    result, missing = [], []
    for y in range(2021, 2026):
        for m in range(3, 10):
            month = f"{y}_{m:02d}"; item = records.get("weather_" + month)
            if not item or item[0] != "complete":
                missing.append(month); continue
            state, path, digest, rows = item
            assert sha(ROOT / path) == digest
            result.append({"month": month, "path": path, "sha256": digest, "rows": rows})
    return result, missing


def static_neighbors(fields, cfg):
    """Nearest eligible neighbours, deterministic distance/identifier tie-break."""
    n, maximum = len(fields), cfg["maximum_potential_peers"]
    indices = np.full((n, maximum), -1, dtype=np.int32)
    distances = np.full((n, maximum), np.inf, dtype=np.float32)
    xy = fields[["x_m", "y_m"]].to_numpy()
    areas = fields.area_official_m2.to_numpy(); widths = fields.minimum_width_m.to_numpy()
    for _, group in fields.groupby(["cell_id", "clear_spatial_support"], sort=True):
        ix = group.index.to_numpy()
        for i in ix:
            distance = np.linalg.norm(xy[ix]-xy[i], axis=1)
            candidate = ix[distance <= cfg["maximum_peer_distance_m"]]
            ratios = np.maximum(areas[i], areas[candidate]) / np.minimum(areas[i], areas[candidate])
            width_ratios = np.maximum(widths[i], widths[candidate]) / np.maximum(1e-9, np.minimum(widths[i], widths[candidate]))
            candidate = candidate[(candidate != i) & (ratios <= cfg["maximum_peer_area_ratio"]) & (width_ratios <= cfg["maximum_peer_width_ratio"])]
            dist = np.linalg.norm(xy[candidate]-xy[i], axis=1)
            order = np.lexsort((fields.internal_parcel_id.to_numpy()[candidate], dist))[:maximum]
            size = len(order); indices[i, :size] = candidate[order]; distances[i, :size] = dist[order]
    return indices, distances


def prepare():
    if (OUT / "prepared.json").exists():
        marker = read(OUT / "prepared.json")
        for name, digest in marker["sources"].items():
            assert sha(ROOT / name) == digest, name
        return marker
    cfg = read(CONFIG)
    for y in cfg["years"]:
        path = EO / "daily" / f"{y}.parquet"
        assert sha(path) == read(path.with_suffix(".json"))["sha256"], str(y)
    for relative, digest in read(EO / "spatial_complete.json")["sha256"].items():
        assert sha(ROOT / relative) == digest, relative
    scope = pd.read_parquet(BASE / "scope.parquet")
    fields = pd.read_parquet(EO / "selection.parquet").reset_index(drop=True)
    assert len(fields) == cfg["expected_eligible"] and fields.internal_parcel_id.is_unique
    eligible = scope.loc[scope.included]
    assert set(eligible.internal_parcel_id) == set(fields.internal_parcel_id)
    assert not eligible.household.any() and not eligible.road_excluded.any()
    static = pd.read_parquet(EO / "spatial.parquet")
    assert static.internal_parcel_id.tolist() == fields.internal_parcel_id.tolist()
    fields = fields[["internal_parcel_id", "cadastre_code", "area_official_m2"]].merge(static[["internal_parcel_id", "minimum_width_m", "pure_pixels_20m"]], on="internal_parcel_id", validate="one_to_one")
    geo = gpd.read_file(GEOM, layer="parcels", columns=["cadastre_code", "area_official_m2"])
    geo = geo.set_index("cadastre_code").loc[fields.cadastre_code].reset_index()
    assert np.array_equal(geo.area_official_m2, fields.area_official_m2)
    geo["internal_parcel_id"] = fields.internal_parcel_id
    geo.to_parquet(OUT / "original_geometry.parquet", index=False)
    center = geo.to_crs(32638).geometry.centroid
    fields["x_m"], fields["y_m"] = center.x.to_numpy(), center.y.to_numpy()
    center_wgs = center.to_crs(4326)
    weather, missing = weather_snapshot()
    grid = pd.read_parquet(ROOT / weather[0]["path"], columns=["latitude", "longitude"]).drop_duplicates().sort_values(["latitude", "longitude"]).reset_index(drop=True)
    dx = (center_wgs.x.to_numpy()[:, None]-grid.longitude.to_numpy())*np.cos(np.deg2rad(center_wgs.y.to_numpy()[:, None]))
    dy = center_wgs.y.to_numpy()[:, None]-grid.latitude.to_numpy()
    closest = np.argmin(dx*dx+dy*dy, axis=1)
    fields["latitude"] = grid.latitude.to_numpy()[closest]; fields["longitude"] = grid.longitude.to_numpy()[closest]
    fields["cell_id"] = [f"era5land_{a:.1f}_{b:.1f}" for a,b in zip(fields.latitude, fields.longitude)]
    fields["clear_spatial_support"] = fields.pure_pixels_20m.ge(cfg["clear_support_minimum_pure_pixels_20m"]) & fields.minimum_width_m.ge(cfg["clear_support_minimum_width_m"])
    context_path = ROOT / "data/analysis/rapid_water_loss/rapid_water_loss_20260906_v1/parcels.parquet"
    context = pd.read_parquet(context_path, columns=["internal_parcel_id", "review_community_hy"])
    fields = fields.merge(context, on="internal_parcel_id", validate="one_to_one").rename(columns={"review_community_hy": "community_hy"})
    fields.to_parquet(OUT / "fields.parquet", index=False)
    indices, distances = static_neighbors(fields, cfg)
    np.savez_compressed(OUT / "neighbors.npz", indices=indices, distances=distances)
    grid.to_parquet(OUT / "weather_grid.parquet", index=False)
    sources = [CONFIG, Path(__file__), ROOT / "run_optical_water_screening.py", ROOT / "docs/OPTICAL_WATER_SCREENING_RULE_EN.md",
               BASE / "scope.parquet", EO / "selection.parquet", EO / "spatial.parquet", EO / "weights_10m.npz", EO / "weights_20m.npz",
               GEOM, context_path, ROOT / "config/releases.json", ROOT / "config/transitions_area_20260906_v1.review.lock.json"]
    sources += [EO / "daily" / f"{y}.parquet" for y in cfg["years"]]
    sources += [ROOT / item["path"] for item in weather]
    sources += [OUT / name for name in ("fields.parquet", "original_geometry.parquet", "neighbors.npz", "weather_grid.parquet")]
    marker = {"version": VERSION, "prepared_utc": now(), "config": cfg, "weather": weather, "missing_weather": missing,
              "sources": {p.relative_to(ROOT).as_posix(): sha(p) for p in sources}, "eligible": len(fields),
              "clear_spatial_support": int(fields.clear_spatial_support.sum()), "radar_inputs_used": False,
              "classifications_used": False, "new_downloads": False}
    write(OUT / "prepared.json", marker)
    print(json.dumps({"phase": "prepared", "parcels": len(fields), "clear_spatial_support": marker["clear_spatial_support"]}), flush=True)
    return marker


def common_fraction(masks, weights):
    assert len(weights) and np.isfinite(weights).all() and (weights >= 0).all() and weights.sum() > 0
    assert all(len(m)*8 >= len(weights) for m in masks)
    common = np.frombuffer(masks[0], dtype=np.uint8).copy()
    for m in masks[1:]:
        common &= np.frombuffer(m, dtype=np.uint8)
    bits = np.unpackbits(common, count=len(weights)).astype(bool)
    return float(weights[bits].sum()/weights.sum())


def weights_by_parcel():
    result = {}
    for res in (10, 20):
        with np.load(EO / f"weights_{res}m.npz", allow_pickle=False) as z:
            order = np.argsort(z["parcel"], kind="stable")
            cut = np.cumsum(np.bincount(z["parcel"], minlength=int(z["count"])))[:-1]
            result[res] = np.split(z["area"][order], cut)
    return result


def make_triplets(raw, fields, weights, cfg):
    raw = raw.sort_values(["internal_parcel_id", "observation_date"]).reset_index(drop=True)
    assert not raw.duplicated(["internal_parcel_id", "observation_date"]).any()
    good = raw.support.ge(cfg["minimum_valid_area_fraction"]) & raw.finite.astype(bool)
    good &= np.isfinite(raw[[x+"_median" for x in ("ndvi", "evi2", "ndmi", "bsi", "ndre")]].to_numpy()).all(axis=1)
    frame = raw.loc[good].copy().reset_index(drop=True)
    frame["position"] = pd.Index(fields.internal_parcel_id).get_indexer(frame.internal_parcel_id)
    assert frame.position.ge(0).all()
    frame["day"] = pd.to_datetime(frame.observation_date).astype("int64")//86400_000_000_000
    active = frame.ndvi_median.ge(cfg["minimum_active_ndvi"]) & frame.evi2_median.ge(cfg["minimum_active_evi2"]) & frame.vegetation_fraction.ge(cfg["minimum_vegetation_fraction"])
    profile = fields[["internal_parcel_id", "clear_spatial_support"]].copy()
    counts = frame.groupby("internal_parcel_id").size()
    greens = frame.loc[active].groupby("internal_parcel_id").day.agg(["size", "min", "max"])
    profile["quality_dates"] = profile.internal_parcel_id.map(counts).fillna(0).astype(int)
    profile["active_dates"] = profile.internal_parcel_id.map(greens["size"]).fillna(0).astype(int)
    profile["active_span_days"] = profile.internal_parcel_id.map(greens["max"]-greens["min"]).fillna(0).astype(int)
    profile["active_observed"] = profile.active_dates.ge(cfg["minimum_active_dates"]) & profile.active_span_days.ge(cfg["minimum_active_span_days"])
    profile["activity_state"] = np.select([profile.active_observed, profile.quality_dates.ge(cfg["minimum_quality_dates_for_activity_review"])], ["Active vegetation observed", "Activity criterion not met"], default="Insufficient observations for activity")
    i = np.arange(1, len(frame)-1)
    pos = frame.position.to_numpy(); days = frame.day.to_numpy(); a = active.to_numpy()
    before = days[i]-days[i-1]; after = days[i+1]-days[i]
    keep = (pos[i-1] == pos[i]) & (pos[i] == pos[i+1]) & a[i-1] & a[i] & a[i+1]
    keep &= profile.active_observed.to_numpy()[pos[i]]
    keep &= (before >= cfg["minimum_step_days"]) & (before <= cfg["maximum_step_days"]) & (after >= cfg["minimum_step_days"]) & (after <= cfg["maximum_step_days"]) & (before+after <= cfg["maximum_triplet_days"])
    for name in ("ndvi", "evi2", "bsi"):
        values = frame[name+"_median"].to_numpy()
        keep &= np.ptp(np.stack([values[i-1], values[i], values[i+1]]), axis=0) <= cfg["maximum_"+name+"_range"]
    spread = (frame.ndvi_p90-frame.ndvi_p10).to_numpy()
    keep &= np.max(np.stack([spread[i-1], spread[i], spread[i+1]]), axis=0) <= cfg["maximum_ndvi_spatial_range"]
    i = i[keep]
    overlap = np.ones(len(i))
    for res in (10, 20):
        masks = frame[f"valid_mask_{res}m"].to_numpy()
        overlap = np.minimum(overlap, np.array([common_fraction([masks[j-1], masks[j], masks[j+1]], weights[res][pos[j]]) for j in i]))
    accepted = overlap >= cfg["minimum_common_area_fraction"]
    i, overlap = i[accepted], overlap[accepted]
    values = {"internal_parcel_id": frame.internal_parcel_id.to_numpy()[i], "position": pos[i], "first_day": days[i-1], "middle_day": days[i], "last_day": days[i+1], "common_support": overlap}
    for k, offset in enumerate((-1, 0, 1)):
        values[f"scene_{k}"] = frame.scene_id.to_numpy()[i+offset]
        for name in ("ndvi", "evi2", "ndmi", "bsi"):
            values[f"{name}_{k}"] = frame[name+"_median"].to_numpy()[i+offset]
    triplets = pd.DataFrame(values)
    triplets["drop"] = triplets.ndmi_0-triplets.ndmi_1
    triplets["recovery"] = triplets.ndmi_2-triplets.ndmi_1
    triplets["drying_rate"] = triplets["drop"]/(triplets.middle_day-triplets.first_day)
    return triplets, profile


def match_triplets(triplets, fields, neighbor_ids, distances, cfg):
    result, links = [], []
    for _, g in triplets.groupby(["first_day", "middle_day", "last_day"], sort=True):
        g = g.reset_index(drop=True)
        position = g.position.to_numpy(); n = len(g)
        lookup = np.full(len(fields), -1, dtype=np.int32); lookup[position] = np.arange(n)
        candidates = neighbor_ids[position]
        candidate_rows = lookup[np.maximum(candidates, 0)]
        valid = (candidates >= 0) & (candidate_rows >= 0)
        row_ix = np.maximum(candidate_rows, 0)
        score = np.zeros(valid.shape)
        for name in ("ndvi", "evi2", "bsi"):
            limit = cfg["maximum_peer_"+name+"_difference"]
            for k in range(3):
                v = g[f"{name}_{k}"].to_numpy()
                difference = np.abs(v[:, None]-v[row_ix])
                valid &= difference <= limit; score += difference/limit
        v = g.ndmi_0.to_numpy(); difference = np.abs(v[:, None]-v[row_ix])
        valid &= difference <= cfg["maximum_peer_initial_ndmi_difference"]
        score += difference/cfg["maximum_peer_initial_ndmi_difference"]
        score[~valid] = np.inf
        order = np.argsort(score, axis=1, kind="stable")[:, :cfg["maximum_peers"]]
        chosen = np.take_along_axis(row_ix, order, axis=1)
        usable = np.take_along_axis(valid, order, axis=1)
        g["peer_count"] = usable.sum(axis=1)
        g["comparable"] = g.peer_count.ge(cfg["minimum_peers"])
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", RuntimeWarning)
            for name in ("drop", "recovery", "drying_rate"):
                array = np.where(usable, g[name].to_numpy()[chosen], np.nan)
                median = np.nanmedian(array, axis=1)
                g["peer_median_"+name] = median
                if name == "drying_rate":
                    g["peer_drying_mad"] = np.nanmedian(np.abs(array-median[:, None]), axis=1)*1.4826
        for threshold in cfg["moisture_change_sensitivity"]:
            tag = f"{threshold:.2f}"
            raw = g["drop"].ge(threshold) & g.recovery.ge(threshold)
            g["raw_"+tag] = raw
            frequency = np.sum(usable & raw.to_numpy()[chosen], axis=1)/np.maximum(g.peer_count.to_numpy(), 1)
            g["peer_fraction_"+tag] = np.where(g.comparable, frequency, np.nan)
            g["relative_"+tag] = (g.comparable & raw & (g["drop"]-g.peer_median_drop).ge(cfg["minimum_relative_change"])
                & (g.recovery-g.peer_median_recovery).ge(cfg["minimum_relative_change"])
                & (g.drying_rate-g.peer_median_drying_rate).ge(np.maximum(cfg["minimum_relative_drying_rate_per_day"], cfg["minimum_drying_robust_deviations"]*g.peer_drying_mad)))
        # Save the selected peers for each emitted relative pattern at any setting.
        any_signal = g[["relative_"+f"{t:.2f}" for t in cfg["moisture_change_sensitivity"]]].any(axis=1).to_numpy()
        for row in np.flatnonzero(any_signal):
            for j in np.flatnonzero(usable[row]):
                peer_row = chosen[row, j]
                links.append({"internal_parcel_id": g.internal_parcel_id.iloc[row], "middle_day": int(g.middle_day.iloc[row]),
                    "peer_id": g.internal_parcel_id.iloc[peer_row], "first_day": int(g.first_day.iloc[row]), "last_day": int(g.last_day.iloc[row]),
                    "match_score": float(score[row, order[row, j]]), "distance_m": float(distances[position[row], order[row, j]])})
        result.append(g)
    if result:
        output = pd.concat(result, ignore_index=True)
    else:
        output = triplets.copy()
        for name in ("peer_count", "peer_median_drop", "peer_median_recovery", "peer_median_drying_rate", "peer_drying_mad"):
            output[name] = pd.Series(dtype=float)
        output["comparable"] = pd.Series(dtype=bool)
        for t in cfg["moisture_change_sensitivity"]:
            for prefix in ("raw_", "relative_", "peer_fraction_"):
                output[prefix+f"{t:.2f}"] = pd.Series(dtype=float if prefix == "peer_fraction_" else bool)
    return output, pd.DataFrame(links, columns=["internal_parcel_id", "middle_day", "peer_id", "first_day", "last_day", "match_score", "distance_m"])


def independent_patterns(g, cfg):
    chosen, end, trough = [], -np.inf, -np.inf
    for index, row in g.sort_values(["middle_day", "first_day", "last_day"]).iterrows():
        if row.first_day >= end and row.middle_day-trough >= cfg["minimum_independent_event_separation_days"]:
            chosen.append(index); end, trough = row.last_day, row.middle_day
    return chosen


def season_metrics(diag, profile, cfg):
    rows, event_rows = [], []
    for pid, g in diag.groupby("internal_parcel_id", sort=True):
        comparable = g.loc[g.comparable]
        n = len(comparable)
        span = int(comparable.last_day.max()-comparable.first_day.min()) if n else 0
        enough = n >= cfg["minimum_comparable_triplets"] and span >= cfg["minimum_comparable_span_days"]
        for threshold in cfg["moisture_change_sensitivity"]:
            tag = f"{threshold:.2f}"
            patterns = comparable.loc[comparable["relative_"+tag]]
            selected = independent_patterns(patterns, cfg)
            own = float(comparable["raw_"+tag].mean()) if n else np.nan
            peer_rate = float(comparable["peer_fraction_"+tag].mean()) if n else np.nan
            rows.append({"internal_parcel_id": pid, "threshold": threshold, "structural_triplets": len(g), "comparable_triplets": n,
                "comparable_span_days": span, "assessable": enough, "independent_relative_patterns": len(selected) if n else None,
                "raw_pattern_fraction": own, "peer_pattern_fraction": peer_rate, "excess_pattern_fraction": own-peer_rate,
                "season_signal": bool(len(selected) >= cfg["minimum_independent_relative_patterns"] and own-peer_rate >= cfg["minimum_excess_pattern_fraction"]) if enough else None})
            for index in selected:
                event_rows.append({**diag.loc[index].to_dict(), "threshold": threshold})
    metrics = pd.DataFrame(rows, columns=["internal_parcel_id", "threshold", "structural_triplets", "comparable_triplets", "comparable_span_days", "assessable", "independent_relative_patterns", "raw_pattern_fraction", "peer_pattern_fraction", "excess_pattern_fraction", "season_signal"])
    universe = pd.MultiIndex.from_product([profile.internal_parcel_id, cfg["moisture_change_sensitivity"]], names=["internal_parcel_id", "threshold"])
    metrics = metrics.set_index(["internal_parcel_id", "threshold"]).reindex(universe).reset_index()
    for name in ("structural_triplets", "comparable_triplets", "comparable_span_days"):
        metrics[name] = metrics[name].fillna(0).astype(int)
    metrics["assessable"] = metrics.assessable.fillna(False).astype(bool)
    metrics["season_signal"] = pd.array(metrics.season_signal, dtype="boolean")
    return metrics, pd.DataFrame(event_rows, columns=[*diag.columns, "threshold"])


def daily_rain(weather, tolerance):
    raw = pd.concat([pd.read_parquet(ROOT / item["path"], columns=["valid_time", "latitude", "longitude", "tp"]) for item in weather], ignore_index=True)
    raw["valid_time"] = pd.to_datetime(raw.valid_time, utc=True)
    raw = raw.sort_values(["latitude", "longitude", "valid_time"])
    assert not raw.duplicated(["latitude", "longitude", "valid_time"]).any()
    groups = raw.groupby(["latitude", "longitude"], sort=False)
    change = groups.tp.diff().where(groups.valid_time.diff().eq(pd.Timedelta(hours=1)))
    change = change.where(raw.valid_time.dt.hour.ne(1), raw.tp)*1000.
    raw["precision_corrected"] = np.isfinite(change) & change.lt(0) & change.ge(-tolerance)
    raw["rain_mm"] = change.where(np.isfinite(change) & change.ge(0))
    raw.loc[raw.precision_corrected, "rain_mm"] = 0.
    # Hour timestamp denotes interval end; assign to the preceding hourly interval.
    raw["day"] = (raw.valid_time-pd.Timedelta(seconds=1)).dt.floor("D")
    rows = []
    for (lat, lon, day), g in raw.groupby(["latitude", "longitude", "day"], sort=True):
        complete = len(g) == 24 and g.rain_mm.notna().all()
        rows.append({"cell_id": f"era5land_{lat:.1f}_{lon:.1f}", "day": int(day.value//86400_000_000_000),
                     "complete": bool(complete), "rain_mm": float(g.rain_mm.sum()) if complete else None})
    return pd.DataFrame(rows)


def annotate_weather(events, fields, weather_daily, cfg):
    events = events.merge(fields[["internal_parcel_id", "cell_id"]], on="internal_parcel_id", validate="many_to_one")
    series = {cell: g.set_index("day").rain_mm for cell, g in weather_daily.groupby("cell_id")}
    totals = []
    for row in events.itertuples():
        values = series[row.cell_id].reindex(range(row.middle_day, row.last_day+1))
        totals.append(float(values.sum()) if np.isfinite(values.to_numpy()).all() else np.nan)
    events["recovery_interval_rain_mm"] = totals
    events["rain_context"] = np.select([events.recovery_interval_rain_mm.isna(), events.recovery_interval_rain_mm.le(cfg["rain_context_limit_mm"])], ["Weather unavailable", "Low recorded rain"], default="Rainfall present")
    return events


def run_year(year):
    folder = OUT / "years" / str(year)
    if (folder / "complete.json").exists():
        for name, digest in read(folder / "complete.json")["files"].items():
            assert sha(folder/name) == digest
        return read(folder / "report.json")
    cfg = read(CONFIG); fields = pd.read_parquet(OUT / "fields.parquet")
    columns = ["internal_parcel_id", "observation_date", "scene_id", "support", "finite", "vegetation_fraction", "ndvi_p10", "ndvi_p90", "valid_mask_10m", "valid_mask_20m"] + [x+"_median" for x in ("ndvi", "evi2", "ndmi", "bsi", "ndre")]
    raw = pd.read_parquet(EO / "daily" / f"{year}.parquet", columns=columns,
                          filters=[("observation_date", ">=", f"{year}-"+cfg["season_start"]), ("observation_date", "<=", f"{year}-"+cfg["season_end"])])
    triplets, profile = make_triplets(raw, fields, weights_by_parcel(), cfg)
    print(json.dumps({"phase": "optical_triplets", "year": year, "active_parcels": int(profile.active_observed.sum()), "triplets": len(triplets)}), flush=True)
    with np.load(OUT / "neighbors.npz", allow_pickle=False) as z:
        diag, links = match_triplets(triplets, fields, z["indices"], z["distances"], cfg)
    metrics, events = season_metrics(diag, profile, cfg)
    weather = [x for x in read(OUT / "prepared.json")["weather"] if x["month"].startswith(str(year))]
    rain = daily_rain(weather, cfg["negative_precipitation_tolerance_mm"])
    events = annotate_weather(events, fields, rain, cfg)
    for f in (profile, diag, links, metrics, events):
        f["year"] = year
    nominal = metrics.loc[metrics.threshold.eq(cfg["nominal_moisture_change"])]
    report = {"year": year, "season": [f"{year}-"+cfg["season_start"], f"{year}-"+cfg["season_end"]], "input_rows": len(raw),
        "active_parcels": int(profile.active_observed.sum()), "structural_triplets": len(diag), "comparable_triplets": int(diag.comparable.sum()),
        "assessable_parcels": int(nominal.assessable.sum()), "season_signal_parcels": int(nominal.season_signal.fillna(False).sum()),
        "nominal_patterns": int(events.threshold.eq(cfg["nominal_moisture_change"]).sum()), "weather_months": [x["month"] for x in weather]}
    folder.mkdir(parents=True, exist_ok=True)
    files = {"activity.parquet": profile, "triplets.parquet": diag, "event_peers.parquet": links, "metrics.parquet": metrics, "events.parquet": events, "weather_daily.parquet": rain}
    for name, frame in files.items():
        assert not (folder/name).exists(), "Preserve interrupted results: " + name
        frame.to_parquet(folder/name, index=False)
    write(folder / "report.json", report)
    write(folder / "complete.json", {"files": {name: sha(folder/name) for name in [*files, "report.json"]}, "completed_utc": now()})
    print(json.dumps({"phase": "year_complete", **report}), flush=True)
    return report


def aggregate(activity, metrics, events, fields, cfg):
    result = fields.copy()
    pid = result.internal_parcel_id
    result["active_years"] = pid.map(activity.loc[activity.active_observed].groupby("internal_parcel_id").year.nunique()).fillna(0).astype(int)
    nominal = metrics.loc[metrics.threshold.eq(cfg["nominal_moisture_change"])]
    positive = nominal.loc[nominal.season_signal.fillna(False)]
    result["assessable_years"] = pid.map(nominal.loc[nominal.assessable].groupby("internal_parcel_id").year.nunique()).fillna(0).astype(int)
    result["positive_years"] = pid.map(positive.groupby("internal_parcel_id").year.nunique()).fillna(0).astype(int)
    result["positive_year_list"] = pid.map(positive.groupby("internal_parcel_id").year.agg(lambda x: ", ".join(map(str, sorted(set(x)))))).fillna("")
    result["comparable_triplets"] = pid.map(nominal.groupby("internal_parcel_id").comparable_triplets.sum()).fillna(0).astype(int)
    main_events = events.loc[events.threshold.eq(cfg["nominal_moisture_change"])]
    result["independent_relative_patterns"] = pid.map(main_events.groupby("internal_parcel_id").size()).fillna(0).astype(int)
    for name, subset in (("low_rain_patterns", main_events.loc[main_events.rain_context.eq("Low recorded rain")]), ("unknown_weather_patterns", main_events.loc[main_events.rain_context.eq("Weather unavailable")])):
        result[name] = pid.map(subset.groupby("internal_parcel_id").size()).fillna(0).astype(int)
    for threshold in cfg["moisture_change_sensitivity"]:
        t = metrics.loc[metrics.threshold.eq(threshold) & metrics.season_signal.fillna(False)]
        counts = pid.map(t.groupby("internal_parcel_id").year.nunique()).fillna(0)
        result["recurrent_"+f"{threshold:.2f}"] = counts.ge(cfg["minimum_recurrent_years"])
    recurrent = result.positive_years.ge(cfg["minimum_recurrent_years"])
    result["evidence_status"] = np.select([recurrent & result.clear_spatial_support, recurrent, result.positive_years.gt(0), result.independent_relative_patterns.gt(0), result.assessable_years.gt(0), result.active_years.gt(0)],
        ["Recurrent optical candidate", "Recurrent pattern - mixed spatial support", "One-season relative pattern", "Isolated relative pattern", "No recurrent pattern in assessable years", "Active; insufficient comparable evidence"], default="Activity not established by this EO rule")
    result["water_demand_status"] = "Unassessed"
    return result


def summarize(reports):
    cfg = read(CONFIG); fields = pd.read_parquet(OUT / "fields.parquet")
    frames = {name: pd.concat([pd.read_parquet(OUT / "years" / str(y) / (name+".parquet")) for y in cfg["years"]], ignore_index=True) for name in ("activity", "metrics", "events")}
    attributes = aggregate(frames["activity"], frames["metrics"], frames["events"], fields, cfg)
    geo = gpd.read_parquet(OUT / "original_geometry.parquet")[["internal_parcel_id", "geometry"]]
    result = geo.merge(attributes, on="internal_parcel_id", validate="one_to_one")
    point = result.geometry.representative_point()
    result["parcel_latitude"], result["parcel_longitude"] = point.y, point.x
    result["area_ha"] = result.area_official_m2/10000.
    result = result.sort_values(["positive_years", "independent_relative_patterns", "community_hy", "cadastre_code"], ascending=[False, False, True, True]).reset_index(drop=True)
    candidates = result.loc[result.evidence_status.eq("Recurrent optical candidate")]
    mixed = result.loc[result.evidence_status.eq("Recurrent pattern - mixed spatial support")]
    one = result.loc[result.evidence_status.eq("One-season relative pattern")]
    active = result.loc[result.active_years.gt(0)]
    for name, frame in (("all_parcels", result), ("active_parcels", active), ("recurrent_candidates", candidates), ("mixed_support_candidates", mixed), ("one_season_leads", one)):
        frame.to_parquet(OUT / (name+".parquet"), index=False)
        public_fields = ["internal_parcel_id", "cadastre_code", "area_official_m2", "area_ha", "community_hy", "parcel_latitude", "parcel_longitude", "evidence_status", "water_demand_status", "active_years", "assessable_years", "positive_years", "positive_year_list", "independent_relative_patterns", "comparable_triplets", "low_rain_patterns", "unknown_weather_patterns", "clear_spatial_support", "recurrent_0.03", "recurrent_0.04", "recurrent_0.05"]
        frame[public_fields].to_csv(OUT / (name+".csv"), encoding="utf-8-sig", index=False)
    result.to_file(OUT / "parcel_results.gpkg", layer="all_22802_parcels", driver="GPKG", index=False)
    for layer, frame in (("recurrent_optical_candidates", candidates), ("mixed_support_review", mixed)):
        if len(frame):
            frame.to_file(OUT / "parcel_results.gpkg", layer=layer, driver="GPKG", index=False)
    communities = result.groupby("community_hy", dropna=False).agg(eligible=("internal_parcel_id", "size"), active=("active_years", lambda x: int(x.gt(0).sum())),
        recurrent_candidates=("evidence_status", lambda x: int(x.eq("Recurrent optical candidate").sum())), mixed_support_candidates=("evidence_status", lambda x: int(x.eq("Recurrent pattern - mixed spatial support").sum()))).reset_index()
    communities.to_csv(OUT / "community_summary.csv", encoding="utf-8-sig", index=False)
    report = {"version": VERSION, "completed_utc": now(), "eligible": len(result), "active_parcels": len(active), "recurrent_candidates": len(candidates),
        "mixed_support_candidates": len(mixed), "one_season_leads": len(one), "state_counts": result.evidence_status.value_counts().to_dict(),
        "threshold_sensitivity_clear_support": {f"{t:.2f}": int((result["recurrent_"+f"{t:.2f}"] & result.clear_spatial_support).sum()) for t in cfg["moisture_change_sensitivity"]},
        "years": sorted(reports, key=lambda x: x["year"]), "missing_weather": read(OUT / "prepared.json")["missing_weather"],
        "radar_used": False, "prior_land_use_labels_used": False, "eo_downloads": 0, "map_changed": False, "high_water_demand_assessment": "Unassessed", "interpretation": cfg["interpretation"]}
    write(OUT / "report.json", report)
    files = [p for p in OUT.iterdir() if p.is_file() and p.name not in ("run.lock", "complete.json")]
    write(OUT / "complete.json", {"files": {p.name: sha(p) for p in files}, "year_manifests": {str(y): sha(OUT / "years" / str(y) / "complete.json") for y in cfg["years"]}})
    return report


def main():
    with lock():
        prepare()
        if (OUT / "complete.json").exists():
            for name, digest in read(OUT / "complete.json")["files"].items():
                assert sha(OUT/name) == digest
            print(json.dumps(read(OUT / "report.json"), ensure_ascii=False, indent=2)); return
        cfg = read(CONFIG)
        with ProcessPoolExecutor(max_workers=min(cfg["workers"], os.cpu_count() or 1)) as pool:
            futures = [pool.submit(run_year, y) for y in cfg["years"]]
            reports = [f.result() for f in as_completed(futures)]
        print(json.dumps(summarize(reports), ensure_ascii=False, indent=2), flush=True)
