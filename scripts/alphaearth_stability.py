"""Year-to-year stability of each parcel's AlphaEarth embedding, 2021-2025 (product .venv).

Perennial plantings should look alike every year; annual fields change with rotation and fallow. This is a
hypothesis to test, not a rule: the script also reports how stability relates to today's use-type map.
Outputs per parcel: cosine similarity between parcel-mean embeddings for every pair of years, and the
area-weighted pixel-level similarity between consecutive years. Local only; never overwrites.
"""
from __future__ import annotations
import itertools
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from release_tools import sha256
from wp_core.alphaearth import dequantise, parcel_means, weighted_parcel_mean
from scripts.alphaearth_prepare_grid import load_sealed_grid

CONFIG = json.loads((ROOT / "config/alphaearth_groups_20261030_v1.json").read_text())
DATA = ROOT / CONFIG["outputs"]["data"]
YEARS = [2021, 2022, 2023, 2024, 2025]


def rows(year, pixel):
    source = DATA / "embeddings" / f"embeddings_{year}.npy"
    manifest = json.loads(source.with_suffix(".json").read_text())
    if sha256(source) != manifest["sha256"]:
        raise ValueError(f"{year} embeddings changed since download")
    flat = np.load(source, mmap_mode="r").reshape(-1, 64)
    return dequantise(np.asarray(flat[pixel]), manifest["quantisation_scale"], manifest["nodata"])


def main():
    out = DATA / "stability"
    if out.exists():
        raise SystemExit(f"{out.relative_to(ROOT)} already exists; it is never overwritten.")
    weights = load_sealed_grid(CONFIG)
    parcel, pixel, area = weights["parcel"], weights["pixel"], weights["area"]
    count = int(weights["count"])
    table = pd.read_parquet(DATA / "parcels_2025/parcels.parquet")[
        ["internal_parcel_id", "cadastre_code", "area_official_m2", "group", "group_share", "crop_type", "annual_cycle", "has_whole_pixel"]]
    means, previous = {}, None
    for year in YEARS:
        unit, valid = rows(year, pixel)
        means[year] = parcel_means(unit, valid, parcel, area, count)
        if previous is not None:
            cos = (unit * previous[0]).sum(1)
            table[f"pixel_cos_{year - 1}_{year}"] = weighted_parcel_mean(cos, valid & previous[1], parcel, area, count).round(4)
        previous = (unit, valid)
        print(f"{year}: parcel means ready", flush=True)
    pairs = []
    for a, b in itertools.combinations(YEARS, 2):
        name = f"cos_{a}_{b}"
        table[name] = (means[a] * means[b]).sum(1).round(4)
        pairs.append(name)
    table["stability_mean"] = table[pairs].mean(1).round(4)
    table["stability_min"] = table[pairs].min(1).round(4)
    consecutive = [f"pixel_cos_{y}_{y + 1}" for y in YEARS[:-1]]
    table["pixel_stability_mean"] = table[consecutive].mean(1).round(4)
    out.mkdir(parents=True)
    table.to_parquet(out / "parcels.parquet", index=False)
    np.savez_compressed(out / "parcel_means.npz", **{str(y): means[y].astype(np.float32) for y in YEARS})
    report = {"version": CONFIG["version"], "years": YEARS, "parcels": count,
              "by_today_use_type": {f"{column}_q{int(q * 100)}": table.groupby("crop_type")[column].quantile(q).round(3).to_dict()
                                    for column in ("stability_mean", "stability_min", "pixel_stability_mean") for q in (.25, .5, .75)},
              "by_group": table.groupby("group")[["stability_mean", "pixel_stability_mean"]].median().round(3).to_dict(),
              "files": {name: sha256(out / name) for name in ("parcels.parquet", "parcel_means.npz")}}
    (out / "stability.json").write_text(json.dumps(report, indent=1, default=str), encoding="utf-8")
    view = table.groupby("crop_type")[["stability_mean", "pixel_stability_mean"]].quantile([.25, .5, .75]).unstack().round(3)
    print(view.to_string())
    print(table.groupby("group")[["stability_mean", "pixel_stability_mean"]].median().round(3).to_string())


if __name__ == "__main__":
    main()
