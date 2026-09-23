"""Independent arithmetic and coverage check for the completed partial comparison."""
import json
from pathlib import Path

import numpy as np
import pandas as pd
import run_sentinel1_peer_partial as p


def main():
    report = p.verify_files()
    snapshot = p.base.read(p.OUT / "snapshot.json")
    cfg = snapshot["rules"]
    radar = pd.read_parquet(p.OUT / "radar_input.parquet")
    dates = pd.to_datetime(radar.datetime, utc=True, format="ISO8601")
    assert dates.dt.strftime("%Y_%m").isin(snapshot["weather_months"]).all()
    original = pd.read_parquet(p.ROOT / cfg["pilot"] / "analysis/radar_observations.parquet")
    expected = original.loc[pd.to_datetime(original.datetime, utc=True, format="ISO8601").dt.strftime("%Y_%m").isin(snapshot["weather_months"])]
    pd.testing.assert_frame_equal(radar.reset_index(drop=True), expected.reset_index(drop=True))
    fields = pd.read_parquet(p.base.OUT / "fields.parquet").set_index("internal_parcel_id")
    parcels = pd.read_parquet(p.OUT / "parcels.parquet").set_index("internal_parcel_id")
    pd.testing.assert_frame_equal(parcels[["cadastre_code", "area_official_m2"]], fields[["cadastre_code", "area_official_m2"]])
    assert len(parcels) == 96 and parcels.high_water_demand_candidate.isna().all()
    weather = pd.concat([pd.read_parquet(p.ROOT / x["path"], columns=["valid_time", "latitude", "longitude", "tp"]) for x in snapshot["weather_sources"]], ignore_index=True)
    accum = {(r.latitude, r.longitude, pd.Timestamp(r.valid_time)): float(r.tp) for r in weather.itertuples()}
    def rain(parcel, first, last):
        f = fields.loc[parcel]
        numbers = []
        for hour in pd.date_range(first.floor("h")+pd.Timedelta(hours=1), last.ceil("h"), freq="h"):
            a = accum.get((f.latitude, f.longitude, hour), np.nan)
            b = 0. if hour.hour == 1 else accum.get((f.latitude, f.longitude, hour-pd.Timedelta(hours=1)), np.nan)
            numbers.append(1000*(a-b) if np.isfinite(a-b) and a >= b else np.nan)
        return sum(numbers) if numbers and np.isfinite(numbers).all() else np.nan
    raw = radar.set_index(["internal_parcel_id", "source_item"])
    diag = pd.read_parquet(p.OUT / "comparisons.parquet")
    links = pd.read_parquet(p.OUT / "matched_peers.parquet")
    counts = 0
    for row in diag.drop_duplicates(["internal_parcel_id", "middle_item"]).itertuples():
        items = [row.first_item, row.middle_item, row.last_item]
        a = raw.loc[[(row.internal_parcel_id, item) for item in items]]
        t = pd.to_datetime(a.datetime, utc=True, format="ISO8601").tolist()
        assert a.platform.nunique() == a.relative_orbit.nunique() == 1
        totals = [rain(row.internal_parcel_id, t[0], t[1]), rain(row.internal_parcel_id, t[1], t[2]), rain(row.internal_parcel_id, t[1]-pd.Timedelta(hours=48), t[1])]
        np.testing.assert_allclose(totals, [row.rain_before_mm, row.rain_after_mm, row.rain_recent_48h_mm], equal_nan=True)
        np.testing.assert_allclose([a.vv_inner_median_db.iloc[1]-a.vv_inner_median_db.iloc[0], a.vv_inner_median_db.iloc[1]-a.vv_inner_median_db.iloc[2]], [row.rise_db, row.fall_db], equal_nan=True)
        peer_ids = links.loc[links.internal_parcel_id.eq(row.internal_parcel_id) & links.middle_item.eq(row.middle_item), "peer_id"].tolist() if len(links) else []
        assert len(peer_ids) == row.peer_count and row.internal_parcel_id not in peer_ids
        rises, falls = [], []
        for peer in peer_ids:
            b = raw.loc[[(peer, item) for item in items]]
            assert fields.loc[peer, "cell_id"] == fields.loc[row.internal_parcel_id, "cell_id"]
            assert b.radar_usable.all() and a.radar_usable.all()
            for index in ("ndvi", "bsi"):
                assert (np.abs(b["eo_"+index].to_numpy()-a["eo_"+index].to_numpy()) <= cfg["maximum_"+index+"_difference"]).all()
            rises.append(b.vv_inner_median_db.iloc[1]-b.vv_inner_median_db.iloc[0])
            falls.append(b.vv_inner_median_db.iloc[1]-b.vv_inner_median_db.iloc[2])
        if peer_ids:
            np.testing.assert_allclose([np.median(rises), np.median(falls)], [row.peer_median_rise_db, row.peer_median_fall_db])
        counts += 1
    eligible = diag.loc[diag.comparable]
    assert eligible.supported.all() and eligible.peer_count.ge(cfg["minimum_peers"]).all()
    assert eligible[["rain_before_mm", "rain_after_mm", "rain_recent_48h_mm"]].le(eligible.rain_limit_mm, axis=0).all().all()
    assert diag.loc[~diag.comparable, "observed_excursion"].isna().all()
    metrics = pd.read_parquet(p.OUT / "metrics.parquet")
    assert metrics.loc[metrics.comparable_triplets.lt(cfg["minimum_comparable_triplets"]), "repeated_relative_signal"].isna().all()
    assert report["high_water_demand_parcels"] is None and not report["map_changed"]
    result = {"status": "partial_comparison_independently_verified", "parcels_checked": len(parcels),
              "triplets_recalculated": counts, "rainfall_recomputed_from_raw_accumulations": True,
              "outside_weather_dates_excluded": True, "checker_sha256": p.base.sha(Path(__file__)),
              "result_manifest_sha256": p.base.sha(p.OUT / "complete.json")}
    dest = p.ROOT / "server_data/review" / p.VERSION / "verification.json"
    if dest.exists():
        assert p.base.read(dest) == result
    else:
        p.base.write(dest, result)
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
