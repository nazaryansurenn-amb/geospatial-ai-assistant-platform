"""Independent full-area checks: identity, pixels, weather, rules and exports."""
import json
import numpy as np
import pandas as pd
import geopandas as gpd
import rasterio
from rasterio.features import geometry_mask, geometry_window

from wp_core import sentinel1_area_data as data
from wp_core import sentinel1_area_compare as fast
from wp_core import sentinel1_peer_comparison as peer
from wp_core import sentinel1_multiyear as multi
from wp_core import sentinel1_pilot as pilot

ROOT, DATA, OUT = data.ROOT, data.DATA, data.OUT
REVIEW = ROOT / "server_data/review" / data.VERSION


def same_table(a, b, keys, cols=None):
    cols = cols or a.columns.tolist()
    pd.testing.assert_frame_equal(a.sort_values(keys)[cols].reset_index(drop=True), b.sort_values(keys)[cols].reset_index(drop=True), check_dtype=False, rtol=1e-7, atol=1e-7)


def main():
    data.pinned()
    prepared = peer.read(OUT / "prepared.json")
    for path, digest in prepared["sources"].items():
        assert peer.sha(ROOT / path) == digest, path
    complete = peer.read(OUT / "complete.json")
    for name, digest in complete["files"].items():
        assert peer.sha(OUT / name) == digest, name
    for year, digest in complete["year_manifests"].items():
        folder = OUT / "years" / year
        assert peer.sha(folder / "complete.json") == digest
        for name, file_hash in peer.read(folder / "complete.json")["files"].items():
            assert peer.sha(folder / name) == file_hash, name
    scope = pd.read_parquet(pilot.SCOPE)
    eligible = scope.loc[scope.included]
    result = gpd.read_parquet(OUT / "all_parcels.parquet").sort_values("internal_parcel_id").reset_index(drop=True)
    assert len(result) == len(eligible) == 22802 and result.internal_parcel_id.is_unique
    assert not eligible.household.any() and not eligible.road_excluded.any()
    assert set(result.internal_parcel_id) == set(eligible.internal_parcel_id)
    original = gpd.read_file(pilot.GEOM, layer="parcels", columns=["cadastre_code", "area_official_m2"])
    original = original.set_index("cadastre_code").loc[result.cadastre_code].reset_index()
    assert original.crs == result.crs
    assert np.array_equal(original.geometry.to_wkb().to_numpy(), result.geometry.to_wkb().to_numpy())
    np.testing.assert_array_equal(original.area_official_m2, result.area_official_m2)
    exported = gpd.read_file(OUT / "parcel_results.gpkg", layer="all_22802_parcels").sort_values("internal_parcel_id").reset_index(drop=True)
    assert len(exported) == 22802 and exported.crs == result.crs
    assert np.array_equal(exported.geometry.to_wkb().to_numpy(), result.geometry.to_wkb().to_numpy())
    same_table(result.drop(columns="geometry"), exported.drop(columns="geometry"), ["internal_parcel_id"])
    csv = pd.read_csv(OUT / "all_parcels.csv", dtype={"cadastre_code": str}, keep_default_na=False)
    assert set(csv.cadastre_code) == set(result.cadastre_code) and len(csv) == 22802
    assert result.water_demand_status.eq("Unassessed").all()
    unsupported = result.sample_group.ne("clean_interior")
    assert unsupported.sum() == 21069 and result.loc[unsupported, "nominal_comparisons"].isna().all()
    assert result.loc[unsupported, "evidence_status"].eq("Insufficient spatial support").all()
    scenes = peer.read(DATA / "scenes.json")
    for item in scenes["items"]:
        assert data.image_checkpoint(item)
        with rasterio.open(DATA / "clips" / (item["id"] + ".tif")) as src:
            assert src.crs.to_epsg() == 32638 and src.count == 2
            assert src.width == scenes["width"] and src.height == scenes["height"]
            np.testing.assert_allclose(src.bounds, scenes["bounds"], rtol=0, atol=1e-6)
    print(json.dumps({"phase": "identity_and_image_checks_passed", "parcels": len(result), "images": len(scenes["items"])}), flush=True)
    fields = pd.read_parquet(DATA / "fields.parquet")
    clean = fields.loc[fields.sample_group.eq("clean_interior")]
    potential = pd.read_parquet(OUT / "potential_peers.parquet")
    cfg = peer.read(peer.CONFIG)
    geoms = gpd.read_parquet(DATA / "parcels_utm.parquet").set_index("internal_parcel_id").geometry
    counters = {"pilot_radar_rows_reproduced": 0, "independent_pixel_statistics": 0, "checked_rain_windows": 0, "reference_comparison_parcels": 0, "checked_metric_rows": 0}
    reference_cases = []
    all_signal_ids, nominal_ids = set(), set()
    for year in range(2021, 2026):
        folder = OUT / "years" / str(year)
        obs = pd.read_parquet(folder / "radar_input.parquet")
        assert not obs.duplicated(["internal_parcel_id", "source_item"]).any()
        assert set(obs.internal_parcel_id) == set(clean.internal_parcel_id)
        assert len(obs) == obs.source_item.nunique() * len(clean)
        old_record = peer.read(multi.DATA / "tables" / f"{year}.json")
        old = pd.read_parquet(ROOT / old_record["path"])
        old = old.loc[old.internal_parcel_id.isin(clean.internal_parcel_id) & old.source_item.isin(obs.source_item)]
        new = obs.merge(old[["internal_parcel_id", "source_item"]], on=["internal_parcel_id", "source_item"], validate="one_to_one")
        cols = [c for c in old.columns if c.startswith(("vv_", "vh_", "eo_"))] + ["radar_usable", "internal_parcel_id", "source_item"]
        same_table(old, new, ["internal_parcel_id", "source_item"], cols)
        counters["pilot_radar_rows_reproduced"] += len(new)
        for row in obs.sample(n=4, random_state=year).itertuples():
            with rasterio.open(DATA / "clips" / (row.source_item + ".tif")) as src:
                for mask_name, geom in (("full", geoms[row.internal_parcel_id]), ("inner", geoms[row.internal_parcel_id].buffer(-10))):
                    window = geometry_window(src, [geom.__geo_interface__])
                    arrays = src.read(window=window)
                    mask = geometry_mask([geom.__geo_interface__], out_shape=arrays.shape[1:], transform=src.window_transform(window), invert=True)
                    for band, array in zip(("vv", "vh"), arrays):
                        valid = array[mask & np.isfinite(array) & (array > 0)]
                        assert mask.sum() == getattr(row, f"{band}_{mask_name}_pixel_count")
                        assert len(valid) == getattr(row, f"{band}_{mask_name}_valid_pixel_count")
                        np.testing.assert_allclose(np.median(10*np.log10(valid)), getattr(row, f"{band}_{mask_name}_median_db"), rtol=0, atol=1e-6)
                        counters["independent_pixel_statistics"] += 1
        diag = pd.read_parquet(folder / "comparisons.parquet")
        links = pd.read_parquet(folder / "matched_peers.parquet")
        metrics = pd.read_parquet(folder / "metrics.parquet")
        signals = pd.read_parquet(folder / "signals.parquet")
        hourly = pd.read_parquet(folder / "weather_hourly.parquet")
        rain = {f"era5land_{a:.1f}_{b:.1f}": g.set_index("valid_time").rain_mm for (a,b),g in hourly.groupby(["latitude", "longitude"])}
        dk = ["internal_parcel_id", "middle_item"]
        assert not diag.duplicated(dk).any()
        selected = signals.loc[signals.relative_excursion].drop_duplicates(dk).merge(diag, on=dk, suffixes=("_signal", ""), validate="one_to_one")
        all_signal_ids.update(selected.internal_parcel_id)
        nominal = signals.loc[signals.relative_excursion & signals.rain_limit_mm.eq(1) & signals.excursion_db.eq(2)]
        nominal_ids.update(nominal.internal_parcel_id)
        for row in selected.itertuples():
            series = rain[row.cell_id]
            for start, end, value in ((row.first_datetime, row.middle_datetime, row.rain_before_mm), (row.middle_datetime, row.last_datetime, row.rain_after_mm), (pd.Timestamp(row.middle_datetime)-pd.Timedelta(hours=48), row.middle_datetime, row.rain_recent_48h_mm)):
                times = pd.date_range(pd.Timestamp(start).floor("h")+pd.Timedelta(hours=1), pd.Timestamp(end).ceil("h"), freq="h")
                values = series.reindex(times).to_numpy()
                assert np.isfinite(values).all()
                np.testing.assert_allclose(np.sum(values), value, rtol=0, atol=1e-6)
                counters["checked_rain_windows"] += 1
            choices = links.loc[links.internal_parcel_id.eq(row.internal_parcel_id) & links.middle_item.eq(row.middle_item)]
            peers = diag.loc[diag.middle_item.eq(row.middle_item) & diag.internal_parcel_id.isin(choices.peer_id)]
            assert len(peers) == row.peer_count >= 3 and peers.supported.all() and peers.cell_id.eq(row.cell_id).all()
            np.testing.assert_allclose(peers.rise_db.median(), row.peer_median_rise_db, rtol=0, atol=1e-7)
            np.testing.assert_allclose(peers.fall_db.median(), row.peer_median_fall_db, rtol=0, atol=1e-7)
        keys = ["internal_parcel_id", "relative_orbit", "platform"]
        for (rain_limit, db), values in metrics.groupby(["rain_limit_mm", "excursion_db"]):
            comparable = diag.supported & diag.peer_count.ge(3) & diag.weather_complete & diag[["rain_before_mm", "rain_after_mm", "rain_recent_48h_mm"]].max(axis=1).le(rain_limit)
            event = comparable & diag.rise_db.ge(db) & diag.fall_db.ge(db)
            relative = event & (diag.rise_db-diag.peer_median_rise_db).ge(1) & (diag.fall_db-diag.peer_median_fall_db).ge(1)
            expected_signals = diag.loc[event, dk].sort_values(dk).reset_index(drop=True)
            actual_signals = signals.loc[signals.rain_limit_mm.eq(rain_limit) & signals.excursion_db.eq(db), dk].sort_values(dk).reset_index(drop=True)
            pd.testing.assert_frame_equal(expected_signals, actual_signals)
            ordered = values.set_index(keys)
            for column, mask in (("comparable_triplets", comparable), ("observed_excursions", event), ("relative_excursions", relative)):
                expected = diag.loc[mask].groupby(keys).size().reindex(ordered.index, fill_value=0)
                np.testing.assert_array_equal(expected, ordered[column].fillna(0))
            assert ordered.loc[ordered.comparable_triplets.lt(6), "repeated_relative_signal"].isna().all()
            counters["checked_metric_rows"] += len(ordered)
        # Real positive triplets with ALL peers in their weather cell retained.
        if len(nominal):
            target = diag.merge(nominal[dk], on=dk, validate="one_to_one").iloc[0]
            cell_fields = clean.loc[clean.cell_id.eq(target.cell_id)]
            items = [target.first_item, target.middle_item, target.last_item]
            subset = obs.loc[obs.internal_parcel_id.isin(cell_fields.internal_parcel_id) & obs.source_item.isin(items)]
            choices = potential.loc[potential.internal_parcel_id.isin(cell_fields.internal_parcel_id)]
            old_diag, old_links, old_metrics, _ = peer.compare(subset, cell_fields, choices, rain, cfg)
            new_diag, new_links, new_metrics, new_signals = fast.compare(subset, cell_fields, choices, rain, cfg)
            same_table(old_metrics, new_metrics, keys+["rain_limit_mm", "excursion_db"])
            same_table(old_links, new_links, dk+["peer_id"])
            cols = dk+["supported", "peer_count", "weather_complete", "rise_db", "fall_db", "peer_median_rise_db", "peer_median_fall_db"]
            same_table(old_diag.drop_duplicates(dk), new_diag, dk, cols)
            same_table(old_diag.loc[old_diag.observed_excursion.fillna(False)], new_signals, dk+["rain_limit_mm", "excursion_db"], dk+["rain_limit_mm", "excursion_db", "relative_excursion"])
            counters["reference_comparison_parcels"] += len(cell_fields)
            reference_cases.append({"year": year, "cell": target.cell_id, "parcels": len(cell_fields), "middle_item": target.middle_item})
        print(json.dumps({"phase": "year_verified", "year": year, **counters}), flush=True)
    assert nominal_ids == set(result.loc[result.nominal_relative_excursions.fillna(0).gt(0), "internal_parcel_id"])
    assert all_signal_ids == set(result.loc[result.relative_excursions_any_sensitivity.fillna(0).gt(0), "internal_parcel_id"])
    report = {"status": "passed", "checked_utc": peer.utc(), "parcels": 22802, "images": len(scenes["items"]), **counters,
              "nominal_inspection_parcels": len(nominal_ids), "all_inspection_parcels": len(all_signal_ids), "reference_cases": reference_cases,
              "result_manifest_sha256": peer.sha(OUT / "complete.json"), "checker_sha256": peer.sha(ROOT / "verify_sentinel1_area_analysis.py"),
              "geometry_codes_official_area_unchanged": True, "household_and_road_exclusions_preserved": True, "map_configuration_unchanged": True}
    peer.write(REVIEW / "verification.json", report)
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
