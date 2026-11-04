"""AlphaEarth Foundations embeddings for the product grid's rectangle (GPU/Earth Engine environment).

Usage (run with a GPU-enabled Python environment):
  alphaearth_download.py check [--year 2025]          environment checks and size report; tiny probe only
  alphaearth_download.py download --year 2025 --confirm
Only the rectangle and grid numbers are sent to Earth Engine; no cadastral code or geometry.
The AlphaEarth Foundations Satellite Embedding dataset is produced by Google and Google DeepMind.
"""
from __future__ import annotations
import argparse
import hashlib
import io
import json
import platform
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from wp_core.alphaearth import dequantise, tile_requests

CONFIG = json.loads((ROOT / "config/alphaearth_groups_20261030_v1.json").read_text())
OUT = ROOT / CONFIG["outputs"]["data"] / "embeddings"


def digest(path):
    with open(path, "rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def image(ee, year):
    g = CONFIG["grid"]
    res = g["resolution_m"]
    rectangle = ee.Geometry.Rectangle([g["origin_x"], g["origin_y"] - g["height"] * res,
                                       g["origin_x"] + g["width"] * res, g["origin_y"]], g["crs"], False)
    collection = (ee.ImageCollection(CONFIG["dataset"]).filterDate(f"{year}-01-01", f"{year + 1}-01-01")
                  .filterBounds(rectangle))
    d = CONFIG["download"]
    scaled = collection.mosaic().multiply(d["quantisation_scale"]).round()
    return collection, rectangle, scaled.clamp(-d["quantisation_scale"], d["quantisation_scale"]).unmask(d["nodata"]).toInt8()


def fetch(ee, expression, request):
    for attempt in range(CONFIG["download"]["maximum_attempts"]):
        try:
            raw = ee.data.computePixels({"expression": expression, "fileFormat": "NPY", "grid": request["grid"]})
            array = np.load(io.BytesIO(raw))
            return np.stack([array[name] for name in array.dtype.names], axis=-1).astype(np.int8)
        except Exception as exc:
            if attempt == CONFIG["download"]["maximum_attempts"] - 1:
                raise
            print(f"  retry tile {request['row']},{request['col']}: {type(exc).__name__}", flush=True)
            time.sleep(5 * (attempt + 1))


def check(year):
    import torch
    import ee
    report = {"checked_at": datetime.now(timezone.utc).isoformat(), "python": platform.python_version(),
              "torch": torch.__version__, "earthengine_api": ee.__version__}
    x = torch.randn(4096, 4096, device="cuda")
    torch.cuda.synchronize()
    report["gpu"] = torch.cuda.get_device_name(0)
    report["gpu_matmul_ok"] = bool(torch.isfinite((x @ x).sum()).item())
    ee.Initialize(project=CONFIG["earth_engine_project"])
    collection, rectangle, expression = image(ee, year)
    report["images_covering_rectangle"] = collection.size().getInfo()
    projection = collection.first().select(0).projection().getInfo()
    report["native_projection"] = {"crs": projection.get("crs"), "transform": projection.get("transform")}
    full = collection.geometry().contains(rectangle, 1).getInfo()
    report["rectangle_fully_covered"] = bool(full)
    g = CONFIG["grid"]
    probe = {"row": g["height"] // 2, "col": g["width"] // 2, "width": 64, "height": 64}
    request = next(r for r in tile_requests({**g, "origin_x": g["origin_x"] + probe["col"] * g["resolution_m"],
                                             "origin_y": g["origin_y"] - probe["row"] * g["resolution_m"],
                                             "width": 64, "height": 64}, 64))
    values = fetch(ee, expression, request).reshape(-1, 64)
    unit, valid = dequantise(values, CONFIG["download"]["quantisation_scale"], CONFIG["download"]["nodata"])
    raw_norm = np.linalg.norm(values[valid].astype(np.float32) / CONFIG["download"]["quantisation_scale"], axis=1)
    report["probe"] = {"pixels": int(len(values)), "valid_share": float(valid.mean()),
                       "dequantised_norm_mean": float(raw_norm.mean()) if valid.any() else None,
                       "dequantised_norm_min": float(raw_norm.min()) if valid.any() else None,
                       "dequantised_norm_max": float(raw_norm.max()) if valid.any() else None}
    tiles = list(tile_requests(g, CONFIG["download"]["tile_px"]))
    report["download"] = {"year": year, "tiles": len(tiles), "pixels": g["width"] * g["height"],
                          "bytes": g["width"] * g["height"] * 64, "format": "int8 x 64 bands (NPY)"}
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / f"check_{year}.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report, indent=2))


def download(year):
    import ee
    ee.Initialize(project=CONFIG["earth_engine_project"])
    g = CONFIG["grid"]
    target = OUT / f"embeddings_{year}.npy"
    if target.exists():
        raise SystemExit(f"{target.relative_to(ROOT)} already exists; it is never overwritten.")
    tiles_dir = OUT / f"tiles_{year}"
    tiles_dir.mkdir(parents=True, exist_ok=True)
    _, _, expression = image(ee, year)
    requests = list(tile_requests(g, CONFIG["download"]["tile_px"]))
    for i, request in enumerate(requests, 1):
        path = tiles_dir / f"tile_{request['row']:05d}_{request['col']:05d}.npy"
        if not path.exists():
            tile = fetch(ee, expression, request)
            if tile.shape != (request["height"], request["width"], 64):
                raise ValueError(f"Unexpected tile shape {tile.shape}")
            np.save(path.with_suffix(".partial.npy"), tile)
            path.with_suffix(".partial.npy").replace(path)
        print(f"  {i}/{len(requests)} tiles", flush=True)
    mosaic = np.lib.format.open_memmap(target.with_suffix(".partial.npy"), mode="w+", dtype=np.int8,
                                       shape=(g["height"], g["width"], 64))
    for request in requests:
        tile = np.load(tiles_dir / f"tile_{request['row']:05d}_{request['col']:05d}.npy")
        mosaic[request["row"]:request["row"] + request["height"], request["col"]:request["col"] + request["width"]] = tile
    mosaic.flush()
    del mosaic
    target.with_suffix(".partial.npy").replace(target)
    manifest = {"version": CONFIG["version"], "dataset": CONFIG["dataset"], "year": year, "credit": CONFIG["credit"],
                "downloaded_at": datetime.now(timezone.utc).isoformat(), "grid": g,
                "quantisation_scale": CONFIG["download"]["quantisation_scale"], "nodata": CONFIG["download"]["nodata"],
                "tiles": len(requests), "bytes": target.stat().st_size, "sha256": digest(target)}
    (OUT / f"embeddings_{year}.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    print(json.dumps(manifest, indent=2))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=["check", "download"])
    parser.add_argument("--year", type=int, default=CONFIG["years_initial"][0])
    parser.add_argument("--confirm", action="store_true")
    args = parser.parse_args()
    allowed = CONFIG["years_initial"] + CONFIG["years_followup_after_naming"]
    if args.year not in allowed:
        raise SystemExit(f"Year must be one of {allowed}")
    if args.action == "check":
        check(args.year)
    elif not args.confirm:
        raise SystemExit("Downloading requires --confirm after the owner approves the reported size.")
    else:
        download(args.year)


if __name__ == "__main__":
    main()
