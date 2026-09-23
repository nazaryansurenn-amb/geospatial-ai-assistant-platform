"""Run a separate comparison using only weather batches complete at snapshot time."""
import os
for name in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS"):
    os.environ[name] = "1"

import argparse
import json
import shutil
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd
from wp_core import sentinel1_peer_comparison as base

ROOT = Path(__file__).resolve().parent
VERSION = "sentinel1_peer_partial_20260906_v1"
OUT = ROOT / "data/analysis/rapid_water_loss" / VERSION


@contextmanager
def lock():
    import msvcrt
    OUT.mkdir(parents=True, exist_ok=True)
    with (OUT / "run.lock").open("a+b") as stream:
        if stream.tell() == 0:
            stream.write(b"0")
            stream.flush()
        stream.seek(0)
        try:
            msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            raise RuntimeError("A partial comparison already owns this run lock") from None
        try:
            yield
        finally:
            stream.seek(0)
            msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)


def select_covered_observations(observations, months):
    """Never borrow another year's weather or assume that an uncovered month is dry."""
    month = pd.to_datetime(observations.datetime, utc=True, format="ISO8601").dt.strftime("%Y_%m")
    return observations.loc[month.isin(months)].copy()


def check_snapshot():
    base.verify_prepared()
    snapshot = base.read(OUT / "snapshot.json")
    for name, digest in snapshot["sources"].items():
        assert base.sha(ROOT / name) == digest, "Partial source changed: " + name
    return snapshot


def snapshot_inputs():
    if (OUT / "snapshot.json").exists():
        return check_snapshot()
    base.verify_prepared()
    cfg = base.read(base.CONFIG)
    status = base.weather_status(cfg)
    available = status["completed"]
    if not available:
        raise ValueError("No completed weather batches available")
    sources = [Path(__file__), ROOT / "verify_sentinel1_peer_partial.py", ROOT / "docs/SENTINEL1_PEER_PARTIAL_EN.md",
               base.OUT / "prepared.json", base.CONFIG, ROOT / "wp_core/sentinel1_peer_comparison.py"]
    sources += [ROOT / item["path"] for item in available]
    for p in sources[:3]:
        dest = OUT / "source" / p.relative_to(ROOT)
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(p, dest)
    snapshot = {"version": VERSION, "created_utc": base.utc(), "weather_sources": available,
                "weather_months": [item["month"] for item in available], "excluded_weather_months": status["missing_months"],
                "rules": cfg, "sources": {p.relative_to(ROOT).as_posix(): base.sha(p) for p in sources},
                "method_change": "temporal_subset_only_matching_and_rainfall_thresholds_unchanged"}
    base.write(OUT / "snapshot.json", snapshot)
    return snapshot


def verify_files():
    snapshot = check_snapshot()
    complete = base.read(OUT / "complete.json")
    for name, digest in complete["files"].items():
        assert base.sha(OUT / name) == digest, name
    return base.read(OUT / "report.json")


def run():
    snapshot = snapshot_inputs()
    if (OUT / "complete.json").exists():
        return verify_files()
    cfg = snapshot["rules"]
    hourly, rain = base.load_weather({"completed": snapshot["weather_sources"]})
    observations = pd.read_parquet(ROOT / cfg["pilot"] / "analysis/radar_observations.parquet")
    covered = select_covered_observations(observations, snapshot["weather_months"])
    fields = pd.read_parquet(base.OUT / "fields.parquet")
    potential = pd.read_parquet(base.OUT / "potential_peers.parquet")
    trips = base.triplets(covered, cfg)
    if trips.empty:
        raise ValueError("Available weather/radar overlap contains no same-platform three-visit sequence")
    comparisons, links, metrics, parcels = base.compare(covered, fields, potential, rain, cfg)
    # Keep raw matching separate from eligibility for a low-rainfall signal.
    one = comparisons.drop_duplicates(["internal_parcel_id", "middle_item"]).copy()
    raw_matched = one.loc[one.supported & one.peer_count.ge(cfg["minimum_peers"])]
    raw_counts = raw_matched.groupby("internal_parcel_id").size()
    parcels["matched_triplets_before_weather_screen"] = parcels.internal_parcel_id.map(raw_counts).fillna(0).astype(int)
    parcels["repeated_signal_assessable"] = parcels.internal_parcel_id.isin(metrics.loc[metrics.repeated_relative_signal.notna(), "internal_parcel_id"])
    parcels["weather_snapshot_version"] = VERSION
    community = []
    for name, g in parcels.groupby("review_community_hy", sort=True):
        community.append({"community_hy": name, "parcels": len(g),
                          "with_spatial_support": int(g.sample_group.eq("clean_interior").sum()),
                          "with_matched_triplets": int(g.matched_triplets_before_weather_screen.gt(0).sum()),
                          "with_low_rain_comparisons": int(g.comparable_middle_acquisitions_any_sensitivity.gt(0).sum()),
                          "repetition_assessable": int(g.repeated_signal_assessable.sum())})
    report = {"version": VERSION, "status": "partial_weather_comparison_complete", "completed_utc": base.utc(),
              "weather_months_used": snapshot["weather_months"], "weather_months_excluded": snapshot["excluded_weather_months"],
              "radar_first_utc": str(pd.to_datetime(covered.datetime, utc=True, format="ISO8601").min()),
              "radar_last_utc": str(pd.to_datetime(covered.datetime, utc=True, format="ISO8601").max()),
              "radar_acquisitions": int(covered.source_item.nunique()), "radar_rows": len(covered), "parcels": len(parcels),
              "triplets": len(one), "structurally_supported_triplets": int(one.supported.sum()),
              "matched_triplets_before_weather_screen": len(raw_matched),
              "parcels_with_matched_triplets": int(parcels.matched_triplets_before_weather_screen.gt(0).sum()),
              "matched_triplets_with_complete_rainfall": int(raw_matched.weather_complete.sum()),
              "parcels_with_low_rain_comparisons": int(parcels.comparable_middle_acquisitions_any_sensitivity.gt(0).sum()),
              "parcels_with_enough_exposure_for_repetition": int(parcels.repeated_signal_assessable.sum()),
              "maximum_comparable_triplets_per_track_platform_sensitivity": int(metrics.comparable_triplets.max()),
              "low_rain_comparisons_by_limit": {str(v): int(comparisons.loc[comparisons.rain_limit_mm.eq(v) & comparisons.comparable].drop_duplicates(["internal_parcel_id", "middle_item"]).shape[0]) for v in cfg["rain_sensitivity_mm"]},
              "community_summary": community, "high_water_demand_parcels": None, "confirmed_irrigation_events": None,
              "rapid_root_zone_drying_measured": False, "normative_volume_estimated": False, "map_changed": False,
              "classification_inputs_used": False, "coarse_250m_data_used": False,
              "limits": ["Only dates covered by the frozen available-weather snapshot were selected",
                         "Missing weather or inconsistent hourly accumulation remains unknown",
                         "Raw matched changes may reflect rainfall, canopy or management",
                         "Short overlap may not meet repetition requirements; null is not a negative finding",
                         "No community prevalence, irrigation frequency, water volume or soil drainage is established"]}
    attempt = OUT / "attempts" / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    attempt.mkdir(parents=True, exist_ok=False)
    outputs = {"radar_input.parquet": covered, "comparisons.parquet": comparisons, "matched_peers.parquet": links,
               "metrics.parquet": metrics, "parcels.parquet": parcels}
    for name, frame in outputs.items():
        frame.to_parquet(attempt / name, index=False)
    base.write(attempt / "report.json", report)
    names = list(outputs) + ["report.json"]
    for name in names:
        if (OUT / name).exists():
            raise ValueError("Unsealed existing output preserved for inspection: " + name)
        shutil.copy2(attempt / name, OUT / name)
    base.write(OUT / "complete.json", {"snapshot_sha256": base.sha(OUT / "snapshot.json"),
               "files": {name: base.sha(OUT / name) for name in names}})
    return verify_files()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=["run", "verify"], default="run", nargs="?")
    with lock():
        result = run() if parser.parse_args().action == "run" else verify_files()
    print(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False))
