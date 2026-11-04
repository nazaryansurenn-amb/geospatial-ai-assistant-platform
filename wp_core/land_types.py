"""Land-type decision rules, consistency checks, field blocks and Stehman estimates (numpy/shapely only).

Rules follow config/alphaearth_types_20261030_v1.json: non-agricultural first, then a single perennial type,
then cultivated share (type, MIX or PART), then not cultivated. Shares are fractions of the parcel area.
"""
from __future__ import annotations
import math
import numpy as np

CULTIVATED = ("perennial", "annual", "protected")


def normalise_shares(shares, config):
    r = config["rules"]
    step, zero = r["share_step"], r["zero_below"]
    known = config["types"]
    if not shares or any(t not in known for t in shares):
        raise ValueError("Unknown land type in shares")
    if any(not isinstance(v, (int, float)) or v < 0 for v in shares.values()):
        raise ValueError("Shares must be non-negative numbers")
    total = sum(shares.values())
    if not math.isclose(total, 1, abs_tol=.051):
        raise ValueError(f"Shares must add up to 100% (got {total:.0%})")
    rounded = {t: round(round(v / step) * step, 6) for t, v in shares.items()}
    return {t: v for t, v in rounded.items() if v >= zero - 1e-9}


def decide(shares, config):
    """Shares -> {'type', 'main', 'components'}; 'type' is a type code, 'MIX', 'PART' or 'DEFER'."""
    s = normalise_shares(shares, config)
    if not s:
        raise ValueError("Every share is below the zero threshold")
    types, majority = config["types"], config["rules"]["majority"] - 1e-9
    group = lambda t: types[t]["group"]
    rank = {t: i for i, t in enumerate(types)}  # ties go to the type listed first in the config
    pick = lambda codes: min(codes, key=lambda t: (-s[t], rank[t])) if codes else None
    components = sorted(s, key=lambda t: (-s[t], rank[t]))
    non_ag = [t for t in s if group(t) == "non_agricultural"]
    if sum(s[t] for t in non_ag) >= majority:
        trees = config["rules"].get("tree_stand")  # v2_4 owner rule: trees covering >= 50% make the parcel TRE
        if trees and s.get(trees["type"], 0) >= trees["min_share"] - 1e-9:
            return {"type": trees["type"], "main": trees["type"], "components": components}
        main = pick([t for t in non_ag if t != "TRE"])
        return {"type": main or "DEFER", "main": main, "components": components,
                **({} if main else {"reason": "tree rows alone are a component, not a parcel type"})}
    for t in s:
        if group(t) == "perennial" and s[t] >= majority:
            return {"type": t, "main": t, "components": components}
    cultivated = [t for t in s if group(t) in CULTIVATED]
    share = sum(s[t] for t in cultivated)
    main = pick(cultivated)
    if share >= majority:
        return {"type": main if s[main] >= majority else "MIX", "main": main, "components": components}
    if share > 0:
        return {"type": "PART", "main": main, "components": components}
    rest = pick([t for t in s if t in ("NCU", "GRS")]) or pick([t for t in s if t != "TRE"])
    return {"type": rest or "DEFER", "main": rest, "components": components}


def weak_spring_peak(hints, rule=None):
    """Owner rule (v2): 2025 NDVI only a spring peak < 0.5 (Mar-May), mean Jun-Sep < 0.3, no detected annual cycle.
    v2_1 (rule min_peak / min_rise_over_winter): the spring peak must be real, i.e. reach min_peak and rise at least
    min_rise_over_winter above the Jan-Feb mean; v2 also fired on fields bare all year."""
    rule = rule or {}
    curve = (hints.get("ndvi") or {}).get("2025")
    if not curve or (hints.get("cycles") or {}).get("2025", 0) > 0:
        return False
    spring = [v for v in curve[2:5] if v is not None]
    summer = [v for v in curve[5:9] if v is not None]
    winter = [v for v in curve[0:2] if v is not None]
    if not spring or not summer or not max(spring) < .5 or not sum(summer) / len(summer) < .3:
        return False
    if max(spring) < rule.get("min_peak", 0):
        return False
    return not winter or max(spring) - sum(winter) / len(winter) >= rule.get("min_rise_over_winter", 0)


def cut_events(points, green=.5, drop=.2, regrow=.15, drop_days=21, regrow_days=50):
    """Cut-and-regrow events in one year's frequent NDVI series (alfalfa is cut every 4-6 weeks; monthly medians
    hide this). points: [(datetime, ndvi), ...] sorted by date; only April-October counts. An event: NDVI >= green,
    then down by >= drop within drop_days, then back up by >= regrow within regrow_days of the low."""
    pts = [(t, v) for t, v in points if v is not None and 4 <= t.month <= 10]
    n, i = 0, 0
    while i < len(pts):
        t0, v0 = pts[i]
        if v0 < green:
            i += 1
            continue
        low = None  # lowest point within drop_days (series are sorted, so the scans stop early)
        for k in range(i + 1, len(pts)):
            if (pts[k][0] - t0).days > drop_days:
                break
            if low is None or pts[k][1] < low[2]:
                low = (k, pts[k][0], pts[k][1])
        if low and low[2] <= v0 - drop:
            back = None
            for k in range(low[0] + 1, len(pts)):
                if (pts[k][0] - low[1]).days > regrow_days:
                    break
                if pts[k][1] >= low[2] + regrow:
                    back = k
                    break
            if back is not None:
                n += 1
                i = back
                continue
        i += 1
    return n


def green_days(points, level=.5):
    """Days between consecutive observations that are both at or above `level` (length of the green season)."""
    pts = [(t, v) for t, v in points if v is not None]
    return sum((b[0] - a[0]).days for a, b in zip(pts, pts[1:]) if a[1] >= level and b[1] >= level)


def photo_shares(shares, config):
    """Validate a structure-only photo reading (v2): perennials, buildings, water, natural, tree rows, FLD field."""
    allowed = set(config["photo_stage"]["allowed"])
    if not isinstance(shares, dict) or not shares or set(shares) - allowed:
        raise ValueError(f"Photo reading may only use {sorted(allowed)}")
    values = {k: float(v) for k, v in shares.items()}
    if any(v < 0 for v in values.values()) or not math.isclose(sum(values.values()), 1, abs_tol=.051):
        raise ValueError("Photo shares must be non-negative and add up to 100%")
    return {k: round(round(v / .1) * .1, 6) for k, v in values.items() if v > 0}


def mean_width(area_m2, perimeter_m):
    """Mean width of a parcel (2 * area / perimeter); about the true width for long strips."""
    return 2 * area_m2 / perimeter_m if perimeter_m > 0 else 0.0


def contradictions(answer, evidence):
    """Signals that disagree with a decided type; evidence keys are optional (missing -> not checked)."""
    t, issues = answer.get("type"), []
    # Greenness comes from the monthly NDVI curve. The cycle counter detects annual cycles (green-up then bare
    # soil) and stays 0 for vineyards and orchards, so it must not be used to judge perennials (pilot round 1).
    green_years = evidence.get("green_years")          # years 2021-2025 whose monthly NDVI peak reaches 0.4
    cycles_2025 = evidence.get("cycles_2025")           # detected annual-type cycles in 2025
    peak_ndvi_2025 = evidence.get("peak_ndvi_2025")
    large = evidence.get("area_ha") is None or evidence["area_ha"] >= .5   # small strips mix with neighbour pixels
    if t in ("VIN", "ORC", "ALF") and green_years == 0:
        issues.append("perennial type but NDVI never reaches 0.4 in any year 2021-2025")
    if t == "NCU" and ((cycles_2025 or 0) > 0 or (peak_ndvi_2025 is not None and peak_ndvi_2025 >= .5)):
        issues.append("not cultivated but a 2025 growing season is visible")
    if t == "BLT" and large and peak_ndvi_2025 is not None and peak_ndvi_2025 >= .6:
        issues.append("buildings but strong 2025 greenness")
    alfalfa_years = evidence.get("alfalfa_cut_years")   # v2_3: years with the frequent-data cut signature
    if t in ("ANN", "GRS", "NCU") and alfalfa_years:
        issues.append(f"repeated cuts like alfalfa in {', '.join(alfalfa_years)}: reconsider as alfalfa (ALF), "
                      "MIX/PART if only part is alfalfa, or defer")
    if t == "ALF" and evidence.get("alfalfa_without_cuts"):
        issues.append("alfalfa but no year with 2 or more cuts and no long green season: reconsider or defer")
    crop_years = evidence.get("crop_cycle_years")       # v2_2: years 2021-2025 with a detected annual crop cycle
    if t == "NAT" and crop_years:
        issues.append(f"natural ground but a crop cycle was detected in {', '.join(crop_years)}: "
                      "reconsider as not-cultivated field (NCU), annual (ANN) or defer")
    flags = answer.get("flags") or {}
    if t in ("VIN", "ORC") and flags.get("perennial_structure") is False:
        issues.append("perennial type without a visible perennial structure")
    if t == "NCU" and flags.get("active_2025") is True:
        issues.append("not cultivated but marked active in 2025")
    if t == "BLT" and flags.get("built") is False:
        issues.append("buildings type but not marked built")
    return issues


def field_blocks(centroids, bounds, max_parcels, max_window):
    """Group neighbouring parcels into review blocks whose combined extent stays within max_window metres.

    centroids: (n, 2) metric coordinates; bounds: (n, 4) metric bounds. Deterministic greedy grouping.
    """
    centroids, bounds = np.asarray(centroids, float), np.asarray(bounds, float)
    order = np.lexsort((centroids[:, 0], -centroids[:, 1]))
    free = np.ones(len(centroids), bool)
    blocks = []
    for seed in order:
        if not free[seed]:
            continue
        free[seed] = False
        members, box = [seed], bounds[seed].copy()
        near = np.flatnonzero(free)
        near = near[np.argsort(np.hypot(*(centroids[near] - centroids[seed]).T), kind="stable")]
        for j in near:
            if len(members) >= max_parcels or np.hypot(*(centroids[j] - centroids[seed])) > max_window:
                break
            grown = np.r_[np.minimum(box[:2], bounds[j][:2]), np.maximum(box[2:], bounds[j][2:])]
            if max(grown[2] - grown[0], grown[3] - grown[1]) <= max_window:
                members.append(j); box = grown; free[j] = False
        blocks.append(members)
    return blocks


def stratified_ratio(y, x, stratum, population_units):
    """Combined ratio estimate sum(N_h*mean(y_h)) / sum(N_h*mean(x_h)) with its linearised standard error.

    Stratified random sampling of units (parcels); x is usually unit area, y = area * indicator.
    """
    y, x, stratum = np.asarray(y, float), np.asarray(x, float), np.asarray(stratum)
    strata = np.unique(stratum)
    units = {h: float(population_units[h]) for h in strata}  # float: N**2 overflows numpy int32 on Windows
    ty = sum(units[h] * y[stratum == h].mean() for h in strata)
    tx = sum(units[h] * x[stratum == h].mean() for h in strata)
    ratio = ty / tx
    variance = 0.0
    for h in strata:
        d = y[stratum == h] - ratio * x[stratum == h]
        n, N = len(d), units[h]
        if n > 1:
            variance += N ** 2 * (1 - n / N) * d.var(ddof=1) / n
    return float(ratio), float(math.sqrt(variance) / tx)


def stehman_accuracy(map_class, reference, area, stratum, population_units, classes):
    """Accuracy and area estimates when sampling strata differ from the map classes (Stehman 2014),
    area-weighted with the combined ratio estimator for parcels of unequal size."""
    map_class, reference, area = np.asarray(map_class), np.asarray(reference), np.asarray(area, float)
    est = lambda indicator, base=None: stratified_ratio(area * indicator, area if base is None else area * base,
                                                        stratum, population_units)
    overall = est(map_class == reference)
    out = {"overall": overall, "classes": {}}
    for c in classes:
        mapped, ref, both = map_class == c, reference == c, (map_class == c) & (reference == c)
        out["classes"][c] = {
            "area_proportion": est(ref),
            "users": est(both, mapped) if mapped.any() else (math.nan, math.nan),
            "producers": est(both, ref) if ref.any() else (math.nan, math.nan),
            "sampled_map": int(mapped.sum()), "sampled_reference": int(ref.sum())}
    return out
