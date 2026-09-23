"""Repeated whole-sample vegetation evidence for partial historical seasons."""
import math
import numpy as np
import pandas as pd
from wp_core.activity_change_history import transition, YEARS, CLASSES

MIN_SHARE = 0.50
MIN_DATES = 2


def dated_area_evidence(observations):
    required = {"cadastre_code", "observation_date", "scene_id", "sampling_method",
                "quality_status", "parcel_pixel_count", "valid_pixel_count", "vegetation_fraction"}
    if not required.issubset(observations.columns):
        raise ValueError("Missing dated area evidence columns")
    f = observations.copy()
    f["observation_date"] = pd.to_datetime(f.observation_date, errors="raise").dt.normalize()
    if f.observation_date.isna().any() or f.observation_date.dt.year.nunique() > 1:
        raise ValueError("Evidence must belong to one calendar year")
    for name in ("parcel_pixel_count", "valid_pixel_count", "vegetation_fraction"):
        f[name] = pd.to_numeric(f[name], errors="coerce")
    total, valid, veg = f.parcel_pixel_count, f.valid_pixel_count, f.vegetation_fraction
    usable = (f.quality_status.eq("usable") & f.sampling_method.eq("pixel_center_10m")
              & np.isfinite(total) & np.isfinite(valid) & total.gt(0) & valid.gt(0)
              & valid.le(total) & total.eq(total.round()) & valid.eq(valid.round())
              & np.isfinite(veg) & veg.between(0, 1))
    # The source stores vegetation/valid rounded to four decimals. Recover integer
    # count bounds; use the lower bound when rounding leaves more than one answer.
    lower_count = np.ceil(np.maximum(0, veg - .00005) * valid - 1e-8)
    upper_count = np.floor(np.minimum(1, veg + .00005) * valid + 1e-8)
    usable &= lower_count.le(upper_count) & lower_count.le(valid)
    f["area_share_lower"] = (lower_count / total).where(usable)
    f["area_share_upper"] = (upper_count / total).where(usable)
    f["count_recovery_exact"] = (lower_count.eq(upper_count) & usable)
    f["usable_area_date"] = usable
    f["sample_valid_share"] = (valid / total).where(usable)
    # A same-date second scene must not count as temporal corroboration. Choose
    # the best observed support, not the greenest overlapping scene.
    f = f.sort_values(["cadastre_code", "observation_date", "usable_area_date", "sample_valid_share", "scene_id"],
                      ascending=[True, True, False, False, True])
    return f.drop_duplicates(["cadastre_code", "observation_date"])


def annual_area_evidence(observations, year):
    daily = dated_area_evidence(observations)
    if not daily.empty and not daily.observation_date.dt.year.eq(year).all():
        raise ValueError("Wrong observation year")
    daily["qualifying_date"] = daily.area_share_lower.ge(MIN_SHARE)
    grouped = daily.groupby("cadastre_code", sort=True)
    annual = grouped.agg(usable_area_dates=("usable_area_date", "sum"),
                         qualifying_area_dates=("qualifying_date", "sum"),
                         raw_distinct_dates=("scene_id", "size"))
    usable = daily[daily.usable_area_date].sort_values(["cadastre_code", "area_share_lower", "observation_date"], ascending=[True, False, True])
    second = usable[usable.groupby("cadastre_code").cumcount().eq(1)].set_index("cadastre_code")
    annual["repeated_area_share_lower"] = second.area_share_lower
    annual["passes_area50"] = annual.qualifying_area_dates.ge(MIN_DATES)
    annual["area_evidence_state"] = np.select(
        [annual.usable_area_dates.lt(MIN_DATES), annual.passes_area50],
        ["insufficient_area_evidence", "passes"], default="area50_not_demonstrated")
    annual["analysis_year"] = year
    return annual.reset_index(), daily


def gated_transition(codes, profile_year_count, repeated_shares, household=False, road=False):
    result = transition(codes, profile_year_count, household, road)
    if result[0] not in CLASSES:
        return (*result, "existing_selection")
    if isinstance(codes, str):
        import json
        codes = json.loads(codes)
    if not isinstance(repeated_shares, (list, tuple)) or len(repeated_shares) != len(YEARS):
        return "insufficient", None, "missing_area_evidence"
    partial = [repeated_shares[i] for i, code in enumerate(codes) if code == 2]
    if any(not isinstance(v, (int, float)) or isinstance(v, bool) or not math.isfinite(v) or not 0 <= v <= 1 for v in partial):
        return "insufficient", None, "missing_area_evidence"
    if any(v < MIN_SHARE for v in partial):
        return "not_selected", None, "partial_area50_not_demonstrated"
    return (*result, "passes_area50" if partial else "no_partial_seasons")
