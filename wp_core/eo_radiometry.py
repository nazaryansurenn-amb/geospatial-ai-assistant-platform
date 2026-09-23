"""Explicit radiometry contracts for Earth Search COGs, never double offset."""
from copy import deepcopy


def normalized_scene(scene):
    result = deepcopy(scene)
    properties = result.get("properties", {})
    collection = result.get("collection", "")
    applied = properties.get("earthsearch:boa_offset_applied")
    baseline = float(properties.get("s2:processing_baseline", 0))
    if collection == "sentinel-2-c1-l2a":
        rule = "collection_1_asset_scale_offset"
    elif collection == "sentinel-2-l2a" and (applied is True or baseline < 4):
        rule = "legacy_cog_already_offset_or_pre_baseline_4"
    else:
        raise ValueError("Ambiguous legacy radiometry: exclude scene pending independent verification")
    for name in ("red", "green", "blue", "nir", "swir16"):
        asset = result["assets"][name]
        bands = asset.get("raster:bands", [])
        if not bands or "scale" not in bands[0]:
            raise ValueError("Missing radiometric scale")
        asset["scale"] = float(bands[0]["scale"])
        asset["offset"] = float(bands[0].get("offset", 0)) if collection == "sentinel-2-c1-l2a" else 0.0
    result["radiometry_rule"] = rule
    return result
