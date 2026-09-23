from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

import geopandas as gpd
import pandas as pd
from shapely import make_valid, union_all


PRODUCT_ROOT = Path(__file__).resolve().parents[1]

SOURCE_FOOTPRINTS = (
    PRODUCT_ROOT / "data" / "source" / "halo_inputs"
    / "lower_hrazdan_command_footprints.geojson"
)
SOURCE_PARCELS = PRODUCT_ROOT / "data" / "source" / "cadastre" / "parcels_wua.gpkg"

PUBLIC_OUTPUT = PRODUCT_ROOT / "public" / "data" / "lower_hrazdan_observed_zones.geojson"
MEMBERSHIP_OUTPUT = PRODUCT_ROOT / "server_data" / "lower_hrazdan_zone_membership.parquet"
REVIEW_OUTPUT = (
    PRODUCT_ROOT
    / "server_data"
    / "review"
    / "lower_hrazdan_zone_excluded_components.gpkg"
)
METADATA_OUTPUT = PRODUCT_ROOT / "server_data" / "lower_hrazdan_zone_metadata.json"

ANALYSIS_VERSION = "lower_hrazdan_observed_stages_v1_2026"
METRIC_CRS = "EPSG:32638"
SOURCE_SCOPE = "ever_contracted_2010_2025"
GAP_CLOSE_M = 50.0
MIN_NEAR_COMPONENT_HA = 5.0
MAX_NEAR_COMPONENT_GAP_M = 250.0
MAJORITY_OVERLAP_RATIO = 0.5
DISPLAY_GAP_CLOSE_M = 3.0
DISPLAY_SIMPLIFY_M = 2.0

# These three legacy labels are owner-confirmed aliases for two public stages.
LEGACY_NAME_MAP = {
    "Ստորին Հրազդան II հերթ, I տեղամաս": "lower_hrazdan_stage_1",
    "Ստորին Հրազդան I հերթ, I տեղամաս": "lower_hrazdan_stage_1",
    "Ստորին Հրազդան II հերթ, II տեղամաս": "lower_hrazdan_stage_2",
}

STAGES = {
    "lower_hrazdan_stage_1": {
        "source_stage_key": "lower_hrazdan_stage_2_section_1",
        "name_hy": "Ստորին Հրազդան I հերթ",
        "name_ru": "Нижний Раздан, I очередь",
        "name_en": "Lower Hrazdan, Stage I",
        "zone_name_hy": "I հերթի դիտարկվող գոտի",
        "color": "#16c7dd",
    },
    "lower_hrazdan_stage_2": {
        "source_stage_key": "lower_hrazdan_stage_2_section_2",
        "name_hy": "Ստորին Հրազդան II հերթ",
        "name_ru": "Нижний Раздан, II очередь",
        "name_en": "Lower Hrazdan, Stage II",
        "zone_name_hy": "II հերթի դիտարկվող գոտի",
        "color": "#f2a81d",
    },
}


def stable_internal_id(cadastre_code: str) -> str:
    digest = hashlib.sha256(cadastre_code.encode("utf-8")).hexdigest()[:20]
    return f"am_cad_{digest}"


def clean_source_geometry(geometry):
    valid = make_valid(geometry)
    closed = make_valid(valid.buffer(GAP_CLOSE_M).buffer(-GAP_CLOSE_M))
    components = list(closed.geoms) if hasattr(closed, "geoms") else [closed]
    components = sorted(components, key=lambda item: item.area, reverse=True)
    main = components[0]

    kept = []
    excluded = []
    for index, component in enumerate(components):
        area_ha = component.area / 10_000
        distance_m = component.distance(main)
        keep = index == 0 or (
            area_ha >= MIN_NEAR_COMPONENT_HA
            and distance_m <= MAX_NEAR_COMPONENT_GAP_M
        )
        record = {
            "component_rank": index + 1,
            "component_area_ha": area_ha,
            "distance_to_main_m": distance_m,
            "review_reason": "kept_main_or_near_component" if keep else "isolated_or_small_component",
            "geometry": component,
        }
        (kept if keep else excluded).append(record)

    return make_valid(union_all([item["geometry"] for item in kept])), excluded


def parcel_candidates(parcels: gpd.GeoDataFrame, stage_id: str, zone_geometry):
    positions = parcels.sindex.query(zone_geometry, predicate="intersects")
    candidates = parcels.iloc[positions].copy()
    if candidates.empty:
        return candidates

    parcel_areas = candidates.geometry.area
    intersection_areas = candidates.geometry.intersection(zone_geometry).area
    candidates["overlap_ratio"] = (intersection_areas / parcel_areas).clip(0, 1)
    candidates["representative_point_inside"] = candidates.geometry.centroid.within(
        zone_geometry
    )
    candidates = candidates[
        candidates["representative_point_inside"]
        | (candidates["overlap_ratio"] > MAJORITY_OVERLAP_RATIO)
    ].copy()
    candidates["stage_id"] = stage_id
    return candidates


def main() -> None:
    PUBLIC_OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    MEMBERSHIP_OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    REVIEW_OUTPUT.parent.mkdir(parents=True, exist_ok=True)

    footprints = gpd.read_file(SOURCE_FOOTPRINTS).to_crs(METRIC_CRS)
    parcels = gpd.read_file(
        SOURCE_PARCELS,
        columns=["cadastre_code", "area_official_m2", "geometry"],
    ).to_crs(METRIC_CRS)

    if parcels["cadastre_code"].isna().any() or not parcels["cadastre_code"].is_unique:
        raise ValueError("Cadastral code must be present and unique")
    if (~parcels.geometry.is_valid).any() or parcels.geometry.is_empty.any():
        raise ValueError("Cadastral geometry must be valid and non-empty")

    cleaned_source_zones = {}
    review_records = []
    for stage_id, stage in STAGES.items():
        source = footprints[
            (footprints["scope"] == SOURCE_SCOPE)
            & (footprints["stage_key"] == stage["source_stage_key"])
        ]
        if len(source) != 1:
            raise ValueError(f"Expected one source footprint for {stage_id}, found {len(source)}")
        cleaned, excluded = clean_source_geometry(source.geometry.iloc[0])
        cleaned_source_zones[stage_id] = cleaned
        for record in excluded:
            review_records.append(
                {
                    "stage_id": stage_id,
                    "analysis_version": ANALYSIS_VERSION,
                    **record,
                }
            )

    candidates = pd.concat(
        [
            parcel_candidates(parcels, stage_id, zone)
            for stage_id, zone in cleaned_source_zones.items()
        ],
        ignore_index=True,
    )
    if candidates.empty:
        raise ValueError("No cadastral parcels matched the cleaned source zones")

    stage_options = (
        candidates.groupby("cadastre_code")["stage_id"]
        .agg(lambda values: ";".join(sorted(set(values))))
        .rename("candidate_stage_ids")
    )
    candidates = candidates.join(stage_options, on="cadastre_code")
    candidates["assignment_review"] = candidates["candidate_stage_ids"].str.contains(";")
    candidates = candidates.sort_values(
        ["cadastre_code", "overlap_ratio", "representative_point_inside", "stage_id"],
        ascending=[True, False, False, True],
    ).drop_duplicates("cadastre_code", keep="first")

    candidates["internal_parcel_id"] = candidates["cadastre_code"].map(stable_internal_id)
    candidates["selection_rule"] = candidates.apply(
        lambda row: (
            "centroid_inside"
            if row["representative_point_inside"]
            else "majority_area_overlap"
        ),
        axis=1,
    )
    candidates["analysis_version"] = ANALYSIS_VERSION

    public_features = []
    stage_summaries = {}
    for stage_id, stage in STAGES.items():
        selected = candidates[candidates["stage_id"] == stage_id]
        if selected.empty:
            raise ValueError(f"No final cadastral parcels assigned to {stage_id}")
        final_geometry = make_valid(union_all(selected.geometry.to_numpy()))
        display_geometry = make_valid(
            final_geometry.buffer(DISPLAY_GAP_CLOSE_M)
            .buffer(-DISPLAY_GAP_CLOSE_M)
            .simplify(DISPLAY_SIMPLIFY_M, preserve_topology=True)
        )
        official_area_ha = selected["area_official_m2"].sum() / 10_000
        geometry_area_ha = final_geometry.area / 10_000
        display_area_delta_pct = (display_geometry.area / final_geometry.area - 1) * 100
        stage_summaries[stage_id] = {
            "parcel_count": int(len(selected)),
            "official_area_ha": round(float(official_area_ha), 3),
            "geometry_area_ha": round(float(geometry_area_ha), 3),
            "display_area_delta_pct": round(float(display_area_delta_pct), 3),
            "ambiguous_assignment_count": int(selected["assignment_review"].sum()),
        }
        public_features.append(
            {
                "stage_id": stage_id,
                "name_hy": stage["zone_name_hy"],
                "name_ru": f"Наблюдаемая зона, {stage['name_ru']}",
                "name_en": f"Observed zone, {stage['name_en']}",
                "color": stage["color"],
                "parcel_count": int(len(selected)),
                "official_area_ha": round(float(official_area_ha), 2),
                "analysis_version": ANALYSIS_VERSION,
                "legal_boundary": False,
                "hydraulic_service_confirmed": False,
                "geometry": display_geometry,
            }
        )

    public_gdf = gpd.GeoDataFrame(public_features, crs=METRIC_CRS).to_crs(4326)
    public_geojson = json.loads(public_gdf.to_json(drop_id=True))
    PUBLIC_OUTPUT.write_text(
        json.dumps(public_geojson, ensure_ascii=False, separators=(",", ":")),
        encoding="utf-8",
    )

    membership_columns = [
        "internal_parcel_id",
        "cadastre_code",
        "stage_id",
        "candidate_stage_ids",
        "representative_point_inside",
        "overlap_ratio",
        "selection_rule",
        "assignment_review",
        "analysis_version",
    ]
    candidates[membership_columns].sort_values("cadastre_code").to_parquet(
        MEMBERSHIP_OUTPUT,
        index=False,
        compression="zstd",
    )

    review_gdf = gpd.GeoDataFrame(review_records, crs=METRIC_CRS).to_crs(4326)
    if REVIEW_OUTPUT.exists():
        REVIEW_OUTPUT.unlink()
    review_gdf.to_file(REVIEW_OUTPUT, layer="excluded_components", driver="GPKG")

    metadata = {
        "analysis_version": ANALYSIS_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "source_scope": SOURCE_SCOPE,
        "legacy_name_map": LEGACY_NAME_MAP,
        "selection_rule": (
            "whole immutable cadastral parcel when centroid is inside cleaned source "
            "contour or overlap ratio is greater than 0.5"
        ),
        "cleaning": {
            "gap_close_m": GAP_CLOSE_M,
            "minimum_near_component_ha": MIN_NEAR_COMPONENT_HA,
            "maximum_near_component_gap_m": MAX_NEAR_COMPONENT_GAP_M,
            "excluded_components_preserved_for_review": len(review_records),
        },
        "public_display_geometry": {
            "gap_close_m": DISPLAY_GAP_CLOSE_M,
            "topology_preserving_simplify_m": DISPLAY_SIMPLIFY_M,
            "scope": "display contour only; cadastral geometry and membership remain unchanged",
        },
        "stage_summaries": stage_summaries,
        "public_interpretation": "analytical observed zone; not a legal boundary or confirmed hydraulic command area",
    }
    METADATA_OUTPUT.write_text(
        json.dumps(metadata, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    print(json.dumps(metadata, ensure_ascii=True, indent=2))


if __name__ == "__main__":
    main()
