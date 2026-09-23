"""Conservative transition checks over the immutable screening-v2 algorithm.

This module may withdraw an unsupported type/cycle, never infer a missing event.
No cadastral identifiers, previously assigned labels or external data are used.
"""
from dataclasses import dataclass

import numpy as np
import pandas as pd

from .observation_screening import Policy, classify_year as screening_year, isolated_excursions, weighted_overlap


@dataclass(frozen=True)
class TransitionPolicy:
    maximum_transition_gap_days: int = 20
    minimum_repeat_days: int = 5
    minimum_flanking_green_span_days: int = 10
    minimum_moisture_drop: float = .08


def observations(frame, policy, phase):
    frame = frame.sort_values('observation_date').reset_index(drop=True)
    days = pd.to_datetime(frame.observation_date).dt.dayofyear.to_numpy()
    core = ('ndvi', 'evi2', 'ndmi', 'bsi', 'ndre')
    a = {k: frame[k+'_median'].to_numpy(float) for k in core}
    support = frame[[k+'_valid_fraction' for k in core]].min(axis=1, skipna=False).to_numpy()
    good = (support >= policy.valid_fraction) & np.isfinite(np.column_stack(list(a.values()))).all(axis=1)
    good &= ~isolated_excursions(days, a['ndvi'], a['evi2'], good, policy)
    if phase is not None:
        good[np.flatnonzero(good)[phase::2]] = False
    high = good & (a['ndvi'] >= policy.green_ndvi) & (a['evi2'] >= policy.green_evi2)
    high &= (a['ndre'] >= policy.green_ndre) & frame.vegetation_fraction.ge(policy.green_fraction).to_numpy()
    reset = good & (a['ndvi'] <= policy.reset_ndvi) & (a['evi2'] <= policy.reset_evi2)
    reset &= frame.bare_fraction.ge(policy.reset_bare_fraction).to_numpy() & (a['bsi'] > 0)
    return frame, days, a, good, high, reset


def transition_checks(frame, weights10, weights20, policy, transition_policy, phase=None):
    frame, days, a, good, high, reset = observations(frame, policy, phase)
    date_values = pd.to_datetime(frame.observation_date)
    year = int(date_values.dt.year.iloc[0])
    start, end = pd.Timestamp(year, 3, 1).dayofyear, pd.Timestamp(year, 11, 30).dayofyear
    high &= days <= end
    def overlap(i, j):
        return min(weighted_overlap(frame.valid_mask_10m.iloc[i], frame.valid_mask_10m.iloc[j], weights10),
                   weighted_overlap(frame.valid_mask_20m.iloc[i], frame.valid_mask_20m.iloc[j], weights20))
    valleys, seen = [], set()
    hs = np.flatnonzero(high)
    # A repeatable spectral dip can disprove uninterrupted canopy without proving bare soil or a second sowing.
    dip = good & (a['ndvi'] <= policy.reset_ndvi) & (a['evi2'] <= policy.reset_evi2) & (a['bsi'] > 0)
    for anchor in np.flatnonzero(dip & (days >= start) & (days <= end)):
        before = hs[(days[hs] < days[anchor]) & (days[hs] >= days[anchor]-policy.maximum_event_gap)]
        after = hs[(days[hs] > days[anchor]) & (days[hs] <= days[anchor]+policy.maximum_event_gap)]
        if len(before) < 2 or len(after) < 2:
            continue
        if min(np.ptp(days[before]), np.ptp(days[after])) < transition_policy.minimum_flanking_green_span_days:
            continue
        left, right = int(before[-1]), int(after[0])
        if (left, right) in seen:
            continue
        peak1, peak2 = before[np.argmax(a['ndvi'][before])], after[np.argmax(a['ndvi'][after])]
        low = good & (np.arange(len(frame)) > left) & (np.arange(len(frame)) < right)
        low &= (abs(days-days[anchor]) <= policy.maximum_reset_interval)
        low &= (a['ndvi'] < policy.green_ndvi) & (a['evi2'] < policy.green_evi2)
        low &= (min(a['ndvi'][peak1], a['ndvi'][peak2])-a['ndvi'] >= policy.minimum_drop_ndvi)
        low &= (min(a['evi2'][peak1], a['evi2'][peak2])-a['evi2'] >= policy.minimum_drop_evi2)
        low &= (a['bsi'] > 0) | (min(a['ndmi'][peak1], a['ndmi'][peak2])-a['ndmi'] >= transition_policy.minimum_moisture_drop)
        candidates = np.flatnonzero(low)
        if anchor not in candidates or len(candidates) < 2:
            continue
        companions = [int(j) for j in candidates if transition_policy.minimum_repeat_days <= abs(days[j]-days[anchor]) <= policy.maximum_reset_interval]
        if not companions:
            continue
        # The low-signal anchor and its companion must refer to the same ground.
        supported = [j for j in companions if min(overlap(peak1, anchor), overlap(peak2, anchor),
                                                  overlap(peak1, j), overlap(peak2, j)) >= policy.valid_fraction]
        companion = min(supported or companions, key=lambda j: (abs(days[j]-days[anchor]), j))
        seen.add((left, right))
        valleys.append({'before_date': str(frame.observation_date.iloc[left]),
                        'low_date': str(frame.observation_date.iloc[anchor]),
                        'companion_date': str(frame.observation_date.iloc[companion]),
                        'after_date': str(frame.observation_date.iloc[right]),
                        'state': ('observed_interruption' if reset[anchor] else 'possible_interruption') if supported else 'incomparable_interruption',
                        'bare_soil_anchor': bool(reset[anchor]),
                        'confirmed_second_cycle': False})
    seasonal_highs = hs[days[hs] >= start]
    gaps = []
    if len(seasonal_highs) >= 2:
        indices = np.flatnonzero(good & (days >= days[seasonal_highs[0]]) & (days <= days[seasonal_highs[-1]]))
        for first, last in zip(indices[:-1], indices[1:]):
            if days[last]-days[first] > transition_policy.maximum_transition_gap_days:
                gaps.append({'start_date': str(frame.observation_date.iloc[first]),
                             'end_date': str(frame.observation_date.iloc[last]), 'days': int(days[last]-days[first])})
    return frame, days, good, high, valleys, gaps


def bridged_soil_intervals(frame, good, high, event, policy, transition_policy):
    """Find observed soil separating growth inside an allegedly single episode."""
    dates = pd.to_datetime(frame.observation_date)
    days = dates.dt.dayofyear.to_numpy()
    inside = dates.between(pd.Timestamp(event['start_date']), pd.Timestamp(event['end_date'])).to_numpy()
    green = np.flatnonzero(high & inside)
    reset = good & inside & frame.ndvi_median.le(policy.reset_ndvi).to_numpy() & frame.evi2_median.le(policy.reset_evi2).to_numpy()
    reset &= frame.bare_fraction.ge(policy.reset_bare_fraction).to_numpy() & frame.bsi_median.gt(0).to_numpy()
    lows = np.flatnonzero(reset)
    intervals, seen = [], set()
    for first, last in zip(lows[:-1], lows[1:]):
        if not transition_policy.minimum_repeat_days <= days[last]-days[first] <= policy.maximum_reset_interval:
            continue
        before, after = green[green < first], green[green > last]
        if not len(before) or not len(after) or np.any((green > first) & (green < last)):
            continue
        pair = int(before[-1]), int(after[0])
        if pair in seen:
            continue
        seen.add(pair)
        intervals.append({'first_low_date': str(frame.observation_date.iloc[first]),
                          'second_low_date': str(frame.observation_date.iloc[last]),
                          'earlier_growth_date': str(frame.observation_date.iloc[pair[0]]),
                          'later_growth_date': str(frame.observation_date.iloc[pair[1]]),
                          'confirmed_additional_cycle': False})
    return intervals


def classify_year(frame, spatial, weights10, weights20, policy=Policy(), transition_policy=TransitionPolicy(), drop_alternate=None):
    previous, events = screening_year(frame, spatial, weights10, weights20, policy, drop_alternate)
    frame, days, good, high, valleys, gaps = transition_checks(frame, weights10, weights20, policy, transition_policy, drop_alternate)
    dates = pd.to_datetime(frame.observation_date)
    checks = []
    for event in events:
        first_soil = np.flatnonzero(dates.eq(pd.Timestamp(event['soil_first_date'])))[0]
        end = np.flatnonzero(dates.eq(pd.Timestamp(event['end_date'])))[0]
        prior_green = np.flatnonzero(high & (days < days[first_soil]))
        before = int(prior_green[-1]) if len(prior_green) else first_soil
        observed = np.flatnonzero(good & (days >= days[before]) & (days <= days[end]))
        gap = int(np.diff(days[observed]).max()) if len(observed) > 1 else policy.maximum_event_gap
        bridges = bridged_soil_intervals(frame, good, high, event, policy, transition_policy)
        checks.append({**event, 'bridged_soil_intervals': bridges, 'transition_max_gap_days': gap,
                       'transition_supported': bool(len(prior_green) and gap <= transition_policy.maximum_transition_gap_days)})
    supported_events = [e for e in checks if e['transition_supported']]
    blocking = [v for v in valleys if (pd.Timestamp(v['after_date'])-pd.Timestamp(v['low_date'])).days > policy.rapid_regrowth_days]
    renewed = False
    for event in supported_events:
        later = np.flatnonzero(high & dates.gt(pd.Timestamp(event['end_date'])).to_numpy())
        if len(later) >= 2 and (dates.iloc[later[0]]-pd.Timestamp(event['soil_first_date'])).days > policy.rapid_regrowth_days:
            renewed = True
    kind, cycle, reason = previous['crop_type_candidate'], previous['annual_cycle_candidate'], previous['reason']
    bridge_count = sum(len(e['bridged_soil_intervals']) for e in checks)
    if previous['covered'] and not previous['spatial_reason']:
        if kind == 'perennial':
            if blocking or renewed or bridge_count:
                kind, cycle, reason = 'undetermined', 'undetermined', 'interrupted_canopy_review'
            elif gaps:
                kind, cycle, reason = 'undetermined', 'undetermined', 'canopy_continuity_not_observed'
        elif kind == 'annual':
            if bridge_count:
                cycle, reason = 'undetermined', 'bridged_growth_episodes_review'
            elif len(supported_events) != len(events):
                kind = 'annual' if supported_events else 'undetermined'
                cycle, reason = 'undetermined', 'event_transition_not_observed'
            elif cycle == 'single_cycle' and blocking:
                cycle, reason = 'undetermined', 'unresolved_intercycle_transition'
    result = {**previous, 'previous_type': previous['crop_type_candidate'], 'previous_cycle': previous['annual_cycle_candidate'],
              'previous_reason': previous['reason'], 'crop_type_candidate': kind, 'annual_cycle_candidate': cycle,
              'reason': reason, 'transition_rule_changed_result': (kind, cycle) != (previous['crop_type_candidate'], previous['annual_cycle_candidate']),
              'interior_interruptions': len(valleys), 'blocking_interruptions': len(blocking),
              'short_regrowth_interruptions': len(valleys)-len(blocking), 'continuity_gaps': len(gaps),
              'supported_episode_transitions': len(supported_events), 'post_reset_growth': renewed,
              'bridged_soil_intervals': bridge_count,
              'transition_checks': valleys, 'continuity_gap_details': gaps, 'accepted': False}
    return result, checks
