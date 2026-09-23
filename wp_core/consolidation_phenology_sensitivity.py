"""Re-score sealed phenology metrics without relaxing observation-quality gates."""
from dataclasses import dataclass
import json

import numpy as np


@dataclass(frozen=True)
class Policy:
    minimum_years: int = 2
    minimum_correlation: float = .65
    maximum_rmse: tuple = (.15, .12, .12, .15)
    minimum_phase_agreement: float = .70

    def __post_init__(self):
        if not 2 <= self.minimum_years <= 5:
            raise ValueError("At least two completed seasons required")
        if not 0 < self.minimum_correlation <= 1 or not 0 < self.minimum_phase_agreement <= 1:
            raise ValueError("Invalid correlation or phase threshold")
        if len(self.maximum_rmse) != 4 or not np.isfinite(self.maximum_rmse).all() or min(self.maximum_rmse) <= 0:
            raise ValueError("Invalid index tolerances")


def rescore_pair(record, policy):
    if record["screen"] not in ("four_index", "additional_10m"):
        raise ValueError("Unknown evidence screen")
    details = json.loads(record["year_results"])
    if "shared" in record["reason"]:
        if details or record["support_year_mask"]:
            raise ValueError("Contradictory shared-pixel exclusion")
        return {"support_year_mask": 0, "similar": False, "state": "shared_pixel_review", "year_results": []}
    if sorted(d["year"] for d in details) != list(range(2021, 2026)):
        raise ValueError("Five unique completed-season decisions required")
    mask, results = 0, []
    for saved in sorted(details, key=lambda d: d["year"]):
        item = {"year": saved["year"], "similar": False, "reason": saved["reason"]}
        if saved["comparable"]:
            # Coverage and contrast failures never become matches through relaxed tolerances.
            if saved["reason"] not in ("similar", "different_profile"):
                raise ValueError("Inconsistent comparable state")
            count = 4 if record["screen"] == "four_index" else 2
            rms, corr = np.asarray(saved["rmse"]), np.asarray(saved["correlations"])
            phase = saved["phase_agreement"]
            if rms.shape != (count,) or corr.shape != (2,) or not np.isfinite(np.r_[rms, corr, phase]).all():
                raise ValueError("Incomplete phenology metrics")
            if (rms < 0).any() or (np.abs(corr) > 1.000001).any() or not 0 <= phase <= 1:
                raise ValueError("Invalid phenology metrics")
            failures = []
            if np.any(rms > np.asarray(policy.maximum_rmse[:count])):
                failures.append("index_difference")
            if min(corr) < policy.minimum_correlation:
                failures.append("curve_correlation")
            if phase < policy.minimum_phase_agreement:
                failures.append("phase_agreement")
            item.update(similar=not failures, reason="similar" if not failures else "different_profile", failed_checks=failures)
            if not failures:
                mask |= 1 << (saved["year"] - 2021)
        results.append(item)
    similar = mask.bit_count() >= policy.minimum_years
    return {"support_year_mask": mask, "similar": similar,
            "state": "similar" if similar else "not_repeatedly_similar", "year_results": results}


def connected_components(ids, edges, area_m2):
    """Connectivity upper bounds only; components are NOT all-pair-similar groups."""
    if len(ids) != len(set(ids)):
        raise ValueError("Duplicate parcel identities")
    if set(area_m2) != set(ids) or any(not np.isfinite(v) or v <= 0 or v > 5000 for v in area_m2.values()):
        raise ValueError("Invalid or oversized member area")
    parent = {i: i for i in ids}
    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i
    for a, b in sorted(edges):
        if a not in parent or b not in parent or a == b:
            raise ValueError("Invalid adjacency endpoint")
        left, right = sorted((find(a), find(b)))
        parent[right] = left
    groups = {}
    for pid in sorted(ids):
        groups.setdefault(find(pid), []).append(pid)
    result = [{"member_ids": group, "parcel_count": len(group),
               "area_ha": float(sum(area_m2[i] for i in group)) / 10000}
              for group in groups.values() if len(group) >= 2]
    return sorted(result, key=lambda g: (-g["area_ha"], g["member_ids"]))
