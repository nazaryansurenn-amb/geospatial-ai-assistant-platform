"""Independently check preserved scope and recompute recorded peer comparisons."""
import json
from pathlib import Path

import numpy as np
import pandas as pd

from wp_core import sentinel1_peer_comparison as m


def main():
    manifest = m.verify_prepared()
    cfg = m.read(m.CONFIG)
    fields = pd.read_parquet(m.OUT / "fields.parquet")
    peers = pd.read_parquet(m.OUT / "potential_peers.parquet")
    scope = pd.read_parquet(m.ROOT / "data/observations/observations_2021_2025_v1/scope.parquet").set_index("internal_parcel_id")
    original = scope.loc[fields.internal_parcel_id]
    assert len(fields) == 96 and fields.internal_parcel_id.is_unique
    assert original.included.all() and not original.household.astype(bool).any() and not original.road_excluded.astype(bool).any()
    assert fields.cadastre_code.tolist() == original.cadastre_code.tolist()
    assert fields.area_official_m2.tolist() == original.area_official_m2.tolist()
    f = fields.set_index("internal_parcel_id")
    for p in peers.itertuples():
        a, b = f.loc[p.internal_parcel_id], f.loc[p.peer_id]
        assert p.internal_parcel_id != p.peer_id and a.cell_id == b.cell_id
        assert a.sample_group == b.sample_group == "clean_interior"
        distance = np.hypot(a.x_m-b.x_m, a.y_m-b.y_m)
        ratio = max(a.area_official_m2, b.area_official_m2)/min(a.area_official_m2, b.area_official_m2)
        np.testing.assert_allclose([distance, ratio], [p.distance_m, p.area_ratio])
        assert distance <= cfg["maximum_neighbor_distance_m"] and ratio <= cfg["maximum_area_ratio"]
    status = "prepared_verified_waiting_for_weather"
    checked = 0
    if (m.OUT / "complete.json").exists():
        report = m.verify_result()
        diag = pd.read_parquet(m.OUT / "comparisons.parquet")
        links = pd.read_parquet(m.OUT / "matched_peers.parquet")
        metrics = pd.read_parquet(m.OUT / "metrics.parquet")
        parcels = pd.read_parquet(m.OUT / "parcels.parquet")
        assert parcels.high_water_demand_candidate.isna().all()
        for column in ("internal_parcel_id", "cadastre_code", "area_official_m2"):
            assert parcels[column].tolist() == fields[column].tolist()
        assert not report["map_changed"] and report["high_water_demand_parcels"] is None
        assert report["confirmed_irrigation_events"] is None and not report["rapid_root_zone_drying_measured"]
        raw = pd.read_parquet(m.ROOT / cfg["pilot"] / "analysis/radar_observations.parquet").set_index(["internal_parcel_id", "source_item"])
        weather_sources = m.read(m.OUT / "complete.json")["weather_sources"]
        weather = pd.concat([pd.read_parquet(m.ROOT / s["path"], columns=["valid_time", "latitude", "longitude", "tp"])
                             for s in weather_sources], ignore_index=True)
        weather["valid_time"] = pd.to_datetime(weather.valid_time, utc=True)
        accum = {(float(r.latitude), float(r.longitude), r.valid_time): float(r.tp) for r in weather.itertuples()}
        rain_cache = {}
        def independent_rain(parcel, start, end):
            lat, lon = float(f.loc[parcel, "latitude"]), float(f.loc[parcel, "longitude"])
            first, last = pd.Timestamp(start).floor("h") + pd.Timedelta(hours=1), pd.Timestamp(end).ceil("h")
            key = (lat, lon, first, last)
            if key not in rain_cache:
                values = []
                for hour in pd.date_range(first, last, freq="h"):
                    current = accum.get((lat, lon, hour), np.nan)
                    previous = 0. if hour.hour == 1 else accum.get((lat, lon, hour-pd.Timedelta(hours=1)), np.nan)
                    delta = current-previous
                    values.append(1000*delta if np.isfinite(delta) and delta >= 0 else np.nan)
                rain_cache[key] = float(sum(values)) if values and np.isfinite(values).all() else np.nan
            return rain_cache[key]
        grouped_links = {k: g.peer_id.tolist() for k, g in links.groupby(["internal_parcel_id", "middle_item"])} if len(links) else {}
        for row in diag.drop_duplicates(["internal_parcel_id", "middle_item"]).itertuples():
            names = [row.first_item, row.middle_item, row.last_item]
            ids = grouped_links.get((row.internal_parcel_id, row.middle_item), [])
            assert len(ids) == row.peer_count and len(ids) <= cfg["maximum_peers"]
            target = raw.loc[[(row.internal_parcel_id, name) for name in names]]
            dates = pd.to_datetime(target.datetime, utc=True, format="ISO8601").tolist()
            totals = [independent_rain(row.internal_parcel_id, dates[0], dates[1]),
                      independent_rain(row.internal_parcel_id, dates[1], dates[2]),
                      independent_rain(row.internal_parcel_id, dates[1]-pd.Timedelta(hours=48), dates[1])]
            np.testing.assert_allclose(totals, [row.rain_before_mm, row.rain_after_mm, row.rain_recent_48h_mm], equal_nan=True)
            rise = target.vv_inner_median_db.iloc[1]-target.vv_inner_median_db.iloc[0]
            fall = target.vv_inner_median_db.iloc[1]-target.vv_inner_median_db.iloc[2]
            np.testing.assert_allclose([rise, fall], [row.rise_db, row.fall_db], equal_nan=True)
            rises, falls = [], []
            for peer in ids:
                b = raw.loc[[(peer, name) for name in names]]
                assert peer != row.internal_parcel_id and f.loc[peer, "cell_id"] == f.loc[row.internal_parcel_id, "cell_id"]
                assert b.radar_usable.all() and target.radar_usable.all()
                assert b.platform.nunique() == target.platform.nunique() == 1
                assert b.platform.iloc[0] == target.platform.iloc[0]
                intervals = np.diff([v.value for v in dates])/86400e9
                assert np.all((intervals > 0) & (intervals <= cfg["maximum_track_gap_days"]))
                for name in ("ndvi", "bsi"):
                    assert np.all(np.abs(b["eo_"+name].to_numpy()-target["eo_"+name].to_numpy()) <= cfg["maximum_"+name+"_difference"])
                rises.append(b.vv_inner_median_db.iloc[1]-b.vv_inner_median_db.iloc[0])
                falls.append(b.vv_inner_median_db.iloc[1]-b.vv_inner_median_db.iloc[2])
            if ids:
                np.testing.assert_allclose([np.median(rises), np.median(falls)], [row.peer_median_rise_db, row.peer_median_fall_db])
            checked += 1
        for row in metrics.itertuples():
            g = diag.loc[diag.internal_parcel_id.eq(row.internal_parcel_id) & diag.relative_orbit.eq(row.relative_orbit)
                         & diag.platform.eq(row.platform) & diag.rain_limit_mm.eq(row.rain_limit_mm) & diag.excursion_db.eq(row.excursion_db) & diag.comparable]
            assert len(g) == row.comparable_triplets
            if len(g):
                count = int((g.rise_db.ge(row.excursion_db) & g.fall_db.ge(row.excursion_db)).sum())
                assert count == row.observed_excursions
                np.testing.assert_allclose(count/len(g), row.observed_excursion_fraction)
            else:
                assert pd.isna(row.observed_excursions) and pd.isna(row.repeated_relative_signal)
        assert diag.loc[~diag.comparable, "observed_excursion"].isna().all()
        available = diag.loc[diag.comparable]
        assert available.supported.all() and available.peer_count.ge(cfg["minimum_peers"]).all()
        assert available[["rain_before_mm", "rain_after_mm", "rain_recent_48h_mm"]].le(available.rain_limit_mm, axis=0).all().all()
        status = "completed_comparison_independently_verified"
    result = {"status": status, "parcels_checked": len(fields), "potential_pairs_checked": len(peers),
              "triplet_statistics_recalculated": checked, "checker_sha256": m.sha(Path(__file__)),
              "prepared_manifest_sha256": m.sha(m.OUT / "prepared.json")}
    dest = m.ROOT / "server_data/review" / m.VERSION / ("result_verification.json" if checked else "preparation_verification.json")
    if dest.exists():
        assert m.read(dest) == result
    else:
        m.write(dest, result)
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
