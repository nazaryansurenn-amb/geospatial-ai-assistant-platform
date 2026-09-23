"""Independent arithmetic replay of candidate pair evidence, without map writes."""
import json
from pathlib import Path

import numpy as np
import pandas as pd

from release_tools import sha256
from run_observation_analysis import write_json
from run_land_consolidation import OUT, ROOT, verify
from wp_core.observation_screening import Policy, isolated_excursions


def main():
    verify()
    manifest = json.loads((OUT / "manifest.json").read_text())
    config = manifest["config"]
    policy = config["policy"]
    pairs = pd.read_parquet(OUT / "pair_checks.parquet")
    members = pd.read_parquet(OUT / "members.parquet")
    candidates = set(members.internal_parcel_id)
    eligible = pairs[pairs.a.isin(candidates) & pairs.b.isin(candidates)].sort_values(["a", "b"])
    # Deterministically spread replay checks throughout the candidate population.
    if len(eligible):
        sample = eligible.iloc[np.unique(np.linspace(0, len(eligible) - 1, min(20, len(eligible))).astype(int))]
    else:
        sample = eligible
    ids = sorted(set(sample.a) | set(sample.b))
    checks = 0
    for year in config["years"]:
        if not ids:
            break
        cols = ["internal_parcel_id", "observation_date", "support", "finite", "ndvi_median", "evi2_median", "ndmi_median", "bsi_median"]
        raw = pd.read_parquet(ROOT / config["eo"] / "daily" / f"{year}.parquet", columns=cols,
                              filters=[("internal_parcel_id", "in", ids)])
        tables = {}
        for pid, frame in raw.groupby("internal_parcel_id", sort=True):
            frame = frame.sort_values("observation_date").copy()
            days = pd.to_datetime(frame.observation_date).dt.dayofyear.to_numpy()
            values = frame[cols[4:]].to_numpy(float)
            valid = (frame.support.to_numpy() >= policy["minimum_valid_fraction"]) & frame.finite.to_numpy() & np.isfinite(values).all(axis=1)
            valid &= ~isolated_excursions(days, values[:, 0], values[:, 1], valid, Policy())
            frame["valid"] = valid
            frame["day"] = days
            tables[pid] = frame.set_index("observation_date")
        for row in sample.itertuples():
            details = {d["year"]: d for d in json.loads(row.year_results)}
            d = details.get(year)
            if not d or not d["similar"]:
                continue
            left, right = tables[row.a], tables[row.b]
            common = left.index.intersection(right.index)
            a, b = left.loc[common], right.loc[common]
            month = pd.to_datetime(common).month
            good = a.valid.to_numpy() & b.valid.to_numpy() & (month >= 3) & (month <= 11)
            a, b = a.loc[good], b.loc[good]
            assert len(a) == d["common_dates"] >= policy["minimum_dates"]
            start = pd.Timestamp(year, 3, 1).dayofyear
            end = pd.Timestamp(year, 11, 30).dayofyear
            gap = int(np.diff(np.r_[start, a.day, end]).max())
            assert gap == d["max_gap_days"] <= policy["maximum_gap_days"]
            av, bv = a[cols[4:]].to_numpy(), b[cols[4:]].to_numpy()
            rms = np.sqrt(np.mean((av - bv) ** 2, axis=0))
            assert np.allclose(rms, d["rmse"], rtol=1e-5, atol=1e-7)
            assert (rms <= np.asarray(policy["maximum_rmse"]) + 1e-7).all()
            corr = [np.corrcoef(av[:, k], bv[:, k])[0, 1] for k in (0, 1)]
            assert min(corr) >= policy["minimum_correlation"]
            checks += 1
    receipt = {"complete_sha256": sha256(OUT / "complete.json"),
        "checker_sha256": sha256(Path(__file__)), "sample_pairs": len(sample),
        "similar_pair_years_independently_replayed": checks, "result": "passed",
        "accuracy_measured": False, "accepted": False}
    destination = ROOT / "server_data/review" / config["version"] / "verification.json"
    if destination.exists():
        assert json.loads(destination.read_text()) == receipt, "Do not replace previous verification"
    else:
        write_json(destination, receipt)
    print(json.dumps(receipt), flush=True)


if __name__ == "__main__":
    main()
