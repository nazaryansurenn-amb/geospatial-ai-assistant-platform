from __future__ import annotations

import json
from pathlib import Path

from shapely.geometry import mapping, shape


PRODUCT_ROOT = Path(__file__).resolve().parents[1]
SOURCE_DIR = PRODUCT_ROOT / "data" / "source"
OUTPUT_DIR = PRODUCT_ROOT / "public" / "data"
CANAL_SOURCE = SOURCE_DIR / "lower_hrazdan.geojson"
POINT_SOURCE = SOURCE_DIR / "lower_hrazdan_points.geojson"
EXPECTED_STAGES = {"stage_1", "stage_2"}


def load_collection(path: Path) -> dict:
    collection = json.loads(path.read_text(encoding="utf-8"))
    if collection.get("type") != "FeatureCollection" or not collection.get("features"):
        raise ValueError(f"Invalid or empty GeoJSON: {path.name}")
    return collection


def write_collection(path: Path, collection: dict) -> None:
    path.write_text(
        json.dumps(collection, ensure_ascii=False, separators=(",", ":")),
        encoding="utf-8",
    )


def main() -> None:
    canal = load_collection(CANAL_SOURCE)
    points = load_collection(POINT_SOURCE)
    stages = {feature["properties"].get("stage") for feature in canal["features"]}
    if stages != EXPECTED_STAGES or len(canal["features"]) != 2:
        raise ValueError("Lower Hrazdan must contain exactly Stage I and Stage II")

    label_features = []
    for feature in canal["features"]:
        line = shape(feature["geometry"])
        if line.is_empty or not line.is_valid:
            raise ValueError("Lower Hrazdan contains invalid line geometry")
        anchor = line.interpolate(0.5, normalized=True)
        label_features.append(
            {
                "type": "Feature",
                "properties": {
                    "stage": feature["properties"]["stage"],
                    "display_name_hy": feature["properties"]["display_name_hy"],
                },
                "geometry": mapping(anchor),
            }
        )

    if len(points["features"]) != 70:
        raise ValueError("Unexpected Lower Hrazdan divider-point count")
    if {
        feature["properties"].get("nearest_stage") for feature in points["features"]
    } != EXPECTED_STAGES:
        raise ValueError("Divider points are not assigned to both canal stages")

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    write_collection(OUTPUT_DIR / "lower_hrazdan.geojson", canal)
    write_collection(OUTPUT_DIR / "lower_hrazdan_points.geojson", points)
    write_collection(
        OUTPUT_DIR / "lower_hrazdan_labels.geojson",
        {"type": "FeatureCollection", "features": label_features},
    )
    print("Prepared 2 Lower Hrazdan stages and 70 divider points")


if __name__ == "__main__":
    main()
