import pytest
from wp_core.eo_radiometry import normalized_scene


def scene(collection, applied=True, baseline="05.00"):
    return {"collection": collection, "properties": {"earthsearch:boa_offset_applied": applied,
            "s2:processing_baseline": baseline}, "assets": {name: {"raster:bands": [{"scale": .0001, "offset": -.1}]}
            for name in ("red", "green", "blue", "nir", "swir16")}}


def test_legacy_offset_not_twice():
    original = scene("sentinel-2-l2a")
    result = normalized_scene(original)
    assert result['assets']['red']['offset'] == 0
    assert 'offset' not in original['assets']['red']


def test_c1_offset_required():
    assert normalized_scene(scene('sentinel-2-c1-l2a', None))['assets']['red']['offset'] == -.1


def test_ambiguous_legacy_rejected():
    with pytest.raises(ValueError):
        normalized_scene(scene('sentinel-2-l2a', False))


def test_pre_baseline_four_no_offset():
    assert normalized_scene(scene('sentinel-2-l2a', False, '03.01'))['assets']['red']['offset'] == 0
