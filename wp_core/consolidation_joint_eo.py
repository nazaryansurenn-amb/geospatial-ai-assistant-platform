"""Narrow-parcel consolidation screening with explicitly joint satellite evidence."""
import numpy as np

from wp_core.land_consolidation_10m import compare_season


def eligible_mask(register, maximum_m2=5000.):
    good = np.isfinite(register.area_official_m2) & register.area_official_m2.gt(0) & register.area_official_m2.le(maximum_m2)
    good &= register.activity_class.eq("active") & register.activity_state.eq("ready")
    good &= register.screening_state.isin(["candidate", "no_qualifying_group", "similar_group_below_5ha", "spatial_support_review"])
    for field in ("household", "household_agriculture", "road_excluded", "road_excluded_release", "mask_conflict"):
        good &= register[field].notna() & ~register[field].astype(bool)
    return good


def compare_joint_season(days, first, second, support_a, support_b, year, policy):
    # Shared pixels are explicitly permitted, so disable only that veto in the
    # frozen numeric helper. Actual overlap is measured and stored by the caller.
    result = compare_season(days, first, second, support_a, support_b, 0., year, policy)
    result["evidence_mode"] = "joint_10m_not_independent_parcel_confirmation"
    return result


def sampling_footprint(member_indices, pixels):
    memberships = [pixels[i][0] for i in member_indices]
    flattened = np.concatenate(memberships) if memberships else np.array([], dtype=int)
    unique, counts = np.unique(flattened, return_counts=True)
    return {"unique_10m_footprint_pixels": len(unique), "parcel_pixel_memberships": len(flattened),
            "pixels_shared_by_members": int((counts > 1).sum())}
