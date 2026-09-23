"""All-date STAC discovery and native-band COG-window caching."""
import json
import math
import re
from pathlib import Path
import shutil
import time
from urllib.parse import urlparse
import numpy as np
import pandas as pd
import requests
import rasterio
from rasterio.enums import Resampling
from rasterio.transform import Affine
from rasterio.vrt import WarpedVRT
from rasterio.windows import Window, from_bounds
from rasterio.warp import transform_bounds
from shapely.geometry import shape, box
from .indices import BANDS, FORMULAS, calculate
from .storage import atomic_json, atomic_parquet, digest, json_hash
from .spatial import summarize

SEARCH = "https://earth-search.aws.element84.com/v1/search"
ASSET_HOSTS = {"sentinel-cogs.s3.us-west-2.amazonaws.com", "e84-earth-search-sentinel-data.s3.us-west-2.amazonaws.com"}


def request(session, method, url, **kwargs):
    for attempt in range(3):
        try:
            result = session.request(method, url, timeout=60, **kwargs)
            result.raise_for_status()
            return result
        except requests.RequestException:
            if attempt == 2:
                raise RuntimeError(f"Provider request failed ({urlparse(url).hostname})") from None
            time.sleep(2**attempt)


def normalize(scene):
    scene = json.loads(json.dumps(scene))
    if not re.fullmatch(r"[A-Za-z0-9_-]+", scene["id"]):
        raise ValueError("unsafe_scene_identifier")
    c1 = scene.get("collection") == "sentinel-2-c1-l2a"
    props = scene["properties"]
    baseline = props.get("s2:processing_baseline")
    if not c1 and not (props.get("earthsearch:boa_offset_applied") is True or
                       (baseline is not None and float(baseline) < 4)):
        raise ValueError("ambiguous_legacy_radiometry")
    assets = {}
    for name in BANDS:
        if name not in scene["assets"]:
            raise ValueError("missing_band_"+name)
        a = scene["assets"][name]
        parsed = urlparse(a["href"])
        if parsed.scheme != "https" or parsed.hostname not in ASSET_HOSTS:
            raise ValueError("unapproved_asset_host")
        rb = (a.get("raster:bands") or [{}])[0]
        if name != "scl" and "scale" not in rb:
            raise ValueError("missing_radiometric_scale")
        assets[name] = {"href": a["href"], "scale": float(rb.get("scale", 1)),
                        "offset": float(rb.get("offset", 0)) if c1 else 0.,
                        "resolution_m": BANDS[name]}
    scene["assets"] = assets
    scene["radiometry"] = "c1_asset_scale_offset" if c1 else "legacy_already_offset"
    return scene


def discover(store, scope):
    path = store.base / "scene_manifest.json"
    if path.exists():
        return json.loads(path.read_text())
    session = requests.Session()
    scenes, excluded, seen = {}, [], set()
    for year in store.config["years"]:
        for collection in store.config["eo_collections"]:
            body = {"collections": [collection], "bbox": scope["bounds_wgs84"],
                    "datetime": f"{year}-01-01T00:00:00Z/{year}-12-31T23:59:59Z", "limit": 100}
            url, method, payload, visited = SEARCH, "POST", body, set()
            while url:
                if urlparse(url).hostname != "earth-search.aws.element84.com":
                    raise ValueError("STAC next-page host changed")
                key = json_hash([url, payload])
                if key in visited:
                    raise ValueError("STAC pagination loop")
                visited.add(key)
                page = request(session, method, url, **({"json": payload} if method == "POST" else {})).json()
                for item in page["features"]:
                    unique = collection+":"+item["id"]
                    if unique in seen:
                        continue
                    seen.add(unique)
                    if not shape(item["geometry"]).intersects(box(*scope["bounds_wgs84"])):
                        continue
                    try:
                        normalized = normalize(item)
                    except ValueError as error:
                        excluded.append({"scene_id": unique, "reason": str(error)})
                        continue
                    # One product per acquisition/tile; never a ten-day sampling cap.
                    p = item["properties"]
                    tile = str(p.get("grid:code", item["id"].split("_")[1]))
                    acquisition = p["datetime"][:19]+":"+tile
                    priority = (collection == "sentinel-2-c1-l2a", float(p.get("s2:processing_baseline", 0)), item["id"])
                    old = scenes.get(acquisition)
                    if old is None or priority > old[0]:
                        if old:
                            excluded.append({"scene_id": old[1]["collection"]+":"+old[1]["id"], "reason": "superseded_same_acquisition"})
                        scenes[acquisition] = (priority, normalized)
                    else:
                        excluded.append({"scene_id": unique, "reason": "duplicate_same_acquisition"})
                link = next((v for v in page.get("links", []) if v.get("rel") == "next"), None)
                if not link:
                    break
                url, method = link["href"], link.get("method", "GET")
                payload = {**body, **link.get("body", {})} if link.get("merge") else link.get("body")
            print(f"Discovered {year} {collection}: {len(scenes)} distinct acquisitions", flush=True)
    manifest = {"query": "all_dates_no_scene_cloud_cutoff", "scene_count": len(scenes),
                "scenes": sorted([v[1] for v in scenes.values()], key=lambda s: s["properties"]["datetime"]),
                "excluded_products": excluded}
    atomic_json(path, manifest)
    return manifest


def check_space(store):
    if shutil.disk_usage(store.root).free < store.config["minimum_free_gb"]*1024**3:
        raise RuntimeError("disk_free_limit")


def cache_band(store, scene, name, bounds):
    root = store.root / "data/cache/copernicus" / json_hash(bounds)[:16] / scene["collection"] / scene["id"]
    path = root / f"{name}.tif"
    sidecar = path.with_suffix(".json")
    asset = scene["assets"][name]
    identity = json_hash([asset, bounds])
    if path.exists() and sidecar.exists():
        info = json.loads(sidecar.read_text())
        if info["identity"] != identity or digest(path) != info["sha256"]:
            raise ValueError("Cached band checksum or identity mismatch")
        return path
    check_space(store)
    root.mkdir(parents=True, exist_ok=True)
    env = {"GDAL_DISABLE_READDIR_ON_OPEN": "EMPTY_DIR", "CPL_VSIL_CURL_ALLOWED_EXTENSIONS": ".tif",
           "GDAL_HTTP_TIMEOUT": "60", "GDAL_HTTP_CONNECTTIMEOUT": "20", "GDAL_HTTP_MAX_RETRY": "2"}
    temp = path.with_suffix(".partial.tif")
    with rasterio.Env(**env):
        with rasterio.open(asset["href"]) as source:
            native = transform_bounds("EPSG:4326", source.crs, *bounds, densify_pts=21)
            win = from_bounds(*native, source.transform)
            col, row = math.floor(win.col_off)-1, math.floor(win.row_off)-1
            win = Window(col, row, math.ceil(win.col_off+win.width)-col+1, math.ceil(win.row_off+win.height)-row+1)
            pixels = source.read(1, window=win, boundless=True, fill_value=source.nodata or 0)
            profile = {"driver": "GTiff", "height": pixels.shape[0], "width": pixels.shape[1], "count": 1,
                       "dtype": pixels.dtype, "crs": source.crs, "transform": source.window_transform(win),
                       "nodata": source.nodata or 0, "compress": "deflate", "tiled": True}
            with rasterio.open(temp, "w", **profile) as target:
                target.write(pixels, 1)
                target.update_tags(source_asset=asset["href"], scale=asset["scale"], offset=asset["offset"],
                                   radiometry=scene["radiometry"], original_resolution_m=BANDS[name])
    temp.replace(path)
    atomic_json(sidecar, {"identity": identity, "sha256": digest(path), "bytes": path.stat().st_size,
                          "scale": asset["scale"], "offset": asset["offset"], "resolution_m": BANDS[name]})
    return path


def read_grid(path, asset, weights, crs, categorical=False):
    transform = Affine(*weights["transform"][:6])
    with rasterio.open(path) as source:
        with WarpedVRT(source, crs=crs, transform=transform, width=int(weights["width"]), height=int(weights["height"]),
                       resampling=Resampling.nearest if categorical else Resampling.average) as vrt:
            raw = vrt.read(1, masked=True).astype(np.float32).filled(np.nan)
    return raw if categorical else raw*asset["scale"]+asset["offset"]


def process_scene(store, scene, parcels, sampling, scope):
    cache_root = store.root / "data/cache/copernicus"
    used = sum(p.stat().st_size for p in cache_root.rglob("*.tif")) if cache_root.exists() else 0
    if used >= store.config["maximum_cache_gb"]*1024**3:
        raise RuntimeError("cache_size_limit")
    paths = {name: cache_band(store, scene, name, scope["bounds_wgs84"]) for name in BANDS}
    frame = parcels[["internal_parcel_id", "cadastre_code"]].copy()
    frame["observation_time_utc"] = pd.to_datetime(scene["properties"]["datetime"], utc=True)
    frame["observation_date"] = frame.observation_time_utc.dt.strftime("%Y-%m-%d")
    frame["scene_id"] = scene["id"]
    frame["collection"] = scene["collection"]
    frame["data_version"] = store.version
    frame["geometry_version"] = scope["basis_sha256"]
    frame["radiometry"] = scene["radiometry"]
    for res, weights in sampling.items():
        required = list(BANDS) if res == 20 else ["blue", "green", "red", "nir", "scl"]
        arrays = {n: read_grid(paths[n], scene["assets"][n], weights, store.config["crs"], n == "scl") for n in required}
        scl = arrays["scl"]
        clear = np.isin(scl, [4, 5, 6, 7])
        measured = np.isfinite(scl) & (scl != 0)
        for mask, name in ((clear, "clear_fraction"), (measured, "observed_fraction"),
                           (np.isin(scl, [8, 9, 10]), "cloud_fraction"), (scl == 3, "shadow_fraction"),
                           (scl == 11, "snow_fraction")):
            frame[f"{name}_{res}m"] = summarize(mask.astype(float), np.ones(scl.shape, bool), weights)["mean"]
        results = calculate(arrays, res)
        for name, values in results.items():
            # Missing bands and undefined ratios are excluded individually, not filled.
            stats = summarize(values, clear, weights)
            for stat, value in stats.items():
                frame[f"{name}_{stat}"] = value.astype(np.int32 if stat == "count" else np.float32)
        if res == 10:
            ndvi = results["ndvi"]
            for name, mask in (("vegetation_fraction", (scl == 4) & (ndvi >= .30)),
                               ("bare_fraction", (scl == 5) & (ndvi <= .25)), ("water_fraction", scl == 6)):
                frame[name] = summarize(mask.astype(float), clear & np.isfinite(ndvi), weights)["mean"].astype(np.float32)
    frame["quality_state"] = np.where(frame.ndvi_count == 0, "no_usable_pixels", "observed")
    if len(frame) != len(parcels) or frame.internal_parcel_id.duplicated().any():
        raise ValueError("Incomplete or duplicated scene output")
    year = frame.observation_date.iloc[0][:4]
    path = store.base / "eo" / f"year={year}" / f"{scene['id']}.parquet"
    info = atomic_parquet(path, frame)
    return {"output": path.relative_to(store.root).as_posix(), "sha256": info["sha256"], "rows": len(frame)}
