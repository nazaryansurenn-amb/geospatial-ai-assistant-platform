from __future__ import annotations

import json
from pathlib import Path

from shapely.geometry import mapping, shape
from shapely.ops import unary_union


PRODUCT_ROOT = Path(__file__).resolve().parents[1]
SOURCE = PRODUCT_ROOT / "data" / "source" / "wua_areas.geojson"
OUTPUT_DIR = PRODUCT_ROOT / "public" / "data"


def feature_collection(features: list[dict]) -> dict:
    return {"type": "FeatureCollection", "features": features}


def main() -> None:
    source = json.loads(SOURCE.read_text(encoding="utf-8"))
    features = source.get("features", [])
    if not features:
        raise ValueError("The WUA community source is empty")

    geometries = [shape(feature["geometry"]) for feature in features]
    if any(geometry.is_empty or not geometry.is_valid for geometry in geometries):
        raise ValueError("The WUA community source contains invalid geometry")

    boundary = unary_union(geometries)
    labels = []
    for feature, geometry in zip(features, geometries, strict=True):
        label_point = geometry.representative_point()
        labels.append(
            {
                "type": "Feature",
                "properties": {
                    "name_hy": feature["properties"]["name_hy"],
                    "display_priority": round(float(geometry.area), 12),
                },
                "geometry": mapping(label_point),
            }
        )

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    (OUTPUT_DIR / "communities.geojson").write_text(
        json.dumps(feature_collection(features), ensure_ascii=False, separators=(",", ":")),
        encoding="utf-8",
    )
    (OUTPUT_DIR / "community_labels.geojson").write_text(
        json.dumps(feature_collection(labels), ensure_ascii=False, separators=(",", ":")),
        encoding="utf-8",
    )
    (OUTPUT_DIR / "echmiadzin_wua_boundary.geojson").write_text(
        json.dumps(
            feature_collection(
                [
                    {
                        "type": "Feature",
                        "properties": {
                            "name_hy": "Էջմիածին ջրօգտագործողների ընկերություն"
                        },
                        "geometry": mapping(boundary),
                    }
                ]
            ),
            ensure_ascii=False,
            separators=(",", ":"),
        ),
        encoding="utf-8",
    )
    print(f"Prepared {len(features)} community polygons and labels")


if __name__ == "__main__":
    main()
