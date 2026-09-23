"""Separate 10 m review screen; it does not relax or overwrite the first screen."""
from dataclasses import dataclass
from datetime import date

import numpy as np

from wp_core.land_consolidation import Policy as BasePolicy


@dataclass(frozen=True)
class Policy(BasePolicy):
    minimum_pure_pixels_20m: int = 0
    green_ndvi: float = .48
    green_evi2: float = .30
    low_ndvi: float = .35
    low_evi2: float = .25


def target_mask(register, policy):
    selected = register.screening_state.eq("spatial_support_review")
    selected &= register.minimum_width_m.ge(policy.minimum_width_m)
    selected &= register.pure_pixels_10m.ge(policy.minimum_pure_pixels_10m)
    selected &= register.pure_pixels_20m.eq(0)
    selected &= register.activity_class.eq("active") & register.activity_state.eq("ready")
    for field in ("household", "household_agriculture", "road_excluded", "road_excluded_release", "mask_conflict"):
        selected &= register[field].notna() & ~register[field].astype(bool)
    return selected


def mask_observations(values, fractions, policy):
    values = np.asarray(values, dtype=np.float32).copy()
    fractions = np.asarray(fractions)
    good = np.isfinite(values) & np.isfinite(fractions)
    good &= (fractions >= policy.minimum_valid_fraction) & (fractions <= 1.)
    values[~good] = np.nan
    return values


def compare_season(days, first, second, support_a, support_b, shared_10m, year, policy):
    days = np.asarray(days)
    if len(days) > 1 and not np.all(np.diff(days) > 0):
        raise ValueError("Dates must be unique and ordered")
    start = (date(year, 3, 1) - date(year, 1, 1)).days + 1
    end = (date(year, 11, 30) - date(year, 1, 1)).days + 1
    good = np.isfinite(first[:, :2]).all(axis=1) & np.isfinite(second[:, :2]).all(axis=1)
    support = np.minimum(support_a, support_b)
    good &= np.isfinite(support) & (support >= policy.minimum_valid_fraction) & (support <= 1.)
    good &= (days >= start) & (days <= end)
    # Conservative bound: all shared-pixel area could lie within the valid area.
    clean = shared_10m <= policy.maximum_shared_pixel_fraction * support
    removed = int((good & ~clean).sum())
    good &= clean
    d = days[good]
    gap = int(np.diff(np.r_[start, d, end]).max())
    result = {"year": year, "common_dates": len(d), "max_gap_days": gap,
              "shared_pixel_dates_excluded": removed, "comparable": False,
              "similar": False, "reason": "insufficient_common_dates", "context_20m": {}}
    if len(d) < policy.minimum_dates or gap > policy.maximum_gap_days:
        return result
    a, b = first[good], second[good]
    ranges = np.minimum(np.ptp(a[:, :2], axis=0), np.ptp(b[:, :2], axis=0))
    if ranges[0] < policy.minimum_ndvi_range or ranges[1] < policy.minimum_evi2_range:
        result["reason"] = "weak_seasonal_contrast"
        return result
    rms = np.sqrt(np.mean((a[:, :2] - b[:, :2]) ** 2, axis=0))
    correlations = [float(np.corrcoef(a[:, k], b[:, k])[0, 1]) for k in (0, 1)]
    green_a = (a[:, 0] >= policy.green_ndvi) & (a[:, 1] >= policy.green_evi2)
    green_b = (b[:, 0] >= policy.green_ndvi) & (b[:, 1] >= policy.green_evi2)
    low_a = (a[:, 0] <= policy.low_ndvi) & (a[:, 1] <= policy.low_evi2)
    low_b = (b[:, 0] <= policy.low_ndvi) & (b[:, 1] <= policy.low_evi2)
    phase = min(float(np.mean(green_a == green_b)), float(np.mean(low_a == low_b)))
    similar = (np.all(rms <= np.asarray(policy.maximum_rmse[:2]))
               and min(correlations) >= policy.minimum_correlation
               and phase >= policy.minimum_phase_agreement)
    result.update(comparable=True, similar=bool(similar), rmse=rms.tolist(),
                  correlations=correlations, phase_agreement=phase,
                  reason="similar" if similar else "different_profile")
    for k, name in ((2, "ndmi"), (3, "bsi")):
        valid = np.isfinite(a[:, k]) & np.isfinite(b[:, k])
        context_days = d[valid]
        context_gap = int(np.diff(np.r_[start, context_days, end]).max())
        item = {"common_dates": len(context_days), "max_gap_days": context_gap,
                "state": "insufficient_context", "rmse": None}
        if len(context_days) >= policy.minimum_dates and context_gap <= policy.maximum_gap_days:
            error = float(np.sqrt(np.mean((a[valid, k] - b[valid, k]) ** 2)))
            item.update(rmse=error, state="similar_mixed_context" if error <= policy.maximum_rmse[k] else "different_mixed_context")
        result["context_20m"][name] = item
    return result
