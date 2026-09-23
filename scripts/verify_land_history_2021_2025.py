from __future__ import annotations

import json
from pathlib import Path

import numpy as np
from PIL import Image


PRODUCT_ROOT = Path(__file__).resolve().parents[1]
HISTORY_PATH = (
    PRODUCT_ROOT / "server_data" / "review" / "land_history_2021_2025_preview.json"
)
ACTIVITY_PATH = (
    PRODUCT_ROOT / "server_data" / "review" / "land_activity_2026_preview.json"
)
GRID_PATH = PRODUCT_ROOT / "data" / "analysis" / "eo_grid_metadata_2026.json"
EXPECTED_VERSION = "lower_hrazdan_land_history_v1_4_2021_2025"
EXPECTED_CURRENT_PARCELS = 43_984
EXPECTED_CLASSES = {
    "stable_active",
    "periodic",
    "stable_no_activity",
    "insufficient",
}
EXPECTED_RECORD_CLASSES = EXPECTED_CLASSES | {"not_calculated"}


def verify() -> dict[str, object]:
    payload = json.loads(HISTORY_PATH.read_text(encoding="utf-8"))
    activity = json.loads(ACTIVITY_PATH.read_text(encoding="utf-8"))
    grid = json.loads(GRID_PATH.read_text(encoding="utf-8"))
    assert payload["analysis_version"] == EXPECTED_VERSION
    assert payload["period"] == [2021, 2025]
    assert payload["cadastral_geometry_modified"] is False
    assert payload["legacy_250m_grid_used"] is False
    assert payload["public_release_approved"] is False
    assert set(payload["class_colors"]) == EXPECTED_CLASSES

    activity_rows = {
        row[1]: row for row in activity["records"]
    }
    assert len(activity_rows) == EXPECTED_CURRENT_PARCELS
    eligible_codes = {
        code
        for code, row in activity_rows.items()
        if not bool(row[6]) and not bool(row[8])
    }
    records = payload["records"]
    assert len(records) == len(eligible_codes)
    assert len({row[1] for row in records}) == len(records)
    assert {row[1] for row in records} == eligible_codes
    assert all(row[3] in EXPECTED_RECORD_CLASSES for row in records)
    assert all(len(row[4]) == 5 for row in records)
    assert all(all(int(value) in {0, 1, 2, 3} for value in row[4]) for row in records)
    assert all(0 <= int(row[5]) <= 5 for row in records)
    assert all(0 <= int(row[6]) <= 5 for row in records)
    assert all(0 <= int(row[7]) <= 5 for row in records)
    assert all(
        (row[3] == "not_calculated") == (int(row[7]) == 0)
        for row in records
    )
    assert all(int(row[7]) > 0 for row in records if row[3] == "insufficient")

    total = payload["summaries"]["lower_hrazdan"]
    assert total["eligible_parcel_count"] == len(records)
    assert sum(total["class_counts"].values()) == len(records)
    assert total["class_counts"]["not_calculated"] == total["without_source_history_count"]
    assert total["processed_parcel_count"] == len(records)
    assert total["without_source_history_count"] == 0
    assert (
        total["automatic_classified_count"] + total["review_count"]
        == len(records)
    )
    assert (
        payload["summaries"]["stage_1"]["eligible_parcel_count"]
        + payload["summaries"]["stage_2"]["eligible_parcel_count"]
        == len(records)
    )

    expected_size = (int(grid["width"]), int(grid["height"]))
    for history_class, scopes in payload["class_rasters"].items():
        assert history_class in EXPECTED_CLASSES
        for url in scopes.values():
            path = PRODUCT_ROOT / "public" / url.removeprefix("/")
            assert path.exists()
            with Image.open(path) as image:
                assert image.mode == "RGBA"
                assert image.size == expected_size
    assert "not_calculated" not in payload["class_rasters"]

    for scope in ("lower_hrazdan", "stage_1", "stage_2"):
        masks = []
        for history_class in EXPECTED_CLASSES:
            url = payload["class_rasters"][history_class][scope]
            path = PRODUCT_ROOT / "public" / url.removeprefix("/")
            masks.append(np.asarray(Image.open(path).convert("RGBA"))[:, :, 3] > 0)
        overlap = np.sum(np.stack(masks, axis=0), axis=0)
        assert int((overlap > 1).sum()) == 0

    return {
        "status": "passed",
        "analysis_version": payload["analysis_version"],
        "eligible_parcel_count": len(records),
        "source_history_parcel_count": total["source_history_parcel_count"],
        "without_source_history_count": total["without_source_history_count"],
        "class_counts": total["class_counts"],
    }


if __name__ == "__main__":
    print(json.dumps(verify(), ensure_ascii=False, indent=2))
