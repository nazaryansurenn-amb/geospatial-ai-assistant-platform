"""Immutable-geometry shape diagnostics and narrow-strip priority filtering."""
from dataclasses import dataclass

import numpy as np


@dataclass(frozen=True)
class StripPolicy:
    minimum_length_m: float = 100.
    maximum_width_m: float = 40.
    minimum_elongation: float = 4.
    minimum_rectangularity: float = .55
    minimum_group_strip_area_fraction: float = .5


def shape_metrics(geometry, policy):
    if geometry.is_empty or not geometry.is_valid or geometry.geom_type not in ("Polygon", "MultiPolygon"):
        raise ValueError("A valid immutable polygon in metric CRS is required")
    rectangle = geometry.minimum_rotated_rectangle
    if rectangle.geom_type != "Polygon" or rectangle.area <= 0:
        raise ValueError("Degenerate parcel")
    lengths = np.linalg.norm(np.diff(np.asarray(rectangle.exterior.coords), axis=0), axis=1)
    length, width = float(lengths.max()), float(lengths.min())
    elongation = length / width
    rectangularity = float(geometry.area / rectangle.area)
    parts = 1 if geometry.geom_type == "Polygon" else len(geometry.geoms)
    strip = (parts == 1 and length >= policy.minimum_length_m and width <= policy.maximum_width_m
             and elongation >= policy.minimum_elongation and rectangularity >= policy.minimum_rectangularity)
    return {"strip_length_m": length, "strip_width_m": width, "strip_elongation": elongation,
            "strip_rectangularity": rectangularity, "geometry_parts": parts, "long_narrow": bool(strip)}


def group_priority(areas_m2, strip_flags, policy):
    areas, flags = np.asarray(areas_m2, dtype=float), np.asarray(strip_flags, dtype=bool)
    if areas.shape != flags.shape or not len(areas) or not np.isfinite(areas).all() or (areas <= 0).any():
        raise ValueError("Invalid member areas")
    fraction = float(areas[flags].sum() / areas.sum())
    return {"strip_parcel_count": int(flags.sum()), "strip_area_ha": float(areas[flags].sum()) / 10000,
            "strip_area_fraction": fraction, "has_strip_members": bool(flags.any()),
            "all_members_strips": bool(flags.all()),
            "strip_priority": fraction >= policy.minimum_group_strip_area_fraction}


def qualifies_group(areas_m2, maximum_member_m2=10000., minimum_group_ha=3.):
    areas = np.asarray(areas_m2, dtype=float)
    return bool(len(areas) >= 2 and np.isfinite(areas).all() and (areas > 0).all()
                and (areas <= maximum_member_m2).all() and areas.sum() >= minimum_group_ha * 10000)
