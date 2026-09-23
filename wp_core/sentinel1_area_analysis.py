"""Private full-area execution of the previously tested parcel comparison rules."""
from __future__ import annotations
import json
import os
from concurrent.futures import ProcessPoolExecutor, as_completed
from contextlib import contextmanager

import geopandas as gpd
import numpy as np
import pandas as pd

from wp_core import sentinel1_area_data as data
from wp_core import sentinel1_area_compare as fast
from wp_core import sentinel1_peer_comparison as peer
from wp_core.sentinel1_parcel_delivery import corrected_precipitation

ROOT, OUT, DATA = data.ROOT, data.OUT, data.DATA


@contextmanager
def lock():
    import msvcrt
    OUT.mkdir(parents=True, exist_ok=True)
    with (OUT / "run.lock").open("a+b") as f:
        if f.tell() == 0:
            f.write(b"0"); f.flush()
        f.seek(0)
        msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
        try:
            yield
        finally:
            f.seek(0); msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)


def prepare():
    snapshot = data.pinned()
    assert (DATA / "extraction_complete.json").exists(), "Radar extraction is not complete"
    if (OUT / "prepared.json").exists():
        result = peer.read(OUT / "prepared.json")
        for name, digest in result["sources"].items():
            assert peer.sha(ROOT / name) == digest, name
        return result
    fields = pd.read_parquet(DATA / "fields.parquet")
    fields = fields.loc[fields.sample_group.eq("clean_interior")].copy()
    potential = fast.potential_peers(fields, peer.read(peer.CONFIG))
    potential.to_parquet(OUT / "potential_peers.parquet", index=False)
    sources = [data.CONFIG, ROOT / "wp_core/sentinel1_area_analysis.py", ROOT / "wp_core/sentinel1_area_compare.py",
               ROOT / "run_sentinel1_area_analysis.py", ROOT / "wp_core/sentinel1_parcel_delivery.py",
               peer.CONFIG, ROOT / "wp_core/sentinel1_peer_comparison.py", DATA / "prepared.json",
               DATA / "extraction_complete.json", OUT / "potential_peers.parquet",
               ROOT / "config/releases.json", ROOT / "config/transitions_area_20260906_v1.review.lock.json"]
    for item in peer.read(DATA / "scenes.json")["items"]:
        path = DATA / "scene_tables" / (item["id"] + ".parquet")
        marker = peer.read(path.with_suffix(".json"))
        assert peer.sha(path) == marker["sha256"]
        sources.append(path)
    result = {"version": data.VERSION, "prepared_utc": peer.utc(), "weather": snapshot["weather"],
              "sources": {p.relative_to(ROOT).as_posix(): peer.sha(p) for p in sources},
              "potential_peer_pairs": len(potential), "analyzed_spatially_supported": len(fields)}
    peer.write(OUT / "prepared.json", result)
    return result


def run_year(year):
    folder = OUT / "years" / str(year)
    if (folder / "complete.json").exists():
        for name, digest in peer.read(folder / "complete.json")["files"].items():
            assert peer.sha(folder / name) == digest, name
        return peer.read(folder / "report.json")
    local, cfg = peer.read(data.CONFIG), peer.read(peer.CONFIG)
    prepared = peer.read(OUT / "prepared.json")
    fields = pd.read_parquet(DATA / "fields.parquet")
    fields = fields.loc[fields.sample_group.eq("clean_interior")].copy()
    potential = pd.read_parquet(OUT / "potential_peers.parquet")
    items = [x for x in peer.read(DATA / "scenes.json")["items"] if x["properties"]["datetime"].startswith(str(year))]
    obs = pd.concat([pd.read_parquet(DATA / "scene_tables" / (x["id"] + ".parquet")) for x in items], ignore_index=True)
    eo = pd.read_parquet(ROOT / "data/analysis/observation_screening/transitions_area_20260906_v1/daily" / f"{year}.parquet",
                         columns=["internal_parcel_id", "observation_date", "ndvi_median", "ndmi_median", "bsi_median", "support", "finite"],
                         filters=[("internal_parcel_id", "in", fields.internal_parcel_id.tolist())])
    obs = fast.join_optical(obs, eo)
    weather = [x for x in prepared["weather"] if x["month"].startswith(str(year))]
    raw, _ = peer.load_weather({"completed": weather})
    hourly = corrected_precipitation(raw, local["negative_increment_tolerance_mm"])
    rain = {f"era5land_{a:.1f}_{b:.1f}": g.set_index("valid_time").rain_mm for (a,b),g in hourly.groupby(["latitude", "longitude"])}
    diag, links, metrics, signals = fast.compare(obs, fields, potential, rain, cfg)
    for f in (diag, links, metrics, signals):
        f["year"] = year
    matched = diag.loc[diag.supported & diag.peer_count.ge(cfg["minimum_peers"])]
    nominal_metrics = metrics.loc[metrics.rain_limit_mm.eq(local["nominal_rain_limit_mm"]) & metrics.excursion_db.eq(local["nominal_excursion_db"])]
    nominal_signals = signals.loc[signals.rain_limit_mm.eq(local["nominal_rain_limit_mm"]) & signals.excursion_db.eq(local["nominal_excursion_db"]) & signals.relative_excursion]
    report = {"year": year, "weather_months": [x["month"] for x in weather], "radar_acquisitions": len(items),
              "radar_parcel_rows": len(obs), "matched_triplets": len(matched), "weather_complete_matched_triplets": int(matched.weather_complete.sum()),
              "nominal_comparable_triplets": int(nominal_metrics.comparable_triplets.sum()),
              "nominal_relative_observations": len(nominal_signals), "nominal_signal_parcels": int(nominal_signals.internal_parcel_id.nunique()),
              "any_sensitivity_signal_parcels": int(signals.loc[signals.relative_excursion, "internal_parcel_id"].nunique()),
              "repetition_assessable_parcels": int(metrics.loc[metrics.repeated_relative_signal.notna(), "internal_parcel_id"].nunique()),
              "repetition_positive_parcels_any_sensitivity": int(metrics.loc[metrics.repeated_relative_signal.fillna(False), "internal_parcel_id"].nunique()),
              "precision_corrected_cell_hours": int(hourly.precision_corrected.sum()),
              "material_negative_cell_hours": int(hourly.signed_increment_mm.lt(-local["negative_increment_tolerance_mm"]).sum())}
    folder.mkdir(parents=True, exist_ok=True)
    files = {"radar_input.parquet": obs, "weather_hourly.parquet": hourly, "comparisons.parquet": diag,
             "matched_peers.parquet": links, "metrics.parquet": metrics, "signals.parquet": signals}
    for name, frame in files.items():
        assert not (folder / name).exists(), "Preserve interrupted results: " + name
        frame.to_parquet(folder / name, index=False)
    peer.write(folder / "report.json", report)
    peer.write(folder / "complete.json", {"files": {n: peer.sha(folder / n) for n in [*files, "report.json"]}, "completed_utc": peer.utc()})
    return report


def summarize(reports):
    local = peer.read(data.CONFIG)
    metrics = pd.concat([pd.read_parquet(OUT / "years" / str(y) / "metrics.parquet") for y in local["years"]], ignore_index=True)
    signals = pd.concat([pd.read_parquet(OUT / "years" / str(y) / "signals.parquet") for y in local["years"]], ignore_index=True)
    nominal = metrics.loc[metrics.rain_limit_mm.eq(local["nominal_rain_limit_mm"]) & metrics.excursion_db.eq(local["nominal_excursion_db"])]
    events = signals.loc[signals.relative_excursion & signals.rain_limit_mm.eq(local["nominal_rain_limit_mm"]) & signals.excursion_db.eq(local["nominal_excursion_db"])]
    any_events = signals.loc[signals.relative_excursion].drop_duplicates(["internal_parcel_id", "year", "middle_item"])
    fields = pd.read_parquet(DATA / "fields.parquet")
    result = gpd.read_parquet(DATA / "original_geometry.parquet")[["internal_parcel_id", "cadastre_code", "area_official_m2", "geometry"]]
    result = result.merge(fields[["internal_parcel_id", "sample_group", "review_community_hy"]].rename(columns={"review_community_hy": "community_hy"}), on="internal_parcel_id", validate="one_to_one")
    assert len(result) == local["expected_eligible"] and result.crs.to_epsg() == 4326
    clean = result.sample_group.eq("clean_interior")
    point = result.geometry.representative_point()
    result["parcel_latitude"], result["parcel_longitude"] = point.y, point.x
    result["area_ha"] = result.area_official_m2 / 10000.
    counts = {
        "nominal_comparisons": nominal.groupby("internal_parcel_id").comparable_triplets.sum(),
        "nominal_relative_excursions": events.groupby("internal_parcel_id").size(),
        "relative_excursions_any_sensitivity": any_events.groupby("internal_parcel_id").size(),
        "years_with_nominal_signals": events.groupby("internal_parcel_id").year.nunique(),
        "years_with_any_sensitivity_signals": any_events.groupby("internal_parcel_id").year.nunique(),
        "years_repetition_assessable_nominal": nominal.loc[nominal.repeated_relative_signal.notna()].groupby("internal_parcel_id").year.nunique(),
        "years_passing_repetition_nominal": nominal.loc[nominal.repeated_relative_signal.fillna(False)].groupby("internal_parcel_id").year.nunique(),
        "years_passing_repetition_any_sensitivity": metrics.loc[metrics.repeated_relative_signal.fillna(False)].groupby("internal_parcel_id").year.nunique(),
    }
    for name, values in counts.items():
        result[name] = pd.array(result.internal_parcel_id.map(values).fillna(0).where(clean), dtype="Int64")
    result["signal_years"] = result.internal_parcel_id.map(any_events.groupby("internal_parcel_id").year.agg(lambda x: ", ".join(map(str, sorted(set(x)))))).fillna("")
    nominal_positive = result.nominal_relative_excursions.fillna(0).gt(0)
    any_positive = result.relative_excursions_any_sensitivity.fillna(0).gt(0)
    conditions = [~clean, result.years_passing_repetition_nominal.fillna(0).gt(0), nominal_positive, any_positive, result.nominal_comparisons.fillna(0).gt(0)]
    result["evidence_status"] = np.select([x.to_numpy(dtype=bool) for x in conditions],
        ["Insufficient spatial support", "Repeated relative radar signal at nominal setting", "Relative radar signal at nominal setting", "Signal only at another sensitivity", "Comparable observations without nominal relative signal"],
        default="Insufficient comparable observations")
    result["water_demand_status"] = "Unassessed"
    result["coverage_note"] = "2021-2024 Apr-Sep; 2025 Apr-Jun only"
    result = result.sort_values(["years_passing_repetition_nominal", "years_with_nominal_signals", "nominal_relative_excursions", "relative_excursions_any_sensitivity", "community_hy", "cadastre_code"],
                                ascending=[False, False, False, False, True, True], na_position="last").reset_index(drop=True)
    shortlist = result.loc[result.nominal_relative_excursions.fillna(0).gt(0)].copy()
    weak = result.loc[result.nominal_relative_excursions.fillna(0).eq(0) & result.relative_excursions_any_sensitivity.fillna(0).gt(0)].copy()
    combined = result.loc[result.relative_excursions_any_sensitivity.fillna(0).gt(0)].copy()
    for name, frame in (("all_parcels", result), ("nominal_inspection_parcels", shortlist), ("sensitivity_only_parcels", weak), ("all_inspection_parcels", combined)):
        frame.to_parquet(OUT / (name + ".parquet"), index=False)
        frame.drop(columns="geometry").to_csv(OUT / (name + ".csv"), index=False, encoding="utf-8-sig")
    result.to_file(OUT / "parcel_results.gpkg", layer="all_22802_parcels", driver="GPKG", index=False)
    if len(combined):
        combined.to_file(OUT / "parcel_results.gpkg", layer="inspection_leads", driver="GPKG", index=False)
    identity = result[["internal_parcel_id", "cadastre_code", "community_hy", "area_official_m2"]]
    signals.merge(identity, on="internal_parcel_id", validate="many_to_one").to_parquet(OUT / "signal_evidence.parquet", index=False)
    communities = result.groupby("community_hy", dropna=False).agg(parcels=("internal_parcel_id", "size"),
        spatially_supported=("sample_group", lambda x: int(x.eq("clean_interior").sum())),
        nominal_signal_parcels=("nominal_relative_excursions", lambda x: int(x.fillna(0).gt(0).sum())),
        any_sensitivity_signal_parcels=("relative_excursions_any_sensitivity", lambda x: int(x.fillna(0).gt(0).sum()))).reset_index()
    communities.to_csv(OUT / "community_summary.csv", index=False, encoding="utf-8-sig")
    summary = {"version": data.VERSION, "completed_utc": peer.utc(), "scope": len(result), "spatially_supported": int(clean.sum()),
               "insufficient_spatial_support": int((~clean).sum()), "nominal_inspection_parcels": len(shortlist), "sensitivity_only_parcels": len(weak),
               "all_inspection_parcels": len(combined), "nominal_relative_observations": len(events),
               "parcels_with_nominal_signals_in_multiple_years": int(result.years_with_nominal_signals.fillna(0).ge(2).sum()),
               "parcels_passing_repetition_nominal": int(result.years_passing_repetition_nominal.fillna(0).gt(0).sum()),
               "parcels_passing_repetition_any_sensitivity": int(result.years_passing_repetition_any_sensitivity.fillna(0).gt(0).sum()),
               "water_demand_assessment": "Unassessed for every parcel; no confirmed water volumes, rapid soil loss, or above-normative demand",
               "state_counts": result.evidence_status.value_counts().to_dict(), "years": sorted(reports, key=lambda r: r["year"]),
               "household_excluded": 20096, "roads_excluded": 1086, "map_changed": False,
               "missing_weather_in_frozen_snapshot": peer.read(DATA / "prepared.json")["missing_weather"],
               "interpretation": "Private inspection leads from the existing experimental relative radar rules. Missing support is unknown, not normal demand."}
    peer.write(OUT / "report.json", summary)
    files = [p for p in OUT.iterdir() if p.is_file() and p.name not in ("run.lock", "complete.json")]
    peer.write(OUT / "complete.json", {"files": {p.name: peer.sha(p) for p in files},
        "year_manifests": {str(y): peer.sha(OUT / "years" / str(y) / "complete.json") for y in local["years"]}})
    return summary


def main():
    with lock():
        prepare()
        if (OUT / "complete.json").exists():
            for name, digest in peer.read(OUT / "complete.json")["files"].items():
                assert peer.sha(OUT / name) == digest, name
            print(json.dumps(peer.read(OUT / "report.json"), ensure_ascii=False, indent=2)); return
        cfg = peer.read(data.CONFIG)
        reports = []
        with ProcessPoolExecutor(max_workers=min(len(cfg["years"]), cfg["analysis_workers"], os.cpu_count() or 1)) as pool:
            futures = [pool.submit(run_year, y) for y in cfg["years"]]
            for f in as_completed(futures):
                report = f.result(); reports.append(report)
                print(json.dumps(report, ensure_ascii=False), flush=True)
        print(json.dumps(summarize(reports), ensure_ascii=False, indent=2), flush=True)
