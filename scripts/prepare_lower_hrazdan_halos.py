from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import geopandas as gpd
from shapely import make_valid
from shapely.geometry import MultiPolygon, Polygon


PRODUCT_ROOT = Path(__file__).resolve().parents[1]
SOURCE_PATH = (
    PRODUCT_ROOT / "data" / "source" / "halo_inputs"
    / "lower_hrazdan_command_footprints.geojson"
)
PUBLIC_OUTPUT = PRODUCT_ROOT / "public" / "data" / "lower_hrazdan_halos.geojson"
METADATA_OUTPUT = PRODUCT_ROOT / "server_data" / "lower_hrazdan_halos_metadata.json"

METRIC_CRS = "EPSG:32638"
SOURCE_SCOPE = "ever_contracted_2010_2025"
GAP_CLOSE_M = 100.0
SIMPLIFY_M = 8.0
ANALYSIS_VERSION = "lower_hrazdan_halos_v1_2026"

# The two approved analytical outlines are built independently from their source branches.
RELEASED_HALOS = {
    "lower_hrazdan_stage_1": {
        "stage": "stage_1",
        "source_stage_keys": [
            "lower_hrazdan_stage_2_section_1",
            "lower_hrazdan_stage_2_section_2",
        ],
        "confirmed_legacy_names_hy": [
            "Ստորին Հրազդան II հերթ, I տեղամաս",
            "Ստորին Հրազդան II հերթ, II տեղամաս",
        ],
        "name_hy": "Ստորին Հրազդան I հերթի տարածքի ուրվագիծ",
        "name_ru": "Контур территории I очереди Нижнего Раздана",
        "name_en": "Lower Hrazdan Stage I area outline",
        "color": "#16c7dd",
    },
    "lower_hrazdan_stage_2": {
        "stage": "stage_2",
        "source_stage_keys": ["lower_hrazdan_stage_2_khoy"],
        "confirmed_legacy_names_hy": [
            "Ստորին Հրազդան II հերթ, Խոյ ՋՕԸ",
        ],
        "name_hy": "Ստորին Հրազդան II հերթի տարածքի ուրվագիծ",
        "name_ru": "Контур территории II очереди Нижнего Раздана",
        "name_en": "Lower Hrazdan Stage II area outline",
        "color": "#f2a81d",
    },
}


def exterior_only(geometry):
    parts = list(geometry.geoms) if hasattr(geometry, "geoms") else [geometry]
    polygons = [Polygon(part.exterior) for part in parts if isinstance(part, Polygon)]
    if len(polygons) == 1:
        return polygons[0]
    return MultiPolygon(polygons)


def main() -> None:
    PUBLIC_OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    METADATA_OUTPUT.parent.mkdir(parents=True, exist_ok=True)

    source = gpd.read_file(SOURCE_PATH).to_crs(METRIC_CRS)
    features = []
    summaries = {}
    stage_one_geometry = None

    for halo_id, config in RELEASED_HALOS.items():
        row = source[
            (source["scope"] == SOURCE_SCOPE)
            & (source["stage_key"].isin(config["source_stage_keys"]))
        ]
        if len(row) != len(config["source_stage_keys"]):
            raise ValueError(
                f"Expected {len(config['source_stage_keys'])} source footprints "
                f"for {halo_id}, found {len(row)}"
            )

        source_geometries = [make_valid(geometry) for geometry in row.geometry]
        source_geometry = source_geometries[0]
        for geometry in source_geometries[1:]:
            source_geometry = make_valid(source_geometry.union(geometry))
        connected = make_valid(
            source_geometry.buffer(GAP_CLOSE_M).buffer(-GAP_CLOSE_M)
        )
        components = list(connected.geoms) if hasattr(connected, "geoms") else [connected]
        components = sorted(components, key=lambda item: item.area, reverse=True)
        main_component = components[0]
        halo_geometry = make_valid(
            exterior_only(main_component).simplify(SIMPLIFY_M, preserve_topology=True)
        )

        overlap_removed_ha = 0.0
        if config["stage"] == "stage_1":
            stage_one_geometry = halo_geometry
        elif stage_one_geometry is not None:
            overlap_removed_ha = halo_geometry.intersection(stage_one_geometry).area / 10_000
            halo_geometry = make_valid(halo_geometry.difference(stage_one_geometry))

        summaries[halo_id] = {
            "source_stage_keys": config["source_stage_keys"],
            "source_area_ha": round(float(source_geometry.area / 10_000), 3),
            "halo_area_ha": round(float(halo_geometry.area / 10_000), 3),
            "source_component_count_after_closing": len(components),
            "excluded_component_count": len(components) - 1,
            "overlap_removed_ha": round(float(overlap_removed_ha), 3),
        }
        features.append(
            {
                "halo_id": halo_id,
                "stage": config["stage"],
                "name_hy": config["name_hy"],
                "name_ru": config["name_ru"],
                "name_en": config["name_en"],
                "color": config["color"],
                "analysis_version": ANALYSIS_VERSION,
                "legal_boundary": False,
                "hydraulic_service_confirmed": False,
                "geometry": halo_geometry,
            }
        )

    public = gpd.GeoDataFrame(features, crs=METRIC_CRS).to_crs(4326)
    PUBLIC_OUTPUT.write_text(
        json.dumps(
            json.loads(public.to_json(drop_id=True)),
            ensure_ascii=False,
            separators=(",", ":"),
        ),
        encoding="utf-8",
    )

    metadata = {
        "analysis_version": ANALYSIS_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "source_scope": SOURCE_SCOPE,
        "released_halo_ids": list(RELEASED_HALOS),
        "confirmed_legacy_name_groups": {
            halo_id: config["confirmed_legacy_names_hy"]
            for halo_id, config in RELEASED_HALOS.items()
        },
        "gap_close_m": GAP_CLOSE_M,
        "topology_preserving_simplify_m": SIMPLIFY_M,
        "overlap_rule": "Stage I is preserved; any Stage II overlap is removed from Stage II",
        "construction": "union of approved source footprints, main connected component, exterior contour only",
        "public_payload": "halo geometry and public labels only",
        "public_interpretation": "analytical outline; not a legal boundary or confirmed hydraulic command area",
        "summaries": summaries,
    }
    METADATA_OUTPUT.write_text(
        json.dumps(metadata, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(json.dumps(metadata, ensure_ascii=True, indent=2))


if __name__ == "__main__":
    main()
