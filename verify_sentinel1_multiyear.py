"""Independent checks of multi-year data extraction, weather and result coverage."""
import json
from pathlib import Path
import geopandas as gpd
import numpy as np
import pandas as pd
import rasterio
from rasterio.features import geometry_mask
from shapely.geometry import mapping
from wp_core import sentinel1_multiyear as m


def main():
    report = m.verify_snapshot()
    scenes = m.peer.read(m.DATA / "scenes.json")
    fields = gpd.read_parquet(m.OLD / "selection.parquet")
    clips = pixel_rows = optical_rows = rain_rows = 0
    for year in range(2021, 2026):
        items = [x for x in scenes["items"] if x["properties"]["datetime"].startswith(str(year))]
        table = m.peer.read(m.DATA / "tables" / f"{year}.json")
        assert m.peer.sha(m.ROOT / table["path"]) == table["sha256"]
        frame = pd.read_parquet(m.ROOT / table["path"])
        assert len(frame) == len(items)*96 and not frame.duplicated(["internal_parcel_id", "source_item"]).any()
        assert set(frame.internal_parcel_id) == set(fields.internal_parcel_id)
        eo = pd.read_parquet(m.ROOT / f"data/analysis/observation_screening/transitions_area_20260906_v1/daily/{year}.parquet",
                             columns=["internal_parcel_id", "observation_date", "support", "finite", "ndvi_median", "ndmi_median", "bsi_median"],
                             filters=[("internal_parcel_id", "in", fields.internal_parcel_id.tolist())])
        eo = eo.loc[eo.support.ge(.6) & eo.finite.astype(bool)].copy()
        eo["date"] = pd.to_datetime(eo.observation_date, utc=True)
        for item in items:
            assert m.checkpoint(item)
            for window in scenes["windows"]:
                with rasterio.open(m.clip_path(item, window)) as src:
                    assert src.count == 2 and src.crs.to_epsg() == 32638 and src.res == (10, 10)
                    assert list(src.bounds) == window["bounds"] and src.tags()["source_item"] == item["id"]
                    clips += 1
                    if item not in (items[0], items[-1]):
                        continue
                    for kind in ("clean_interior", "boundary_challenge"):
                        parcel = fields.loc[fields.window_id.eq(window["id"]) & fields.sample_group.eq(kind)].iloc[0]
                        geom = parcel.geometry.buffer(-10)
                        mask = np.zeros((src.height, src.width), dtype=bool) if geom.is_empty else geometry_mask([mapping(geom)], out_shape=(src.height, src.width), transform=src.transform, invert=True)
                        row = frame.loc[frame.internal_parcel_id.eq(parcel.internal_parcel_id) & frame.source_item.eq(item["id"])].iloc[0]
                        for band, idx in (("vv", 1), ("vh", 2)):
                            values = src.read(idx)
                            values = values[mask & np.isfinite(values) & (values > 0)]
                            expected = np.median(10*np.log10(values)) if len(values) else np.nan
                            np.testing.assert_allclose(expected, row[f"{band}_inner_median_db"], rtol=1e-6, equal_nan=True)
                            assert len(values) == row[f"{band}_inner_valid_pixel_count"]
                        pixel_rows += 1
                        e = eo.loc[eo.internal_parcel_id.eq(parcel.internal_parcel_id)].sort_values("date")
                        d = (e.date-pd.Timestamp(row.datetime)).abs()
                        if len(d) and d.min() <= pd.Timedelta(days=3):
                            hit = e.loc[d.idxmin()]
                            assert str(hit.observation_date) == row.eo_date
                            np.testing.assert_allclose([hit.ndvi_median, hit.ndmi_median, hit.bsi_median], [row.eo_ndvi, row.eo_ndmi, row.eo_bsi], equal_nan=True)
                        else:
                            assert pd.isna(row.eo_date)
                        optical_rows += 1
        annual = next(x for x in report["years"] if x["year"] == year)
        folder = m.OUT / "years" / annual["key"]
        marker = m.peer.read(folder / "complete.json")
        diag = pd.read_parquet(folder / "comparisons.parquet")
        analyzed = pd.read_parquet(folder / "radar_input.parquet")
        assert pd.to_datetime(analyzed.datetime, utc=True, format="ISO8601").dt.strftime("%Y_%m").isin(annual["weather_months_used"]).all()
        weather = pd.concat([pd.read_parquet(m.ROOT / x["path"], columns=["valid_time", "latitude", "longitude", "tp"]) for x in marker["weather_sources"]], ignore_index=True)
        accum = {(float(r.latitude), float(r.longitude), pd.Timestamp(r.valid_time)): float(r.tp) for r in weather.itertuples()}
        points = pd.read_parquet(m.peer.OUT / "fields.parquet").set_index("internal_parcel_id")
        raw = analyzed.set_index(["internal_parcel_id", "source_item"])
        unique = diag.drop_duplicates(["internal_parcel_id", "middle_item"])
        # Check both complete and incomplete totals across the season, without using pipeline deaccumulation.
        indices = sorted(set(np.linspace(0, len(unique)-1, min(40, len(unique))).astype(int)))
        for row in unique.iloc[indices].itertuples():
            sample = raw.loc[[(row.internal_parcel_id, x) for x in (row.first_item, row.middle_item, row.last_item)]]
            assert sample.platform.nunique() == sample.relative_orbit.nunique() == 1
            t = pd.to_datetime(sample.datetime, utc=True, format="ISO8601").tolist()
            point = points.loc[row.internal_parcel_id]
            totals = []
            for start, end in ((t[0],t[1]), (t[1],t[2]), (t[1]-pd.Timedelta(hours=48),t[1])):
                values = []
                for hour in pd.date_range(start.floor("h")+pd.Timedelta(hours=1), end.ceil("h"), freq="h"):
                    current = accum.get((point.latitude, point.longitude, hour), np.nan)
                    previous = 0. if hour.hour == 1 else accum.get((point.latitude, point.longitude, hour-pd.Timedelta(hours=1)), np.nan)
                    values.append(1000*(current-previous) if np.isfinite(current-previous) and current >= previous else np.nan)
                totals.append(sum(values) if values and np.isfinite(values).all() else np.nan)
            np.testing.assert_allclose(totals, [row.rain_before_mm, row.rain_after_mm, row.rain_recent_48h_mm], equal_nan=True)
            np.testing.assert_allclose([sample.vv_inner_median_db.iloc[1]-sample.vv_inner_median_db.iloc[0], sample.vv_inner_median_db.iloc[1]-sample.vv_inner_median_db.iloc[2]], [row.rise_db, row.fall_db], equal_nan=True)
            rain_rows += 1
        eligible = diag.loc[diag.comparable]
        cfg = m.peer.read(m.peer.CONFIG)
        assert eligible.supported.all() and eligible.peer_count.ge(cfg["minimum_peers"]).all()
        assert eligible[["rain_before_mm", "rain_after_mm", "rain_recent_48h_mm"]].le(eligible.rain_limit_mm, axis=0).all().all()
        assert diag.loc[~diag.comparable, "observed_excursion"].isna().all()
        metrics = pd.read_parquet(folder / "metrics.parquet")
        assert metrics.loc[metrics.comparable_triplets.lt(cfg["minimum_comparable_triplets"]), "repeated_relative_signal"].isna().all()
    summary = m.OUT / "snapshots" / report["snapshot_key"]
    parcel_years = pd.read_parquet(summary / "parcel_years.parquet")
    assert len(parcel_years) == 480 and not parcel_years.duplicated(["internal_parcel_id", "year"]).any()
    source = pd.read_parquet(m.ROOT / "data/observations/observations_2021_2025_v1/scope.parquet").set_index("internal_parcel_id")
    selected = source.loc[parcel_years.internal_parcel_id]
    assert selected.included.all() and not selected.household.astype(bool).any() and not selected.road_excluded.astype(bool).any()
    assert selected.cadastre_code.tolist() == parcel_years.cadastre_code.tolist() and selected.area_official_m2.tolist() == parcel_years.area_official_m2.tolist()
    assert parcel_years.high_water_demand_candidate.isna().all()
    assert report["high_water_demand_parcels"] is None and not report["map_changed"]
    result = {"status": "multiyear_independent_verification_passed", "snapshot_key": report["snapshot_key"], "clips_checked": clips,
              "pixel_statistic_rows_recalculated": pixel_rows, "optical_matches_rechecked": optical_rows, "rainfall_windows_recalculated": rain_rows,
              "parcel_seasons_checked": len(parcel_years), "checker_sha256": m.peer.sha(Path(__file__))}
    dest = m.ROOT / "server_data/review" / m.VERSION / ("verification_"+report["snapshot_key"]+".json")
    if dest.exists():
        assert m.peer.read(dest) == result
    else:
        m.peer.write(dest, result)
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
