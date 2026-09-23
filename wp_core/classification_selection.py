"""Apply the owner's parcel-category decision after Transitions v3 aggregation."""
import pandas as pd


def select_parcel_categories(parcels: pd.DataFrame) -> pd.DataFrame:
    required = {"internal_parcel_id", "crop_type_candidate", "annual_cycle_candidate"}
    if not required.issubset(parcels.columns):
        raise ValueError("Missing parcel summary fields")
    if not parcels.internal_parcel_id.is_unique:
        raise ValueError("Duplicate parcel summary")
    valid = {("annual", "single_cycle"), ("annual", "two_cycles"),
             ("annual", "undetermined"), ("perennial", ""),
             ("undetermined", "undetermined")}
    if not set(zip(parcels.crop_type_candidate, parcels.annual_cycle_candidate)) <= valid:
        raise ValueError("Unexpected baseline category")
    result = parcels.copy(deep=True)
    result["annual_cycle_candidate_v3"] = parcels.annual_cycle_candidate
    assign = parcels.crop_type_candidate.eq("annual") & parcels.annual_cycle_candidate.eq("undetermined")
    result.loc[assign, "annual_cycle_candidate"] = "single_cycle"
    result["owner_cycle_assignment_applied"] = assign
    result["cycle_assignment_basis"] = "transitions_v3_result"
    result.loc[assign, "cycle_assignment_basis"] = "owner_rule_annual_uncertain_to_single"
    result["selected_baseline"] = "transitions_20260906_v3"
    return result
