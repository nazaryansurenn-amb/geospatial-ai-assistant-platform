"""Deterministic completed-season EO screening, not a crop ground-truth model.

No dependency on current-year activity, old crop classes or a coarse grid.
Every unassessable season remains explicit; missing evidence never votes.
"""
from __future__ import annotations

from dataclasses import dataclass
import numpy as np


@dataclass(frozen=True)
class Policy:
    minimum_years: int = 3
    minimum_observations: int = 16
    valid_fraction: float = 0.60
    maximum_gap_days: int = 35
    green_ndvi: float = 0.48
    green_fraction: float = 0.45
    reset_ndvi: float = 0.30
    reset_bare: float = 0.45
    harvest_drop: float = 0.20
    minimum_growth_days: int = 25
    growth_end_lookback_days: int = 60
    minimum_perennial_span_days: int = 150


POLICY = Policy()
VERSION = "predominant_use_2021_2025_v2"


def aggregate_years(types, cycles, covered, policy=POLICY):
    types = np.asarray(types, dtype=np.int16)
    cycles = np.asarray(cycles, dtype=np.int16)
    covered = np.asarray(covered, dtype=bool)
    if types.shape != cycles.shape or types.shape != covered.shape or types.shape[-1] != 5:
        raise ValueError("Exactly five aligned completed years are required")
    assessed = covered.sum(axis=-1)
    annual = ((types == 1) & covered).sum(axis=-1)
    perennial = ((types == 2) & covered).sum(axis=-1)
    result = np.full(assessed.shape, "undetermined", dtype=object)
    enough = assessed >= policy.minimum_years
    result[enough & (annual * 2 > assessed)] = "annual"
    result[enough & (perennial * 2 > assessed)] = "perennial"
    eligible = (types == 1) & covered & np.isin(cycles, [1, 2])
    cycle_years = eligible.sum(axis=-1)
    doubles = (eligible & (cycles == 2)).sum(axis=-1)
    cycle_result = np.full(assessed.shape, "", dtype=object)
    usable_annual = (result == "annual") & (cycle_years * 2 > annual)
    cycle_result[usable_annual] = "single_cycle"
    cycle_result[usable_annual & (doubles * 2 > cycle_years)] = "two_cycle_recurring"
    result[(result == "annual") & ~usable_annual] = "undetermined"
    return result, cycle_result, assessed, cycle_years


def classify_season(days, ndvi, ndmi, bsi, vegetation, bare, water, valid, pixels, policy=POLICY):
    """Arrays are parcel x date; day-of-year is shared. No temporal filling.

    The harvest/reset detector requires both optical greenness loss and exposed
    soil, followed by sustained growth. Broad simultaneous canopy cover gives a
    conservative lower bound for spatial overlap of two peaks in the parcel.
    It still cannot independently establish actual sowing or woody structure.
    """
    days = np.asarray(days)
    ndvi, ndmi, bsi, vegetation, bare, water, valid, pixels = [
        np.asarray(a, dtype=float) for a in (ndvi, ndmi, bsi, vegetation, bare, water, valid, pixels)]
    if ndvi.ndim != 2 or ndvi.shape[1] != len(days) or np.any(np.diff(days) <= 0):
        raise ValueError("One observation per increasing day is required")
    if any(a.shape != ndvi.shape for a in (ndmi, bsi, vegetation, bare, water, valid, pixels)):
        raise ValueError("Indicator matrices are not aligned")
    good = (np.isfinite(ndvi) & np.isfinite(ndmi) & np.isfinite(bsi)
            & (valid >= policy.valid_fraction) & (pixels >= 3) & (water < 0.2))
    growing = (days >= 60) & (days <= 335)
    good_growing = good & growing
    n = good_growing.sum(axis=1)
    first = np.where(good_growing, days, 999).min(axis=1)
    last = np.where(good_growing, days, -999).max(axis=1)
    previous = np.full(len(ndvi), -1, dtype=int)
    max_gap = np.zeros(len(ndvi), dtype=int)
    for j, day in enumerate(days):
        present = good_growing[:, j]
        gap = np.where(present & (previous >= 0), day - previous, 0)
        max_gap = np.maximum(max_gap, gap)
        previous[present] = day
    covered = ((n >= policy.minimum_observations) & (first <= 100) & (last >= 300)
               & (max_gap <= policy.maximum_gap_days))
    context = good & (days <= 335)
    high = context & (ndvi >= policy.green_ndvi) & (vegetation >= policy.green_fraction)
    reset = context & (ndvi <= policy.reset_ndvi) & (bare >= policy.reset_bare) & (bsi > 0)
    event_counts = np.zeros(len(ndvi), dtype=np.int16)
    event_end = np.full(len(ndvi), -999, dtype=int)
    first_start = np.full(len(ndvi), -999, dtype=int)
    spatial_peak = np.zeros(len(ndvi))
    previous_peak = np.zeros(len(ndvi))
    repeated_cut = np.zeros(len(ndvi), dtype=bool)
    double_supported = np.zeros(len(ndvi), dtype=bool)
    high_count = np.zeros(len(ndvi), dtype=np.int16)
    peak_ndvi = np.zeros(len(ndvi))
    peak_moisture = np.full(len(ndvi), -1.0)
    previous_good = np.full(len(ndvi), -999, dtype=int)
    reset_count = np.zeros(len(ndvi), dtype=np.int16)
    regrowth_day = np.full(len(ndvi), -999, dtype=int)

    for j, day in enumerate(days):
        gap = context[:, j] & (day - previous_good > policy.maximum_gap_days)
        high_count[gap] = 0
        first_start[gap] = -999
        spatial_peak[gap] = 0
        reset_count[gap] = 0
        previous_good[context[:, j]] = day
        new_high = high[:, j] & (high_count == 0)
        first_start[new_high] = day
        # Rapid re-greening after a cut is not evidence of a second sowing.
        repeated_cut |= high[:, j] & (event_counts > 0) & (day - event_end <= 25)
        regrowth_day[new_high] = day
        high_count += high[:, j]
        peak_ndvi = np.where(high[:, j], np.maximum(peak_ndvi, ndvi[:, j]), peak_ndvi)
        peak_moisture = np.where(high[:, j], np.maximum(peak_moisture, ndmi[:, j]), peak_moisture)
        spatial_peak = np.where(high[:, j], np.maximum(spatial_peak, vegetation[:, j] * valid[:, j]), spatial_peak)
        reset_count += reset[:, j] & (high_count >= 2)
        # Senescence can precede soil exposure by several observations. A sharp
        # NDVI step is not required; leaf fall is handled by canopy persistence.
        recent = (days < day) & (days >= day - policy.growth_end_lookback_days)
        prior_high = np.any(high[:, recent], axis=1) if recent.any() else np.zeros(len(ndvi), dtype=bool)
        end = (reset[:, j] & (high_count >= 2) & (reset_count >= 2) & prior_high
               & (day - first_start >= policy.minimum_growth_days)
               & (peak_ndvi - ndvi[:, j] >= policy.harvest_drop)
               & (peak_moisture - ndmi[:, j] >= 0.08))
        # If each peak covers at least 75% of the whole parcel, their intersection
        # is at least 50%; independent half-field peaks cannot pass this test.
        double_supported |= (end & (event_counts == 1) & ~repeated_cut
                             & (previous_peak + spatial_peak >= 1.50)
                             & (regrowth_day - event_end > 25))
        previous_peak[end] = spatial_peak[end]
        event_counts += end & (day >= 1)
        event_end[end] = day
        high_count[end] = 0
        first_start[end] = -999
        spatial_peak[end] = 0
        peak_ndvi[end] = 0
        peak_moisture[end] = -1
        reset_count[end] = 0

    current_high = high & growing
    first_high = np.where(current_high, days, 999).min(axis=1)
    last_high = np.where(current_high, days, -999).max(axis=1)
    canopy_window = good_growing & (days >= first_high[:, None]) & (days <= last_high[:, None])
    continuous = canopy_window & (ndvi >= 0.38) & (vegetation >= 0.4)
    persistent = ((last_high-first_high >= policy.minimum_perennial_span_days)
                  & (continuous.sum(axis=1) >= 0.75 * np.maximum(canopy_window.sum(axis=1), 1))
                  & (current_high.sum(axis=1) >= 5) & ((reset & canopy_window).sum(axis=1) <= 2))
    active = (high & growing).sum(axis=1) >= 2
    # 0 gap; 1 annual; 2 perennial; 3 no observed crop activity; 4 ambiguous.
    types = np.where(covered, np.where(active, 4, 3), 0).astype(np.int16)
    types[covered & active & (persistent | repeated_cut)] = 2
    annual = covered & active & (event_counts >= 1) & ~persistent & ~repeated_cut
    types[annual] = 1
    cycles = np.zeros(len(ndvi), dtype=np.int16)
    cycles[annual & (event_counts == 1)] = 1
    cycles[annual & double_supported & (event_counts == 2)] = 2
    # Multiple unsynchronized resets remain ambiguous, not forced to one cycle.
    types[annual & (cycles == 0)] = 4
    return {"type_code": types, "cycle_code": cycles, "covered": covered,
            "observations": n, "maximum_gap": max_gap, "events": event_counts,
            "repeated_cut": repeated_cut, "persistent": persistent}
