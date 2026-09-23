"""Independent, observation-first phenology experiment for private control review.

This module consumes neutral, prepared observations only. It performs no I/O,
interpolation, model calls, crop identification or promotion to a map class.
Thresholds are explicit experimental settings, not calibrated agronomic facts.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

import numpy as np
import pandas as pd


INDICES = ("ndvi", "evi2", "ndmi", "bsi", "ndre")
REQUIRED_DAILY_COLUMNS = (
    "observation_date",
    *(f"{name}_median" for name in INDICES),
    *(f"{name}_valid_fraction" for name in INDICES),
    "valid_mask_10m", "valid_mask_20m", "ndvi_p10", "ndvi_p90",
    "ndvi_mean", "ndvi_inner5_mean", "ndvi_inner5_valid_fraction",
)
REQUIRED_SPATIAL_COLUMNS = (
    "minimum_width_m", "area_pixel_equivalents_10m",
    "pure_pixels_10m", "pure_pixels_20m",
)


@dataclass(frozen=True)
class EventPolicy:
    """Explicit settings for this isolated event segmentation experiment."""

    minimum_valid_fraction: float = 0.60
    minimum_usable_dates: int = 16
    maximum_season_gap_days: int = 35
    season_start_month_day: str = "03-01"
    season_end_month_day: str = "11-30"
    latest_first_month_day: str = "04-15"
    earliest_last_month_day: str = "11-01"
    minimum_dates_per_phase: int = 2
    minimum_width_m: float = 10.0
    minimum_area_pixel_equivalents_10m: float = 3.0
    heterogeneous_ndvi_width: float = 0.40
    heterogeneous_fraction: float = 0.50
    edge_difference: float = 0.15
    edge_fraction: float = 0.50
    minimum_edge_dates: int = 5
    minimum_common_support: float = 0.60
    green_ndvi: float = 0.48
    green_evi2: float = 0.30
    green_ndre: float = 0.10
    soil_ndvi_maximum: float = 0.35
    soil_evi2_maximum: float = 0.25
    soil_bsi_minimum: float = 0.0
    minimum_ndvi_amplitude: float = 0.20
    minimum_evi2_amplitude: float = 0.15
    minimum_soil_dates: int = 2
    minimum_soil_span_days: int = 5
    maximum_soil_step_days: int = 20
    maximum_event_gap_days: int = 20
    minimum_growth_duration_days: int = 25
    minimum_growth_dates: int = 3
    rapid_regrowth_days: int = 25
    perennial_minimum_duration_days: int = 150
    perennial_minimum_green_fraction: float = 0.75

    def __post_init__(self) -> None:
        for name in (
            "minimum_valid_fraction", "heterogeneous_fraction", "edge_fraction",
            "minimum_common_support", "perennial_minimum_green_fraction",
        ):
            value = getattr(self, name)
            if not np.isfinite(value) or not 0 < value <= 1:
                raise ValueError(f"{name} must be in (0, 1]")
        for name in (
            "minimum_usable_dates", "maximum_season_gap_days", "minimum_dates_per_phase",
            "minimum_edge_dates", "minimum_soil_dates", "minimum_soil_span_days",
            "maximum_soil_step_days", "maximum_event_gap_days",
            "minimum_growth_duration_days", "minimum_growth_dates",
            "rapid_regrowth_days", "perennial_minimum_duration_days",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ValueError(f"{name} must be a positive integer")
        if self.minimum_soil_dates < 2:
            raise ValueError("A soil interval requires at least two observations")
        for name in (
            "minimum_width_m", "minimum_area_pixel_equivalents_10m",
            "heterogeneous_ndvi_width", "edge_difference", "minimum_ndvi_amplitude",
            "minimum_evi2_amplitude",
        ):
            if not np.isfinite(getattr(self, name)) or getattr(self, name) < 0:
                raise ValueError(f"{name} must be finite and nonnegative")
        for name in (
            "green_ndvi", "green_evi2", "green_ndre", "soil_ndvi_maximum",
            "soil_evi2_maximum", "soil_bsi_minimum",
        ):
            if not np.isfinite(getattr(self, name)):
                raise ValueError(f"{name} must be finite")
        if self.green_ndvi <= self.soil_ndvi_maximum or self.green_evi2 <= self.soil_evi2_maximum:
            raise ValueError("Green and soil thresholds must not overlap")
        dates = [pd.Timestamp(f"2024-{getattr(self, name)}") for name in (
            "season_start_month_day", "latest_first_month_day",
            "earliest_last_month_day", "season_end_month_day",
        )]
        if dates != sorted(dates):
            raise ValueError("Season coverage dates must be chronologically ordered")


def _weights(values: Any, name: str) -> np.ndarray:
    if values is None:
        raise ValueError(f"{name} is required; support length cannot be inferred")
    result = np.asarray(values, dtype=float)
    if result.ndim != 1 or not len(result):
        raise ValueError(f"{name} must be a nonempty one-dimensional array")
    if not np.isfinite(result).all() or (result < 0).any() or result.sum() <= 0:
        raise ValueError(f"{name} must have finite nonnegative area weights with positive sum")
    return result


def _mask(value: Any, length: int, name: str) -> np.ndarray:
    if not isinstance(value, (bytes, bytearray, memoryview)):
        raise ValueError(f"{name} must contain packed bytes")
    raw = np.frombuffer(value, dtype=np.uint8)
    if len(raw) != (length + 7) // 8:
        raise ValueError(f"{name} byte length does not match supplied support weights")
    # The frozen neutral preprocessing uses np.packbits with its default big order.
    unpacked = np.unpackbits(raw, bitorder="big")
    if unpacked[length:].any():
        raise ValueError(f"{name} has nonzero bits outside supplied support length")
    return unpacked[:length].astype(bool)


def _common(indices: list[int], masks: np.ndarray, weights: np.ndarray) -> float:
    if not indices:
        return 0.0
    common = np.logical_and.reduce(masks[np.unique(indices)], axis=0)
    return float(weights[common].sum() / weights.sum())


def _runs(selected: np.ndarray, days: np.ndarray, maximum_step: int) -> list[list[int]]:
    result: list[list[int]] = []
    for index in np.flatnonzero(selected):
        i = int(index)
        if result and i == result[-1][-1] + 1 and days[i] - days[result[-1][-1]] <= maximum_step:
            result[-1].append(i)
        else:
            result.append([i])
    return result


def classify_year(
    frame: pd.DataFrame,
    spatial: Mapping[str, Any],
    weights10: Any,
    weights20: Any,
    policy: EventPolicy = EventPolicy(),
    drop_alternate: int | None = None,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Segment observed episodes and classify one completed 2021-2025 season.

    ``drop_alternate`` removes usable date positions of the supplied parity (0
    or 1), before coverage and events are recomputed. No missing date is filled.
    An invalid input schema/support encoding raises ValueError. Missing numeric
    observations reduce coverage and remain unknown rather than becoming soil.
    """
    missing = set(REQUIRED_DAILY_COLUMNS) - set(frame.columns)
    if missing:
        raise ValueError(f"Missing neutral observation columns: {sorted(missing)}")
    missing_spatial = set(REQUIRED_SPATIAL_COLUMNS) - set(spatial)
    if missing_spatial:
        raise ValueError(f"Missing neutral spatial columns: {sorted(missing_spatial)}")
    if frame.empty:
        raise ValueError("A season frame must contain dated observations, including missing rows")
    if drop_alternate not in (None, 0, 1) or isinstance(drop_alternate, bool):
        raise ValueError("drop_alternate must be None, 0 or 1")
    w10, w20 = _weights(weights10, "weights10"), _weights(weights20, "weights20")
    dates = pd.to_datetime(frame["observation_date"], errors="raise")
    if dates.isna().any() or dates.dt.tz is not None:
        raise ValueError("Observation dates must be valid timezone-free dates")
    dates = dates.dt.normalize()
    years = dates.dt.year.unique()
    if len(years) != 1 or not 2021 <= int(years[0]) <= 2025:
        raise ValueError("A frame must contain exactly one completed year within 2021-2025")
    if dates.duplicated().any():
        raise ValueError("Duplicate dates must be resolved by neutral scene selection before classification")
    year = int(years[0])
    order = np.argsort(dates.to_numpy(), kind="stable")
    data = frame.iloc[order].reset_index(drop=True).copy()
    dates = dates.iloc[order].reset_index(drop=True)
    values = data[[f"{x}_median" for x in INDICES]].to_numpy(dtype=float)
    fractions = data[[f"{x}_valid_fraction" for x in INDICES]].to_numpy(dtype=float)
    finite_fraction = np.isfinite(fractions)
    if ((fractions[finite_fraction] < 0) | (fractions[finite_fraction] > 1 + 1e-6)).any():
        raise ValueError("Valid fractions must lie in [0, 1]")
    masks10 = np.stack([_mask(v, len(w10), "valid_mask_10m") for v in data.valid_mask_10m])
    masks20 = np.stack([_mask(v, len(w20), "valid_mask_20m") for v in data.valid_mask_20m])
    actual10 = masks10 @ w10 / w10.sum()
    actual20 = masks20 @ w20 / w20.sum()
    start = pd.Timestamp(f"{year}-{policy.season_start_month_day}")
    end = pd.Timestamp(f"{year}-{policy.season_end_month_day}")
    usable = (
        np.isfinite(values).all(axis=1)
        & finite_fraction.all(axis=1)
        & (fractions.min(axis=1) >= policy.minimum_valid_fraction)
        & (actual10 >= policy.minimum_valid_fraction)
        & (actual20 >= policy.minimum_valid_fraction)
        & (dates >= start).to_numpy() & (dates <= end).to_numpy()
    )
    if drop_alternate is not None:
        usable[np.flatnonzero(usable)[drop_alternate::2]] = False
    data = data.loc[usable].reset_index(drop=True)
    dates = dates.loc[usable].reset_index(drop=True)
    values, masks10, masks20 = values[usable], masks10[usable], masks20[usable]
    day = ((dates - start).dt.days).to_numpy(dtype=int)
    n = len(data)
    maximum_gap = int(np.diff(np.r_[0, day, (end - start).days]).max())
    quality: list[str] = []
    if n < policy.minimum_usable_dates:
        quality.append("too_few_usable_dates")
    if maximum_gap > policy.maximum_season_gap_days:
        quality.append("unobserved_season_gap")
    if not n or dates.iloc[0] > pd.Timestamp(f"{year}-{policy.latest_first_month_day}"):
        quality.append("early_season_not_observed")
    if not n or dates.iloc[-1] < pd.Timestamp(f"{year}-{policy.earliest_last_month_day}"):
        quality.append("late_season_not_observed")
    phase_counts = [int(((dates.dt.month >= lo) & (dates.dt.month <= hi)).sum()) for lo, hi in (
        (3, 4), (5, 6), (7, 8), (9, 11),
    )]
    if any(count < policy.minimum_dates_per_phase for count in phase_counts):
        quality.append("season_phase_not_observed")
    spatial_reasons: list[str] = []
    for name in REQUIRED_SPATIAL_COLUMNS:
        if not np.isfinite(float(spatial[name])) or float(spatial[name]) < 0:
            raise ValueError(f"{name} must be finite and nonnegative")
    if float(spatial["minimum_width_m"]) < policy.minimum_width_m:
        spatial_reasons.append("parcel_too_narrow")
    if float(spatial["area_pixel_equivalents_10m"]) < policy.minimum_area_pixel_equivalents_10m:
        spatial_reasons.append("too_few_pixel_equivalents")
    if n:
        widths = data.ndvi_p90.to_numpy(float) - data.ndvi_p10.to_numpy(float)
        finite_width = np.isfinite(widths)
        if (widths[finite_width] < 0).any():
            raise ValueError("NDVI p90 cannot be smaller than p10")
        if finite_width.any() and np.sum(widths >= policy.heterogeneous_ndvi_width) / n >= policy.heterogeneous_fraction:
            spatial_reasons.append("spatially_heterogeneous_signal")
        inner = data.ndvi_inner5_mean.to_numpy(float)
        whole = data.ndvi_mean.to_numpy(float)
        inner_support = data.ndvi_inner5_valid_fraction.to_numpy(float)
        edge_ok = np.isfinite(inner) & np.isfinite(whole) & (inner_support >= policy.minimum_valid_fraction)
        if edge_ok.sum() >= policy.minimum_edge_dates:
            if np.mean(np.abs(inner[edge_ok] - whole[edge_ok]) > policy.edge_difference) >= policy.edge_fraction:
                spatial_reasons.append("edge_sensitive_signal")
    result: dict[str, Any] = {
        "year": year, "crop_type_candidate": "undetermined", "annual_cycle_candidate": "undetermined",
        "reason": "no_supported_growth_episode", "covered": not quality,
        "quality_reason": ";".join(quality), "usable_dates": n,
        "maximum_gap_days": maximum_gap, "spatial_reason": ";".join(spatial_reasons),
        "accepted": False, "training_eligible": False, "episode_count": 0, "complete_cycles": 0,
        "incomplete_episode_count": 0, "soil_interval_count": 0,
        "short_regrowth_candidate_count": 0, "gap_interrupted_count": 0,
        "unconfirmed_interruption_count": 0, "noncomparable_reset_count": 0,
        "observed_green_duration_days": 0, "observed_green_fraction": 0.0,
        "perennial_common_support_10m": 0.0, "perennial_common_support_20m": 0.0,
        "pure_pixels_10m": int(spatial["pure_pixels_10m"]),
        "pure_pixels_20m": int(spatial["pure_pixels_20m"]),
        "phase_1_dates": phase_counts[0], "phase_2_dates": phase_counts[1],
        "phase_3_dates": phase_counts[2], "phase_4_dates": phase_counts[3],
    }
    if not n:
        result["reason"] = "insufficient_observation_coverage"
        return result, []
    ndvi, evi2, ndmi, bsi, ndre = values.T
    green = (ndvi >= policy.green_ndvi) & (evi2 >= policy.green_evi2) & (ndre >= policy.green_ndre)
    soil = (ndvi <= policy.soil_ndvi_maximum) & (evi2 <= policy.soil_evi2_maximum) & (bsi > policy.soil_bsi_minimum)
    green_indices = np.flatnonzero(green)

    def support(indices: list[int]) -> tuple[float, float]:
        return _common(indices, masks10, w10), _common(indices, masks20, w20)

    def comparable(indices: list[int]) -> bool:
        return min(support(indices)) >= policy.minimum_common_support

    # Segmentation precedes any qualification of a growth episode. A short early
    # episode therefore cannot swallow a later episode across repeated soil.
    reset_intervals: list[list[int]] = []
    for run in _runs(soil, day, policy.maximum_soil_step_days):
        if len(run) < policy.minimum_soil_dates or day[run[-1]] - day[run[0]] < policy.minimum_soil_span_days:
            continue
        before = green_indices[green_indices < run[0]]
        after = green_indices[green_indices > run[-1]]
        witnesses = [run[0], run[-1]]
        if len(before):
            witnesses.append(int(before[-1]))
        if len(after):
            witnesses.append(int(after[0]))
        if comparable(witnesses):
            reset_intervals.append(run)
        else:
            result["noncomparable_reset_count"] += 1
    result["soil_interval_count"] = len(reset_intervals)
    reset_mask = np.zeros(n, dtype=bool)
    reset_before: dict[int, list[int]] = {}
    reset_after: dict[int, list[int]] = {}
    for run in reset_intervals:
        reset_mask[run] = True
        reset_before[run[-1] + 1] = run
        reset_after[run[0] - 1] = run
    gap_after = np.r_[np.diff(day) > policy.maximum_event_gap_days, False]
    segments: list[list[int]] = []
    for i in range(n):
        if reset_mask[i]:
            continue
        if segments and i == segments[-1][-1] + 1 and not gap_after[i - 1]:
            segments[-1].append(i)
        else:
            segments.append([i])
    events: list[dict[str, Any]] = []
    for segment in segments:
        g = [i for i in segment if green[i]]
        if not g:
            continue
        first, last = g[0], g[-1]
        peak = g[int(np.argmax(ndvi[g]))]
        preceding = reset_before.get(segment[0])
        following = reset_after.get(segment[-1])
        left_gap = segment[0] > 0 and bool(gap_after[segment[0] - 1])
        right_gap = bool(gap_after[segment[-1]])
        # Only actual observed values contribute to rise and fall. A soil date
        # beyond a gap does not certify the unseen beginning or end of a cycle.
        leading = [i for i in segment if i < peak]
        trailing = [i for i in segment if i > peak]
        if preceding and day[segment[0]] - day[preceding[-1]] <= policy.maximum_event_gap_days:
            leading = [preceding[-1], *leading]
        if following and day[following[0]] - day[segment[-1]] <= policy.maximum_event_gap_days:
            trailing = [*trailing, *following]
        rise_witnesses = [i for i in leading if ndvi[peak] - ndvi[i] >= policy.minimum_ndvi_amplitude
                          and evi2[peak] - evi2[i] >= policy.minimum_evi2_amplitude
                          and comparable([i, peak])]
        end_witnesses = [] if not following else [i for i in trailing if i in following
                         and ndvi[peak] - ndvi[i] >= policy.minimum_ndvi_amplitude
                         and evi2[peak] - evi2[i] >= policy.minimum_evi2_amplitude
                         and comparable([peak, i])]
        observed_rise = bool(rise_witnesses)
        observed_end = (len(end_witnesses) >= policy.minimum_soil_dates
                        and day[end_witnesses[-1]] - day[end_witnesses[0]] >= policy.minimum_soil_span_days)
        end_confirmation = (next(i for position, i in enumerate(end_witnesses)
                                 if position + 1 >= policy.minimum_soil_dates
                                 and day[i] - day[end_witnesses[0]] >= policy.minimum_soil_span_days)
                            if observed_end else None)
        duration = int(day[last] - day[first])
        sufficient_growth = duration >= policy.minimum_growth_duration_days and len(g) >= policy.minimum_growth_dates
        witnesses = [first, peak, last]
        if rise_witnesses:
            witnesses.append(rise_witnesses[-1])
        if end_witnesses:
            witnesses.extend([end_witnesses[0], end_witnesses[-1]])
        area10, area20 = support(witnesses)
        event_comparable = min(area10, area20) >= policy.minimum_common_support
        complete = sufficient_growth and observed_rise and observed_end and event_comparable and not left_gap and not right_gap
        reasons: list[str] = []
        if not sufficient_growth:
            reasons.append("short_or_sparsely_observed_growth")
        if not observed_rise:
            reasons.append("growth_start_not_observed")
        if not observed_end:
            reasons.append("repeated_soil_end_not_observed")
        if not event_comparable:
            reasons.append("growth_boundaries_not_on_comparable_support")
        if left_gap or right_gap:
            reasons.append("unobserved_interval_at_episode_boundary")
        if complete:
            status = "complete"
        elif left_gap or right_gap:
            status = "gap_interrupted"
        elif not sufficient_growth:
            status = "incomplete"
        elif not observed_rise:
            status = "left_censored"
        elif not observed_end:
            status = "right_censored"
        else:
            status = "incomplete"
        iso = lambda i: None if i is None else dates.iloc[i].date().isoformat()
        events.append({
            "event_index": len(events) + 1,
            "start_date": iso(first), "peak_date": iso(peak),
            "end_date": iso(end_witnesses[0]) if observed_end else iso(last),
            "end_confirmed_date": iso(end_confirmation),
            "soil_start_date": iso(following[0]) if following else None,
            "soil_end_date": iso(following[-1]) if following else None,
            "preceding_soil_start_date": iso(preceding[0]) if preceding else None,
            "preceding_soil_end_date": iso(preceding[-1]) if preceding else None,
            "status": status, "reason": ";".join(reasons) if reasons else "observed_growth_and_repeated_soil_end",
            "duration_days": duration, "observed_dates": len(segment), "green_dates": len(g),
            "complete_cycle": bool(complete), "observed_rise": observed_rise,
            "observed_soil_end": observed_end, "left_censored": not observed_rise,
            "right_censored": not observed_end, "gap_interrupted": left_gap or right_gap,
            "support_10m": area10, "support_20m": area20,
            "peak_ndvi": float(ndvi[peak]), "peak_evi2": float(evi2[peak]),
            "short_regrowth_candidate": False, "accepted": False, "training_eligible": False,
            "_first": first, "_last": last, "_peak": peak,
        })
    # Retain competing explanations around isolated dips, or rapid recovery
    # after a repeated-soil split. These are not new crop cycles by assertion.
    internal_interruptions = 0
    for i in range(1, n - 1):
        before = green_indices[green_indices < i]
        after = green_indices[green_indices > i]
        if not len(before) or not len(after) or reset_mask[i]:
            continue
        a, b = int(before[-1]), int(after[0])
        if ndvi[a] - ndvi[i] >= policy.minimum_ndvi_amplitude and evi2[a] - evi2[i] >= policy.minimum_evi2_amplitude:
            if min(ndvi[a], ndvi[b]) - ndvi[i] >= policy.minimum_ndvi_amplitude and comparable([a, i, b]):
                internal_interruptions += 1
    result["unconfirmed_interruption_count"] = internal_interruptions
    for previous, later in zip(events, events[1:]):
        if previous["soil_end_date"] and later["preceding_soil_end_date"] == previous["soil_end_date"]:
            # Measure the entire observed low interval, not merely the final
            # low-to-green revisit. The latter would call any well-observed
            # long bare interval "rapid" when imagery happens to be frequent.
            elapsed = (pd.Timestamp(later["start_date"]) - pd.Timestamp(previous["soil_start_date"])).days
            if elapsed <= policy.rapid_regrowth_days:
                later["short_regrowth_candidate"] = True
                later["reason"] += ";rapid_recovery_after_soil_interval"
    complete_events = [event for event in events if event["complete_cycle"]]
    result.update({
        "episode_count": len(events), "complete_cycles": len(complete_events),
        "incomplete_episode_count": sum(not event["complete_cycle"] for event in events),
        "short_regrowth_candidate_count": sum(event["short_regrowth_candidate"] for event in events),
        "gap_interrupted_count": sum(event["gap_interrupted"] for event in events),
    })
    duration_green = int(day[green_indices[-1]] - day[green_indices[0]]) if len(green_indices) else 0
    core_indices = np.arange(green_indices[0], green_indices[-1] + 1) if len(green_indices) else np.array([], dtype=int)
    green_fraction = float(green[core_indices].mean()) if len(core_indices) else 0.0
    internal_reset = any(run[0] > green_indices[0] and run[-1] < green_indices[-1] for run in reset_intervals) if len(green_indices) else False
    perennial_witnesses = ([int(green_indices[0]), int(green_indices[len(green_indices) // 2]),
                            int(green_indices[-1]), int(green_indices[np.argmax(ndvi[green_indices])])]
                           if len(green_indices) else [])
    perennial_support10, perennial_support20 = support(perennial_witnesses)
    perennial_profile = (
        duration_green >= policy.perennial_minimum_duration_days
        and green_fraction >= policy.perennial_minimum_green_fraction
        and len(core_indices) > 0 and not gap_after[core_indices[:-1]].any()
        and not internal_reset and not internal_interruptions
        and result["noncomparable_reset_count"] == 0
        and min(perennial_support10, perennial_support20) >= policy.minimum_common_support
    )
    result["observed_green_duration_days"] = duration_green
    result["observed_green_fraction"] = green_fraction
    result["perennial_common_support_10m"] = perennial_support10
    result["perennial_common_support_20m"] = perennial_support20
    if quality:
        result["reason"] = "insufficient_observation_coverage"
    elif spatial_reasons:
        result["reason"] = "spatial_interpretation_requires_review"
    elif perennial_profile:
        # A terminal winter decline alone is not proof of annual land use.
        result.update(crop_type_candidate="perennial", annual_cycle_candidate="", reason="sustained_profile_without_observed_internal_reset")
    elif complete_events:
        result.update(crop_type_candidate="annual", reason="observed_completed_growth_episode")
        competing = (len(events) != len(complete_events) or internal_interruptions
                     or result["noncomparable_reset_count"] or result["short_regrowth_candidate_count"])
        if not competing and len(complete_events) in (1, 2):
            peaks = [event["_peak"] for event in complete_events]
            if comparable(peaks):
                result["annual_cycle_candidate"] = "single_cycle" if len(complete_events) == 1 else "two_cycles"
            else:
                result["reason"] = "annual_episode_but_multiple_peaks_not_comparable"
        else:
            result["reason"] = "annual_episode_with_unresolved_competing_episodes"
    elif events:
        result["reason"] = "growth_observed_but_complete_cycle_or_sustained_type_not_established"
    for event in events:
        for key in ("_first", "_last", "_peak"):
            event.pop(key)
    return result, events


def predominant(seasons: Any, minimum_years: int = 3) -> dict[str, Any]:
    """Strict majority of all covered years; ambiguous covered years still vote.

    Single and double cycle year counts describe annual candidate histories.
    A variable history is descriptive and never a new public land-use class.
    """
    if isinstance(minimum_years, bool) or not isinstance(minimum_years, int) or minimum_years < 1:
        raise ValueError("minimum_years must be a positive integer")
    rows = seasons.to_dict("records") if isinstance(seasons, pd.DataFrame) else list(seasons)
    required = {"year", "covered", "crop_type_candidate", "annual_cycle_candidate"}
    years: set[int] = set()
    for row in rows:
        if not required <= set(row):
            raise ValueError(f"Missing season fields: {sorted(required - set(row))}")
        year = int(row["year"])
        if year in years:
            raise ValueError("A year cannot contribute more than one vote")
        if not 2021 <= year <= 2025:
            raise ValueError("Only completed 2021-2025 seasons may contribute")
        years.add(year)
        if not isinstance(row["covered"], (bool, np.bool_)):
            raise ValueError("covered must be a boolean")
        if row["crop_type_candidate"] not in {"annual", "perennial", "undetermined"}:
            raise ValueError("Unsupported crop type candidate")
        if row["annual_cycle_candidate"] not in {"single_cycle", "two_cycles", "undetermined", ""}:
            raise ValueError("Unsupported annual cycle candidate")
        if row["crop_type_candidate"] != "annual" and row["annual_cycle_candidate"] in {"single_cycle", "two_cycles"}:
            raise ValueError("An exact annual cycle count requires an annual candidate")
    if years != set(range(2021, 2026)):
        raise ValueError("Predominant history requires all five years 2021-2025, including uncovered years")
    covered = [row for row in rows if row["covered"]]
    total = len(covered)
    annual = sum(row["crop_type_candidate"] == "annual" for row in covered)
    perennial = sum(row["crop_type_candidate"] == "perennial" for row in covered)
    single = sum(row["crop_type_candidate"] == "annual" and row["annual_cycle_candidate"] == "single_cycle" for row in covered)
    double = sum(row["crop_type_candidate"] == "annual" and row["annual_cycle_candidate"] == "two_cycles" for row in covered)
    result = {
        "crop_type_candidate": "undetermined", "annual_cycle_candidate": "undetermined",
        "covered_years": total, "assessable_years": total,
        "annual_years": annual, "perennial_years": perennial,
        "undetermined_years": total - annual - perennial,
        "single_cycle_years": single, "two_cycle_years": double,
        "cycle_history_status": "insufficient_resolved_evidence",
        "reason": "insufficient_covered_years", "accepted": False, "training_eligible": False,
    }
    if total < minimum_years:
        return result
    result["reason"] = "no_strict_majority_of_all_covered_years"
    if perennial > total / 2:
        result.update(crop_type_candidate="perennial", annual_cycle_candidate="", cycle_history_status="not_applicable", reason="strict_perennial_majority")
    elif annual > total / 2:
        result.update(crop_type_candidate="annual", reason="strict_annual_majority")
        if single and double:
            result["cycle_history_status"] = "variable_candidate_years"
        elif single > total / 2:
            result["cycle_history_status"] = "predominant_single"
        elif double > total / 2:
            result["cycle_history_status"] = "predominant_double"
        if single > total / 2:
            result["annual_cycle_candidate"] = "single_cycle"
        elif double > total / 2:
            result["annual_cycle_candidate"] = "two_cycles"
    return result
