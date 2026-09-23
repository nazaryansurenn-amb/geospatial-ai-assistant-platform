from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
from PIL import Image


PRODUCT_ROOT = Path(__file__).resolve().parents[1]
PAYLOAD_PATH = PRODUCT_ROOT / "server_data" / "review" / "land_activity_2026_preview.json"
QUALITY_PATH = (
    PRODUCT_ROOT / "server_data" / "review" / "land_activity_2026_preview_quality.json"
)
GRID_METADATA_PATH = (
    PRODUCT_ROOT / "data" / "source" / "activity_2026"
    / "lower_hrazdan_eo_terrain_grid_2026.json"
)
PREVIOUS_LAND_REVIEW_PATH = (
    PRODUCT_ROOT / "data" / "source" / "activity_2026"
    / "parcel_land_review.parquet"
)
EXPECTED_VERSION = "lower_hrazdan_activity_road_exclusions_v3_5_2026"
EXPECTED_TOTAL = 43_984
EXPECTED_STAGE_COUNTS = {"stage_1": 21_324, "stage_2": 22_660}
REQUIRED_REVIEW_CODES = {"04-006-0286-0008", "04-055-0118-0001"}
HOUSEHOLD_REGRESSION_CODE = "04-087-0020-0022"
ROAD_REGRESSION_CODE = "04-051-0234-0001"
ADDITIONAL_ROAD_REGRESSION_CODES = {
    "04-004-1046-0001",
    "04-002-1401-0001",
    "04-055-0721-0001",
}


def verify() -> dict[str, object]:
    payload = json.loads(PAYLOAD_PATH.read_text(encoding="utf-8"))
    quality = json.loads(QUALITY_PATH.read_text(encoding="utf-8"))
    grid = json.loads(GRID_METADATA_PATH.read_text(encoding="utf-8"))
    records = payload["records"]

    assert payload["analysis_version"] == EXPECTED_VERSION
    assert quality["analysis_version"] == EXPECTED_VERSION
    assert len(records) == EXPECTED_TOTAL
    assert quality["full_halo_parcel_count"] == EXPECTED_TOTAL
    assert quality["full_halo_cadastral_coverage"] is True

    parcel_ids = [row[0] for row in records]
    cadastral_codes = [row[1] for row in records]
    assert len(set(parcel_ids)) == EXPECTED_TOTAL
    assert len(set(cadastral_codes)) == EXPECTED_TOTAL
    assert all(row[5] in {"ready", "review"} for row in records)
    assert all(isinstance(row[6], bool) for row in records)
    assert all(row[7] in {"active", "partial", "no_current_activity"} for row in records)
    assert all(isinstance(row[8], bool) for row in records)

    stage_counts = {
        stage: sum(row[2] == stage for row in records) for stage in EXPECTED_STAGE_COUNTS
    }
    assert stage_counts == EXPECTED_STAGE_COUNTS
    assert (
        payload["summaries"]["lower_hrazdan"]["ready_parcel_count"]
        + payload["summaries"]["lower_hrazdan"]["review_parcel_count"]
        == EXPECTED_TOTAL
    )
    household_count = sum(row[6] for row in records)
    summary = payload["summaries"]["lower_hrazdan"]
    assert household_count > 0
    assert summary["household_parcel_count"] == household_count
    assert (
        quality["household_rule"]["extended_urban_built_rule_household_count"]
        > 0
    )
    assert (
        summary["open_field_parcel_count"] + summary["household_parcel_count"]
        + summary["activity_review_parcel_count"]
        + summary["road_excluded_parcel_count"]
        == EXPECTED_TOTAL
    )
    assert sum(summary["activity_class_counts"].values()) == summary[
        "open_field_parcel_count"
    ]

    indexed = {row[1]: row for row in records}
    previous = pd.read_parquet(
        PREVIOUS_LAND_REVIEW_PATH,
        columns=["cadastre_code", "household_agriculture"],
    )
    previous_household_codes = set(
        previous.loc[previous["household_agriculture"], "cadastre_code"].astype(str)
    )
    preserved_previous_household_codes = previous_household_codes & set(indexed)
    assert preserved_previous_household_codes
    assert all(
        indexed[code][6] or indexed[code][8]
        for code in preserved_previous_household_codes
    )
    assert all(
        indexed[code][8]
        for code in preserved_previous_household_codes
        if not indexed[code][6]
    )
    assert indexed[HOUSEHOLD_REGRESSION_CODE][6] is True
    assert indexed[ROAD_REGRESSION_CODE][8] is True
    assert indexed[ROAD_REGRESSION_CODE][6] is False
    assert all(indexed[code][8] for code in ADDITIONAL_ROAD_REGRESSION_CODES)
    for code in REQUIRED_REVIEW_CODES:
        assert code in indexed
        assert indexed[code][2] == "stage_2"
        assert indexed[code][5] == "ready"

    expected_size = (int(grid["width"]), int(grid["height"]))
    raster_urls = [
        *payload["rasters"].values(),
        *payload["review_rasters"].values(),
        *payload["household_rasters"].values(),
        *[
            url
            for class_rasters in payload["activity_class_rasters"].values()
            for url in class_rasters.values()
        ],
    ]
    for url in raster_urls:
        path = PRODUCT_ROOT / "public" / url.lstrip("/")
        assert path.exists(), path
        with Image.open(path) as image:
            assert image.size == expected_size
            assert image.mode == "RGBA"

    return {
        "status": "passed",
        "analysis_version": EXPECTED_VERSION,
        "parcel_count": EXPECTED_TOTAL,
        "stage_counts": stage_counts,
        "ready_parcel_count": payload["summaries"]["lower_hrazdan"][
            "ready_parcel_count"
        ],
        "review_parcel_count": payload["summaries"]["lower_hrazdan"][
            "review_parcel_count"
        ],
        "open_field_parcel_count": summary["open_field_parcel_count"],
        "household_parcel_count": household_count,
        "household_area_ha": summary["household_area_ha"],
        "preserved_previous_household_count": len(
            preserved_previous_household_codes
        ),
        "household_regression_code": HOUSEHOLD_REGRESSION_CODE,
        "road_regression_code": ROAD_REGRESSION_CODE,
        "additional_road_regression_codes": sorted(ADDITIONAL_ROAD_REGRESSION_CODES),
        "road_excluded_parcel_count": summary["road_excluded_parcel_count"],
        "activity_class_counts": summary["activity_class_counts"],
        "checked_example_codes": sorted(REQUIRED_REVIEW_CODES),
    }


if __name__ == "__main__":
    print(json.dumps(verify(), ensure_ascii=False, indent=2))
