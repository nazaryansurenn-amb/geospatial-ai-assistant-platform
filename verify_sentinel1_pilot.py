"""Independent checks for stored radar numbers, parcel identity and conservative claims."""
from pathlib import Path
import json
import sqlite3
import numpy as np
import pandas as pd
import geopandas as gpd
import rasterio
from rasterio.features import geometry_mask
from shapely.geometry import mapping
from release_tools import sha256, verify_release
from wp_core.sentinel1_pilot import ROOT, OUT, ANALYSIS, verify


def main():
    report = verify()
    selection = gpd.read_parquet(OUT / "selection.parquet")
    observations = pd.read_parquet(ANALYSIS / "radar_observations.parquet")
    scope = pd.read_parquet(ROOT / "data/observations/observations_2021_2025_v1/scope.parquet").set_index("internal_parcel_id")
    original = scope.loc[selection.internal_parcel_id]
    assert original.included.all() and not original.household.astype(bool).any() and not original.road_excluded.astype(bool).any()
    assert selection.cadastre_code.tolist() == original.cadastre_code.tolist()
    assert selection.area_official_m2.tolist() == original.area_official_m2.tolist()
    assert not observations.duplicated(["internal_parcel_id", "source_item"]).any()
    assert observations.groupby("internal_parcel_id").size().eq(57).all()
    assert observations.groupby("internal_parcel_id").relative_orbit.nunique().eq(2).all()
    scene_manifest = json.loads((OUT / "scene_manifest.json").read_text(encoding="utf-8"))
    checked_rows = 0
    checked_clips = 0
    for item in scene_manifest["items"]:
        for window in scene_manifest["windows"]:
            path = OUT / "clips" / item["id"] / f"{window['id']}.tif"
            with rasterio.open(path) as src:
                assert src.count == 2 and src.crs.to_epsg() == 32638 and src.res == (10, 10)
                assert list(src.bounds) == window["bounds"]
                assert src.nodata == -32768 and src.tags()["source_item"] == item["id"]
                assert src.descriptions == ("VV_gamma0_linear_power", "VH_gamma0_linear_power")
                checked_clips += 1
                # Recalculate first and last dates independently from stored pixels.
                if item not in (scene_manifest["items"][0], scene_manifest["items"][-1]):
                    continue
                for kind in ("clean_interior", "boundary_challenge"):
                    field = selection.loc[selection.window_id.eq(window["id"]) & selection.sample_group.eq(kind)].iloc[0]
                    geom = field.geometry.buffer(-10)
                    mask = np.zeros((src.height, src.width), dtype=bool) if geom.is_empty else geometry_mask(
                        [mapping(geom)], out_shape=(src.height,src.width), transform=src.transform, invert=True)
                    row = observations.loc[observations.source_item.eq(item["id"]) & observations.internal_parcel_id.eq(field.internal_parcel_id)].iloc[0]
                    for band, index in (("vv",1),("vh",2)):
                        a = src.read(index)
                        values = a[mask & np.isfinite(a) & (a > 0)]
                        expected = float(np.median(10*np.log10(values))) if len(values) else np.nan
                        np.testing.assert_allclose(row[f"{band}_inner_median_db"], expected, rtol=1e-6, equal_nan=True)
                        assert row[f"{band}_inner_valid_pixel_count"] == len(values)
                    checked_rows += 1
    assert checked_clips == 228 and checked_rows == 16
    assert report["rapid_drying_resolved_excursions"] == 0
    assert report["high_demand_parcels"] is None and report["confirmed_irrigation_events"] is None
    assert report["rainfall_attribution_performed"] is False and report["map_changed"] is False
    preserved = verify_release(ROOT, "working")
    lock = json.loads((ROOT / "config/transitions_area_20260906_v1.review.lock.json").read_text(encoding="utf-8"))
    for relative, record in lock["files"].items():
        assert sha256(ROOT/relative) == record["sha256"]
    result = {"status":"independent_verification_passed", "radar_clips_checked":checked_clips,
              "independent_pixel_stat_rows":checked_rows,"parcel_observations":len(observations),
              "cadastre_identity_and_exclusions_preserved":True,"working_release_verified":True,
              "existing_classification_review_verified":True,"checker_sha256":sha256(Path(__file__))}
    target = ROOT / "server_data/review" / "sentinel1_pilot_20260906_v1"
    target.mkdir(parents=True,exist_ok=True)
    p=target/"verification.json"
    if p.exists():
        assert json.loads(p.read_text()) == result
    else:
        p.write_text(json.dumps(result,indent=2)+"\n",encoding="utf-8")
    print(json.dumps(result,indent=2))


if __name__ == "__main__":
    main()
