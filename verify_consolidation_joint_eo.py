"""Independent raw-date replay across shared, unshared and rejected pair controls."""
from datetime import date
from itertools import combinations
import json
from pathlib import Path

import numpy as np
import pandas as pd

import run_consolidation_joint_eo as joint
from release_tools import local_path, sha256
from run_observation_analysis import now, write_json

ROOT = Path(__file__).resolve().parent
REVIEW = ROOT / "server_data/review/consolidation_joint_eo_20260906_v1"


def execute():
    joint.verify()
    manifest = joint.base.read(joint.OUT / "manifest.json")
    p = manifest["policy"]
    pairs = pd.read_parquet(joint.OUT / "pair_checks.parquet").sort_values(["shared_10m_fraction", "a", "b"])
    groups = pd.read_parquet(joint.OUT / "groups.parquet").sort_values("area_ha", ascending=False)
    chosen = []
    for condition in (pairs.state.eq("joint_profile_match") & pairs.shared_10m_fraction.gt(0),
                      pairs.state.eq("joint_profile_match") & pairs.shared_10m_fraction.eq(0),
                      pairs.state.eq("not_repeatedly_similar")):
        part = pairs[condition]
        if len(part):
            chosen.append(part.iloc[np.unique(np.linspace(0, len(part) - 1, min(40, len(part))).astype(int))])
    for row in groups.head(6).itertuples():
        links = set(combinations(sorted(json.loads(row.member_ids)), 2))
        part = pairs[[tuple(v) in links for v in pairs[["a", "b"]].to_numpy()]]
        chosen.append(part.iloc[np.unique(np.linspace(0, len(part) - 1, min(12, len(part))).astype(int))])
    sample = pd.concat(chosen).drop_duplicates(["a", "b"]).sort_values(["a", "b"])
    ids = sorted(set(sample.a) | set(sample.b))
    eo = local_path(ROOT, manifest["source_config"]["eo"])
    columns = ["internal_parcel_id", "observation_date", "ndvi_median", "evi2_median", "ndvi_valid_fraction", "evi2_valid_fraction"]
    replayed, positives = 0, 0
    for year in range(2021, 2026):
        frame = pd.read_parquet(eo / "daily" / f"{year}.parquet", columns=columns, filters=[("internal_parcel_id", "in", ids)])
        assert not frame.duplicated(["internal_parcel_id", "observation_date"]).any()
        profiles = {}
        for pid, f in frame.groupby("internal_parcel_id", sort=False):
            f = f.sort_values("observation_date")
            dates = pd.to_datetime(f.observation_date)
            assert dates.dt.year.eq(year).all()
            days = dates.dt.dayofyear.to_numpy()
            v = f[["ndvi_median", "evi2_median"]].to_numpy(np.float32)
            fractions = f[["ndvi_valid_fraction", "evi2_valid_fraction"]].to_numpy(np.float32)
            good = np.isfinite(v).all(axis=1) & np.isfinite(fractions).all(axis=1)
            good &= (fractions >= p["minimum_valid_fraction"]).all(axis=1) & (fractions <= 1.).all(axis=1)
            positions = np.flatnonzero(good)
            flagged = []
            for left, mid, right in zip(positions[:-2], positions[1:-1], positions[2:]):
                if max(days[mid] - days[left], days[right] - days[mid]) > 10:
                    continue
                ends = v[[left, right]]
                if np.any(np.abs(ends[0] - ends[1]) > [.12, .10]):
                    continue
                delta = v[mid] - ends
                if np.all(delta > [.35, .25]) or np.all(delta < [-.35, -.25]):
                    flagged.append(mid)
            good[flagged] = False
            profiles[pid] = (days, v, good)
        start = (date(year, 3, 1) - date(year, 1, 1)).days + 1
        end = (date(year, 11, 30) - date(year, 1, 1)).days + 1
        for pair in sample.itertuples():
            saved = next(d for d in json.loads(pair.year_results) if d["year"] == year)
            da, va, ga = profiles[pair.a]
            db, vb, gb = profiles[pair.b]
            days, ia, ib = np.intersect1d(da, db, return_indices=True)
            good = ga[ia] & gb[ib] & (days >= start) & (days <= end)
            d, a, b = days[good], va[ia[good]], vb[ib[good]]
            gap = int(np.diff(np.r_[start, d, end]).max())
            assert saved["common_dates"] == len(d) and saved["max_gap_days"] == gap
            assert saved["shared_pixel_dates_excluded"] == 0
            assert saved["evidence_mode"] == "joint_10m_not_independent_parcel_confirmation"
            similar = False
            if len(d) >= p["minimum_dates"] and gap <= p["maximum_gap_days"]:
                ranges = np.minimum(np.ptp(a, axis=0), np.ptp(b, axis=0))
                if np.all(ranges >= [p["minimum_ndvi_range"], p["minimum_evi2_range"]]):
                    rms = np.sqrt(((a - b) ** 2).mean(axis=0))
                    corr = [float(np.corrcoef(a[:, k], b[:, k])[0, 1]) for k in (0, 1)]
                    phase = min(float(((a >= [p["green_ndvi"], p["green_evi2"]]).all(axis=1) == (b >= [p["green_ndvi"], p["green_evi2"]]).all(axis=1)).mean()),
                                float(((a <= [p["low_ndvi"], p["low_evi2"]]).all(axis=1) == (b <= [p["low_ndvi"], p["low_evi2"]]).all(axis=1)).mean()))
                    np.testing.assert_allclose(rms, saved["rmse"], rtol=1e-6, atol=1e-7)
                    np.testing.assert_allclose(corr, saved["correlations"], atol=1e-10)
                    assert np.isclose(phase, saved["phase_agreement"])
                    similar = bool(np.all(rms <= p["maximum_rmse"][:2]) and min(corr) >= p["minimum_correlation"] and phase >= p["minimum_phase_agreement"])
            assert similar == saved["similar"] == bool(pair.support_year_mask & (1 << (year - 2021)))
            replayed += 1
            positives += int(similar)
    result = {"created_at": now(), "passed": True, "checker_sha256": sha256(Path(__file__)),
        "complete_sha256": sha256(joint.OUT / "complete.json"), "sampled_pairs": len(sample),
        "raw_parcels": len(ids), "pair_years_replayed": replayed, "positive_pair_years": positives,
        "largest_groups_represented": min(6, len(groups)), "all_groups_structurally_verified": len(groups),
        "scope": "Independent primary raw-date numerical replay plus all-group size, identity, connectivity and common-year checks; not field accuracy or independent confirmation of shared-pixel parcels."}
    write_json(REVIEW / "verification.json", result)
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    execute()
