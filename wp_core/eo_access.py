from __future__ import annotations
import hashlib
import math
from pathlib import Path
from typing import Iterable
import numpy as np
import rasterio
from rasterio.enums import Resampling
from rasterio.transform import from_origin
from rasterio.vrt import WarpedVRT

EARTH_SEARCH_URL = "https://earth-search.aws.element84.com/v1/search"
EARTH_SEARCH_COLLECTION = "sentinel-2-c1-l2a"
GRID_CRS = "EPSG:32638"
GRID_RESOLUTION_M = 10.0


def sha256_file(path: Path, chunk_size: int = 8 * 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(chunk_size), b""):
            digest.update(chunk)
    return digest.hexdigest()

def _grid_from_bounds(bounds: Iterable[float]) -> tuple[object, int, int, list[float]]:
    minx, miny, maxx, maxy = [float(value) for value in bounds]
    minx = math.floor(minx / GRID_RESOLUTION_M) * GRID_RESOLUTION_M
    miny = math.floor(miny / GRID_RESOLUTION_M) * GRID_RESOLUTION_M
    maxx = math.ceil(maxx / GRID_RESOLUTION_M) * GRID_RESOLUTION_M
    maxy = math.ceil(maxy / GRID_RESOLUTION_M) * GRID_RESOLUTION_M
    width = int(round((maxx - minx) / GRID_RESOLUTION_M))
    height = int(round((maxy - miny) / GRID_RESOLUTION_M))
    return (
        from_origin(minx, maxy, GRID_RESOLUTION_M, GRID_RESOLUTION_M),
        width,
        height,
        [minx, miny, maxx, maxy],
    )

def _read_asset_to_grid(
    href: str,
    transform,
    width: int,
    height: int,
    *,
    resampling: Resampling,
) -> np.ndarray:
    env_options = {
        "GDAL_DISABLE_READDIR_ON_OPEN": "EMPTY_DIR",
        "CPL_VSIL_CURL_ALLOWED_EXTENSIONS": ".tif",
        "GDAL_HTTP_MULTIRANGE": "YES",
        "GDAL_HTTP_MAX_RETRY": "3",
        "GDAL_HTTP_RETRY_DELAY": "1",
    }
    with rasterio.Env(**env_options):
        with rasterio.open(href) as source:
            with WarpedVRT(
                source,
                crs=GRID_CRS,
                transform=transform,
                width=width,
                height=height,
                resampling=resampling,
                nodata=source.nodata,
            ) as vrt:
                return vrt.read(1)

def _asset_scale_offset(asset: dict) -> tuple[float, float]:
    bands = asset.get("raster:bands") or []
    band = bands[0] if bands else {}
    return float(band.get("scale", 1.0)), float(band.get("offset", 0.0))
