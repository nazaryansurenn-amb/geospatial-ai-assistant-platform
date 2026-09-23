from __future__ import annotations

import math

import geopandas as gpd
import numpy as np
import pandas as pd
import shapely


METRIC_CRS = "EPSG:32638"
MAJOR_ROAD_TYPES = {
    "motorway",
    "trunk",
    "primary",
    "secondary",
    "tertiary",
    "residential",
    "unclassified",
    "service",
}


def _minimum_rectangle_dimensions(geometry) -> tuple[float, float]:
    if geometry is None or geometry.is_empty:
        return 0.0, 0.0
    rectangle = geometry.minimum_rotated_rectangle
    coordinates = list(rectangle.exterior.coords)
    edges = [
        math.dist(coordinates[index], coordinates[index + 1]) for index in range(4)
    ]
    return max(edges), min(edges)


def _road_intersections(
    parcel_geometries: gpd.GeoSeries,
    roads: gpd.GeoDataFrame,
) -> tuple[np.ndarray, np.ndarray, np.ndarray, int]:
    parcels = np.asarray(parcel_geometries.values, dtype=object)
    road_lines = np.asarray(roads.geometry.values, dtype=object)
    road_buffers = np.asarray(
        [
            geometry.buffer(float(width) / 2.0)
            for geometry, width in zip(
                roads.geometry,
                roads["buffer_width_m"],
                strict=True,
            )
        ],
        dtype=object,
    )
    road_types = roads["highway"].astype(str).to_numpy()
    overlap_area = np.zeros(len(parcels), dtype="float64")
    centerline_length = np.zeros(len(parcels), dtype="float64")
    major_centerline_length = np.zeros(len(parcels), dtype="float64")
    pairs = shapely.STRtree(road_buffers).query(parcels, predicate="intersects")
    if pairs.size == 0:
        return overlap_area, centerline_length, major_centerline_length, 0

    order = np.argsort(pairs[0], kind="stable")
    parcel_indexes = pairs[0][order]
    road_indexes = pairs[1][order]
    starts = np.r_[0, np.flatnonzero(np.diff(parcel_indexes)) + 1]
    ends = np.r_[starts[1:], len(parcel_indexes)]

    for start, end in zip(starts, ends, strict=True):
        parcel_index = int(parcel_indexes[start])
        candidate_indexes = road_indexes[start:end]
        buffers = road_buffers[candidate_indexes]
        lines = road_lines[candidate_indexes]
        parcel = parcels[parcel_index]
        buffer_union = buffers[0] if len(buffers) == 1 else shapely.union_all(buffers)
        line_union = lines[0] if len(lines) == 1 else shapely.union_all(lines)
        overlap_area[parcel_index] = float(
            shapely.area(shapely.intersection(parcel, buffer_union))
        )
        centerline_length[parcel_index] = float(
            shapely.length(shapely.intersection(parcel.buffer(1.5), line_union))
        )

        major_indexes = candidate_indexes[
            np.isin(road_types[candidate_indexes], list(MAJOR_ROAD_TYPES))
        ]
        if len(major_indexes):
            major_lines = road_lines[major_indexes]
            major_union = (
                major_lines[0]
                if len(major_lines) == 1
                else shapely.union_all(major_lines)
            )
            major_centerline_length[parcel_index] = float(
                shapely.length(shapely.intersection(parcel.buffer(1.5), major_union))
            )

    return (
        overlap_area,
        centerline_length,
        major_centerline_length,
        int(pairs.shape[1]),
    )


def build_road_exclusion_metrics(
    parcels: gpd.GeoDataFrame,
    roads: gpd.GeoDataFrame,
) -> tuple[pd.DataFrame, dict[str, int]]:
    metric_parcels = parcels.to_crs(METRIC_CRS).copy()
    metric_roads = roads.to_crs(METRIC_CRS).copy()
    parcel_area = metric_parcels.geometry.area.to_numpy(dtype="float64")
    parcel_perimeter = metric_parcels.geometry.length.to_numpy(dtype="float64")
    dimensions = np.asarray(
        [_minimum_rectangle_dimensions(geometry) for geometry in metric_parcels.geometry],
        dtype="float64",
    )
    long_dimension = dimensions[:, 0]
    short_dimension = dimensions[:, 1]
    aspect_ratio = np.divide(
        long_dimension,
        short_dimension,
        out=np.zeros_like(long_dimension),
        where=short_dimension > 0,
    )
    effective_width = np.divide(
        parcel_area,
        long_dimension,
        out=np.zeros_like(parcel_area),
        where=long_dimension > 0,
    )
    compactness = np.divide(
        4.0 * math.pi * parcel_area,
        parcel_perimeter**2,
        out=np.zeros_like(parcel_area),
        where=parcel_perimeter > 0,
    )
    (
        road_overlap_area,
        centerline_length,
        major_centerline_length,
        candidate_pair_count,
    ) = _road_intersections(metric_parcels.geometry, metric_roads)
    road_overlap_fraction = np.divide(
        road_overlap_area,
        parcel_area,
        out=np.zeros_like(parcel_area),
        where=parcel_area > 0,
    ).clip(0, 1)
    road_alignment_ratio = np.divide(
        centerline_length,
        long_dimension,
        out=np.zeros_like(centerline_length),
        where=long_dimension > 0,
    ).clip(0, 2)
    major_road_alignment_ratio = np.divide(
        major_centerline_length,
        long_dimension,
        out=np.zeros_like(major_centerline_length),
        where=long_dimension > 0,
    ).clip(0, 2)

    major_road_shape = (
        (aspect_ratio >= 5.0)
        & (effective_width <= 40.0)
        & (compactness <= 0.15)
        & (road_overlap_fraction >= 0.10)
        & (major_road_alignment_ratio >= 0.35)
    )
    narrow_track_shape = (
        (aspect_ratio >= 12.0)
        & (effective_width <= 20.0)
        & (compactness <= 0.08)
        & (road_overlap_fraction >= 0.12)
        & (road_alignment_ratio >= 0.70)
    )
    overwhelming_road_overlap = (
        (aspect_ratio >= 4.0)
        & (effective_width <= 45.0)
        & (compactness <= 0.18)
        & (road_overlap_fraction >= 0.55)
        & (road_alignment_ratio >= 0.40)
    )

    # Road parcels are not always clean, narrow rectangles. Intersections, curved
    # rights-of-way and divided highways can be compact or wider while still being
    # dominated by the mapped road footprint.
    dominant_road_footprint = road_overlap_fraction >= 0.50
    mapped_road_corridor = (
        (aspect_ratio >= 4.0)
        & (effective_width <= 70.0)
        & (road_overlap_fraction >= 0.12)
        & (major_road_alignment_ratio >= 0.50)
    )
    narrow_aligned_corridor = (
        (aspect_ratio >= 6.0)
        & (effective_width <= 50.0)
        & (compactness <= 0.35)
        & (road_overlap_fraction >= 0.18)
        & (road_alignment_ratio >= 0.65)
    )
    road_excluded = (
        major_road_shape
        | narrow_track_shape
        | overwhelming_road_overlap
        | dominant_road_footprint
        | mapped_road_corridor
        | narrow_aligned_corridor
    )
    road_review_candidate = (
        ~road_excluded
        & (aspect_ratio >= 6.0)
        & (effective_width <= 45.0)
        & (compactness <= 0.15)
        & (road_overlap_fraction >= 0.05)
        & (road_alignment_ratio >= 0.30)
    )

    result = pd.DataFrame(
        {
            "cadastre_code": metric_parcels["cadastre_code"].astype(str).to_numpy(),
            "road_mrr_long_m": np.round(long_dimension, 3),
            "road_mrr_short_m": np.round(short_dimension, 3),
            "road_aspect_ratio": np.round(aspect_ratio, 4),
            "road_effective_width_m": np.round(effective_width, 3),
            "road_compactness": np.round(compactness, 6),
            "osm_road_overlap_m2": np.round(road_overlap_area, 3),
            "osm_road_fraction": np.round(road_overlap_fraction, 6),
            "osm_road_centerline_m": np.round(centerline_length, 3),
            "osm_road_alignment_ratio": np.round(road_alignment_ratio, 6),
            "osm_major_road_alignment_ratio": np.round(
                major_road_alignment_ratio, 6
            ),
            "dominant_road_footprint": dominant_road_footprint,
            "mapped_road_corridor": mapped_road_corridor,
            "narrow_aligned_corridor": narrow_aligned_corridor,
            "road_excluded": road_excluded,
            "road_review_candidate": road_review_candidate,
        }
    )
    return result, {
        "road_feature_count": int(len(metric_roads)),
        "spatial_candidate_pair_count": candidate_pair_count,
        "road_excluded_count": int(road_excluded.sum()),
        "road_review_candidate_count": int(road_review_candidate.sum()),
        "dominant_road_footprint_count": int(dominant_road_footprint.sum()),
        "mapped_road_corridor_count": int(mapped_road_corridor.sum()),
        "narrow_aligned_corridor_count": int(narrow_aligned_corridor.sum()),
    }
