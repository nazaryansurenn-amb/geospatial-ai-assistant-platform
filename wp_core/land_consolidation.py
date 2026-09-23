"""Private, deterministic screening of neighboring active cadastral parcels.

Groups are assessment candidates, not ownership, common management or feasibility.
No source geometry, activity class or crop classification is changed.
"""
from dataclasses import dataclass
from datetime import date
import hashlib

import numpy as np
import shapely


INDICES = ("ndvi", "evi2", "ndmi", "bsi")


@dataclass(frozen=True)
class Policy:
    minimum_area_ha: float = 5.
    minimum_boundary_m: float = 10.
    topology_tolerance_m: float = .25
    maximum_sliver_area_fraction: float = .01
    minimum_years: int = 3
    minimum_dates: int = 16
    maximum_gap_days: int = 35
    minimum_valid_fraction: float = .6
    maximum_shared_pixel_fraction: float = .2
    minimum_correlation: float = .8
    maximum_rmse: tuple = (.10, .08, .08, .10)
    minimum_phase_agreement: float = .8
    minimum_ndvi_range: float = .15
    minimum_evi2_range: float = .12
    minimum_width_m: float = 10.
    minimum_pure_pixels_10m: int = 3
    minimum_pure_pixels_20m: int = 1
    canal_buffer_m: float = 3.
    interior_buffer_m: float = 5.


def negligible_overlap(a, b, policy, a_band=None, b_band=None):
    intersection = a.intersection(b)
    if intersection.area <= .01:
        return True
    if intersection.area / min(a.area, b.area) > policy.maximum_sliver_area_fraction:
        return False
    a_band = a.boundary.buffer(policy.topology_tolerance_m) if a_band is None else a_band
    b_band = b.boundary.buffer(policy.topology_tolerance_m) if b_band is None else b_band
    return a_band.covers(intersection) and b_band.covers(intersection)


def shared_boundary(a, b, barriers, policy, a_band=None, b_band=None):
    if not negligible_overlap(a, b, policy, a_band, b_band):
        return 0., "overlapping_geometry"
    b_band = b.boundary.buffer(policy.topology_tolerance_m) if b_band is None else b_band
    edge = a.boundary.intersection(b_band)
    if edge.length < policy.minimum_boundary_m:
        return float(edge.length), "no_meaningful_boundary"
    if isinstance(barriers, shapely.STRtree):
        indexes = barriers.query(edge, predicate="intersects")
        local = shapely.union_all(barriers.geometries[indexes])
    else:
        local = barriers
    usable = edge.difference(local)
    if usable.length < policy.minimum_boundary_m:
        return float(usable.length), "mapped_barrier"
    return float(usable.length), "adjacent"


def shared_pixels(first, second):
    """Fraction of each parcel sampled by pixels that also sample its neighbor."""
    pa, wa = first
    pb, wb = second
    _, ia, ib = np.intersect1d(pa, pb, assume_unique=True, return_indices=True)
    if wa.sum() <= 0 or wb.sum() <= 0:
        return 1.
    return float(max(wa[ia].sum() / wa.sum(), wb[ib].sum() / wb.sum()))


def compare_season(days, first, second, year, policy):
    """Compare actual common dates only; never interpolate or shift calendars."""
    start = (date(year, 3, 1) - date(year, 1, 1)).days + 1
    end = (date(year, 11, 30) - date(year, 1, 1)).days + 1
    good = np.isfinite(first).all(axis=1) & np.isfinite(second).all(axis=1)
    good &= (days >= start) & (days <= end)
    d = days[good]
    gap = int(np.diff(np.r_[start, d, end]).max())
    result = {"year": year, "common_dates": int(good.sum()), "max_gap_days": gap,
              "comparable": False, "similar": False, "reason": "insufficient_common_dates"}
    if len(d) < policy.minimum_dates or gap > policy.maximum_gap_days:
        return result
    a, b = first[good], second[good]
    # A shared flat/bare profile cannot establish similar cultivation.
    ranges = np.minimum(np.ptp(a[:, :2], axis=0), np.ptp(b[:, :2], axis=0))
    if ranges[0] < policy.minimum_ndvi_range or ranges[1] < policy.minimum_evi2_range:
        result["reason"] = "weak_seasonal_contrast"
        return result
    rms = np.sqrt(np.mean((a - b) ** 2, axis=0))
    correlations = [float(np.corrcoef(a[:, k], b[:, k])[0, 1]) for k in (0, 1)]
    green_a = (a[:, 0] >= .48) & (a[:, 1] >= .30)
    green_b = (b[:, 0] >= .48) & (b[:, 1] >= .30)
    bare_a = (a[:, 0] <= .35) & (a[:, 3] > 0)
    bare_b = (b[:, 0] <= .35) & (b[:, 3] > 0)
    phase = min(float(np.mean(green_a == green_b)), float(np.mean(bare_a == bare_b)))
    similar = (np.all(rms <= np.asarray(policy.maximum_rmse))
               and min(correlations) >= policy.minimum_correlation
               and phase >= policy.minimum_phase_agreement)
    result.update(comparable=True, similar=bool(similar), reason="similar" if similar else "different_profile",
                  rmse=rms.tolist(), correlations=correlations, phase_agreement=phase)
    return result


def connected_complete_link(ids, edges, compare, policy):
    """Deterministic disjoint grouping: adjacency plus all-pair, same-year agreement.

Unlike connected components, A~B and B~C cannot silently imply A~C. The
greedy partition is reproducible but is not an optimization of land allocation.
"""
    groups = {i: {i} for i in range(len(ids))}
    owners = list(range(len(ids)))
    year_masks = {i: 31 for i in range(len(ids))}
    for a, b, length in sorted(edges, key=lambda e: (-e[2], ids[e[0]], ids[e[1]])):
        left, right = owners[a], owners[b]
        if left == right:
            continue
        mask = year_masks[left] & year_masks[right]
        for i in sorted(groups[left]):
            for j in sorted(groups[right]):
                mask &= compare(i, j)
                if mask.bit_count() < policy.minimum_years:
                    break
            if mask.bit_count() < policy.minimum_years:
                break
        if mask.bit_count() < policy.minimum_years:
            continue
        merged = groups[left] | groups.pop(right)
        groups[left] = merged
        year_masks[left] = mask
        year_masks.pop(right)
        for i in merged:
            owners[i] = left
    return [(sorted(members), year_masks[key]) for key, members in sorted(groups.items())]


def block_id(ids):
    return "block_" + hashlib.sha256("\n".join(sorted(ids)).encode()).hexdigest()[:20]


def union_geometry(geometries):
    return shapely.union_all(geometries)
