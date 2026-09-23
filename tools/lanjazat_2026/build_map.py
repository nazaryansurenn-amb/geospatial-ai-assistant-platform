"""Render the saved v2 screening spatially, without downloads or source edits."""
from datetime import date, timedelta
import hashlib
import json

import geopandas as gpd
import numpy as np
import pandas as pd
from pyproj import Transformer
from rasterio.features import shapes
from rasterio.transform import Affine
from shapely.geometry import GeometryCollection, mapping, shape
from shapely.ops import transform, unary_union

from analysis import OUT, build_weights, atomic_json
from with_osm_context import agricultural_areas

DEST = OUT / "map_data"


def polygonal(geom):
    if geom.geom_type in ("Polygon", "MultiPolygon"):
        return geom
    return unary_union([polygonal(g) for g in getattr(geom, "geoms", [])])


def mask_geometry(mask, weights, boundary):
    grid = np.zeros((int(weights["height"]), int(weights["width"])), dtype="uint8")
    grid.ravel()[weights["pixel"]] = mask.astype("uint8")
    parts = [shape(g) for g, value in shapes(
        grid, mask=grid.astype(bool), transform=Affine(*weights["transform"][:6])
    ) if value == 1]
    return polygonal(unary_union(parts).intersection(boundary)) if parts else GeometryCollection()


def temporal_masks(samples, dates, meteo):
    ndvi = np.stack([s["ndvi"] for s in samples])
    ndmi = np.stack([s["ndmi"] for s in samples])
    clear = np.stack([s["clear"] for s in samples])
    summer = np.array([d >= "2026-06-01" for d in dates])
    times = pd.to_datetime(dates).dayofyear.to_numpy()
    green = clear & (ndvi >= .4)
    first = np.min(np.where(green, times[:, None], 999), axis=0)
    last = np.max(np.where(green, times[:, None], -999), axis=0)
    covered = (clear.sum(0) >= 4) & (clear[~summer].sum(0) >= 1) & (clear[summer].sum(0) >= 2)
    repeated = covered & (green.sum(0) >= 3) & (last - first >= 30)
    latest_i = np.max(np.where(clear, np.arange(len(samples))[:, None], -1), axis=0)
    safe_i = np.maximum(latest_i, 0)
    recent = (latest_i >= 0) & (times[safe_i] >= times.max() - 20)
    recent &= ndvi[safe_i, np.arange(len(safe_i))] >= .4
    daily = meteo["daily"]
    rain = dict(zip(daily["time"], daily["precipitation_sum"]))
    et0 = dict(zip(daily["time"], daily["et0_fao_evapotranspiration"]))
    dry = []
    for d in dates:
        days = [(date.fromisoformat(d) - timedelta(days=i)).isoformat() for i in range(1, 15)]
        values = [(rain.get(k), et0.get(k)) for k in days]
        complete = all(r is not None and e is not None and r >= 0 and e >= 0 for r, e in values)
        dry.append(d >= "2026-06-01" and complete and sum(e for _, e in values) >= 35
                   and sum(r for r, _ in values) <= .35 * sum(e for _, e in values))
    supported = green & (ndmi >= 0) & np.array(dry)[:, None]
    first = np.min(np.where(supported, times[:, None], 999), axis=0)
    last = np.max(np.where(supported, times[:, None], -999), axis=0)
    proxy = repeated & (supported.sum(0) >= 3) & (last - first >= 30)
    return repeated, proxy, recent, covered


def main():
    result = json.loads((OUT / "result_v2.json").read_text())
    # Refuse to visualize modified inputs under the name of the saved analysis.
    for name, expected in result["source_hashes"].items():
        source = OUT / name
        if not source.exists():
            source = OUT.parent / name
        assert hashlib.sha256(source.read_bytes()).hexdigest() == expected, name
    feature = json.loads((OUT / "boundary.geojson").read_text(encoding="utf-8"))
    boundary = shape(feature["geometry"])
    weights = build_weights(gpd.GeoDataFrame({"geometry": [boundary]}, crs=4326), 20, 32638)
    forward = Transformer.from_crs(4326, 32638, always_xy=True).transform
    backward = Transformer.from_crs(32638, 4326, always_xy=True).transform
    metric_boundary = transform(forward, boundary)
    with np.load(OUT / "worldcover_2021.npz") as data:
        lc = data["values"]
    ag, _, osm_context = agricultural_areas(
        boundary, weights, lc, json.loads((OUT / "osm_landuse_source.json").read_text())
    )
    context = transform(forward, osm_context).intersection(metric_boundary)
    context = context.union(mask_geometry(lc == 40, weights, metric_boundary))
    context = polygonal(context.difference(mask_geometry(np.isin(lc, [50, 80]), weights, metric_boundary)))
    assert abs(context.area - ag.sum()) < .05, "Agricultural context area mismatch"
    scenes = json.loads((OUT / "scenes.json").read_text())["selected"]
    dates, samples = [], []
    for s in scenes:
        dates.append(s["properties"]["datetime"][:10])
        with np.load(OUT / "samples" / (s["id"] + ".npz")) as data:
            samples.append({k: data[k] for k in data.files})
    assert dates == result["observation_dates"] == sorted(set(dates))
    repeated, proxy, recent, covered = temporal_masks(
        samples, dates, json.loads((OUT / "weather.json").read_text())
    )
    vegetation = mask_geometry(repeated, weights, metric_boundary)
    active = polygonal(vegetation.intersection(context))
    irrigation = polygonal(mask_geometry(proxy, weights, metric_boundary).intersection(context))
    layers = [
        ("active", "Agricultural vegetation", "#20c77a", active,
         "active_vegetation_in_agricultural_context_ha",
         "Repeated vegetation within mapped agricultural land. This is not proof of cultivation."),
        ("irrigation", "Irrigation-compatible growth", "#289eff", irrigation,
         "irrigation_compatible_agricultural_vegetation_ha",
         "Growth during dry-weather periods. Irrigation is unconfirmed; groundwater and stored moisture can also explain it."),
        ("vegetation", "All repeated vegetation", "#e6cd47", vegetation,
         "repeated_vegetation_ha_all_land",
         "Repeated vegetation across the administrative area, including natural vegetation and agricultural land."),
        ("recent", "Recent vegetation", "#f08c46", mask_geometry(recent, weights, metric_boundary),
         "recent_vegetation_ha_all_land",
         "Vegetation at each location's latest clear sampled observation, no more than 20 days before 2 September."),
        ("context", "Mapped agricultural land", "#c0c9cc", context,
         "agricultural_context_ha",
         "OSM agricultural land use and ESA WorldCover 2021. This context may be incomplete or outdated."),
    ]
    DEST.mkdir(parents=True, exist_ok=True)
    checks, summaries = {}, []
    for layer_id, title, color, geom, key, description in layers:
        measured = geom.area / 10000
        assert geom.is_valid, layer_id
        assert geom.difference(metric_boundary).area < .05, layer_id
        assert abs(measured - result[key]) < .00051, (layer_id, measured, result[key])
        public_geom = transform(backward, geom)
        assert public_geom.is_valid, layer_id
        atomic_json(DEST / (layer_id + ".geojson"), {
            "type": "FeatureCollection", "features": [{"type": "Feature",
                "properties": {"layer": layer_id}, "geometry": mapping(public_geom)}]
        })
        checks[layer_id] = {"measured_ha": measured, "saved_ha": result[key], "valid": True}
        summaries.append({"id": layer_id, "title": title, "color": color,
                          "area_ha": result[key], "description": description})
    assert irrigation.difference(active).area < .05
    assert active.difference(vegetation).area < .05
    atomic_json(DEST / "boundary.geojson", {"type": "Feature", "properties": {
        "name": "Lanjazat", "source": "OpenStreetMap contributors, ODbL"},
        "geometry": feature["geometry"]})
    atomic_json(DEST / "summary.json", {
        "place": "Lanjazat", "year": 2026, "boundary_ha": result["boundary_ha"],
        "start": dates[0], "end": dates[-1], "observations": len(dates),
        "bounds": list(boundary.bounds), "layers": summaries,
        "insufficient_observations_ha": round(float(weights["area"][~covered].sum()) / 10000, 3),
        "confirmed_irrigation": False,
    })
    atomic_json(OUT / "map_verification.json", {
        "passed": True, "source_version": result["version"], "layer_areas": checks,
        "valid_geometry": True, "inside_administrative_boundary": True,
        "irrigation_is_subset": True, "downloads": 0, "raw_observations_exposed": False,
        "classification_accuracy_verified": False,
        "files": {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(DEST.iterdir())},
    })
    print(json.dumps({"passed": True, "layers": checks,
                      "map_data_bytes": sum(p.stat().st_size for p in DEST.iterdir())}, indent=2))


if __name__ == "__main__":
    main()
