"""Photo date, resolution and source for every eligible parcel from Esri World Imagery metadata (product .venv).

Queries the imagery footprints of the latest metadata release once for the study rectangle (only the
rectangle is sent; no cadastral codes or outlines), then matches parcel centroids locally.
Layer 4 describes the zoom-19 (30 cm) imagery, layer 5 the zoom 16-18 imagery.
"""
from __future__ import annotations
import json
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

import geopandas as gpd
import pandas as pd
import shapely

ROOT = Path(__file__).resolve().parents[1]
CONFIG = json.loads((ROOT / "config/alphaearth_groups_20261030_v1.json").read_text())
OUT = ROOT / CONFIG["outputs"]["data"] / "photo_meta"
SERVICES = "https://metadata.maptiles.arcgis.com/arcgis/rest/services"
LAYERS = {"zoom19": 4, "zoom16_18": 5}
FIELDS = "SRC_DATE2,SRC_RES,SAMP_RES,SRC_ACC,NICE_NAME,NICE_DESC,ReleaseName"


def get(url, params=None):
    if params:
        url += "?" + urllib.parse.urlencode(params)
    with urllib.request.urlopen(url, timeout=60) as response:
        return json.loads(response.read())


def latest_release():
    names = sorted(s["name"] for s in get(SERVICES, {"f": "json"})["services"] if s["name"].startswith("World_Imagery_Metadata_"))
    return names[-1]


def esri_polygon(rings):
    """Esri JSON rings -> shapely geometry (outer rings clockwise, holes counter-clockwise)."""
    outers, holes = [], []
    for ring in rings:
        (holes if shapely.LinearRing(ring).is_ccw else outers).append(shapely.Polygon(ring))
    shape = shapely.union_all(outers) if outers else shapely.Polygon()
    return shape.difference(shapely.union_all(holes)) if holes else shape


def footprints(release, layer, box):
    # f=geojson fails for large footprints on some layers; Esri JSON with light simplification works.
    params = {"geometry": ",".join(f"{v:.1f}" for v in box), "geometryType": "esriGeometryEnvelope", "inSR": 32638,
              "spatialRel": "esriSpatialRelIntersects", "outFields": FIELDS, "returnGeometry": "true", "outSR": 32638,
              "maxAllowableOffset": 2, "f": "json"}
    data = get(f"{SERVICES}/{release}/MapServer/{layer}/query", params)
    if "error" in data:
        raise RuntimeError(f"Esri metadata query failed for layer {layer}: {data['error'].get('message')}")
    rows = [{**f["attributes"], "geometry": esri_polygon(f["geometry"]["rings"])} for f in data.get("features", [])]
    return gpd.GeoDataFrame(rows, geometry="geometry", crs=32638) if rows else gpd.GeoDataFrame()


def main():
    if OUT.exists():
        raise SystemExit(f"{OUT.relative_to(ROOT)} already exists; it is never overwritten.")
    g = CONFIG["grid"]
    box = [g["origin_x"], g["origin_y"] - g["height"] * g["resolution_m"], g["origin_x"] + g["width"] * g["resolution_m"], g["origin_y"]]
    release = latest_release()
    basis = json.loads((ROOT / "data/observations/observations_2021_2025_v1/specification.json").read_text())["config"]["basis"]
    parcels = pd.read_parquet(ROOT / CONFIG["outputs"]["data"] / "grid/parcels.parquet", columns=["internal_parcel_id"])
    geometry = gpd.read_parquet(ROOT / basis).set_index("internal_parcel_id").loc[parcels.internal_parcel_id].to_crs(32638)
    points = gpd.GeoDataFrame({"internal_parcel_id": parcels.internal_parcel_id.to_numpy()},
                              geometry=shapely.point_on_surface(geometry.geometry.to_numpy()), crs=32638)
    table = parcels.copy()
    report = {"release": release, "queried_at": datetime.now(timezone.utc).isoformat(), "rectangle_epsg32638": box, "layers": {}}
    for name, layer in LAYERS.items():
        frame = footprints(release, layer, box)
        report["layers"][name] = {"layer": layer, "footprints": len(frame)}
        if frame.empty:
            for column in ("date", "resolution_m", "source", "provider"):
                table[f"{name}_{column}"] = None
            continue
        frame["date"] = pd.to_datetime(frame.SRC_DATE2, unit="ms", errors="coerce").dt.date.astype(str)
        joined = gpd.sjoin(points, frame[["geometry", "date", "SAMP_RES", "NICE_NAME", "NICE_DESC"]], how="left", predicate="within")
        joined = joined.sort_values("date", ascending=False).drop_duplicates("internal_parcel_id")
        joined = joined.set_index("internal_parcel_id").loc[table.internal_parcel_id]
        table[f"{name}_date"] = joined.date.where(joined.date.notna() & (joined.date != "NaT")).to_numpy()
        table[f"{name}_resolution_m"] = pd.to_numeric(joined.SAMP_RES, errors="coerce").to_numpy()
        table[f"{name}_source"] = joined.NICE_NAME.to_numpy()
        table[f"{name}_provider"] = joined.NICE_DESC.to_numpy()
        dates = table[f"{name}_date"].dropna()
        report["layers"][name].update({"parcels_with_date": int(len(dates)),
                                       "dates": {str(k): int(v) for k, v in dates.value_counts().items()}})
    cutoff = json.loads((ROOT / "config/alphaearth_types_20261030_v1.json").read_text(encoding="utf-8"))["evidence"]["photo_age"]["current_from"]
    best = table.zoom19_date.fillna(table.zoom16_18_date)
    table["photo_date"] = best
    table["photo_current"] = best.fillna("") >= cutoff
    table["photo_missing"] = best.isna()
    OUT.mkdir(parents=True)
    table.to_parquet(OUT / "parcels.parquet", index=False)
    report.update({"parcels": len(table), "current_photo": int(table.photo_current.sum()),
                   "older_photo": int((~table.photo_current & ~table.photo_missing).sum()), "missing": int(table.photo_missing.sum()),
                   "caveat": "Latest metadata release assumed to match the tiles currently served by World Imagery."})
    (OUT / "photo_meta.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report, indent=1))


if __name__ == "__main__":
    main()
