import json
from pathlib import Path
import sqlite3
import numpy as np
import pandas as pd
import geopandas as gpd
import pytest
from shapely.geometry import box
from wp_core.data_collector import indices, spatial, weather, eo
from wp_core.data_collector.storage import Store, atomic_parquet, digest


def test_indices_formulas_and_resolution():
    b = {"blue": np.array([.05]), "green": np.array([.10]), "red": np.array([.15]),
         "nir": np.array([.6]), "nir08": np.array([.55]), "rededge1": np.array([.25]),
         "rededge2": np.array([.4]), "rededge3": np.array([.5]), "swir16": np.array([.3]), "swir22": np.array([.2])}
    actual = {**indices.calculate(b, 10), **indices.calculate(b, 20)}
    expected = {"ndvi": .6, "evi": 1.125/2.125, "evi2": 1.125/1.96, "gndvi": .5/.7,
                "savi": .675/1.25, "msavi2": (2.2-np.sqrt(1.24))/2,
                "ndwi": -.5/.7, "ndre": .3/.8, "ci_rededge": 1.2, "mtci": 1.5,
                "ireci": .35/.625, "ndmi": 1/3, "msi": .5, "bsi": -.2/1.1,
                "mndwi": -.5, "nbr": .5, "nbr2": .2, "psri": .25}
    assert set(actual) == set(expected) == set(indices.FORMULAS)
    for name, value in expected.items():
        assert actual[name][0] == pytest.approx(value, abs=1e-6), name
    assert indices.FORMULAS["ndre"][0] == 20


def test_undefined_index_is_null_not_zero():
    assert np.isnan(indices.ratio(np.array([1., 0.]), np.array([0., 0.]))).all()


def test_negative_msavi_radicand_is_not_fabricated():
    b = {k: np.array([0.]) for k in ["blue", "green", "red", "nir"]}
    b["red"] = np.array([-1.])
    assert np.isnan(indices.calculate(b, 10)["msavi2"]).all()


def test_skinny_and_overlapping_parcels_are_never_dropped():
    # No pixel centre falls in either one-metre strip. Both must have support.
    frame = gpd.GeoDataFrame(geometry=[box(1, 0, 2, 20), box(1.5, 0, 2.5, 20), box(20, 0, 30, 20)], crs=32638)
    weights = spatial.build_weights(frame, 10, "EPSG:32638")
    assert set(weights["parcel"]) == {0, 1, 2}
    assert np.allclose(np.bincount(weights["parcel"], weights=weights["area"]), [20, 20, 200])
    pixels = np.full((int(weights["height"]), int(weights["width"])), .4)
    stats = spatial.summarize(pixels, np.ones(pixels.shape, bool), weights)
    assert np.allclose(stats["mean"], .4)
    assert np.allclose(stats["valid_fraction"], 1)


def test_weighted_quantiles_and_cloud_support():
    weights = {"parcel": np.array([0, 0, 0, 1]), "pixel": np.array([0, 1, 2, 3]),
               "area": np.array([1., 8., 1., 5.]), "count": np.array(3)}
    stats = spatial.summarize(np.array([0., 10., 20., 7.]), np.array([True, True, True, False]), weights)
    assert stats["mean"][0] == pytest.approx(10.)
    assert stats["median"][0] == 10.
    assert stats["std"][0] == pytest.approx(np.sqrt(20))
    assert np.isnan(stats["mean"][1:]).all()
    assert stats["count"].tolist() == [3, 0, 0]
    assert stats["valid_fraction"].tolist() == [1., 0., 0.]


def scene(collection="sentinel-2-c1-l2a"):
    return {"id": "S2A_TEST", "collection": collection, "properties": {"s2:processing_baseline": "04.00"},
            "assets": {k: {"href": f"https://sentinel-cogs.s3.us-west-2.amazonaws.com/{k}.tif",
                             "raster:bands": [{"scale": .0001, "offset": -.1}]} for k in indices.BANDS}}


def test_all_ten_reflectance_bands_have_explicit_radiometry():
    for key, asset in eo.normalize(scene())["assets"].items():
        assert asset["offset"] == -.1
    old = scene("sentinel-2-l2a")
    with pytest.raises(ValueError, match="ambiguous"):
        eo.normalize(old)
    old["properties"]["earthsearch:boa_offset_applied"] = True
    assert all(a["offset"] == 0 for a in eo.normalize(old)["assets"].values())


def test_missing_scale_and_unapproved_urls_rejected():
    value = scene()
    value["assets"]["rededge1"]["raster:bands"] = []
    with pytest.raises(ValueError, match="scale"):
        eo.normalize(value)
    value = scene()
    value["assets"]["red"]["href"] = "http://127.0.0.1/secret"
    with pytest.raises(ValueError, match="host"):
        eo.normalize(value)


def test_run_specification_and_job_payload_cannot_change(tmp_path):
    store = Store(tmp_path, {"version": "v1"})
    store.pin({"scope": 1})
    store.pin({"scope": 1})
    with pytest.raises(ValueError, match="changed"):
        store.pin({"scope": 2})
    store.add("one", "eo", {"scene": 1})
    with pytest.raises(ValueError, match="change"):
        store.add("one", "eo", {"scene": 2})


def test_crash_resume_keeps_completed_jobs(tmp_path):
    store = Store(tmp_path, {"version": "v1"})
    store.add("a", "eo", {})
    store.add("b", "eo", {})
    store.transition("a", "complete", rows=3)
    store.transition("b", "running")
    with store.lock():
        store.recover()
    assert [j["state"] for j in store.jobs()] == ["complete", "pending"]
    assert not store.status()["complete"]


def test_second_worker_cannot_take_lock(tmp_path):
    store = Store(tmp_path, {"version": "v1"})
    with store.lock():
        with pytest.raises(RuntimeError, match="Another"):
            with Store(tmp_path, {"version": "v1"}).lock():
                pass


def test_path_traversal_version_rejected(tmp_path):
    with pytest.raises(ValueError):
        Store(tmp_path, {"version": "../../../escape"})


def test_atomic_parquet_missing_values_roundtrip(tmp_path):
    path = tmp_path/"part.parquet"
    atomic_parquet(path, pd.DataFrame({"ndvi": [0., np.nan]}))
    result = pd.read_parquet(path)
    assert result.ndvi.iloc[0] == 0
    assert pd.isna(result.ndvi.iloc[1])


def weather_raw():
    times = pd.date_range("2020-12-31 00:00", "2021-01-03 00:00", freq="h", tz="UTC")
    hour = times.hour.to_numpy()
    step = np.where(hour == 0, 24, hour)
    return pd.DataFrame({"valid_time": times, "latitude": 40.2, "longitude": 44.2,
                         "t2m": 293.15, "d2m": 283.15, "u10": 3., "v10": 4., "sp": 92000.,
                         "tp": step*.001, "ssrd": step*100000.})


def test_weather_accumulation_reset_and_local_midnight():
    hourly = weather.hourly_observations(weather_raw(), "Asia/Yerevan")
    assert np.allclose(hourly.precipitation_mm.iloc[1:], 1)
    daily = weather.daily_observations(hourly).set_index("date")
    assert daily.loc["2021-01-01", "precipitation_mm"] == pytest.approx(24)
    assert daily.loc["2021-01-01", "temperature_c_mean"] == pytest.approx(20)
    assert daily.loc["2021-01-01", "solar_radiation_mj_m2"] == pytest.approx(2.4)


def test_missing_weather_hour_is_not_zero_rain():
    raw = weather_raw().drop(index=30)
    daily = weather.daily_observations(weather.hourly_observations(raw, "Asia/Yerevan")).set_index("date")
    assert pd.isna(daily.loc["2021-01-01", "precipitation_mm"])
    assert daily.loc["2021-01-01", "quality"] == "incomplete"


def test_cds_is_blocked_without_secret(tmp_path, monkeypatch):
    monkeypatch.delenv("CDSAPI_KEY", raising=False)
    store = Store(tmp_path, {"version": "v1", "weather": {"credential_env": "CDSAPI_KEY"}})
    assert weather.collect_job(store, {"id": "weather_2021_01"})[0] == "blocked"


def test_weather_requests_preserve_leap_day_and_context():
    planned = weather.jobs({"years": [2021, 2022, 2023, 2024, 2025]}, [44.1, 40.1, 44.4, 40.3])
    assert len(planned) == 61
    assert planned[0][0] == "weather_2020_12"
    assert len(dict(planned)["weather_2024_02"]["day"]) == 29


def test_no_coarse_grid_or_classifier_or_llm_imports():
    import ast
    directory = Path(indices.__file__).parent
    forbidden = ("openai", "anthropic", "predominant_use", "parcel_land_features", "pressure", "grid250")
    for path in directory.glob("*.py"):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                assert not any(term in alias.name for alias in node.names for term in forbidden)
            if isinstance(node, ast.ImportFrom):
                assert not any(term in (node.module or "") for term in forbidden)
