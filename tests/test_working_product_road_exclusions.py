from __future__ import annotations

import sys
from pathlib import Path

import geopandas as gpd
from shapely.geometry import LineString, box


SCRIPT_DIR = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from road_exclusions import build_road_exclusion_metrics


def _roads(geometry, highway: str = "trunk", width: float = 10.0):
    return gpd.GeoDataFrame(
        {"highway": [highway], "buffer_width_m": [width]},
        geometry=[geometry],
        crs="EPSG:32638",
    )


def test_compact_parcel_dominated_by_road_is_excluded() -> None:
    parcels = gpd.GeoDataFrame(
        {"cadastre_code": ["road-intersection"]},
        geometry=[box(0, -5, 10, 5)],
        crs="EPSG:32638",
    )
    metrics, _ = build_road_exclusion_metrics(
        parcels,
        _roads(LineString([(-20, 0), (30, 0)])),
    )

    assert bool(metrics.loc[0, "dominant_road_footprint"])
    assert bool(metrics.loc[0, "road_excluded"])


def test_wide_field_crossed_by_road_is_not_excluded() -> None:
    parcels = gpd.GeoDataFrame(
        {"cadastre_code": ["field"]},
        geometry=[box(0, 0, 200, 100)],
        crs="EPSG:32638",
    )
    metrics, _ = build_road_exclusion_metrics(
        parcels,
        _roads(LineString([(-10, 50), (210, 50)])),
    )

    assert not bool(metrics.loc[0, "road_excluded"])


def test_aligned_highway_corridor_is_excluded() -> None:
    parcels = gpd.GeoDataFrame(
        {"cadastre_code": ["highway-corridor"]},
        geometry=[box(0, -20, 200, 20)],
        crs="EPSG:32638",
    )
    metrics, _ = build_road_exclusion_metrics(
        parcels,
        _roads(LineString([(-10, 0), (210, 0)])),
    )

    assert bool(metrics.loc[0, "mapped_road_corridor"])
    assert bool(metrics.loc[0, "road_excluded"])
