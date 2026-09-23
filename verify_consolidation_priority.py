"""Independent raw-date and shape checks for the private strip-priority screen."""
from datetime import date
from itertools import combinations
import json
from pathlib import Path

import geopandas as gpd
import numpy as np
import pandas as pd

import run_consolidation_priority as run
from release_tools import local_path, sha256
from run_observation_analysis import now, write_json

ROOT = Path(__file__).resolve().parent
REVIEW = ROOT / "server_data/review/consolidation_strip_priority_20260906_v1"


def execute():
    run.verify()
    manifest = run.base.read(run.OUT / "manifest.json")
    policy = manifest["policy"]
    pairs = pd.read_parquet(run.OUT / "pair_checks.parquet").set_index(["a", "b"]).sort_index()
    previous = pd.read_parquet(run.joint.OUT / "pair_checks.parquet").set_index(["a", "b"]).sort_index()
    common = pairs.index.intersection(previous.index)
    pd.testing.assert_frame_equal(pairs.loc[common], previous.loc[common])
    new = pairs.loc[pairs.index.difference(previous.index)]
    chosen = []
    for condition in (new.state.eq("joint_profile_match") & new.shared_10m_fraction.gt(0),
                      new.state.eq("joint_profile_match") & new.shared_10m_fraction.eq(0),
                      new.state.eq("not_repeatedly_similar")):
        part = new[condition]
        if len(part):
            chosen.extend(part.iloc[np.unique(np.linspace(0, len(part) - 1, min(20, len(part))).astype(int))].index)
    blocks = gpd.read_parquet(run.OUT / "blocks.parquet").sort_values(["strip_priority", "area_ha", "block_id"], ascending=[False, False, True])
    for block in blocks.head(6).itertuples():
        links = sorted(combinations(sorted(json.loads(block.member_ids)), 2))
        chosen.extend(links[i] for i in np.unique(np.linspace(0, len(links) - 1, min(12, len(links))).astype(int)))
    sample = pairs.loc[sorted(set(chosen))].reset_index()
    ids = sorted(set(sample.a) | set(sample.b))
    eo = local_path(ROOT, manifest["source_config"]["eo"])
    columns = ["internal_parcel_id", "observation_date", "ndvi_median", "evi2_median", "ndvi_valid_fraction", "evi2_valid_fraction"]
    replayed = positives = 0
    for year in range(2021, 2026):
        frame = pd.read_parquet(eo / "daily" / f"{year}.parquet", columns=columns, filters=[("internal_parcel_id", "in", ids)])
        assert not frame.duplicated(["internal_parcel_id", "observation_date"]).any()
        profiles = {}
        for pid, f in frame.groupby("internal_parcel_id", sort=False):
            f = f.sort_values("observation_date")
            dates = pd.to_datetime(f.observation_date)
            assert dates.dt.year.eq(year).all()
            days = dates.dt.dayofyear.to_numpy()
            values = f[["ndvi_median", "evi2_median"]].to_numpy(np.float32)
            fractions = f[["ndvi_valid_fraction", "evi2_valid_fraction"]].to_numpy(np.float32)
            good = np.isfinite(values).all(axis=1) & np.isfinite(fractions).all(axis=1)
            good &= (fractions >= policy["minimum_valid_fraction"]).all(axis=1) & (fractions <= 1.).all(axis=1)
            positions = np.flatnonzero(good)
            flagged = []
            for left, mid, right in zip(positions[:-2], positions[1:-1], positions[2:]):
                if max(days[mid] - days[left], days[right] - days[mid]) > 10:
                    continue
                ends = values[[left, right]]
                delta = values[mid] - ends
                if np.all(np.abs(ends[0] - ends[1]) <= [.12, .10]) and (np.all(delta > [.35, .25]) or np.all(delta < [-.35, -.25])):
                    flagged.append(mid)
            good[flagged] = False
            profiles[pid] = days, values, good
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
            if len(d) >= policy["minimum_dates"] and gap <= policy["maximum_gap_days"]:
                ranges = np.minimum(np.ptp(a, axis=0), np.ptp(b, axis=0))
                if np.all(ranges >= [policy["minimum_ndvi_range"], policy["minimum_evi2_range"]]):
                    rms = np.sqrt(((a - b) ** 2).mean(axis=0))
                    corr = [float(np.corrcoef(a[:, k], b[:, k])[0, 1]) for k in (0, 1)]
                    phase = min(float(((a >= [policy["green_ndvi"], policy["green_evi2"]]).all(axis=1) == (b >= [policy["green_ndvi"], policy["green_evi2"]]).all(axis=1)).mean()),
                                float(((a <= [policy["low_ndvi"], policy["low_evi2"]]).all(axis=1) == (b <= [policy["low_ndvi"], policy["low_evi2"]]).all(axis=1)).mean()))
                    np.testing.assert_allclose(rms, saved["rmse"], rtol=1e-6, atol=1e-7)
                    np.testing.assert_allclose(corr, saved["correlations"], atol=1e-10)
                    assert np.isclose(phase, saved["phase_agreement"])
                    similar = bool(np.all(rms <= policy["maximum_rmse"][:2]) and min(corr) >= policy["minimum_correlation"] and phase >= policy["minimum_phase_agreement"])
            assert similar == saved["similar"] == bool(pair.support_year_mask & (1 << (year - 2021)))
            replayed += 1
            positives += int(similar)
    register = pd.read_parquet(run.OUT / "register.parquet").set_index("internal_parcel_id")
    basis = gpd.read_parquet(local_path(ROOT, manifest["source_config"]["basis"])).set_index("internal_parcel_id").to_crs(32638)
    shape_ids = set(pd.read_parquet(run.OUT / "members.parquet").internal_parcel_id) | set(ids)
    for pid in sorted(shape_ids):
        geometry = basis.loc[pid].geometry
        rectangle = geometry.minimum_rotated_rectangle
        vertices = list(rectangle.exterior.coords)
        lengths = [((x1 - x0) ** 2 + (y1 - y0) ** 2) ** .5 for (x0, y0), (x1, y1) in zip(vertices, vertices[1:])]
        length, width = max(lengths), min(lengths)
        parts = 1 if geometry.geom_type == "Polygon" else len(geometry.geoms)
        expected = parts == 1 and length >= 100 and width <= 40 and length / width >= 4 and geometry.area / rectangle.area >= .55
        row = register.loc[pid]
        assert bool(row.long_narrow) == expected
        np.testing.assert_allclose([row.strip_length_m, row.strip_width_m, row.strip_elongation], [length, width, length / width], atol=1e-8)
    result = {"created_at": now(), "passed": True, "checker_sha256": sha256(Path(__file__)),
        "complete_sha256": sha256(run.OUT / "complete.json"), "cached_pairs_identical": len(common),
        "sampled_pairs": len(sample), "sampled_new_pairs": len(set(map(tuple, sample[["a", "b"]].to_numpy())) & set(new.index)),
        "raw_parcels": len(ids), "pair_years_replayed": replayed, "positive_pair_years": positives,
        "shape_parcels_rechecked": len(shape_ids), "all_candidate_members_in_shape_check": True,
        "scope": "Independent primary raw-date replay and shape calculations; all groups structurally verified. Not ground-truth or legal feasibility validation."}
    write_json(REVIEW / "verification.json", result)
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    execute()
