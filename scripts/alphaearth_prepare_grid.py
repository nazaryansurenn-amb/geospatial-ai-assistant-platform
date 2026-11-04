"""Pin the AlphaEarth candidate to the sealed product 10 m grid and choose training pixels.

Runs in the product .venv. Reads only sealed local inputs; writes new files under the candidate's
own data folder and never overwrites them. Nothing here contacts an external service.
"""
from __future__ import annotations
import json
import sys
from pathlib import Path

import geopandas as gpd
import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from release_tools import sha256
from wp_core.alphaearth import training_pixels
from wp_core.data_collector.spatial import grid as product_grid

CONFIG = ROOT / "config/alphaearth_groups_20261030_v1.json"


def load_sealed_grid(config):
    g = config["grid"]
    source = ROOT / g["source"]
    pinned = json.loads(source.with_suffix(".json").read_text())["sha256"]
    if sha256(source) != g["source_sha256"] or pinned != g["source_sha256"]:
        raise ValueError("Sealed 10 m sampling grid changed")
    with np.load(source, allow_pickle=False) as z:
        weights = {k: z[k] for k in z.files}
    t = weights["transform"]
    if (t[0], t[4], t[2], t[5], int(weights["width"]), int(weights["height"])) != (
            g["resolution_m"], -g["resolution_m"], g["origin_x"], g["origin_y"], g["width"], g["height"]):
        raise ValueError("Grid definition differs from the configuration")
    if int(weights["count"]) != config["expected_eligible"]:
        raise ValueError("Eligible parcel count changed")
    return weights


def main():
    config = json.loads(CONFIG.read_text())
    out = ROOT / config["outputs"]["data"] / "grid"
    if out.exists():
        raise SystemExit(f"{out.relative_to(ROOT)} already exists; it is never overwritten.")
    weights = load_sealed_grid(config)
    g = config["grid"]
    parcels = pd.read_parquet(ROOT / g["parcel_order"])
    if len(parcels) != config["expected_eligible"] or parcels.internal_parcel_id.duplicated().any():
        raise ValueError("Parcel order table does not match the sealed grid")
    with np.load(ROOT / g["interiors"], allow_pickle=False) as z:
        interiors = {k: z[k] for k in z.files}
    if not (np.array_equal(interiors["parcel"], weights["parcel"]) and np.array_equal(interiors["area"], weights["area"])):
        raise ValueError("Transitions interior weights are not aligned with the sealed grid")

    basis = json.loads((ROOT / "data/observations/observations_2021_2025_v1/specification.json").read_text())["config"]["basis"]
    geometry = gpd.read_parquet(ROOT / basis).set_index("internal_parcel_id").loc[parcels.internal_parcel_id]
    if not np.array_equal(geometry.cadastre_code, parcels.cadastre_code) or not np.array_equal(
            geometry.area_official_m2, parcels.area_official_m2):
        raise ValueError("Cadastral code or official area differs from the immutable source")
    metric = geometry.to_crs(g["crs"])
    transform, width, height = product_grid(metric.total_bounds, g["resolution_m"])
    if (transform.c, transform.f, width, height) != (g["origin_x"], g["origin_y"], g["width"], g["height"]):
        raise ValueError("Grid rebuilt from parcel geometry differs from the sealed grid")
    covered = np.bincount(weights["parcel"], weights=weights["area"], minlength=len(parcels))
    if not np.allclose(covered, metric.area.to_numpy(), rtol=1e-7, atol=.01):
        raise ValueError("Sealed weights no longer cover the immutable geometry")

    t = config["training"]
    pixel, owner = training_pixels(weights["parcel"], weights["pixel"], weights["area"],
                                   t["pure_pixel_min_area_m2"], t["max_pixels_per_parcel"], t["seed"])
    out.mkdir(parents=True)
    np.savez_compressed(out / "training_pixels.npz", pixel=pixel.astype(np.int32), parcel=owner.astype(np.int32))
    table = parcels[["internal_parcel_id", "cadastre_code", "area_official_m2", "activity_stage"]].copy()
    table["pixel_equivalents"] = metric.area.to_numpy() / g["resolution_m"] ** 2
    table.to_parquet(out / "parcels.parquet", index=False)
    pure = np.bincount(weights["parcel"][weights["area"] >= t["pure_pixel_min_area_m2"]], minlength=len(parcels))
    report = {
        "version": config["version"], "grid": g, "grid_source_sha256": g["source_sha256"],
        "checks": ["sealed sampling hash", "grid rebuilt from parcel geometry", "exact area coverage",
                   "transitions interiors aligned", "cadastral code and official area unchanged"],
        "parcels": len(parcels), "grid_pixels": width * height,
        "training_pixels": int(len(pixel)), "parcels_with_training_pixels": int(len(np.unique(owner))),
        "parcels_without_pure_pixel": int((pure == 0).sum()),
        "download_bytes_per_year": width * height * 64,
        "files": {name: sha256(out / name) for name in ("training_pixels.npz", "parcels.parquet")},
    }
    (out / "grid.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps({k: v for k, v in report.items() if k not in ("grid", "files")}, indent=2))


if __name__ == "__main__":
    main()
