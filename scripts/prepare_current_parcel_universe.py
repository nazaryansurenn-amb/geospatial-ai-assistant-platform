from __future__ import annotations

import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import geopandas as gpd
import pandas as pd


PRODUCT_ROOT = Path(__file__).resolve().parents[1]
if str(PRODUCT_ROOT) not in sys.path:
    sys.path.insert(0, str(PRODUCT_ROOT))

from wp_core.parcel_identity import attach_parcel_identity


CADASTRE_SOURCE = PRODUCT_ROOT / "data" / "source" / "cadastre" / "parcels_wua.gpkg"
ACTIVITY_SOURCE = (
    PRODUCT_ROOT / "server_data" / "review" / "land_activity_2026_preview.json"
)
OUTPUT_DIR = PRODUCT_ROOT / "data" / "analysis" / "parcel_eo" / "current_halo_v1"
OUTPUT_PATH = OUTPUT_DIR / "current_parcels.parquet"
QUALITY_PATH = OUTPUT_DIR / "current_parcels_quality.json"

ANALYSIS_VERSION = "lower_hrazdan_current_parcel_universe_v1"
EXPECTED_PARCEL_COUNT = 43_984


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def canonical_geometry_version(frame: gpd.GeoDataFrame) -> str:
    digest = hashlib.sha256()
    ordered = frame.sort_values("cadastre_code")
    for row in ordered.itertuples(index=False):
        digest.update(str(row.cadastre_code).encode("utf-8"))
        digest.update(b"\0")
        digest.update(f"{float(row.area_official_m2):.4f}".encode("ascii"))
        digest.update(b"\0")
        digest.update(row.geometry.wkb)
        digest.update(b"\n")
    return f"sha256:{digest.hexdigest()}"


def build() -> dict[str, object]:
    required = [CADASTRE_SOURCE, ACTIVITY_SOURCE]
    missing = [str(path) for path in required if not path.exists()]
    if missing:
        raise FileNotFoundError(f"Missing current-parcel inputs: {missing}")

    activity = json.loads(ACTIVITY_SOURCE.read_text(encoding="utf-8"))
    records = pd.DataFrame(activity["records"], columns=activity["record_schema"])
    records["cadastre_code"] = records["cadastre_code"].astype(str)
    if len(records) != EXPECTED_PARCEL_COUNT:
        raise ValueError(f"Unexpected current activity parcel count: {len(records)}")
    if records["cadastre_code"].duplicated().any():
        raise ValueError("Current activity contains duplicate cadastral codes")

    cadastre = gpd.read_file(
        CADASTRE_SOURCE,
        layer="parcels",
        columns=["cadastre_code", "area_official_m2", "geometry"],
    )
    cadastre["cadastre_code"] = cadastre["cadastre_code"].astype(str)
    if cadastre.crs is None or cadastre.crs.to_epsg() != 4326:
        raise ValueError(f"Expected cadastral EPSG:4326, found {cadastre.crs}")
    if cadastre["cadastre_code"].duplicated().any():
        raise ValueError("Cadastral source contains duplicate cadastral codes")

    codes = set(records["cadastre_code"])
    parcels = cadastre[cadastre["cadastre_code"].isin(codes)].copy()
    if len(parcels) != EXPECTED_PARCEL_COUNT:
        raise ValueError(f"Current cadastral geometry coverage is {len(parcels)}")
    if parcels.geometry.isna().any() or parcels.geometry.is_empty.any():
        raise ValueError("Current parcel universe contains missing or empty geometry")
    if not parcels.geometry.is_valid.all():
        raise ValueError("Current parcel universe contains invalid geometry")

    context_columns = [
        "cadastre_code",
        "stage",
        "preview_state",
        "household_agriculture",
        "road_excluded",
    ]
    parcels = parcels.merge(
        records[context_columns],
        on="cadastre_code",
        how="inner",
        validate="1:1",
    )
    parcels = attach_parcel_identity(parcels, include_public_id=True)
    parcels = gpd.GeoDataFrame(parcels, geometry="geometry", crs=cadastre.crs)
    parcels["household_agriculture"] = parcels["household_agriculture"].astype(bool)
    parcels["road_excluded"] = parcels["road_excluded"].astype(bool)
    parcels = parcels.sort_values("cadastre_code").reset_index(drop=True)

    geometry_version = canonical_geometry_version(parcels)
    output_columns = [
        "internal_parcel_id",
        "public_parcel_id",
        "cadastre_code",
        "area_official_m2",
        "stage",
        "preview_state",
        "household_agriculture",
        "road_excluded",
        "geometry",
    ]
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    parcels[output_columns].to_parquet(OUTPUT_PATH, index=False, compression="zstd")

    eligible = ~parcels["household_agriculture"] & ~parcels["road_excluded"]
    quality = {
        "schema_version": 1,
        "analysis_version": ANALYSIS_VERSION,
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "spatial_unit": "immutable_cadastral_parcel",
        "crs": "EPSG:4326",
        "cadastral_geometry_modified": False,
        "legacy_historical_presence_used_as_eo_filter": False,
        "legacy_250m_grid_used": False,
        "parcel_count": int(len(parcels)),
        "unique_cadastre_code_count": int(parcels["cadastre_code"].nunique()),
        "unique_internal_parcel_id_count": int(parcels["internal_parcel_id"].nunique()),
        "invalid_geometry_count": int((~parcels.geometry.is_valid).sum()),
        "household_count": int(parcels["household_agriculture"].sum()),
        "road_excluded_count": int(parcels["road_excluded"].sum()),
        "history_eligible_count": int(eligible.sum()),
        "canonical_geometry_version": geometry_version,
        "sources": {
            "cadastre": {"path": str(CADASTRE_SOURCE), "sha256": sha256(CADASTRE_SOURCE)},
            "current_activity_context": {
                "path": str(ACTIVITY_SOURCE),
                "sha256": sha256(ACTIVITY_SOURCE),
            },
        },
        "output": {"path": str(OUTPUT_PATH), "sha256": sha256(OUTPUT_PATH)},
        "public_release_approved": False,
    }
    QUALITY_PATH.write_text(
        json.dumps(quality, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return quality


if __name__ == "__main__":
    print(json.dumps(build(), ensure_ascii=False, indent=2))
