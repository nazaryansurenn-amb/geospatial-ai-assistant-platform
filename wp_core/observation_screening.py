"""Owner-only EO screening v2. Candidate evidence, never verified crop labels."""
from dataclasses import dataclass
from datetime import date

import numpy as np
import pandas as pd

from .observation_rules import Policy as CoveragePolicy, coverage


@dataclass(frozen=True)
class Policy:
    valid_fraction: float = .60
    minimum_observations: int = 16
    maximum_gap_days: int = 35
    minimum_years: int = 3
    green_ndvi: float = .48
    green_evi2: float = .30
    green_ndre: float = .10
    green_fraction: float = .45
    reset_ndvi: float = .35
    reset_evi2: float = .25
    reset_bare_fraction: float = .25
    minimum_growth_days: int = 25
    minimum_drop_ndvi: float = .20
    minimum_drop_evi2: float = .15
    maximum_reset_interval: int = 20
    rapid_regrowth_days: int = 25
    maximum_event_gap: int = 60
    minimum_perennial_span: int = 150
    minimum_width_m: float = 10
    minimum_area_pixel_equivalents: float = 3
    heterogeneous_range: float = .40
    heterogeneous_season_fraction: float = .50
    edge_difference: float = .15
    isolated_ndvi_jump: float = .35
    isolated_evi2_jump: float = .25


def weighted_overlap(a, b, weights):
    weights = np.asarray(weights, dtype=float)
    if weights.ndim != 1 or not np.isfinite(weights).all() or (weights < 0).any():
        raise ValueError('Invalid area weights')
    if min(len(a), len(b))*8 < len(weights):
        raise ValueError('Truncated spatial support mask')
    if not len(weights) or weights.sum() <= 0:
        return 0.
    aa = np.unpackbits(np.frombuffer(a, dtype=np.uint8), count=len(weights)).astype(bool)
    bb = np.unpackbits(np.frombuffer(b, dtype=np.uint8), count=len(weights)).astype(bool)
    return float(weights[aa & bb].sum() / weights.sum())


def isolated_excursions(days, ndvi, evi2, good, policy):
    flagged = np.zeros(len(days), bool)
    valid = np.flatnonzero(good)
    for before, middle, after in zip(valid[:-2], valid[1:-1], valid[2:]):
        if max(days[middle]-days[before], days[after]-days[middle]) > 10:
            continue
        if abs(ndvi[before]-ndvi[after]) > .12 or abs(evi2[before]-evi2[after]) > .10:
            continue
        nd = ndvi[middle] - np.array([ndvi[before], ndvi[after]])
        ev = evi2[middle] - np.array([evi2[before], evi2[after]])
        flagged[middle] = ((np.all(nd > policy.isolated_ndvi_jump) and np.all(ev > policy.isolated_evi2_jump))
                           or (np.all(nd < -policy.isolated_ndvi_jump) and np.all(ev < -policy.isolated_evi2_jump)))
    return flagged


def classify_year(frame, spatial, weights10, weights20, policy=Policy(), drop_alternate=None):
    if drop_alternate not in (None, 0, 1):
        raise ValueError('Unknown temporal thinning phase')
    frame = frame.sort_values('observation_date').reset_index(drop=True)
    dates = pd.to_datetime(frame.observation_date)
    if dates.duplicated().any() or dates.dt.year.nunique() != 1:
        raise ValueError('One coherent row per date and one year required')
    year = int(dates.dt.year.iloc[0]); days = dates.dt.dayofyear.to_numpy()
    core = ['ndvi', 'evi2', 'ndmi', 'bsi', 'ndre']
    arrays = {k: frame[k+'_median'].to_numpy(float) for k in core}
    n, e, moisture, soil, rededge = [arrays[k] for k in core]
    support = frame[[k+'_valid_fraction' for k in core]].min(axis=1, skipna=False).to_numpy()
    good = (support >= policy.valid_fraction) & np.isfinite(np.column_stack(list(arrays.values()))).all(axis=1)
    outliers = isolated_excursions(days, n, e, good, policy)
    good &= ~outliers
    if drop_alternate is not None:
        good[np.flatnonzero(good)[drop_alternate::2]] = False
    cp = CoveragePolicy(valid_fraction=policy.valid_fraction, minimum_observations=policy.minimum_observations,
                        maximum_gap_days=policy.maximum_gap_days)
    quality, seasonal = coverage(days, good[None, :], year, cp)
    seasonal = seasonal[0]
    covered = bool(quality['covered'][0])
    veg = frame.vegetation_fraction.to_numpy(float)
    bare = frame.bare_fraction.to_numpy(float)
    high = good & (n >= policy.green_ndvi) & (e >= policy.green_evi2) & (rededge >= policy.green_ndre) & (veg >= policy.green_fraction)
    reset = good & (n <= policy.reset_ndvi) & (e <= policy.reset_evi2) & (bare >= policy.reset_bare_fraction) & (soil > 0)
    mask10 = frame.valid_mask_10m.tolist(); mask20 = frame.valid_mask_20m.tolist()
    def overlap(i, j):
        return min(weighted_overlap(mask10[i], mask10[j], weights10),
                   weighted_overlap(mask20[i], mask20[j], weights20))
    events, highs, lows, earlier_bare = [], [], [], []
    start_supported = False; pending = False; previous_good = None; broken_events = 0
    rapid = False; common_failures = 0
    end_day = (date(year, 11, 30)-date(year, 1, 1)).days+1
    for j, day in enumerate(days):
        if day > end_day or not good[j]:
            continue
        if previous_good is not None and day-days[previous_good] > policy.maximum_gap_days:
            broken_events += int(len(highs) >= 2)
            highs, lows, earlier_bare = [], [], []
        previous_good = j
        earlier_bare = [k for k in earlier_bare if day-days[k] <= policy.maximum_event_gap]
        if high[j]:
            if not highs:
                start_supported = len(earlier_bare) >= 2
                if events and day-events[-1]['soil_first_day'] <= policy.rapid_regrowth_days:
                    rapid = True
            highs.append(j); lows = []
        elif reset[j] and highs:
            lows = [k for k in lows if day-days[k] <= policy.maximum_reset_interval] + [j]
            if len(lows) < 2 or day-days[lows[0]] < 5 or len(highs) < 2:
                continue
            peak = max(highs, key=lambda k: n[k])
            if days[highs[-1]]-days[highs[0]] < policy.minimum_growth_days:
                continue
            if day-days[highs[-1]] > policy.maximum_event_gap:
                continue
            if n[peak]-n[j] < policy.minimum_drop_ndvi or e[peak]-e[j] < policy.minimum_drop_evi2:
                continue
            if min(overlap(peak, lows[0]), overlap(peak, j)) < policy.valid_fraction:
                common_failures += 1
                continue
            if day >= (date(year, 3, 1)-date(year, 1, 1)).days+1:
                events.append({'start_date': str(frame.observation_date.iloc[highs[0]]),
                               'peak_date': str(frame.observation_date.iloc[peak]),
                               'soil_first_date': str(frame.observation_date.iloc[lows[0]]),
                               'end_date': str(frame.observation_date.iloc[j]),
                               'soil_first_day': int(days[lows[0]]), 'peak_index': int(peak),
                               'left_censored': not start_supported,
                               'common_support': round(min(overlap(peak, lows[0]), overlap(peak, j)), 4),
                               'ndmi_drop': round(float(moisture[peak]-moisture[j]), 4)})
            earlier_bare = lows.copy(); highs, lows = [], []
        elif reset[j]:
            earlier_bare.append(j)
    if len(highs) >= 2 and days[highs[-1]]-days[highs[0]] >= policy.minimum_growth_days:
        pending = True
    hs = np.flatnonzero(high & seasonal)
    signal = len(hs) >= 2 and days[hs[-1]]-days[hs[0]] >= 10
    persistent = False
    if len(hs) >= 5:
        window = seasonal & (days >= days[hs[0]]) & (days <= days[hs[-1]])
        persistent = (days[hs[-1]]-days[hs[0]] >= policy.minimum_perennial_span
                      and np.sum(window & (n >= .38) & (e >= .25) & (veg >= .4)) >= .75*np.sum(window)
                      and np.sum(reset & window) <= 2)
    peak_overlap = 0.
    if len(events) == 2:
        a, b = [x['peak_index'] for x in events]
        peak_overlap = weighted_overlap(frame.vegetated_mask_10m.iloc[a], frame.vegetated_mask_10m.iloc[b], weights10)
    detected = 'undetermined'; cycle = 'undetermined'; reason = 'mixed_profile'
    if not covered:
        reason = 'insufficient_dates'
    elif not signal:
        reason = 'low_vegetation_signal'
    elif rapid:
        reason = 'possible_cut_and_regrowth'
    elif broken_events or (common_failures and not events):
        reason = 'incomparable_event_observations'
    elif persistent and len(events) <= 1:
        detected, cycle, reason = 'perennial', '', 'persistent_profile_candidate'
    elif events and not pending:
        if len(events) == 1:
            detected, cycle, reason = 'annual', 'single_cycle', 'seasonal_episode'
        elif len(events) == 2 and peak_overlap >= .50:
            detected, cycle, reason = 'annual', 'two_cycles', 'two_spatially_supported_episodes'
        else:
            reason = 'multiple_or_spatially_mixed_episodes'
    elif events and pending:
        reason = 'unresolved_late_growth'
    ndvi_range = frame.ndvi_p90.to_numpy()-frame.ndvi_p10.to_numpy()
    heterogeneous = bool(seasonal.any() and np.mean(ndvi_range[seasonal] >= policy.heterogeneous_range) >= policy.heterogeneous_season_fraction)
    edge = abs(frame.ndvi_inner5_mean.to_numpy()-frame.ndvi_mean.to_numpy())
    edge_valid = seasonal & frame.ndvi_inner5_valid_fraction.ge(policy.valid_fraction).to_numpy() & np.isfinite(edge)
    edge_sensitive = bool(edge_valid.sum() >= 5 and np.mean(edge[edge_valid] > policy.edge_difference) >= .5)
    limited = (spatial['minimum_width_m'] < policy.minimum_width_m or
               spatial['area_pixel_equivalents_10m'] < policy.minimum_area_pixel_equivalents)
    spatial_reason = 'spatial_resolution_limit' if limited else 'edge_sensitive' if edge_sensitive else 'spatial_heterogeneity' if heterogeneous else ''
    candidate, candidate_cycle = detected, cycle
    if spatial_reason:
        detected, cycle, reason = 'undetermined', 'undetermined', spatial_reason
    result = {'year': year, 'crop_type_candidate': detected, 'annual_cycle_candidate': cycle,
              'unfiltered_type': candidate, 'unfiltered_cycle': candidate_cycle,
              'reason': reason, 'spatial_reason': spatial_reason, 'covered': covered,
              'quality_reason': str(quality['quality_reason'][0]),
              'usable_dates': int(quality['usable_dates'][0]), 'maximum_gap_days': int(quality['maximum_gap_days'][0]),
              'raw_dates': len(frame), 'strict_rejected_dates': int((support < policy.valid_fraction).sum()),
              'isolated_excursions': int(outliers.sum()), 'green_dates': int((high & seasonal).sum()),
              'complete_episodes': len(events), 'left_censored_episodes': sum(x['left_censored'] for x in events),
              'pending_growth': pending, 'possible_regrowth': rapid, 'persistent_profile': bool(persistent),
              'peak_spatial_overlap': round(peak_overlap, 4), 'common_support_failures': common_failures,
              'heterogeneous': heterogeneous, 'edge_sensitive': edge_sensitive,
              'vegetation_signal': bool(signal), 'accepted': False}
    for event in events:
        event.pop('peak_index'); event.pop('soil_first_day')
    return result, events


def predominant(frame, minimum_years=3):
    if len(frame) != 5 or sorted(frame.year) != list(range(2021, 2026)):
        raise ValueError('Five completed seasons, including missing seasons, required')
    usable = frame.loc[frame.covered]
    n = len(usable); counts = usable.crop_type_candidate.value_counts()
    kind = 'undetermined'; cycle = 'undetermined'
    if n >= minimum_years:
        for k in ('annual', 'perennial'):
            if counts.get(k, 0)*2 > n:
                kind = k
    if kind == 'annual':
        cc = usable.loc[usable.crop_type_candidate.eq('annual'), 'annual_cycle_candidate'].value_counts()
        for k in ('single_cycle', 'two_cycles'):
            if cc.get(k, 0)*2 > n:
                cycle = k
    elif kind == 'perennial':
        cycle = ''
    ordered = frame.sort_values('year').crop_type_candidate.tolist()
    possible_change = any(ordered[i] == ordered[i+1] and ordered[i] in ('annual', 'perennial')
                          and ordered[i+2] == ordered[i+3] and ordered[i+2] in ('annual', 'perennial')
                          and ordered[i] != ordered[i+2] for i in range(2))
    return {'crop_type_candidate': kind, 'annual_cycle_candidate': cycle,
            'assessable_years': n, 'annual_years': int(counts.get('annual', 0)),
            'perennial_years': int(counts.get('perennial', 0)),
            'double_cycle_years': int(((usable.crop_type_candidate == 'annual') & (usable.annual_cycle_candidate == 'two_cycles')).sum()),
            'possible_profile_change': possible_change, 'accepted': False, 'training_eligible': False}
