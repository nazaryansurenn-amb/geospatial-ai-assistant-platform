"""Internal, uncalibrated seasonal screening from native parcel EO observations.

Pure calculations only. Missing dates are never filled and candidate labels are
not reference labels for machine learning or accepted map classifications.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
import warnings

import numpy as np
import pandas as pd


VERSION = "observation_rules_2021_2025_v1"
INDICES = ("ndvi", "evi", "evi2", "gndvi", "savi", "msavi2", "ndre",
           "ci_rededge", "mtci", "ireci", "ndmi", "msi", "bsi", "ndwi",
           "mndwi", "nbr", "nbr2", "psri")
CORE = ("ndvi", "evi2", "ndmi", "bsi", "ndre")
FRACTIONS = ("vegetation_fraction", "bare_fraction", "water_fraction")


@dataclass(frozen=True)
class Policy:
    valid_fraction: float = .60
    minimum_pixels_10m: int = 3
    minimum_observations: int = 16
    maximum_gap_days: int = 35
    minimum_years: int = 3
    green_ndvi: float = .48
    green_evi2: float = .30
    green_ndre: float = .10
    green_fraction: float = .45
    reset_ndvi: float = .30
    reset_evi2: float = .25
    reset_bare_fraction: float = .45
    minimum_growth_days: int = 25
    reset_lookback_days: int = 60
    rapid_regrowth_days: int = 25
    minimum_perennial_span_days: int = 150
    minimum_ndvi_drop: float = .20
    minimum_evi2_drop: float = .15
    minimum_ndmi_drop: float = .08
    double_peak_fraction_sum: float = 1.50


def day_of_year(year, month_day):
    return (date.fromisoformat(f"{year}-{month_day}") - date(year, 1, 1)).days + 1


def quantile(values, q):
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)
        return np.nanquantile(values, q, axis=1).astype(np.float32)


def coverage(days, good, year, policy):
    """Coverage includes season edges; high annual counts cannot hide a gap."""
    start, end = day_of_year(year, "03-01"), day_of_year(year, "11-30")
    seasonal = good & (days >= start) & (days <= end)
    n = seasonal.sum(axis=1)
    first = np.where(seasonal, days, 999).min(axis=1)
    last = np.where(seasonal, days, -999).max(axis=1)
    previous = np.full(len(good), start)
    gaps = np.zeros(len(good), dtype=np.int16)
    for j, day in enumerate(days):
        if start <= day <= end:
            gaps = np.maximum(gaps, np.where(seasonal[:, j], day - previous, 0))
            previous = np.where(seasonal[:, j], day, previous)
    gaps = np.maximum(gaps, end - previous).astype(np.int16)
    phases = (("03-01", "04-30"), ("05-01", "06-30"),
              ("07-01", "08-31"), ("09-01", "11-30"))
    phase_counts = np.column_stack([
        (seasonal & (days >= day_of_year(year, a)) & (days <= day_of_year(year, b))).sum(axis=1)
        for a, b in phases])
    conditions = [n < policy.minimum_observations,
                  first > day_of_year(year, "04-15"),
                  last < day_of_year(year, "11-01"),
                  gaps > policy.maximum_gap_days, (phase_counts < 2).any(axis=1)]
    names = ("few_dates", "missing_early_season", "missing_late_season", "long_gap", "missing_phase")
    reasons = np.full(len(good), "", dtype=object)
    for condition, name in zip(conditions, names):
        reasons[condition] = np.where(reasons[condition] == "", name, reasons[condition] + ";" + name)
    return {"covered": reasons == "", "quality_reason": reasons,
            "usable_dates": n.astype(np.int16), "maximum_gap_days": gaps,
            "first_usable_day": np.where(n > 0, first, -1),
            "last_usable_day": np.where(n > 0, last, -1),
            **{f"phase_{i+1}_dates": phase_counts[:, i] for i in range(4)}}, seasonal


def season_signals(days, values, supports, pixels, year, policy=Policy()):
    days = np.asarray(days)
    shape = values["ndvi"].shape
    if len(shape) != 2 or shape[1] != len(days) or not len(days) or np.any(np.diff(days) <= 0):
        raise ValueError("Expected one aligned observation per increasing date")
    if any(values[k].shape != shape for k in (*INDICES, *FRACTIONS)) or pixels.shape != shape:
        raise ValueError("Unaligned EO matrices")
    if any(supports[k].shape != shape for k in INDICES):
        raise ValueError("Unaligned native-resolution support")
    common = np.minimum.reduce([supports[k] for k in CORE])
    good = (common >= policy.valid_fraction) & (pixels >= policy.minimum_pixels_10m)
    for name in (*CORE, *FRACTIONS):
        good &= np.isfinite(values[name])
    good &= (np.abs(values["ndvi"]) <= 1) & (values["water_fraction"] < .20)
    quality, seasonal = coverage(days, good, year, policy)
    ndvi, evi, ndmi, bsi, ndre = [values[k] for k in CORE]
    veg, bare = values["vegetation_fraction"], values["bare_fraction"]
    high = (good & (ndvi >= policy.green_ndvi) & (evi >= policy.green_evi2)
            & (ndre >= policy.green_ndre) & (veg >= policy.green_fraction))
    reset = (good & (ndvi <= policy.reset_ndvi) & (evi <= policy.reset_evi2)
             & (bare >= policy.reset_bare_fraction) & (bsi > 0))
    size = shape[0]
    growing = np.zeros(size, dtype=bool)
    start_supported = np.zeros(size, dtype=bool)
    start_day = np.full(size, -999)
    last_high = np.full(size, -999)
    previous_good = np.full(size, -999)
    bare_count = np.zeros(size, dtype=np.int16)
    last_bare = np.full(size, -999)
    high_count = np.zeros(size, dtype=np.int16)
    reset_count = np.zeros(size, dtype=np.int16)
    reset_first = np.full(size, -999)
    peak_ndvi, peak_evi, peak_ndmi, peak_area = [np.full(size, -999., dtype=np.float32) for _ in range(4)]
    completed = np.zeros(size, dtype=np.int16)
    boundary_events = np.zeros(size, dtype=np.int16)
    previous_end = np.full(size, -999)
    previous_peak_area = np.zeros(size, dtype=np.float32)
    rapid = np.zeros(size, dtype=bool)
    double = np.zeros(size, dtype=bool)

    for j, day in enumerate(days):
        if day > day_of_year(year, "11-30"):
            break
        gap = good[:, j] & (day - previous_good > policy.maximum_gap_days)
        growing[gap] = False
        bare_count[gap] = 0
        high_count[gap] = 0
        reset_count[gap] = 0
        previous_good[good[:, j]] = day
        bare_count[(day - last_bare) > policy.reset_lookback_days] = 0
        new = high[:, j] & ~growing
        rapid |= new & (completed > 0) & (day - previous_end <= policy.rapid_regrowth_days)
        start_supported[new] = bare_count[new] >= 2
        start_day[new] = day
        high_count[new] = 0
        reset_count[new] = 0
        for a in (peak_ndvi, peak_evi, peak_ndmi, peak_area):
            a[new] = -999
        growing[new] = True
        high_count += high[:, j] & growing
        last_high[high[:, j]] = day
        for target, source in ((peak_ndvi, ndvi), (peak_evi, evi), (peak_ndmi, ndmi)):
            target[:] = np.where(high[:, j], np.maximum(target, source[:, j]), target)
        peak_area[:] = np.where(high[:, j], np.maximum(peak_area, veg[:, j] * supports["ndvi"][:, j]), peak_area)
        low = reset[:, j] & growing
        reset_first[low & (reset_count == 0)] = day
        reset_count += low
        # A renewed canopy cancels an isolated low point, not a harvest event.
        reset_count[high[:, j]] = 0
        end = (low & (reset_count >= 2) & (day - reset_first >= 5) & (high_count >= 2)
               & (last_high - start_day >= policy.minimum_growth_days)
               & (day - last_high <= policy.reset_lookback_days)
               & (peak_ndvi - ndvi[:, j] >= policy.minimum_ndvi_drop)
               & (peak_evi - evi[:, j] >= policy.minimum_evi2_drop)
               & (peak_ndmi - ndmi[:, j] >= policy.minimum_ndmi_drop))
        accepted = end & start_supported & (day >= day_of_year(year, "03-01"))
        double |= (accepted & (completed == 1) & ~rapid
                   & (peak_area + previous_peak_area >= policy.double_peak_fraction_sum))
        boundary_events += end & ~start_supported
        completed += accepted
        previous_peak_area[accepted] = peak_area[accepted]
        previous_end[end] = reset_first[end]
        growing[end] = False
        bare_count[end] = reset_count[end]
        idle_bare = reset[:, j] & ~growing & ~end
        bare_count += idle_bare
        last_bare[reset[:, j]] = day

    high_season = high & seasonal
    first_high = np.where(high_season, days, 999).min(axis=1)
    final_high = np.where(high_season, days, -999).max(axis=1)
    canopy = seasonal & (days >= first_high[:, None]) & (days <= final_high[:, None])
    persistent_dates = canopy & (ndvi >= .38) & (evi >= .25) & (veg >= .4)
    persistent = ((final_high - first_high >= policy.minimum_perennial_span_days)
                  & (persistent_dates.sum(axis=1) >= .75 * np.maximum(canopy.sum(axis=1), 1))
                  & (high_season.sum(axis=1) >= 5) & ((reset & canopy).sum(axis=1) <= 2))
    signal = (high_season.sum(axis=1) >= 2) & (final_high - first_high >= 10)
    annual = signal & (completed >= 1) & ~persistent & ~rapid & ~growing & (boundary_events == 0)
    annual &= (completed == 1) | ((completed == 2) & double)
    # A single terminal loss after a persistent canopy can be seasonal dormancy;
    # it is not sufficient to turn the whole profile into annual cultivation.
    perennial = signal & persistent & (completed <= 1) & ~rapid
    # 0 unassessable; 1 annual candidate; 2 perennial candidate; 3 low signal; 4 conflict.
    types = np.where(quality["covered"], np.where(signal, 4, 3), 0).astype(np.int8)
    types[quality["covered"] & annual] = 1
    types[quality["covered"] & perennial] = 2
    cycles = np.where(types == 1, np.where(completed == 2, 2, 1), 0).astype(np.int8)
    reason = np.select([~quality["covered"], types == 1, types == 2, types == 3, rapid,
                        (completed > 1) & ~double, boundary_events > 0],
                       ["coverage_gap", "observed_growth_and_soil_reset", "persistent_canopy_profile",
                        "no_sustained_crop_signal", "possible_cut_and_regrowth",
                        "multiple_or_spatially_mixed_cycles", "unobserved_cycle_start"],
                       default="mixed_or_unfinished_profile")
    return {**quality, "type_code": types, "cycle_code": cycles, "type_reason": reason,
            "vegetation_signal": signal, "complete_cycles": completed,
            "boundary_growth_events": boundary_events, "possible_regrowth": rapid,
            "persistent_canopy": persistent, "double_spatial_support": double,
            "green_dates": high_season.sum(axis=1).astype(np.int16),
            "soil_reset_dates": (reset & seasonal).sum(axis=1).astype(np.int16),
            "green_span_days": np.where(signal, final_high - first_high, 0)}, seasonal


def seasonal_features(days, values, supports, pixels, year, policy=Policy()):
    signals, _ = season_signals(days, values, supports, pixels, year, policy)
    data = dict(signals)
    season = (days >= day_of_year(year, "03-01")) & (days <= day_of_year(year, "11-30"))
    for name in INDICES:
        valid = (supports[name] >= policy.valid_fraction) & np.isfinite(values[name]) & season
        series = np.where(valid, values[name], np.nan)
        p10, median, p90 = [quantile(series, q) for q in (.1, .5, .9)]
        data.update({name + "_dates": valid.sum(axis=1).astype(np.int16), name + "_p10": p10,
                     name + "_median": median, name + "_p90": p90, name + "_amplitude": p90 - p10})
        # Six within-season windows preserve timing for future supervised methods.
        for number, (lo, hi) in enumerate(((60, 105), (106, 150), (151, 195),
                                         (196, 240), (241, 285), (286, 335)), 1):
            data[f"{name}_window_{number}"] = quantile(np.where((days >= lo) & (days <= hi), series, np.nan), .5)
    for name in FRACTIONS:
        data[name + "_p90"] = quantile(np.where(season & (supports["ndvi"] >= policy.valid_fraction), values[name], np.nan), .9)
    return pd.DataFrame(data)


def predominant(types, cycles, covered, vegetation, policy=Policy()):
    types, cycles, covered, vegetation = [np.asarray(x) for x in (types, cycles, covered, vegetation)]
    if types.ndim != 2 or types.shape[1] != 5 or any(x.shape != types.shape for x in (cycles, covered, vegetation)):
        raise ValueError("Five aligned completed seasons required")
    covered = covered.astype(bool)
    annual = ((types == 1) & covered).sum(axis=1)
    perennial = ((types == 2) & covered).sum(axis=1)
    assessed = covered.sum(axis=1)
    labels = np.full(len(types), "undetermined", dtype=object)
    enough = assessed >= policy.minimum_years
    labels[enough & (annual * 2 > assessed)] = "annual"
    labels[enough & (perennial * 2 > assessed)] = "perennial"
    valid_cycles = (types == 1) & covered & np.isin(cycles, [1, 2])
    cycle_years = valid_cycles.sum(axis=1)
    doubles = (valid_cycles & (cycles == 2)).sum(axis=1)
    cycle_labels = np.full(len(types), "", dtype=object)
    annual_ok = (labels == "annual") & (cycle_years * 2 > annual)
    cycle_labels[annual_ok] = "single_cycle"
    cycle_labels[annual_ok & (doubles * 2 > cycle_years)] = "two_cycles"
    labels[(labels == "annual") & ~annual_ok] = "undetermined"
    active_years = (covered & vegetation.astype(bool)).sum(axis=1)
    history = np.full(len(types), "incomplete_or_mixed_history", dtype=object)
    history[(assessed == 5) & (active_years == 5)] = "stable_vegetation_signal"
    history[(assessed >= 3) & (active_years > 0) & (active_years < assessed)] = "periodic_vegetation_signal"
    history[(assessed == 5) & (active_years == 0)] = "stable_low_vegetation_signal"
    return pd.DataFrame({"crop_type_candidate": labels, "annual_cycle_candidate": cycle_labels,
                         "assessable_years": assessed, "annual_years": annual, "perennial_years": perennial,
                         "cycle_assessable_years": cycle_years, "double_cycle_years": doubles,
                         "history_signal": history, "vegetation_years": active_years,
                         "needs_visual_review": True, "accepted": False})
