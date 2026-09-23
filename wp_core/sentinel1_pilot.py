"""Independent, private Sentinel-1 observability pilot; no irrigation-volume classifier."""
from __future__ import annotations
import json
import math
import os
import shutil
import time
from contextlib import contextmanager
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from urllib.request import urlopen

import geopandas as gpd
import numpy as np
import pandas as pd
import rasterio
from rasterio.features import geometry_mask
from rasterio.windows import from_bounds
from shapely.geometry import box, mapping, shape
from release_tools import sha256

ROOT = Path(__file__).resolve().parents[1]
VERSION = "sentinel1_pilot_20260906_v1"
OUT = ROOT / "data/observations" / VERSION
ANALYSIS = OUT / "analysis"
CONFIG = ROOT / "config" / f"{VERSION}.json"
SCOPE = ROOT / "data/observations/observations_2021_2025_v1/scope.parquet"
GEOM = ROOT / "data/source/cadastre/parcels_wua.gpkg"
SPATIAL = ROOT / "data/analysis/observation_screening/transitions_area_20260906_v1/spatial.parquet"
READINESS = ROOT / "data/analysis/rapid_water_loss/rapid_water_loss_20260906_v1/parcels.parquet"


def read(p):
    return json.loads(Path(p).read_text(encoding="utf-8"))


def write(p, value):
    p = Path(p)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, p)


def utc():
    return datetime.now(timezone.utc).isoformat()


@contextmanager
def run_lock():
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
            raise RuntimeError("A Sentinel-1 pilot worker already holds the run lock") from None
        try:
            yield
        finally:
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)


def check_sources():
    m = read(OUT / "manifest.json")
    for relative, digest in m["sources"].items():
        if sha256(ROOT / relative) != digest:
            raise ValueError("Pinned pilot source changed: " + relative)
    return m


def prepare():
    if (OUT / "manifest.json").exists():
        return check_sources()["summary"]
    cfg = read(CONFIG)
    scope = pd.read_parquet(SCOPE)
    spatial = pd.read_parquet(SPATIAL)
    context = pd.read_parquet(READINESS)
    geo = gpd.read_file(GEOM, layer="parcels", columns=["cadastre_code", "area_official_m2"])
    geo = geo.merge(scope.loc[scope.included, ["internal_parcel_id", "cadastre_code", "area_official_m2"]],
                    on="cadastre_code", validate="one_to_one", suffixes=("_source", ""))
    assert len(geo) == 22802 and geo.area_official_m2.eq(geo.area_official_m2_source).all()
    geo = geo.to_crs(32638).merge(spatial[["internal_parcel_id", "minimum_width_m"]], on="internal_parcel_id", validate="one_to_one")
    geo = geo.merge(context[["internal_parcel_id", "review_community_hy"]], on="internal_parcel_id", validate="one_to_one")
    geo["interior_area_m2"] = geo.geometry.buffer(-cfg["interior_buffer_m"]).area
    clean = ((geo.interior_area_m2 / cfg["nominal_resolution_area_m2"] >= cfg["minimum_nominal_resolution_elements"])
             & geo.minimum_width_m.ge(cfg["minimum_width_m"]))
    geo["sample_group"] = np.where(clean, "clean_interior", "boundary_challenge")
    selected, windows = [], []
    for index, community in enumerate(cfg["communities"]):
        subset = geo.loc[geo.review_community_hy.eq(community["name"])].copy()
        centers = subset.geometry.centroid
        anchor = (float(centers.x.median()), float(centers.y.median()))
        subset["selection_distance_m"] = np.hypot(centers.x-anchor[0], centers.y-anchor[1])
        pieces = []
        for group, count in [("clean_interior", community["clean"]), ("boundary_challenge", community["challenge"])]:
            part = subset.loc[subset.sample_group.eq(group)].sort_values(["selection_distance_m", "internal_parcel_id"]).head(count)
            if len(part) != count:
                raise ValueError("Insufficient pilot parcels in " + community["name"])
            pieces.append(part)
        chosen = pd.concat(pieces).copy()
        chosen["window_id"] = f"community_{index+1}"
        chosen["priority_review"] = community["priority"]
        selected.append(chosen)
        bounds = chosen.total_bounds
        bounds = [math.floor((bounds[0]-30)/10)*10, math.floor((bounds[1]-30)/10)*10,
                  math.ceil((bounds[2]+30)/10)*10, math.ceil((bounds[3]+30)/10)*10]
        windows.append({"id": f"community_{index+1}", "bounds": bounds, "crs": "EPSG:32638", "name_hy": community["name"]})
    selection = gpd.GeoDataFrame(pd.concat(selected, ignore_index=True), geometry="geometry", crs=geo.crs)
    assert len(selection) == 96 and selection.internal_parcel_id.is_unique
    selection = selection.drop(columns="area_official_m2_source").sort_values("internal_parcel_id").reset_index(drop=True)
    selection.to_parquet(OUT / "selection.parquet", index=False)
    catalog = read(ROOT / cfg["catalogue"])
    items = [f for f in catalog["features"] if f["properties"]["sat:relative_orbit"] in cfg["relative_orbits"]]
    keys = set()
    for item in items:
        props = item["properties"]
        key = (props["platform"].lower(), props["sat:relative_orbit"], props["datetime"][:10])
        assert key not in keys
        keys.add(key)
        t = props["proj:transform"]
        assert props["proj:epsg"] == 32638 and t[0] == 10 and t[4] == -10 and t[2] % 10 == 0 and t[5] % 10 == 0
        assert cfg["period"][0] <= props["datetime"][:10] <= cfg["period"][1]
        for band in ("vv", "vh"):
            assert item["assets"][band]["href"].startswith("https://sentinel1euwestrtc.blob.core.windows.net/sentinel1-grd-rtc/")
            assert "?" not in item["assets"][band]["href"]
    items.sort(key=lambda x: (x["properties"]["datetime"], x["id"]))
    assert len(items) <= 60
    expected = read(ROOT / cfg["copernicus_catalogue"])["features"]
    expected_keys = {(f["properties"]["platform"].lower(), f["properties"]["sat:relative_orbit"], f["properties"]["datetime"][:10])
                     for f in expected if f["properties"]["sat:relative_orbit"] in cfg["relative_orbits"]
                     and cfg["period"][0] <= f["properties"]["datetime"][:10] <= cfg["period"][1]}
    write(OUT / "scene_manifest.json", {"items": items, "windows": windows, "missing_from_rtc_catalogue": sorted(expected_keys-keys)})
    paths = [CONFIG, Path(__file__), ROOT / "run_sentinel1_pilot.py", SCOPE, GEOM, SPATIAL, READINESS,
             ROOT / cfg["catalogue"], ROOT / cfg["copernicus_catalogue"], OUT / "selection.parquet", OUT / "scene_manifest.json"]
    sources = {p.relative_to(ROOT).as_posix(): sha256(p) for p in paths}
    for path in [CONFIG, Path(__file__), ROOT / "run_sentinel1_pilot.py"]:
        dest = OUT / "source" / path.relative_to(ROOT)
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, dest)
    pixels = sum(((w["bounds"][2]-w["bounds"][0])/10)*((w["bounds"][3]-w["bounds"][1])/10) for w in windows)
    summary = {"parcels": len(selection), "clean_interior": int(selection.sample_group.eq("clean_interior").sum()),
               "challenge": int(selection.sample_group.eq("boundary_challenge").sum()), "acquisitions": len(items),
               "raster_clips": len(items)*len(windows), "uncompressed_mb": round(pixels*2*4*len(items)/1e6, 1),
               "catalogue_missing_acquisitions": len(expected_keys-keys)}
    write(OUT / "manifest.json", {"version": VERSION, "created_utc": utc(), "sources": sources, "summary": summary,
                                  "interpretation": "private_observability_pilot_not_confirmed_irrigation_or_soil_loss"})
    return summary


def checked_job(item_id):
    marker = OUT / "checkpoints" / f"{item_id}.json"
    if not marker.exists():
        return None
    record = read(marker)
    for rel, info in record["files"].items():
        p = OUT / rel
        if not p.is_file() or p.stat().st_size != info["bytes"] or sha256(p) != info["sha256"]:
            raise ValueError("Completed radar clip changed: " + rel)
    return record


def download_scene(item, windows, token):
    if checked_job(item["id"]):
        return "reused"
    for attempt in range(3):
        try:
            files = {}
            with rasterio.Env(GDAL_DISABLE_READDIR_ON_OPEN="EMPTY_DIR", CPL_VSIL_CURL_ALLOWED_EXTENSIONS=".tiff",
                              GDAL_HTTP_MAX_RETRY=2, GDAL_HTTP_RETRY_DELAY=1, GDAL_HTTP_TIMEOUT=45, GDAL_NUM_THREADS="1"):
                with rasterio.open(item["assets"]["vv"]["href"]+"?"+token) as vv, rasterio.open(item["assets"]["vh"]["href"]+"?"+token) as vh:
                    assert vv.crs == vh.crs and vv.transform == vh.transform and vv.crs.to_epsg() == 32638
                    for group in windows:
                        win = from_bounds(*group["bounds"], transform=vv.transform).round_offsets().round_lengths()
                        assert win.col_off >= 0 and win.row_off >= 0 and win.col_off+win.width <= vv.width and win.row_off+win.height <= vv.height
                        a = np.stack([vv.read(1, window=win), vh.read(1, window=win)])
                        assert a.shape == (2, int(win.height), int(win.width))
                        if not np.isfinite(a).any() or not (a > 0).any():
                            raise ValueError("Radar subset has no valid power measurements")
                        dest = OUT / "clips" / item["id"] / f"{group['id']}.tif"
                        dest.parent.mkdir(parents=True, exist_ok=True)
                        profile = dict(driver="GTiff", dtype="float32", count=2, height=a.shape[1], width=a.shape[2],
                                       crs=vv.crs, transform=vv.window_transform(win), nodata=-32768,
                                       compress="deflate", tiled=True, blockxsize=256, blockysize=256)
                        tmp = dest.with_suffix(".partial.tif")
                        with rasterio.open(tmp, "w", **profile) as dst:
                            dst.write(a)
                            dst.set_band_description(1, "VV_gamma0_linear_power")
                            dst.set_band_description(2, "VH_gamma0_linear_power")
                            dst.update_tags(source_item=item["id"], observation_datetime=item["properties"]["datetime"], units="linear_gamma0")
                        if dest.exists():
                            if sha256(dest) != sha256(tmp):
                                raise ValueError("Uncheckpointed existing radar clip differs; preserved")
                            tmp.unlink()
                        else:
                            os.replace(tmp, dest)
                        files[dest.relative_to(OUT).as_posix()] = {"sha256": sha256(dest), "bytes": dest.stat().st_size}
            write(OUT / "checkpoints" / f"{item['id']}.json", {"source_item": item["id"], "completed_utc": utc(), "files": files})
            return "downloaded"
        except Exception:
            if attempt == 2:
                # Exception URLs may contain short-lived public access signatures.
                raise RuntimeError("Radar acquisition failed after retries: " + item["id"]) from None
            time.sleep(1 + attempt)


def collect():
    check_sources()
    cfg = read(CONFIG)
    manifest = read(OUT / "scene_manifest.json")
    pending = [item for item in manifest["items"] if not checked_job(item["id"])]
    done = len(manifest["items"]) - len(pending)
    if pending:
        with urlopen("https://planetarycomputer.microsoft.com/api/sas/v1/token/sentinel1euwestrtc/sentinel1-grd-rtc", timeout=30) as response:
            token = json.load(response)["token"]
        with ThreadPoolExecutor(max_workers=cfg["download_workers"]) as pool:
            futures = {pool.submit(download_scene, item, manifest["windows"], token): item for item in pending}
            for future in as_completed(futures):
                future.result()
                done += 1
                progress = {"phase": "radar_collection", "completed": done, "total": len(manifest["items"]), "updated_utc": utc()}
                write(OUT / "progress.json", progress)
                print(json.dumps(progress), flush=True)
    for item in manifest["items"]:
        assert checked_job(item["id"])
    if not (OUT / "collection_complete.json").exists():
        write(OUT / "collection_complete.json", {"completed_utc": utc(), "acquisitions": len(manifest["items"]),
              "clips": len(manifest["items"])*len(manifest["windows"]), "image_bytes": sum(p.stat().st_size for p in (OUT/"clips").rglob("*.tif"))})
    return read(OUT / "collection_complete.json")


def power_summary(a, mask):
    valid = mask & np.isfinite(a) & (a > 0)
    n = int(mask.sum())
    values = 10*np.log10(a[valid])
    return {"pixel_count": n, "valid_pixel_count": int(valid.sum()), "valid_fraction": float(valid.sum()/n) if n else 0.,
            "median_db": float(np.median(values)) if len(values) else None,
            "p25_db": float(np.quantile(values, .25)) if len(values) else None,
            "p75_db": float(np.quantile(values, .75)) if len(values) else None}


def excursion_rows(frame, thresholds, maximum_rapid_gap):
    """Radar excursions are diagnostics, never irrigation events or measured drainage."""
    rows = []
    for (parcel, orbit), g in frame.groupby(["internal_parcel_id", "relative_orbit"], sort=True):
        g = g.sort_values("datetime").reset_index(drop=True)
        for i in range(1, len(g)-1):
            a,b,c = [g.iloc[k] for k in (i-1,i,i+1)]
            if not all(bool(x.radar_usable) for x in (a,b,c)):
                continue
            rise, fall = b.vv_inner_median_db-a.vv_inner_median_db, b.vv_inner_median_db-c.vv_inner_median_db
            before = (pd.Timestamp(b.datetime)-pd.Timestamp(a.datetime)).total_seconds()/86400
            after = (pd.Timestamp(c.datetime)-pd.Timestamp(b.datetime)).total_seconds()/86400
            same = len({a.platform,b.platform,c.platform}) == 1
            for threshold in thresholds:
                if rise >= threshold and fall >= threshold:
                    rows.append({"internal_parcel_id": parcel, "relative_orbit": orbit, "middle_datetime": b.datetime,
                                 "threshold_db": threshold, "rise_db": float(rise), "fall_db": float(fall),
                                 "before_gap_days": before, "after_gap_days": after, "same_platform": same,
                                 "rapid_drying_resolved": bool(after <= maximum_rapid_gap and same),
                                 "rainfall_resolved": False, "confirmed_irrigation": False})
    return rows


def analyze():
    check_sources()
    if (ANALYSIS / "complete.json").exists():
        return verify()
    assert (OUT / "collection_complete.json").exists()
    cfg = read(CONFIG)
    selection = gpd.read_parquet(OUT / "selection.parquet")
    manifest = read(OUT / "scene_manifest.json")
    rows = []
    for item in manifest["items"]:
        assert checked_job(item["id"])
        props = item["properties"]
        for group in manifest["windows"]:
            fields = selection.loc[selection.window_id.eq(group["id"])]
            with rasterio.open(OUT / "clips" / item["id"] / f"{group['id']}.tif") as src:
                arrays = src.read()
                for parcel in fields.itertuples():
                    record = {"internal_parcel_id": parcel.internal_parcel_id, "cadastre_code": parcel.cadastre_code,
                              "area_official_m2": parcel.area_official_m2, "community_hy": parcel.review_community_hy,
                              "sample_group": parcel.sample_group, "source_item": item["id"], "datetime": props["datetime"],
                              "platform": props["platform"].lower(), "relative_orbit": props["sat:relative_orbit"],
                              "orbit_direction": props["sat:orbit_state"]}
                    for name, geometry in [("full", parcel.geometry), ("inner", parcel.geometry.buffer(-cfg["interior_buffer_m"]))]:
                        mask = np.zeros(arrays.shape[1:], dtype=bool) if geometry.is_empty else geometry_mask([mapping(geometry)],
                               out_shape=arrays.shape[1:], transform=src.transform, invert=True, all_touched=False)
                        for band, a in zip(["vv", "vh"], arrays):
                            record.update({f"{band}_{name}_{k}":v for k,v in power_summary(a, mask).items()})
                    record["radar_usable"] = bool(record["vv_inner_valid_fraction"] >= cfg["minimum_valid_fraction"]
                         and record["vh_inner_valid_fraction"] >= cfg["minimum_valid_fraction"]
                         and record["vv_inner_valid_pixel_count"]*100/cfg["nominal_resolution_area_m2"] >= cfg["minimum_nominal_resolution_elements"])
                    rows.append(record)
    observations = pd.DataFrame(rows).sort_values(["internal_parcel_id", "datetime", "relative_orbit"]).reset_index(drop=True)
    assert len(observations) == len(selection)*len(manifest["items"])
    daily = ROOT / "data/analysis/observation_screening/transitions_area_20260906_v1/daily/2025.parquet"
    columns = ["internal_parcel_id", "observation_date", "ndvi_median", "ndmi_median", "bsi_median", "support", "finite"]
    eo = pd.read_parquet(daily, columns=columns, filters=[("internal_parcel_id", "in", selection.internal_parcel_id.tolist())])
    eo["date"] = pd.to_datetime(eo.observation_date, utc=True)
    eo = eo.loc[eo.support.ge(.6) & eo.finite.astype(bool)].sort_values("date")
    dates = pd.to_datetime(observations.datetime, utc=True)
    observations["eo_date"] = None
    for index in ("ndvi", "ndmi", "bsi"):
        observations["eo_"+index] = np.nan
    for parcel, indices in observations.groupby("internal_parcel_id").groups.items():
        e = eo.loc[eo.internal_parcel_id.eq(parcel)]
        if e.empty:
            continue
        for i in indices:
            delta = (e.date-dates.iloc[i]).abs()
            j = delta.idxmin()
            if delta.loc[j] <= pd.Timedelta(days=3):
                observations.at[i, "eo_date"] = str(e.loc[j, "observation_date"])
                for name in ("ndvi", "ndmi", "bsi"):
                    observations.at[i, "eo_"+name] = float(e.loc[j, name+"_median"])
    excursions = pd.DataFrame(excursion_rows(observations, cfg["excursion_sensitivity_db"], cfg["maximum_gap_for_rapid_drying_days"]))
    if excursions.empty:
        excursions = pd.DataFrame(columns=["internal_parcel_id", "relative_orbit", "middle_datetime", "threshold_db", "rise_db", "fall_db", "before_gap_days", "after_gap_days", "same_platform", "rapid_drying_resolved", "rainfall_resolved", "confirmed_irrigation"])
    ANALYSIS.mkdir(parents=True, exist_ok=True)
    observations.to_parquet(ANALYSIS / "radar_observations.parquet", index=False)
    excursions.to_parquet(ANALYSIS / "radar_excursions.parquet", index=False)
    result = selection.drop(columns="geometry").copy()
    result["usable_radar_observations"] = result.internal_parcel_id.map(observations.groupby("internal_parcel_id").radar_usable.sum()).fillna(0).astype(int)
    result["assessment_state"] = "insufficient_temporal_and_causal_evidence"
    result.loc[result.usable_radar_observations.eq(0), "assessment_state"] = "insufficient_spatial_support"
    result["high_water_demand_candidate"] = pd.Series(pd.NA, index=result.index, dtype="boolean")
    result.to_parquet(ANALYSIS / "parcels.parquet", index=False)
    weather = ROOT / "data/observations/observations_2021_2025_v1/weather/raw_tables"
    missing_weather = [f"2025-{m:02d}" for m in range(4,10) if not (weather / f"weather_2025_{m:02d}.parquet").is_file()]
    gaps = []
    for orbit, g in observations.drop_duplicates(["source_item"]).groupby("relative_orbit"):
        d = pd.to_datetime(g.datetime, utc=True).sort_values().diff().dropna().dt.total_seconds()/86400
        gaps += [{"relative_orbit": int(orbit), "min_days": float(d.min()), "max_days": float(d.max()), "median_days": float(d.median())}]
    report = {"status": "pilot_complete_no_validated_soil_loss_classification", "completed_utc": utc(),
              "parcels": len(result), "acquisitions": len(manifest["items"]), "radar_rows": len(observations),
              "usable_rows": int(observations.radar_usable.sum()), "parcels_with_usable_radar": int(result.usable_radar_observations.gt(0).sum()),
              "eo_context_rows": int(observations.eo_date.notna().sum()), "same_track_gaps": gaps,
              "diagnostic_excursions_by_db": {str(t):int(excursions.threshold_db.eq(t).sum()) for t in cfg["excursion_sensitivity_db"]},
              "rapid_drying_resolved_excursions": int(excursions.rapid_drying_resolved.sum()),
              "missing_weather_months_at_analysis": missing_weather, "rainfall_attribution_performed": False,
              "confirmed_irrigation_events": None, "high_demand_parcels": None,
              "normative_volume_estimated": False, "map_changed": False, "coarse_250m_data_used": False,
              "limitations": ["Small compact pilot selected for observability; not representative prevalence",
                              "Radar excursions can arise from soil moisture, canopy, roughness or processing",
                              "Same-track cadence cannot resolve drying within three days",
                              "No cross-orbit or cross-platform moisture calibration", "Rainfall attribution and root-zone cause unresolved"]}
    write(ANALYSIS / "report.json", report)
    paths = list(ANALYSIS.glob("*.parquet")) + [ANALYSIS/"report.json"]
    write(ANALYSIS / "complete.json", {"files": {p.name:sha256(p) for p in paths}, "daily_eo_source": {"path": daily.relative_to(ROOT).as_posix(), "sha256": sha256(daily)}})
    return verify()


def verify():
    check_sources()
    for item in read(OUT/"scene_manifest.json")["items"]:
        assert checked_job(item["id"])
    for name,digest in read(ANALYSIS/"complete.json")["files"].items():
        assert sha256(ANALYSIS/name) == digest
    source = read(ANALYSIS/"complete.json")["daily_eo_source"]
    assert sha256(ROOT/source["path"]) == source["sha256"]
    result = pd.read_parquet(ANALYSIS/"parcels.parquet")
    selection = pd.read_parquet(OUT/"selection.parquet")
    identity = ["internal_parcel_id", "cadastre_code", "area_official_m2"]
    pd.testing.assert_frame_equal(result[identity], selection[identity])
    assert result.internal_parcel_id.is_unique and result.high_water_demand_candidate.isna().all()
    return read(ANALYSIS/"report.json")


def main(action):
    with run_lock():
        result = {"prepare":prepare, "collect":collect, "analyze":analyze, "verify":verify}[action]()
    print(json.dumps(result, ensure_ascii=False, indent=2), flush=True)
