"""Independent checker v2: float32-aware comparisons and all sensitivity candidates.

The original sealed checker is preserved. Its 1e-9 relative tolerance was below
the documented float32 storage/arithmetic precision. No analysis value changes.
"""
import json
from pathlib import Path

import geopandas as gpd
import numpy as np
import pandas as pd

from wp_core import sentinel1_parcel_delivery as m


def main():
    prepared = m.verify_pinned()
    marker = m.peer.read(m.OUT / "complete.json")
    for name, digest in marker["files"].items():
        assert m.peer.sha(m.OUT / name) == digest
    cfg = m.peer.read(m.peer.CONFIG)
    fields = pd.read_parquet(m.peer.OUT / "fields.parquet").set_index("internal_parcel_id")
    tolerance = prepared["config"]["negative_increment_tolerance_mm"]
    checked_windows = 0
    sensitivity = []
    for year in prepared["config"]["years"]:
        folder = m.OUT / "years" / str(year)
        assert m.peer.sha(folder / "complete.json") == marker["year_manifests"][str(year)]
        for name, digest in m.peer.read(folder / "complete.json")["files"].items():
            assert m.peer.sha(folder/name) == digest
        sources = [x for x in prepared["weather"] if x["month"].startswith(str(year))]
        raw = pd.concat([pd.read_parquet(m.ROOT / x["path"], columns=["valid_time", "latitude", "longitude", "tp"]) for x in sources], ignore_index=True)
        raw["valid_time"] = pd.to_datetime(raw.valid_time, utc=True)
        cells = {}
        for cell, g in raw.groupby(["latitude", "longitude"]):
            g = g.sort_values("valid_time").set_index("valid_time")
            current = g.tp.to_numpy(dtype="float64")
            previous = np.r_[np.nan, current[:-1]]
            valid_gap = np.r_[False, np.diff(g.index.asi8) == 3600*10**9]
            previous[~valid_gap] = np.nan
            previous[g.index.hour == 1] = 0.
            values = 1000*(current-previous)
            cells[cell] = pd.Series(values, index=g.index)
        corrected = pd.read_parquet(folder / "weather_hourly.parquet")
        for cell, g in corrected.groupby(["latitude", "longitude"]):
            signed = cells[cell].reindex(g.valid_time).to_numpy()
            expected = np.where(np.isfinite(signed) & (signed >= -tolerance), np.maximum(signed, 0), np.nan)
            np.testing.assert_allclose(g.rain_mm, expected, atol=1e-9, rtol=2e-7, equal_nan=True)
        diag = pd.read_parquet(folder / "comparisons.parquet")
        nominal = diag.loc[diag.rain_limit_mm.eq(1.) & diag.excursion_db.eq(2.)]
        positive = diag.loc[diag.relative_excursion.fillna(False)].drop_duplicates(["internal_parcel_id", "middle_item"])
        raw_radar = pd.read_parquet(folder / "radar_input.parquet").set_index(["internal_parcel_id", "source_item"])
        links = pd.read_parquet(folder / "matched_peers.parquet")
        sample = pd.concat([positive, nominal.iloc[np.linspace(0, len(nominal)-1, min(80, len(nominal))).astype(int)]]).drop_duplicates(["internal_parcel_id", "middle_item"])
        for row in sample.itertuples():
            point = fields.loc[row.internal_parcel_id]
            series = cells[(point.latitude, point.longitude)]
            r = raw_radar.loc[[(row.internal_parcel_id, x) for x in (row.first_item, row.middle_item, row.last_item)]]
            dates = pd.to_datetime(r.datetime, utc=True, format="ISO8601").tolist()
            totals = []
            lengths = []
            signed_windows = []
            for start,end in ((dates[0],dates[1]),(dates[1],dates[2]),(dates[1]-pd.Timedelta(hours=48),dates[1])):
                index = pd.date_range(start.floor("h")+pd.Timedelta(hours=1),end.ceil("h"),freq="h")
                signed = series.reindex(index).to_numpy()
                signed_windows.append(signed)
                lengths.append(len(signed))
                totals.append(float(np.maximum(signed,0).sum()) if len(signed) and np.isfinite(signed).all() and (signed >= -tolerance).all() else np.nan)
                checked_windows += 1
            np.testing.assert_allclose(totals,[row.rain_before_mm,row.rain_after_mm,row.rain_recent_48h_mm],atol=1e-8,rtol=3e-7,equal_nan=True)
            p = links.loc[links.internal_parcel_id.eq(row.internal_parcel_id) & links.middle_item.eq(row.middle_item) & links.first_item.eq(row.first_item) & links.last_item.eq(row.last_item)] if len(links) else links
            assert len(p) == row.peer_count
            rises,falls = [],[]
            for peer_id in p.peer_id if len(p) else []:
                pr = raw_radar.loc[[(peer_id, x) for x in (row.first_item,row.middle_item,row.last_item)]]
                rises.append(float(pr.vv_inner_median_db.iloc[1]-pr.vv_inner_median_db.iloc[0]))
                falls.append(float(pr.vv_inner_median_db.iloc[1]-pr.vv_inner_median_db.iloc[2]))
            if len(p):
                np.testing.assert_allclose([np.median(rises),np.median(falls)],[row.peer_median_rise_db,row.peer_median_fall_db])
            if bool(row.relative_excursion) if pd.notna(row.relative_excursion) else False:
                assert row.supported and row.peer_count >= cfg["minimum_peers"] and max(totals) <= row.rain_limit_mm
                assert row.rise_db >= row.excursion_db and row.fall_db >= row.excursion_db
                assert row.rise_db-np.median(rises) >= 1. and row.fall_db-np.median(falls) >= 1.
                sensitivity.append({"year":year,"internal_parcel_id":row.internal_parcel_id,"middle_item":row.middle_item,
                                    "survives_half_tolerance":all(np.isfinite(a).all() and (a >= -tolerance/2).all() for a in signed_windows),
                                    "survives_double_tolerance":all(np.isfinite(a).all() and (a >= -tolerance*2).all() for a in signed_windows),
                                    "survives_plus_tolerance_per_hour":all(total+n*tolerance <= row.rain_limit_mm for total,n in zip(totals,lengths))})
        metrics = pd.read_parquet(folder / "metrics.parquet")
        assert metrics.loc[metrics.comparable_triplets.lt(6),"repeated_relative_signal"].isna().all()
    result = gpd.read_parquet(m.OUT / "all_parcels.parquet").set_index("internal_parcel_id")
    original = gpd.read_parquet(m.multi.OLD / "selection.parquet").set_index("internal_parcel_id").loc[result.index]
    assert len(result)==96 and result.index.is_unique and result.crs==original.crs
    assert result.geometry.to_wkb().tolist()==original.geometry.to_wkb().tolist()
    assert result.cadastre_code.tolist()==original.cadastre_code.tolist()
    np.testing.assert_array_equal(result.area_official_m2,original.area_official_m2)
    gpkg = gpd.read_file(m.OUT / "parcel_results.gpkg",layer="all_pilot_parcels").set_index("internal_parcel_id").loc[result.index]
    assert gpkg.geometry.to_wkb().tolist()==result.geometry.to_wkb().tolist()
    assert gpkg.cadastre_code.tolist()==result.cadastre_code.tolist()
    csv = pd.read_csv(m.OUT / "all_parcels.csv",dtype={"internal_parcel_id":str,"cadastre_code":str}).set_index("internal_parcel_id").loc[result.index]
    assert csv.cadastre_code.tolist()==result.cadastre_code.tolist()
    assert result.water_demand_status.eq("Unassessed").all()
    shortlist = pd.read_parquet(m.OUT / "inspection_parcels.parquet")
    assert set(shortlist.internal_parcel_id)==set(result.index[result.nominal_relative_excursions.gt(0)])
    assert not shortlist.sample_group.eq("boundary_challenge").any()
    output = {"status":"parcel_delivery_independently_verified","checked_parcels":len(result),"checked_rainfall_windows":checked_windows,
              "nominal_inspection_parcels":len(shortlist),"all_positive_observations_sensitivity":sensitivity,
              "all_positive_observations_stable_to_precision_checks":all(x["survives_half_tolerance"] and x["survives_double_tolerance"] and x["survives_plus_tolerance_per_hour"] for x in sensitivity),
              "checker_sha256":m.peer.sha(Path(__file__)),"result_manifest_sha256":m.peer.sha(m.OUT / "complete.json")}
    dest = m.ROOT / "server_data/review" / m.VERSION / "verification_v2.json"
    dest.parent.mkdir(parents=True,exist_ok=True)
    m.peer.write(dest,output)
    print(json.dumps(output,indent=2,ensure_ascii=False))


if __name__ == "__main__":
    main()
