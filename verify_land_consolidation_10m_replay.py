"""Independent raw-date numeric replay of all positive 10 m pairs and controls."""
from datetime import date
import json
from pathlib import Path

import numpy as np
import pandas as pd

import run_land_consolidation as base
import run_land_consolidation_10m as ten
import run_land_consolidation_small_parcels as small
from release_tools import local_path, sha256
from run_observation_analysis import now, write_json

ROOT = Path(__file__).resolve().parent
REVIEW = ROOT / "server_data/review/consolidation_small_parcels_20260906_v1"
NAMES = ("ndvi", "evi2", "ndmi", "bsi")


def execute():
    small.verify()
    manifest = base.read(ten.OUT / "manifest.json")
    policy = manifest["resolved_policy"]
    pairs = pd.read_parquet(ten.OUT / "pair_checks.parquet")
    checked = pd.concat([pairs[pairs.reason.eq("similar")], pairs.groupby("reason", sort=True).head(12)]).drop_duplicates(["a", "b"]).sort_values(["a", "b"])
    ids = sorted(set(checked.a) | set(checked.b))
    positions = {pid: i for i, pid in enumerate(ids)}
    pixels = base.load_pixels(manifest["source_config"], ids)
    shared = {}
    for pair in checked.itertuples():
        ratios = []
        for pid, other in ((pair.a, pair.b), (pair.b, pair.a)):
            p, w = pixels[10][positions[pid]]
            q, _ = pixels[10][positions[other]]
            ratios.append(float(w[np.isin(p, q)].sum() / w.sum()))
        shared[pair.a, pair.b] = max(ratios)
        assert np.isclose(max(ratios), pair.shared_10m_fraction, rtol=1e-7, atol=1e-9)
        if pair.reason == "shared_10m_pixel_review":
            assert max(ratios) > policy["maximum_shared_pixel_fraction"]
            assert pair.support_year_mask == 0 and json.loads(pair.year_results) == []
    replayed, supporting = 0, 0
    eo = local_path(ROOT, manifest["source_config"]["eo"])
    columns = ["internal_parcel_id", "observation_date"] + [n + suffix for suffix in ("_median", "_valid_fraction") for n in NAMES]
    for year in range(2021, 2026):
        raw = pd.read_parquet(eo / "daily" / f"{year}.parquet", columns=columns, filters=[("internal_parcel_id", "in", ids)])
        assert not raw.duplicated(["internal_parcel_id", "observation_date"]).any()
        profiles = {}
        for pid, frame in raw.groupby("internal_parcel_id", sort=False):
            frame = frame.sort_values("observation_date")
            dates = pd.to_datetime(frame.observation_date)
            assert dates.dt.year.eq(year).all()
            days = dates.dt.dayofyear.to_numpy()
            values = frame[[n + "_median" for n in NAMES]].to_numpy(np.float32)
            fractions = frame[[n + "_valid_fraction" for n in NAMES]].to_numpy(np.float32)
            valid = np.isfinite(values) & np.isfinite(fractions) & (fractions >= policy["minimum_valid_fraction"]) & (fractions <= 1.)
            values[~valid] = np.nan
            good = np.flatnonzero(np.isfinite(values[:, :2]).all(axis=1))
            flagged = []
            for left, middle, right in zip(good[:-2], good[1:-1], good[2:]):
                if max(days[middle] - days[left], days[right] - days[middle]) > 10:
                    continue
                ends = values[[left, right], :2]
                if np.any(np.abs(ends[0] - ends[1]) > [.12, .10]):
                    continue
                changes = values[middle, :2] - ends
                if np.all(changes > [.35, .25]) or np.all(changes < [-.35, -.25]):
                    flagged.append(middle)
            values[flagged, :2] = np.nan
            profiles[pid] = (days, values, np.min(fractions[:, :2], axis=1))
        start = (date(year, 3, 1) - date(year, 1, 1)).days + 1
        end = (date(year, 11, 30) - date(year, 1, 1)).days + 1
        for pair in checked.itertuples():
            saved = [d for d in json.loads(pair.year_results) if d["year"] == year]
            if not saved:
                continue
            saved = saved[0]
            da, va, sa = profiles[pair.a]
            db, vb, sb = profiles[pair.b]
            days, ia, ib = np.intersect1d(da, db, return_indices=True)
            a, b, support = va[ia], vb[ib], np.minimum(sa[ia], sb[ib])
            good = np.isfinite(a[:, :2]).all(axis=1) & np.isfinite(b[:, :2]).all(axis=1)
            good &= np.isfinite(support) & (support >= policy["minimum_valid_fraction"]) & (support <= 1.)
            good &= (days >= start) & (days <= end)
            clean = shared[pair.a, pair.b] <= policy["maximum_shared_pixel_fraction"] * support
            assert int((good & ~clean).sum()) == saved["shared_pixel_dates_excluded"]
            good &= clean
            selected_days = days[good]
            gap = int(np.diff(np.r_[start, selected_days, end]).max())
            assert len(selected_days) == saved["common_dates"] and gap == saved["max_gap_days"]
            similar = False
            if len(selected_days) >= policy["minimum_dates"] and gap <= policy["maximum_gap_days"]:
                av, bv = a[good, :2], b[good, :2]
                ranges = np.minimum(np.ptp(av, axis=0), np.ptp(bv, axis=0))
                contrast = np.all(ranges >= [policy["minimum_ndvi_range"], policy["minimum_evi2_range"]])
                if contrast:
                    rms = np.sqrt(((av - bv) ** 2).mean(axis=0))
                    corr = [float(np.corrcoef(av[:, k], bv[:, k])[0, 1]) for k in (0, 1)]
                    green_a = (av >= [policy["green_ndvi"], policy["green_evi2"]]).all(axis=1)
                    green_b = (bv >= [policy["green_ndvi"], policy["green_evi2"]]).all(axis=1)
                    low_a = (av <= [policy["low_ndvi"], policy["low_evi2"]]).all(axis=1)
                    low_b = (bv <= [policy["low_ndvi"], policy["low_evi2"]]).all(axis=1)
                    phase = min(float((green_a == green_b).mean()), float((low_a == low_b).mean()))
                    np.testing.assert_allclose(rms, saved["rmse"], rtol=1e-6, atol=1e-7)
                    np.testing.assert_allclose(corr, saved["correlations"], atol=1e-10)
                    assert np.isclose(phase, saved["phase_agreement"])
                    similar = bool(np.all(rms <= policy["maximum_rmse"][:2]) and min(corr) >= policy["minimum_correlation"] and phase >= policy["minimum_phase_agreement"])
            assert similar == saved["similar"]
            assert similar == bool(pair.support_year_mask & (1 << (year - 2021)))
            replayed += 1
            supporting += int(similar)
    result = {"created_at": now(), "checker_sha256": sha256(Path(__file__)),
              "tenm_complete_sha256": sha256(ten.OUT / "complete.json"),
              "size_rule_complete_sha256": sha256(small.OUT / "complete.json"),
              "pairs_checked": len(checked), "all_positive_10m_pairs_checked": int(pairs.reason.eq("similar").sum()),
              "raw_pair_years_replayed": replayed, "supporting_pair_years": supporting,
              "selected_raw_parcels": len(ids), "passed": True,
              "scope": "Independent raw-date primary quality, contamination, RMSE, correlation and phase replay; not field validation. Sealed-output verifier checks every size-filtered group."}
    write_json(REVIEW / "verification.json", result)
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    execute()
