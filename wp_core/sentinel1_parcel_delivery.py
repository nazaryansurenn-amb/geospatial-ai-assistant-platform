"""Private parcel delivery with a separately versioned precipitation precision fix."""
from __future__ import annotations
import json
import os
from concurrent.futures import ProcessPoolExecutor, as_completed
from contextlib import contextmanager
from pathlib import Path

import geopandas as gpd
import numpy as np
import pandas as pd

from wp_core import sentinel1_multiyear as multi
from wp_core import sentinel1_peer_comparison as peer

ROOT = Path(__file__).resolve().parents[1]
VERSION = "sentinel1_parcel_delivery_20260906_v1"
CONFIG = ROOT / "config" / (VERSION + ".json")
OUT = ROOT / "data/analysis/rapid_water_loss" / VERSION


def corrected_precipitation(raw, tolerance_mm):
    """Correct only finite, tiny negative differences; retain real gaps/negatives.

    Tolerance is an explicit analytical precision policy, not recovered GRIB
    packingError metadata. Original accumulated values are never changed.
    """
    f = raw.copy()
    f["valid_time"] = pd.to_datetime(f.valid_time, utc=True)
    f = f.sort_values(["latitude", "longitude", "valid_time"])
    if f.duplicated(["latitude", "longitude", "valid_time"]).any():
        raise ValueError("Duplicate cell/hour")
    g = f.groupby(["latitude", "longitude"], sort=False)
    delta = g.tp.diff().where(g.valid_time.diff().eq(pd.Timedelta(hours=1)))
    delta = delta.where(f.valid_time.dt.hour.ne(1), f.tp) * 1000.
    corrected = np.isfinite(delta) & delta.lt(0) & delta.ge(-tolerance_mm)
    f["signed_increment_mm"] = delta
    f["precision_corrected"] = corrected
    f["rain_mm"] = delta.where(np.isfinite(delta) & delta.ge(0))
    f.loc[corrected, "rain_mm"] = 0.
    return f


@contextmanager
def lock():
    import msvcrt
    OUT.mkdir(parents=True, exist_ok=True)
    with (OUT / "run.lock").open("a+b") as f:
        if f.tell() == 0:
            f.write(b"0")
            f.flush()
        f.seek(0)
        msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
        try:
            yield
        finally:
            f.seek(0)
            msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)


def verify_pinned():
    marker = peer.read(OUT / "prepared.json")
    for rel, digest in marker["sources"].items():
        assert peer.sha(ROOT / rel) == digest, rel
    for item in marker["weather"]:
        assert peer.sha(ROOT / item["path"]) == item["sha256"], item["month"]
    return marker


def prepare():
    if (OUT / "prepared.json").exists():
        return verify_pinned()
    multi.pinned()
    peer.verify_prepared()
    cfg = peer.read(CONFIG)
    status = peer.weather_status({"weather_months": [f"{y}_{m:02d}" for y in cfg["years"] for m in range(3, 10)]})
    paths = [CONFIG, Path(__file__), ROOT / "run_sentinel1_parcel_delivery.py", ROOT / "verify_sentinel1_parcel_delivery.py",
             ROOT / "docs/SENTINEL1_PARCEL_DELIVERY_EN.md", peer.CONFIG, ROOT / "wp_core/sentinel1_peer_comparison.py",
             multi.DATA / "manifest.json", multi.OLD / "selection.parquet", peer.OUT / "fields.parquet", peer.OUT / "potential_peers.parquet"]
    for y in cfg["years"]:
        record = peer.read(multi.DATA / "tables" / f"{y}.json")
        paths += [multi.DATA / "tables" / f"{y}.json", ROOT / record["path"]]
    marker = {"created_utc": peer.utc(), "sources": {p.relative_to(ROOT).as_posix(): peer.sha(p) for p in paths},
              "weather": status["completed"], "missing_weather": status["missing_months"], "config": cfg}
    peer.write(OUT / "prepared.json", marker)
    return marker


def run_year(year):
    marker = verify_pinned()
    folder = OUT / "years" / str(year)
    if (folder / "complete.json").exists():
        complete = peer.read(folder / "complete.json")
        for name, digest in complete["files"].items():
            assert peer.sha(folder / name) == digest
        return peer.read(folder / "report.json")
    cfg = peer.read(peer.CONFIG)
    local = marker["config"]
    weather = [x for x in marker["weather"] if x["month"].startswith(str(year))]
    record = peer.read(multi.DATA / "tables" / f"{year}.json")
    obs = pd.read_parquet(ROOT / record["path"])
    months = [x["month"] for x in weather]
    obs = obs.loc[pd.to_datetime(obs.datetime, utc=True, format="ISO8601").dt.strftime("%Y_%m").isin(months)].copy()
    raw, _ = peer.load_weather({"completed": weather})
    hourly = corrected_precipitation(raw, local["negative_increment_tolerance_mm"])
    rain = {f"era5land_{a:.1f}_{b:.1f}": g.set_index("valid_time").rain_mm for (a, b), g in hourly.groupby(["latitude", "longitude"])}
    fields = pd.read_parquet(peer.OUT / "fields.parquet")
    potential = pd.read_parquet(peer.OUT / "potential_peers.parquet")
    diag, links, metrics, parcels = peer.compare(obs, fields, potential, rain, cfg)
    diag["year"] = year
    metrics["year"] = year
    parcels["year"] = year
    unique = diag.drop_duplicates(["internal_parcel_id", "middle_item"])
    matched = unique.loc[unique.supported & unique.peer_count.ge(cfg["minimum_peers"])]
    negative = hourly.signed_increment_mm.loc[hourly.signed_increment_mm.lt(0)]
    report = {"year": year, "weather_months": months, "radar_acquisitions": int(obs.source_item.nunique()),
              "matched_triplets": len(matched), "rainfall_complete_matched_triplets": int(matched.weather_complete.sum()),
              "parcels_with_comparisons": int(parcels.comparable_middle_acquisitions_any_sensitivity.gt(0).sum()),
              "repetition_assessable_parcels": int(metrics.loc[metrics.repeated_relative_signal.notna(), "internal_parcel_id"].nunique()),
              "repetition_positive_parcels": int(metrics.loc[metrics.repeated_relative_signal.fillna(False), "internal_parcel_id"].nunique()),
              "relative_excursion_parcels_any_sensitivity": int(diag.loc[diag.relative_excursion.fillna(False), "internal_parcel_id"].nunique()),
              "precision_corrected_cell_hours": int(hourly.precision_corrected.sum()),
              "material_negative_cell_hours": int(hourly.signed_increment_mm.lt(-local["negative_increment_tolerance_mm"]).sum()),
              "minimum_signed_increment_mm": float(negative.min()) if len(negative) else None}
    folder.mkdir(parents=True, exist_ok=True)
    files = {"radar_input.parquet": obs, "weather_hourly.parquet": hourly, "comparisons.parquet": diag,
             "matched_peers.parquet": links, "metrics.parquet": metrics, "parcels.parquet": parcels}
    for name, frame in files.items():
        assert not (folder / name).exists(), "Preserve interrupted results"
        frame.to_parquet(folder / name, index=False)
    peer.write(folder / "report.json", report)
    peer.write(folder / "complete.json", {"files": {n: peer.sha(folder/n) for n in [*files, "report.json"]}, "completed_utc": peer.utc()})
    return report


def summarize(reports):
    cfg = peer.read(CONFIG)
    frames = [pd.read_parquet(OUT / "years" / str(y) / "comparisons.parquet") for y in cfg["years"]]
    diag = pd.concat(frames, ignore_index=True)
    metrics = pd.concat([pd.read_parquet(OUT / "years" / str(y) / "metrics.parquet") for y in cfg["years"]], ignore_index=True)
    nominal = diag.loc[diag.rain_limit_mm.eq(cfg["nominal_rain_limit_mm"]) & diag.excursion_db.eq(cfg["nominal_excursion_db"])].copy()
    events = nominal.loc[nominal.relative_excursion.fillna(False)].copy()
    any_events = diag.loc[diag.relative_excursion.fillna(False)].drop_duplicates(["internal_parcel_id", "year", "middle_item"])
    source = gpd.read_parquet(multi.OLD / "selection.parquet")
    result = source[["internal_parcel_id", "cadastre_code", "area_official_m2", "review_community_hy", "sample_group", "geometry"]].copy()
    result = result.rename(columns={"review_community_hy": "community_hy"})
    locations = result.geometry.representative_point().to_crs(4326)
    result["parcel_latitude"] = locations.y
    result["parcel_longitude"] = locations.x
    result["area_ha"] = result.area_official_m2 / 10000.
    for name, frame in (("nominal_comparisons", nominal.loc[nominal.comparable]), ("nominal_relative_excursions", events), ("relative_excursions_any_sensitivity", any_events)):
        result[name] = result.internal_parcel_id.map(frame.groupby("internal_parcel_id").size()).fillna(0).astype(int)
    result["years_with_nominal_relative_excursions"] = result.internal_parcel_id.map(events.groupby("internal_parcel_id").year.nunique()).fillna(0).astype(int)
    result["nominal_signal_years"] = result.internal_parcel_id.map(events.groupby("internal_parcel_id").year.agg(lambda x: ", ".join(map(str, sorted(set(x)))))).fillna("")
    result["years_with_repetition_assessable"] = result.internal_parcel_id.map(metrics.loc[metrics.repeated_relative_signal.notna()].groupby("internal_parcel_id").year.nunique()).fillna(0).astype(int)
    result["years_passing_original_repetition_rule"] = result.internal_parcel_id.map(metrics.loc[metrics.repeated_relative_signal.fillna(False)].groupby("internal_parcel_id").year.nunique()).fillna(0).astype(int)
    result["evidence_status"] = np.select([result.nominal_relative_excursions.gt(0), result.relative_excursions_any_sensitivity.gt(0), result.nominal_comparisons.gt(0), result.sample_group.eq("boundary_challenge")],
                                         ["Relative radar excursion observed", "Signal only at another sensitivity", "Comparable observations without nominal relative excursion", "Insufficient spatial support"], default="Insufficient comparable observations")
    result["water_demand_status"] = "Unassessed"
    result["maps_url"] = [f"https://www.google.com/maps?q={a:.6f},{b:.6f}" for a,b in zip(result.parcel_latitude, result.parcel_longitude)]
    result = result.sort_values(["years_with_nominal_relative_excursions", "nominal_relative_excursions", "relative_excursions_any_sensitivity", "community_hy", "cadastre_code"], ascending=[False, False, False, True, True]).reset_index(drop=True)
    shortlist = result.loc[result.nominal_relative_excursions.gt(0)].copy()
    sensitivity_only = result.loc[result.nominal_relative_excursions.eq(0) & result.relative_excursions_any_sensitivity.gt(0)].copy()
    for name, frame in (("all_parcels", result), ("inspection_parcels", shortlist), ("sensitivity_only_parcels", sensitivity_only)):
        frame.to_parquet(OUT / (name + ".parquet"), index=False)
        frame.drop(columns="geometry").to_csv(OUT / (name + ".csv"), index=False, encoding="utf-8-sig")
    events = events.merge(result.drop(columns="geometry"), on="internal_parcel_id", validate="many_to_one")
    events.to_parquet(OUT / "nominal_observations.parquet", index=False)
    events.to_csv(OUT / "nominal_observations.csv", index=False, encoding="utf-8-sig")
    result.to_file(OUT / "parcel_results.gpkg", layer="all_pilot_parcels", driver="GPKG", index=False)
    if len(shortlist):
        shortlist.to_file(OUT / "parcel_results.gpkg", layer="inspection_parcels", driver="GPKG", index=False)
    summary = {"version": VERSION, "completed_utc": peer.utc(), "scope": len(result), "nominal_inspection_parcels": len(shortlist),
               "sensitivity_only_parcels": len(sensitivity_only), "nominal_relative_observations": len(events),
               "parcels_with_signals_in_multiple_years": int(shortlist.years_with_nominal_relative_excursions.ge(2).sum()),
               "parcels_passing_original_repetition_rule": int(result.years_passing_original_repetition_rule.gt(0).sum()),
               "high_water_demand_parcels": None, "community_counts": shortlist.groupby("community_hy").size().to_dict(),
               "years": sorted(reports, key=lambda x: x["year"]), "map_changed": False,
               "interpretation": cfg["list_meaning"]}
    peer.write(OUT / "report.json", summary)
    records = json.loads(result.drop(columns="geometry").to_json(orient="records", force_ascii=False))
    peer.write(OUT / "workbook_data.json", {"summary": summary, "parcels": records})
    files = [p for p in OUT.iterdir() if p.is_file() and p.name not in ("run.lock", "complete.json")]
    peer.write(OUT / "complete.json", {"files": {p.name: peer.sha(p) for p in files}, "year_manifests": {str(y): peer.sha(OUT / "years" / str(y) / "complete.json") for y in cfg["years"]}})
    return summary


def main():
    with lock():
        marker = prepare()
        if (OUT / "complete.json").exists():
            for name, digest in peer.read(OUT / "complete.json")["files"].items():
                assert peer.sha(OUT/name) == digest
            print(json.dumps(peer.read(OUT / "report.json"), ensure_ascii=False, indent=2))
            return
        reports = []
        with ProcessPoolExecutor(max_workers=min(marker["config"]["workers"], os.cpu_count() or 1)) as pool:
            futures = {pool.submit(run_year, y): y for y in marker["config"]["years"]}
            for future in as_completed(futures):
                report = future.result()
                reports.append(report)
                print(json.dumps(report, ensure_ascii=False), flush=True)
        print(json.dumps(summarize(reports), ensure_ascii=False, indent=2), flush=True)
