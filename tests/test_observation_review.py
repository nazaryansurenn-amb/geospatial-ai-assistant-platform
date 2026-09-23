import inspect
import json

import numpy as np
import pandas as pd
import pytest

from run_observation_review import choose25, choose_dates, crop_rgb


def sample():
    data = pd.DataFrame({
        "internal_parcel_id": [f"p{i}" for i in range(120)],
        "review_group": ["boundary"] * 30 + ["single"] * 30 + ["double"] * 30 + ["perennial"] * 30,
        "activity_stage": ["stage_1", "stage_2"] * 60,
        "area_group": ["small", "large", "medium"] * 40,
        "history_signal": ["stable_vegetation_signal", "periodic_vegetation_signal", "stable_low_vegetation_signal"] * 40,
        "assessable_years": [5] * 120, "annual_years": [0] * 120,
        "perennial_years": [0] * 120, "accepted": [False] * 120,
        "training_eligible": [False] * 120,
    })
    data.loc[0, "assessable_years"] = 0
    return data


def test_25_is_subset_balanced_and_deterministic():
    data = sample()
    a = choose25(data)
    b = choose25(data.sample(frac=1, random_state=9))
    assert a.internal_parcel_id.tolist() == b.internal_parcel_id.tolist()
    assert len(a) == a.internal_parcel_id.nunique() == 25
    assert a.review_group.value_counts().to_dict() == {"boundary": 10, "single": 5, "double": 5, "perennial": 5}
    assert a.activity_stage.value_counts().sort_values().tolist() == [12, 13]
    assert "p0" in set(a.internal_parcel_id)
    assert not a.accepted.any() and not a.training_eligible.any()


def test_group_shortage_never_fabricates_control_samples():
    data = sample().loc[lambda x: x.review_group.ne("double")]
    with pytest.raises(ValueError, match="shortage"):
        choose25(data)


def test_dates_require_local_validity_and_stay_within_window():
    frame = pd.DataFrame({"day": [90, 91, 135, 180, 220, 265, 310],
                          "ndvi_valid_fraction": [.1, .9, .95, .95, .95, .95, .95],
                          "ndvi_mean": [.6] * 7, "scene_id": list("abcdefg")})
    chosen = choose_dates(frame)
    assert chosen[0].day == 91
    assert len(chosen) == 6
    frame.loc[frame.day.eq(180), "ndvi_mean"] = np.nan
    assert choose_dates(frame)[2] is None


def test_no_network_or_publication_in_review_script():
    import run_observation_review
    text = inspect.getsource(run_observation_review)
    for forbidden in ("requests.", "urlopen", "httpx.", "import openai", "build_tiles", "setFeatureState"):
        assert forbidden not in text
    assert "training_eligible" in text


def rgb_fixture(root):
    import rasterio
    from rasterio.transform import from_origin
    from shapely.geometry import box
    from release_tools import sha256

    folder = root / "data/cache/copernicus/test/collection/scene"
    folder.mkdir(parents=True)
    scene = {"id": "scene", "collection": "collection", "assets": {}}
    for color, value in (("red", 2000), ("green", 1500), ("blue", 1000)):
        path = folder / f"{color}.tif"
        array = np.full((100, 100), value, dtype="uint16")
        array[54, 44] = 0
        with rasterio.open(path, "w", driver="GTiff", width=100, height=100,
                           count=1, dtype="uint16", crs="EPSG:32638", nodata=0,
                           transform=from_origin(0, 1000, 10, 10)) as dst:
            dst.write(array, 1)
            dst.update_tags(scale=.0001, offset=-.05)
        path.with_suffix(".json").write_text(json.dumps({"sha256": sha256(path)}))
        scene["assets"][color] = {"scale": .0001, "offset": -.05}
    return scene, box(400, 400, 500, 500), folder


def test_rgb_preserves_native_grid_radiometry_and_nodata(tmp_path):
    scene, geometry, _ = rgb_fixture(tmp_path)
    rgb, transform, used = crop_rgb(tmp_path, scene, "test", geometry, {})
    assert rgb.shape == (14, 14, 3)
    assert transform.a == 10 and transform.e == -10
    expected = (np.power(np.array([.15, .10, .05]) / .30, .82) * 255).astype("uint8")
    valid = ~np.all(rgb == 195, axis=-1)
    assert (~valid).sum() == 1
    assert np.allclose(rgb[valid], expected, atol=1)
    assert len(used) == 3 and all(len(x["sha256"]) == 64 for x in used)


def test_rgb_rejects_wrong_scale_or_offset(tmp_path):
    scene, geometry, _ = rgb_fixture(tmp_path)
    scene["assets"]["red"]["offset"] = 0
    with pytest.raises(ValueError, match="radiometry"):
        crop_rgb(tmp_path, scene, "test", geometry, {})


def test_rgb_rejects_corrupted_cached_band(tmp_path):
    scene, geometry, folder = rgb_fixture(tmp_path)
    (folder / "red.json").write_text(json.dumps({"sha256": "0" * 64}))
    with pytest.raises(ValueError, match="checksum"):
        crop_rgb(tmp_path, scene, "test", geometry, {})
