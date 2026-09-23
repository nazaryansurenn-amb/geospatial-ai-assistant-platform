"""Versioned multi-year orchestration of the frozen 96-parcel radar comparison.

Only temporal coverage changes. No classification, weather or radar thresholds
are changed. Completed years and evolving weather snapshots remain immutable.
"""
from __future__ import annotations
import hashlib
import json
import os
import shutil
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import contextmanager
from pathlib import Path
from urllib.request import urlopen

import geopandas as gpd
import numpy as np
import pandas as pd
import rasterio
from rasterio.features import geometry_mask
from rasterio.windows import from_bounds
from shapely.geometry import mapping

from wp_core import sentinel1_peer_comparison as peer
from wp_core import sentinel1_pilot as pilot

ROOT = Path(__file__).resolve().parents[1]
VERSION = "sentinel1_peer_2021_2025_20260906_v1"
CONFIG = ROOT / "config" / (VERSION + ".json")
DATA = ROOT / "data/observations/sentinel1_pilot_2021_2025_20260906_v1"
OUT = ROOT / "data/analysis/rapid_water_loss" / VERSION
OLD = ROOT / "data/observations/sentinel1_pilot_20260906_v1"


@contextmanager
def lock():
    import msvcrt
    DATA.mkdir(parents=True, exist_ok=True)
    with (DATA / "run.lock").open("a+b") as f:
        if f.tell() == 0:
            f.write(b"0")
            f.flush()
        f.seek(0)
        try:
            msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            raise RuntimeError("Another multi-year pilot worker owns this lock") from None
        try:
            yield
        finally:
            f.seek(0)
            msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)


def pinned():
    manifest = peer.read(DATA / "manifest.json")
    for name, digest in manifest["sources"].items():
        assert peer.sha(ROOT / name) == digest, "Multi-year source changed: " + name
    return manifest


def prepare():
    if (DATA / "manifest.json").exists():
        return pinned()["summary"]
    pilot.verify()
    peer.verify_prepared()
    cfg = peer.read(CONFIG)
    old = peer.read(OLD / "scene_manifest.json")
    windows = old["windows"]
    items = list(old["items"])
    sources = [CONFIG, Path(__file__), ROOT / "run_sentinel1_multiyear.py", ROOT / "verify_sentinel1_multiyear.py",
               ROOT / "docs/SENTINEL1_MULTIYEAR_EN.md", peer.CONFIG, ROOT / "wp_core/sentinel1_peer_comparison.py",
               ROOT / "wp_core/sentinel1_pilot.py", peer.OUT / "prepared.json", OLD / "manifest.json",
               OLD / "scene_manifest.json", OLD / "selection.parquet", OLD / "analysis/radar_observations.parquet"]
    for year in cfg["years"]:
        sources.append(ROOT / f"data/analysis/observation_screening/transitions_area_20260906_v1/daily/{year}.parquet")
        if year != 2025:
            source = ROOT / cfg["catalogue_directory"] / f"{year}.json"
            catalogue = peer.read(source)
            assert not any(link["rel"] == "next" for link in catalogue.get("links", []))
            items += [x for x in catalogue["features"] if x["properties"]["sat:relative_orbit"] in cfg["relative_orbits"]]
            sources.append(source)
    seen = set()
    for item in items:
        p = item["properties"]
        t, bbox = p["proj:transform"], p["proj:bbox"]
        key = (p["platform"].lower(), p["sat:relative_orbit"], p["datetime"][:10])
        assert key not in seen
        seen.add(key)
        assert int(p["datetime"][:4]) in cfg["years"] and int(p["datetime"][5:7]) in cfg["season_months"]
        assert p["proj:epsg"] == 32638 and t[0] == 10 and t[4] == -10 and t[2] % 10 == t[5] % 10 == 0
        assert all(bbox[0] <= w["bounds"][0] and bbox[1] <= w["bounds"][1] and bbox[2] >= w["bounds"][2] and bbox[3] >= w["bounds"][3] for w in windows)
        for band in ("vv", "vh"):
            href = item["assets"][band]["href"]
            assert href.startswith("https://sentinel1euwestrtc.blob.core.windows.net/sentinel1-grd-rtc/") and "?" not in href
    items.sort(key=lambda x: (x["properties"]["datetime"], x["id"]))
    peer.write(DATA / "scenes.json", {"items": items, "windows": windows})
    sources.append(DATA / "scenes.json")
    summary = {"parcels": 96, "acquisitions_by_year": {str(y): sum(x["properties"]["datetime"].startswith(str(y)) for x in items) for y in cfg["years"]},
               "new_acquisitions": sum(not x["properties"]["datetime"].startswith("2025") for x in items), "reused_2025_acquisitions": len(old["items"])}
    for path in sources[:5]:
        dest = DATA / "source" / path.relative_to(ROOT)
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, dest)
    peer.write(DATA / "manifest.json", {"version": VERSION, "prepared_utc": peer.utc(), "summary": summary,
               "sources": {p.relative_to(ROOT).as_posix(): peer.sha(p) for p in sources}})
    return summary


def clip_path(item, window):
    folder = OLD if item["properties"]["datetime"].startswith("2025") else DATA
    return folder / "clips" / item["id"] / (window["id"] + ".tif")


def checkpoint(item):
    if item["properties"]["datetime"].startswith("2025"):
        return pilot.checked_job(item["id"])
    marker = DATA / "checkpoints" / (item["id"] + ".json")
    if not marker.exists():
        return None
    saved = peer.read(marker)
    for name, record in saved["files"].items():
        path = DATA / name
        assert path.is_file() and path.stat().st_size == record["bytes"] and peer.sha(path) == record["sha256"]
    return saved


def download(item, windows, token):
    if checkpoint(item):
        return
    for attempt in range(3):
        try:
            files = {}
            with rasterio.Env(GDAL_DISABLE_READDIR_ON_OPEN="EMPTY_DIR", CPL_VSIL_CURL_ALLOWED_EXTENSIONS=".tiff",
                              GDAL_HTTP_MAX_RETRY=2, GDAL_HTTP_RETRY_DELAY=1, GDAL_HTTP_TIMEOUT=45, GDAL_NUM_THREADS="1"):
                with rasterio.open(item["assets"]["vv"]["href"] + "?" + token) as vv, rasterio.open(item["assets"]["vh"]["href"] + "?" + token) as vh:
                    assert vv.crs == vh.crs and vv.transform == vh.transform and vv.crs.to_epsg() == 32638
                    for window in windows:
                        win = from_bounds(*window["bounds"], transform=vv.transform).round_offsets().round_lengths()
                        assert win.col_off >= 0 and win.row_off >= 0 and win.col_off+win.width <= vv.width and win.row_off+win.height <= vv.height
                        array = np.stack([vv.read(1, window=win), vh.read(1, window=win)])
                        assert np.isfinite(array).any() and (array > 0).any()
                        path = clip_path(item, window)
                        path.parent.mkdir(parents=True, exist_ok=True)
                        temp = path.with_suffix(".partial.tif")
                        with rasterio.open(temp, "w", driver="GTiff", dtype="float32", count=2,
                                           height=array.shape[1], width=array.shape[2], crs=vv.crs, transform=vv.window_transform(win),
                                           nodata=-32768, compress="deflate", tiled=True, blockxsize=256, blockysize=256) as target:
                            target.write(array)
                            target.set_band_description(1, "VV_gamma0_linear_power")
                            target.set_band_description(2, "VH_gamma0_linear_power")
                            target.update_tags(source_item=item["id"], observation_datetime=item["properties"]["datetime"], units="linear_gamma0")
                        if path.exists():
                            assert peer.sha(path) == peer.sha(temp), "Uncheckpointed clip differs; preserve original"
                            temp.unlink()
                        else:
                            os.replace(temp, path)
                        files[path.relative_to(DATA).as_posix()] = {"bytes": path.stat().st_size, "sha256": peer.sha(path)}
            peer.write(DATA / "checkpoints" / (item["id"] + ".json"), {"source_item": item["id"], "completed_utc": peer.utc(), "files": files})
            return
        except Exception:
            if attempt == 2:
                raise RuntimeError("Radar subset failed after retries: " + item["id"]) from None
            time.sleep(attempt+1)


def collect():
    pinned()
    scenes = peer.read(DATA / "scenes.json")
    pending = [x for x in scenes["items"] if not checkpoint(x)]
    total, completed = len(scenes["items"]), len(scenes["items"])-len(pending)
    if pending:
        with urlopen("https://planetarycomputer.microsoft.com/api/sas/v1/token/sentinel1euwestrtc/sentinel1-grd-rtc", timeout=30) as response:
            token = json.load(response)["token"]
        with ThreadPoolExecutor(max_workers=peer.read(CONFIG)["download_workers"]) as pool:
            futures = [pool.submit(download, x, scenes["windows"], token) for x in pending]
            for future in as_completed(futures):
                future.result()
                completed += 1
                value = {"phase": "radar_collection", "completed": completed, "total": total, "updated_utc": peer.utc()}
                peer.write(DATA / "progress.json", value)
                if completed % 10 == 0 or completed == total:
                    print(json.dumps(value), flush=True)
    if not (DATA / "collection_complete.json").exists():
        peer.write(DATA / "collection_complete.json", {"completed_utc": peer.utc(), "acquisitions": total,
                   "new_clips": len(list((DATA / "clips").glob("*/*.tif"))), "new_clip_bytes": sum(x.stat().st_size for x in (DATA / "clips").glob("*/*.tif")),
                   "reused_2025_clips": 228})
    return peer.read(DATA / "collection_complete.json")


def join_optical(observations, eo):
    """Exact original nearest usable observation rule; no interpolation or classes."""
    result = observations.copy()
    eo = eo.loc[eo.support.ge(.6) & eo.finite.astype(bool)].copy()
    eo["date"] = pd.to_datetime(eo.observation_date, utc=True)
    eo = eo.sort_values("date")
    dates = pd.to_datetime(result.datetime, utc=True, format="ISO8601")
    result["eo_date"] = None
    for name in ("ndvi", "ndmi", "bsi"):
        result["eo_"+name] = np.nan
    for parcel, indices in result.groupby("internal_parcel_id").groups.items():
        e = eo.loc[eo.internal_parcel_id.eq(parcel)]
        if e.empty:
            continue
        for i in indices:
            delta = (e.date-dates.iloc[i]).abs()
            j = delta.idxmin()
            if delta.loc[j] <= pd.Timedelta(days=3):
                result.at[i, "eo_date"] = str(e.loc[j, "observation_date"])
                for name in ("ndvi", "ndmi", "bsi"):
                    result.at[i, "eo_"+name] = float(e.loc[j, name+"_median"])
    return result


def extract_year(year, scenes, fields, radar_cfg):
    folder = DATA / "tables"
    folder.mkdir(exist_ok=True)
    marker = folder / f"{year}.json"
    if marker.exists():
        record = peer.read(marker)
        assert peer.sha(ROOT / record["path"]) == record["sha256"]
        return record
    if year == 2025:
        path = OLD / "analysis/radar_observations.parquet"
        frame = pd.read_parquet(path)
    else:
        rows = []
        for item in scenes["items"]:
            if not item["properties"]["datetime"].startswith(str(year)):
                continue
            assert checkpoint(item)
            props = item["properties"]
            for window in scenes["windows"]:
                with rasterio.open(clip_path(item, window)) as src:
                    arrays = src.read()
                    for parcel in fields.loc[fields.window_id.eq(window["id"])].itertuples():
                        row = {"internal_parcel_id": parcel.internal_parcel_id, "cadastre_code": parcel.cadastre_code,
                               "area_official_m2": parcel.area_official_m2, "community_hy": parcel.review_community_hy,
                               "sample_group": parcel.sample_group, "source_item": item["id"], "datetime": props["datetime"],
                               "platform": props["platform"].lower(), "relative_orbit": props["sat:relative_orbit"], "orbit_direction": props["sat:orbit_state"]}
                        for name, geom in (("full", parcel.geometry), ("inner", parcel.geometry.buffer(-radar_cfg["interior_buffer_m"]))):
                            mask = np.zeros(arrays.shape[1:], dtype=bool) if geom.is_empty else geometry_mask([mapping(geom)], out_shape=arrays.shape[1:], transform=src.transform, invert=True, all_touched=False)
                            for band, array in zip(("vv", "vh"), arrays):
                                row.update({f"{band}_{name}_{k}": v for k, v in pilot.power_summary(array, mask).items()})
                        row["radar_usable"] = bool(row["vv_inner_valid_fraction"] >= radar_cfg["minimum_valid_fraction"] and row["vh_inner_valid_fraction"] >= radar_cfg["minimum_valid_fraction"] and row["vv_inner_valid_pixel_count"]*100/radar_cfg["nominal_resolution_area_m2"] >= radar_cfg["minimum_nominal_resolution_elements"])
                        rows.append(row)
        frame = pd.DataFrame(rows).sort_values(["internal_parcel_id", "datetime", "relative_orbit"]).reset_index(drop=True)
        columns = ["internal_parcel_id", "observation_date", "ndvi_median", "ndmi_median", "bsi_median", "support", "finite"]
        eo = pd.read_parquet(ROOT / f"data/analysis/observation_screening/transitions_area_20260906_v1/daily/{year}.parquet", columns=columns, filters=[("internal_parcel_id", "in", fields.internal_parcel_id.tolist())])
        frame = join_optical(frame, eo)
        path = folder / f"{year}.parquet"
        assert not path.exists(), "Unsealed table preserved for inspection"
        frame.to_parquet(path, index=False)
    record = {"year": year, "path": path.relative_to(ROOT).as_posix(), "sha256": peer.sha(path), "rows": len(frame),
              "acquisitions": int(frame.source_item.nunique()), "usable_radar_rows": int(frame.radar_usable.sum()), "optical_context_rows": int(frame.eo_date.notna().sum())}
    peer.write(marker, record)
    print(json.dumps({"phase": "extracted", **record}), flush=True)
    return record


def extract():
    pinned()
    assert (DATA / "collection_complete.json").exists()
    scenes = peer.read(DATA / "scenes.json")
    fields = gpd.read_parquet(OLD / "selection.parquet")
    cfg = peer.read(pilot.CONFIG)
    result = [extract_year(year, scenes, fields, cfg) for year in peer.read(CONFIG)["years"]]
    if not (DATA / "tables_complete.json").exists():
        peer.write(DATA / "tables_complete.json", {"years": result})
    return result


def year_key(year, weather):
    payload = {"year": year, "weather": [(x["month"], x["sha256"]) for x in weather]}
    return str(year) + "_" + hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()[:16]


def verify_year(folder):
    complete = peer.read(folder / "complete.json")
    for name, digest in complete["files"].items():
        assert peer.sha(folder / name) == digest
    for record in complete["weather_sources"]:
        assert peer.sha(ROOT / record["path"]) == record["sha256"]
    return peer.read(folder / "report.json")


def analyze_year(year, weather, cfg):
    key = year_key(year, weather)
    folder = OUT / "years" / key
    if (folder / "complete.json").exists():
        return key, verify_year(folder)
    record = peer.read(DATA / "tables" / f"{year}.json")
    assert peer.sha(ROOT / record["path"]) == record["sha256"]
    observations = pd.read_parquet(ROOT / record["path"])
    months = [x["month"] for x in weather]
    dates = pd.to_datetime(observations.datetime, utc=True, format="ISO8601")
    selected = observations.loc[dates.dt.strftime("%Y_%m").isin(months)].copy().reset_index(drop=True)
    hourly, rain = peer.load_weather({"completed": weather})
    fields = pd.read_parquet(peer.OUT / "fields.parquet")
    potential = pd.read_parquet(peer.OUT / "potential_peers.parquet")
    diag, links, metrics, parcels = peer.compare(selected, fields, potential, rain, cfg)
    one = diag.drop_duplicates(["internal_parcel_id", "middle_item"])
    matched = one.loc[one.supported & one.peer_count.ge(cfg["minimum_peers"])]
    parcels["year"] = year
    parcels["matched_triplets_before_rain_screen"] = parcels.internal_parcel_id.map(matched.groupby("internal_parcel_id").size()).fillna(0).astype(int)
    parcels["repetition_assessable"] = parcels.internal_parcel_id.isin(metrics.loc[metrics.repeated_relative_signal.notna(), "internal_parcel_id"])
    parcels["positive_signal_any_sensitivity"] = pd.array([bool(metrics.loc[metrics.internal_parcel_id.eq(pid), "repeated_relative_signal"].fillna(False).any()) if pid in set(parcels.loc[parcels.repetition_assessable, "internal_parcel_id"]) else None for pid in parcels.internal_parcel_id], dtype="boolean")
    full = all(f"{year}_{m:02d}" in months for m in range(3, 10))
    parcels["weather_season_complete"] = full
    report = {"year": year, "key": key, "status": "complete_season_inputs" if full else "partial_weather_coverage",
              "weather_months_used": months, "weather_months_missing": [f"{year}_{m:02d}" for m in range(3,10) if f"{year}_{m:02d}" not in months],
              "radar_acquisitions_cached": record["acquisitions"], "radar_acquisitions_analyzed": int(selected.source_item.nunique()),
              "radar_rows": len(selected), "triplets": len(one), "structurally_supported_triplets": int(one.supported.sum()),
              "parcels_with_matched_triplets": int(parcels.matched_triplets_before_rain_screen.gt(0).sum()),
              "matched_triplets": len(matched), "matched_triplets_with_complete_rainfall": int(matched.weather_complete.sum()),
              "parcels_with_low_rain_comparisons": int(parcels.comparable_middle_acquisitions_any_sensitivity.gt(0).sum()),
              "parcels_with_assessable_repetition": int(parcels.repetition_assessable.sum()),
              "parcels_with_positive_signal_any_sensitivity": int(parcels.positive_signal_any_sensitivity.sum()),
              "max_comparable_triplets_per_track_platform_sensitivity": int(metrics.comparable_triplets.max()),
              "high_water_demand_parcels": None, "confirmed_irrigation_events": None, "map_changed": False}
    folder.mkdir(parents=True, exist_ok=True)
    outputs = {"radar_input.parquet": selected, "comparisons.parquet": diag, "matched_peers.parquet": links, "metrics.parquet": metrics, "parcels.parquet": parcels}
    attempt = folder / "attempts" / str(time.time_ns())
    attempt.mkdir(parents=True, exist_ok=False)
    for name, frame in outputs.items():
        frame.to_parquet(attempt / name, index=False)
    peer.write(attempt / "report.json", report)
    names = list(outputs)+["report.json"]
    for name in names:
        assert not (folder / name).exists(), "Unsealed result preserved for inspection"
        shutil.copy2(attempt / name, folder / name)
    peer.write(folder / "complete.json", {"files": {n: peer.sha(folder/n) for n in names}, "weather_sources": weather,
               "radar_table_sha256": record["sha256"], "completed_utc": peer.utc()})
    print(json.dumps({"phase": "year_analyzed", **report}), flush=True)
    return key, report


def analyze():
    pinned()
    cfg = peer.read(peer.CONFIG)
    all_months = [f"{y}_{m:02d}" for y in range(2021,2026) for m in range(3,10)]
    weather = peer.weather_status({"weather_months": all_months})
    groups = {year: [x for x in weather["completed"] if x["month"].startswith(str(year))] for year in range(2021,2026)}
    # Freeze readiness once per invocation; later arrivals create a distinct snapshot.
    keys = [year_key(y, groups[y]) for y in range(2021,2026)]
    snapshot_key = hashlib.sha256(json.dumps(keys).encode()).hexdigest()[:20]
    summary_dir = OUT / "snapshots" / snapshot_key
    if (summary_dir / "complete.json").exists():
        return verify_snapshot(snapshot_key)
    years = [analyze_year(y, groups[y], cfg) for y in range(2021,2026)]
    parcel_years = pd.concat([pd.read_parquet(OUT / "years" / key / "parcels.parquet") for key, _ in years], ignore_index=True)
    fields = pd.read_parquet(peer.OUT / "fields.parquet")
    result = fields.copy()
    result["years_with_assessable_repetition"] = result.internal_parcel_id.map(parcel_years.groupby("internal_parcel_id").repetition_assessable.sum()).astype(int)
    result["years_with_positive_signal_any_sensitivity"] = result.internal_parcel_id.map(parcel_years.groupby("internal_parcel_id").positive_signal_any_sensitivity.sum()).astype(int)
    result["high_water_demand_candidate"] = pd.array([None]*len(result), dtype="boolean")
    report = {"version": VERSION, "snapshot_key": snapshot_key, "completed_utc": peer.utc(), "parcels": len(result), "parcel_seasons": len(parcel_years),
              "years": [r for _, r in years], "all_weather_seasons_complete": not weather["missing_months"],
              "parcels_assessable_in_any_year": int(result.years_with_assessable_repetition.gt(0).sum()),
              "parcels_with_repeated_relative_signal_in_two_or_more_years": int(result.years_with_positive_signal_any_sensitivity.ge(2).sum()),
              "high_water_demand_parcels": None, "classification_changed": False, "map_changed": False,
              "limits": ["Same compact 96-parcel sample; not a prevalence estimate", "Zero supported signals does not make unresolved parcels normal",
                         "Sensitivity settings overlap and are not independent confirmations", "Twelve-day same-platform revisit misses frequent irrigation and rapid drying",
                         "ERA5-Land is coarse reanalysis; canopy, local rain and management remain competing explanations", "No irrigation volume, root-zone loss or normative excess measured"]}
    summary_dir.mkdir(parents=True, exist_ok=True)
    parcel_years.to_parquet(summary_dir / "parcel_years.parquet", index=False)
    result.to_parquet(summary_dir / "parcels.parquet", index=False)
    peer.write(summary_dir / "report.json", report)
    peer.write(summary_dir / "complete.json", {"year_keys": keys, "files": {n: peer.sha(summary_dir/n) for n in ("parcel_years.parquet", "parcels.parquet", "report.json")}})
    peer.write(OUT / "latest.json", {"snapshot_key": snapshot_key})
    return report


def verify_snapshot(key=None):
    pinned()
    key = key or peer.read(OUT / "latest.json")["snapshot_key"]
    folder = OUT / "snapshots" / key
    marker = peer.read(folder / "complete.json")
    for name, digest in marker["files"].items():
        assert peer.sha(folder / name) == digest
    for year in marker["year_keys"]:
        verify_year(OUT / "years" / year)
    return peer.read(folder / "report.json")


def main(action):
    with lock():
        result = {"prepare": prepare, "collect": collect, "extract": extract, "analyze": analyze, "verify": verify_snapshot}[action]()
    print(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False), flush=True)
