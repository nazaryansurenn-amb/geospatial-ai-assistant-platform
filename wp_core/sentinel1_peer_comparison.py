"""Private matched-parcel radar diagnostics; never measured irrigation or drainage.

No collector mutation, new downloads, classification labels, public assets or LLM calls.
All thresholds are declared exploratory sensitivity parameters. Missing support is null.
"""
from __future__ import annotations

import calendar
import hashlib
import json
import os
import shutil
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

import geopandas as gpd
import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
VERSION = "sentinel1_peer_comparison_20260906_v1"
CONFIG = ROOT / "config" / (VERSION + ".json")
OUT = ROOT / "data/analysis/rapid_water_loss" / VERSION
WEATHER = ROOT / "data/observations/observations_2021_2025_v1/weather/raw_tables"
JOBS = ROOT / "server_data/collector/observations_2021_2025_v1/jobs.sqlite3"


def read(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def sha(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def utc():
    return datetime.now(timezone.utc).isoformat()


def write(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(data, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    os.replace(temp, path)


@contextmanager
def lock():
    import msvcrt
    OUT.mkdir(parents=True, exist_ok=True)
    with (OUT / "run.lock").open("a+b") as handle:
        if handle.tell() == 0:
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        try:
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            raise RuntimeError("Comparison already running; no duplicate worker started") from None
        try:
            yield
        finally:
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)


def verify_prepared():
    manifest = read(OUT / "prepared.json")
    for name, digest in manifest["sources"].items():
        if sha(ROOT / name) != digest:
            raise ValueError("Prepared source changed: " + name)
    return manifest


def prepare():
    if (OUT / "prepared.json").exists():
        return verify_prepared()["summary"]
    cfg = read(CONFIG)
    # The completed pilot verifies its immutable sources, clips and EO table.
    from wp_core.sentinel1_pilot import verify as verify_pilot
    verify_pilot()
    pilot = ROOT / cfg["pilot"]
    selection = gpd.read_parquet(pilot / "selection.parquet")
    scope_path = ROOT / "data/observations/observations_2021_2025_v1/scope.parquet"
    scope = pd.read_parquet(scope_path).set_index("internal_parcel_id").loc[selection.internal_parcel_id]
    assert scope.included.all() and not scope.household.astype(bool).any() and not scope.road_excluded.astype(bool).any()
    assert scope.cadastre_code.tolist() == selection.cadastre_code.tolist()
    assert scope.area_official_m2.tolist() == selection.area_official_m2.tolist()
    assert len(selection) == 96 and selection.internal_parcel_id.is_unique
    context = WEATHER / "weather_2025_03.parquet"
    cells = pd.read_parquet(context, columns=["latitude", "longitude"]).drop_duplicates().sort_values(["latitude", "longitude"]).reset_index(drop=True)
    points = selection.geometry.centroid
    ll = points.to_crs(4326)
    d = ((ll.x.to_numpy()[:, None] - cells.longitude.to_numpy()) * np.cos(np.deg2rad(ll.y.to_numpy()[:, None])))**2 + (ll.y.to_numpy()[:, None] - cells.latitude.to_numpy())**2
    nearest = cells.iloc[d.argmin(axis=1)].reset_index(drop=True)
    fields = selection.drop(columns="geometry").copy().reset_index(drop=True)
    fields["x_m"], fields["y_m"] = points.x.to_numpy(), points.y.to_numpy()
    fields["latitude"], fields["longitude"] = nearest.latitude, nearest.longitude
    fields["cell_id"] = [f"era5land_{a:.1f}_{b:.1f}" for a, b in zip(nearest.latitude, nearest.longitude)]
    potential = []
    for a in fields.itertuples():
        for b in fields.itertuples():
            distance = float(np.hypot(a.x_m-b.x_m, a.y_m-b.y_m))
            ratio = max(a.area_official_m2, b.area_official_m2) / min(a.area_official_m2, b.area_official_m2)
            if (a.internal_parcel_id != b.internal_parcel_id and a.sample_group == b.sample_group == "clean_interior"
                    and distance <= cfg["maximum_neighbor_distance_m"] and ratio <= cfg["maximum_area_ratio"]
                    and a.cell_id == b.cell_id):
                potential.append({"internal_parcel_id": a.internal_parcel_id, "peer_id": b.internal_parcel_id,
                                  "distance_m": distance, "area_ratio": ratio})
    fields.to_parquet(OUT / "fields.parquet", index=False)
    pd.DataFrame(potential, columns=["internal_parcel_id", "peer_id", "distance_m", "area_ratio"]).to_parquet(OUT / "potential_peers.parquet", index=False)
    cells.to_parquet(OUT / "weather_grid.parquet", index=False)
    paths = [CONFIG, Path(__file__), ROOT / "run_sentinel1_peer_comparison.py", ROOT / "verify_sentinel1_peer_comparison.py",
             ROOT / "docs/SENTINEL1_PEER_COMPARISON_EN.md", pilot / "manifest.json", pilot / "analysis/complete.json",
             pilot / "selection.parquet", pilot / "analysis/radar_observations.parquet", scope_path, context,
             ROOT / "config/data_collector.json", ROOT / "config/releases.json", ROOT / "config/transitions_area_20260906_v1.review.lock.json",
             OUT / "fields.parquet", OUT / "potential_peers.parquet", OUT / "weather_grid.parquet"]
    summary = {"status": "prepared_waiting_for_weather", "parcels": len(fields), "potential_peer_pairs": len(potential),
               "weather_months": cfg["weather_months"], "analysis_launched": False}
    for p in paths[:5]:
        dest = OUT / "source" / p.relative_to(ROOT)
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(p, dest)
    write(OUT / "prepared.json", {"version": VERSION, "prepared_utc": utc(), "summary": summary,
                                  "sources": {p.relative_to(ROOT).as_posix(): sha(p) for p in paths}})
    return summary


def weather_status(cfg, jobs=JOBS, weather=WEATHER):
    with sqlite3.connect(Path(jobs).resolve().as_uri() + "?mode=ro", uri=True) as db:
        records = {r[0]: r[1:] for r in db.execute("select id,state,output,sha256,rows from jobs where kind='weather'")}
    missing, complete = [], []
    for month in cfg["weather_months"]:
        job = records.get("weather_" + month)
        path = Path(weather) / ("weather_" + month + ".parquet")
        if not job or job[0] != "complete":
            missing.append(month)
            continue
        state, rel, digest, rows = job
        if not path.is_file() or (ROOT / rel).resolve() != path.resolve() or sha(path) != digest:
            raise ValueError("Completed weather file missing or changed: " + month)
        complete.append({"month": month, "path": rel, "sha256": digest, "rows": rows})
    return {"status": "waiting_for_weather" if missing else "ready", "missing_months": missing, "completed": complete}


def deaccumulate(raw):
    """Standard reanalysis-era5-land: 01 UTC resets; 00 UTC ends prior 24 h.

    Hourly tp is missing when a predecessor is absent or a negative increment
    occurs. This deliberately does not clip inconsistent values to zero rain.
    """
    f = raw.sort_values(["latitude", "longitude", "valid_time"]).copy()
    f["valid_time"] = pd.to_datetime(f.valid_time, utc=True)
    if f.duplicated(["latitude", "longitude", "valid_time"]).any():
        raise ValueError("Duplicate weather cell/hour")
    g = f.groupby(["latitude", "longitude"], sort=False)
    delta = g.tp.diff().where(g.valid_time.diff().eq(pd.Timedelta(hours=1)))
    delta = delta.where(f.valid_time.dt.hour.ne(1), f.tp)
    f["rain_mm"] = (1000 * delta).where(np.isfinite(delta) & delta.ge(0))
    return f


def rain_between(series, start, end):
    """Conservative total of every hourly interval touching (start, end].

    Boundary hours are included in full, never fractionally invented. A single
    missing hour invalidates the total. Timestamp denotes interval end in UTC.
    """
    start, end = pd.Timestamp(start), pd.Timestamp(end)
    expected = pd.date_range(start.floor("h") + pd.Timedelta(hours=1), end.ceil("h"), freq="h")
    values = series.reindex(expected)
    if len(expected) == 0 or not np.isfinite(values.to_numpy()).all():
        return np.nan
    return float(values.sum())


def load_weather(status):
    frames = []
    grid = pd.read_parquet(OUT / "weather_grid.parquet")
    for item in status["completed"]:
        f = pd.read_parquet(ROOT / item["path"], columns=["valid_time", "latitude", "longitude", "tp"])
        assert len(f) == item["rows"] and not f.duplicated(["valid_time", "latitude", "longitude"]).any()
        pd.testing.assert_frame_equal(f[["latitude", "longitude"]].drop_duplicates().sort_values(["latitude", "longitude"]).reset_index(drop=True), grid)
        year, month = map(int, item["month"].split("_"))
        expected = pd.date_range(f"{year}-{month:02d}-01", periods=calendar.monthrange(year, month)[1]*24, freq="h", tz="UTC")
        for _, g in f.groupby(["latitude", "longitude"]):
            assert pd.DatetimeIndex(pd.to_datetime(g.valid_time, utc=True).sort_values()).equals(expected), "Incomplete weather month"
        frames.append(f)
    hourly = deaccumulate(pd.concat(frames, ignore_index=True))
    series = {f"era5land_{a:.1f}_{b:.1f}": g.set_index("valid_time").rain_mm
              for (a, b), g in hourly.groupby(["latitude", "longitude"])}
    return hourly, series


def triplets(observations, cfg):
    rows = []
    for (parcel, orbit, platform), g in observations.groupby(["internal_parcel_id", "relative_orbit", "platform"], sort=True):
        g = g.sort_values("datetime").reset_index(drop=True)
        for i in range(1, len(g)-1):
            three = g.iloc[i-1:i+2]
            a, b, c = [three.iloc[k] for k in range(3)]
            times = pd.to_datetime(three.datetime, utc=True, format="ISO8601")
            gaps = times.diff().dt.total_seconds().iloc[1:].to_numpy()/86400
            eo = three[["eo_ndvi", "eo_bsi"]].to_numpy(dtype=float)
            supported = (three.radar_usable.all() and three.platform.nunique() == 1 and
                         np.all((gaps > 0) & (gaps <= cfg["maximum_track_gap_days"])) and
                         np.isfinite(eo).all() and three.eo_date.notna().all() and
                         np.ptp(eo[:, 0]) <= cfg["maximum_ndvi_range"] and np.ptp(eo[:, 1]) <= cfg["maximum_bsi_range"] and
                         cfg["minimum_middle_ndvi"] <= b.eo_ndvi <= cfg["maximum_middle_ndvi"])
            row = {"internal_parcel_id": parcel, "relative_orbit": int(orbit), "platform": b.platform,
                   "first_item": a.source_item, "middle_item": b.source_item, "last_item": c.source_item,
                   "first_datetime": a.datetime, "middle_datetime": b.datetime, "last_datetime": c.datetime,
                   "supported": bool(supported), "before_days": float(gaps[0]), "after_days": float(gaps[1]),
                   "rise_db": float(b.vv_inner_median_db-a.vv_inner_median_db),
                   "fall_db": float(b.vv_inner_median_db-c.vv_inner_median_db),
                   "vh_rise_db": float(b.vh_inner_median_db-a.vh_inner_median_db),
                   "vh_fall_db": float(b.vh_inner_median_db-c.vh_inner_median_db)}
            for k in range(3):
                for name in ("ndvi", "bsi", "ndmi"):
                    row[f"{name}_{k}"] = float(three.iloc[k]["eo_"+name])
                row[f"eo_date_{k}"] = three.iloc[k].eo_date
            rows.append(row)
    return pd.DataFrame(rows)


def match_peers(target, same_acquisition, potential, cfg):
    eligible = potential.loc[potential.internal_parcel_id.eq(target.internal_parcel_id)]
    choices = same_acquisition.loc[same_acquisition.supported & same_acquisition.internal_parcel_id.isin(eligible.peer_id)].copy()
    if choices.empty or not target.supported:
        return choices.iloc[:0]
    score = np.zeros(len(choices))
    keep = np.ones(len(choices), dtype=bool)
    for k in range(3):
        for name in ("ndvi", "bsi"):
            diff = (choices[f"{name}_{k}"] - target[f"{name}_{k}"]).abs().to_numpy()
            limit = cfg[f"maximum_{name}_difference"]
            keep &= diff <= limit
            score += diff / limit
        date_gap = (pd.to_datetime(choices[f"eo_date_{k}"], utc=True) - pd.to_datetime(target[f"eo_date_{k}"], utc=True)).abs().dt.total_seconds()/86400
        keep &= date_gap.le(cfg["maximum_optical_date_difference_days"]).to_numpy()
    choices["match_score"] = score
    choices = choices.loc[keep].merge(eligible[["peer_id", "distance_m"]], left_on="internal_parcel_id", right_on="peer_id", validate="one_to_one")
    return choices.sort_values(["match_score", "distance_m", "internal_parcel_id"]).head(cfg["maximum_peers"])


def compare(observations, fields, potential, rain, cfg):
    t = triplets(observations, cfg)
    t = t.merge(fields[["internal_parcel_id", "cell_id"]], on="internal_parcel_id", validate="many_to_one")
    diagnostics, links = [], []
    for _, g in t.groupby(["relative_orbit", "platform", "first_item", "middle_item", "last_item"], sort=True):
        for _, a in g.iterrows():
            peers = match_peers(a, g, potential, cfg)
            series = rain[a.cell_id]
            before = rain_between(series, a.first_datetime, a.middle_datetime)
            after = rain_between(series, a.middle_datetime, a.last_datetime)
            recent = rain_between(series, pd.Timestamp(a.middle_datetime)-pd.Timedelta(hours=48), a.middle_datetime)
            base = {"internal_parcel_id": a.internal_parcel_id, "relative_orbit": int(a.relative_orbit), "platform": a.platform,
                    "first_item": a.first_item, "middle_item": a.middle_item, "last_item": a.last_item,
                    "middle_datetime": a.middle_datetime, "cell_id": a.cell_id, "supported": bool(a.supported),
                    "peer_count": len(peers), "rain_before_mm": before, "rain_after_mm": after, "rain_recent_48h_mm": recent,
                    "rise_db": a.rise_db, "fall_db": a.fall_db, "fall_db_per_day": a.fall_db/a.after_days,
                    "vh_rise_db": a.vh_rise_db, "vh_fall_db": a.vh_fall_db,
                    "ndmi_change_after": a.ndmi_2-a.ndmi_1,
                    "peer_median_rise_db": float(peers.rise_db.median()) if len(peers) else np.nan,
                    "peer_median_fall_db": float(peers.fall_db.median()) if len(peers) else np.nan}
            enough = bool(a.supported and len(peers) >= cfg["minimum_peers"])
            known_rain = bool(np.isfinite([before, after, recent]).all())
            for _, b in peers.iterrows():
                links.append({"internal_parcel_id": a.internal_parcel_id, "peer_id": b.internal_parcel_id,
                              "middle_item": a.middle_item, "first_item": a.first_item, "last_item": a.last_item,
                              "match_score": b.match_score, "distance_m": b.distance_m})
            for rain_limit in cfg["rain_sensitivity_mm"]:
                low_rain = known_rain and max(before, after, recent) <= rain_limit
                comparable = enough and low_rain
                for db in cfg["excursion_sensitivity_db"]:
                    event = bool(a.rise_db >= db and a.fall_db >= db)
                    peer_rate = float((peers.rise_db.ge(db) & peers.fall_db.ge(db)).mean()) if comparable else np.nan
                    relative = (event and a.rise_db-base["peer_median_rise_db"] >= cfg["minimum_peer_contrast_db"]
                                and a.fall_db-base["peer_median_fall_db"] >= cfg["minimum_peer_contrast_db"])
                    diagnostics.append({**base, "rain_limit_mm": rain_limit, "excursion_db": db,
                                        "comparable": comparable, "weather_complete": known_rain,
                                        "observed_excursion": event if comparable else None,
                                        "relative_excursion": bool(relative) if comparable else None,
                                        "peer_excursion_fraction": peer_rate})
    diag = pd.DataFrame(diagnostics)
    rows = []
    keys = ["internal_parcel_id", "relative_orbit", "platform", "rain_limit_mm", "excursion_db"]
    for key, g in diag.groupby(keys, sort=True):
        usable = g.loc[g.comparable]
        n = len(usable)
        count = int(usable.observed_excursion.sum()) if n else None
        relative = int(usable.relative_excursion.sum()) if n else None
        own_rate = count/n if n else np.nan
        peer_rate = float(usable.peer_excursion_fraction.mean()) if n else np.nan
        enough = n >= cfg["minimum_comparable_triplets"]
        rows.append({**dict(zip(keys, key)), "all_triplets": len(g), "comparable_triplets": n,
                     "observed_excursions": count, "relative_excursions": relative,
                     "observed_excursion_fraction": own_rate, "matched_peer_fraction": peer_rate,
                     "excess_fraction": own_rate-peer_rate,
                     "repeated_relative_signal": bool(relative >= cfg["minimum_relative_excursions"] and own_rate-peer_rate >= cfg["minimum_excess_rate"]) if enough else None})
    metrics = pd.DataFrame(rows)
    for name in ("observed_excursion", "relative_excursion"):
        diag[name] = pd.array(diag[name], dtype="boolean")
    metrics["repeated_relative_signal"] = pd.array(metrics.repeated_relative_signal, dtype="boolean")
    result = fields.copy()
    exposure = diag.loc[diag.comparable].drop_duplicates(["internal_parcel_id", "middle_item"])
    result["comparable_middle_acquisitions_any_sensitivity"] = result.internal_parcel_id.map(exposure.groupby("internal_parcel_id").size()).fillna(0).astype(int)
    result["assessment_state"] = np.where(result.sample_group.eq("boundary_challenge"), "insufficient_spatial_support",
                                          np.where(result.comparable_middle_acquisitions_any_sensitivity.gt(0), "relative_radar_diagnostics_only", "insufficient_comparable_observations"))
    result["high_water_demand_candidate"] = pd.array([None]*len(result), dtype="boolean")
    return diag, pd.DataFrame(links), metrics, result


def verify_result():
    verify_prepared()
    marker = read(OUT / "complete.json")
    for name, digest in marker["files"].items():
        assert sha(OUT / name) == digest, name
    for item in marker["weather_sources"]:
        assert sha(ROOT / item["path"]) == item["sha256"]
    return read(OUT / "report.json")


def run_if_ready():
    verify_prepared()
    if (OUT / "complete.json").exists():
        return verify_result()
    cfg = read(CONFIG)
    status = weather_status(cfg)
    if status["status"] != "ready":
        return status
    hourly, rain = load_weather(status)
    observations = pd.read_parquet(ROOT / cfg["pilot"] / "analysis/radar_observations.parquet")
    fields = pd.read_parquet(OUT / "fields.parquet")
    potential = pd.read_parquet(OUT / "potential_peers.parquet")
    outputs = compare(observations, fields, potential, rain, cfg)
    # A new attempt directory preserves any interrupted prior attempt.
    attempt = OUT / "attempts" / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    attempt.mkdir(parents=True, exist_ok=False)
    names = ["comparisons.parquet", "matched_peers.parquet", "metrics.parquet", "parcels.parquet"]
    for name, frame in zip(names, outputs):
        frame.to_parquet(attempt / name, index=False)
    diag, links, metrics, parcels = outputs
    report = {"version": VERSION, "status": "comparison_complete_private_diagnostics", "completed_utc": utc(),
              "parcels": len(parcels), "matched_peer_links": len(links),
              "parcels_with_comparisons": int(parcels.comparable_middle_acquisitions_any_sensitivity.gt(0).sum()),
              "repeated_relative_signal_rows": int(metrics.repeated_relative_signal.sum()),
              "interpretation": "Signal fractions per comparable triplet, separately by orbit/platform and sensitivity; not irrigation event counts",
              "high_water_demand_parcels": None, "confirmed_irrigation_events": None,
              "rapid_root_zone_drying_measured": False, "normative_volume_estimated": False,
              "classification_inputs_used": False, "coarse_250m_data_used": False, "map_changed": False,
              "limitations": ["Compact observability sample; no community prevalence estimate", "Peers are not verified normal-water controls",
                              "ERA5-Land is coarse reanalysis and cannot exclude local showers", "Twelve-day same-platform intervals miss irrigation and fast drying",
                              "Canopy, soil roughness and management differences remain possible causes", "Thresholds exploratory; no independent ground truth"]}
    # Promote only internal files after all calculations succeed. Existing partial files stay in attempts.
    write(attempt / "report.json", report)
    names.append("report.json")
    for name in names:
        if (OUT / name).exists():
            raise ValueError("Unsealed comparison output already exists; preserve and inspect")
        shutil.copy2(attempt / name, OUT / name)
    write(OUT / "complete.json", {"files": {name: sha(OUT / name) for name in names}, "weather_sources": status["completed"]})
    return verify_result()


def main(action):
    with lock():
        if action == "prepare":
            result = prepare()
        elif action == "verify":
            result = verify_result() if (OUT / "complete.json").exists() else verify_prepared()["summary"]
        elif action == "status":
            verify_prepared()
            result = verify_result() if (OUT / "complete.json").exists() else weather_status(read(CONFIG))
        else:
            result = run_if_ready()
    print(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False))
