from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd


PRODUCT_ROOT = Path(__file__).resolve().parents[1]
if str(PRODUCT_ROOT) not in sys.path:
    sys.path.insert(0, str(PRODUCT_ROOT))

from wp_core.parcel_land_features import (
    build_seasonal_parcel_features,
    validate_seasonal_parcel_features,
)
from scripts.build_lower_hrazdan_parcel_eo_timeseries import (
    _output_path,
    build_timeseries,
)


PARCEL_SOURCE = (
    PRODUCT_ROOT
    / "data"
    / "analysis"
    / "parcel_eo"
    / "current_halo_v1"
    / "current_parcels.parquet"
)
OUTPUT_DIR = (
    PRODUCT_ROOT
    / "data"
    / "analysis"
    / "parcel_eo"
    / "v5_full_halo_2021_2025"
)
LEGACY_MANIFEST_DIR = PRODUCT_ROOT / "data" / "source" / "scene_manifests"
DATASET_ID = "full_current_halo_v1"


def build(year: int, *, refresh: bool = False) -> dict[str, object]:
    if not PARCEL_SOURCE.exists():
        raise FileNotFoundError(
            "Build the current parcel universe before running parcel EO"
        )
    source_manifest = (
        LEGACY_MANIFEST_DIR
        / f"lower_hrazdan_parcel_eo_scene_manifest_{year}.json"
    )
    if not source_manifest.exists():
        source_manifest = None

    eo_quality = build_timeseries(
        year=year,
        refresh=refresh,
        parcel_source=PARCEL_SOURCE,
        output_dir=OUTPUT_DIR,
        dataset_id=DATASET_ID,
        scene_manifest_source=source_manifest,
    )
    observations_path = _output_path(year, OUTPUT_DIR)
    observations = pd.read_parquet(observations_path)
    analysis_version = f"lower_hrazdan_full_halo_seasonal_features_v1_{year}"
    seasonal = build_seasonal_parcel_features(
        observations,
        analysis_version=analysis_version,
    )
    validation = validate_seasonal_parcel_features(seasonal)
    latest = pd.to_datetime(observations["observation_date"], errors="raise").max()
    seasonal["analysis_year"] = year
    seasonal["latest_observation_date"] = latest.date().isoformat()
    seasonal["season_complete"] = bool(latest.month == 9 and latest.day >= 25)
    seasonal["public_release_approved"] = False
    seasonal_path = OUTPUT_DIR / f"seasonal_features_{year}.parquet"
    seasonal.to_parquet(seasonal_path, index=False, compression="zstd")

    quality = {
        "schema_version": 1,
        "analysis_version": analysis_version,
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "analysis_year": year,
        "parcel_universe": "all_current_parcels_inside_approved_lower_hrazdan_halos",
        "historical_presence_used_as_eo_filter": False,
        "legacy_250m_grid_used": False,
        "eo_quality": eo_quality,
        "seasonal_validation": validation,
        "observations": str(observations_path),
        "seasonal_features": str(seasonal_path),
        "public_release_approved": False,
    }
    quality_path = OUTPUT_DIR / f"seasonal_features_quality_{year}.json"
    quality_path.write_text(
        json.dumps(quality, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return quality


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--year", type=int, required=True)
    parser.add_argument("--refresh", action="store_true")
    args = parser.parse_args()
    print(json.dumps(build(args.year, refresh=args.refresh), ensure_ascii=False, indent=2))
