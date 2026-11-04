"""Spherical k-means of AlphaEarth embeddings on the GPU, then group assignment for every grid pixel.

Usage (run with a GPU-enabled Python environment):
  alphaearth_cluster.py [--year 2025]
Learns groups from whole pixels inside eligible parcels (see alphaearth_prepare_grid.py), chooses k by
cosine silhouette among configurations whose restarts agree (median pairwise ARI), and times CPU vs GPU.
Local only; no network access.
"""
from __future__ import annotations
import argparse
import hashlib
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import torch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from wp_core.alphaearth import (assign, choose_k, dequantise, restart_stability, silhouette_cosine,
                                spherical_kmeans_torch)

CONFIG = json.loads((ROOT / "config/alphaearth_groups_20261030_v1.json").read_text())
DATA = ROOT / CONFIG["outputs"]["data"]


def digest(path):
    with open(path, "rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def load(year):
    source = DATA / "embeddings" / f"embeddings_{year}.npy"
    manifest = json.loads(source.with_suffix(".json").read_text())
    if digest(source) != manifest["sha256"]:
        raise ValueError("Embedding file changed since download")
    grid = json.loads((DATA / "grid/grid.json").read_text())
    if digest(DATA / "grid/training_pixels.npz") != grid["files"]["training_pixels.npz"]:
        raise ValueError("Training pixel selection changed")
    return np.load(source, mmap_mode="r"), manifest


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--year", type=int, default=CONFIG["years_initial"][0])
    args = parser.parse_args()
    out = DATA / f"clusters_{args.year}"
    if out.exists():
        raise SystemExit(f"{out.relative_to(ROOT)} already exists; it is never overwritten.")
    embeddings, manifest = load(args.year)
    flat = embeddings.reshape(-1, 64)
    with np.load(DATA / "grid/training_pixels.npz") as z:
        pixel = z["pixel"]
    x, valid = dequantise(np.asarray(flat[pixel]), manifest["quantisation_scale"], manifest["nodata"])
    x = x[valid]
    c = CONFIG["clustering"]
    seed = CONFIG["training"]["seed"]
    device = "cuda"
    xg = torch.from_numpy(x).to(device)
    subsample = np.random.default_rng(seed).choice(len(x), min(c["silhouette_sample"], len(x)), replace=False)
    print(f"training pixels: {len(x):,}; device: {torch.cuda.get_device_name(0)}", flush=True)

    results, centroids = {}, {}
    started = time.perf_counter()
    for k in c["k_values"]:
        fit = spherical_kmeans_torch(xg, k, c["iterations"], c["restarts"], seed + k, device)
        stability = restart_stability(fit["restart_labels"])
        silhouette = silhouette_cosine(x[subsample], fit["labels"][subsample], k)
        shares = np.bincount(fit["labels"], minlength=k) / len(x)
        results[k] = {"mean_cosine_to_centroid": round(fit["score"], 4), "silhouette": round(silhouette, 4),
                      "restart_ari_median": round(stability["median"], 3), "restart_ari_min": round(stability["min"], 3),
                      "restart_pairs": stability["pairs"], "smallest_group_share": round(float(shares.min()), 4)}
        centroids[k] = fit["centroids"]
        print(f"k={k:2d}  silhouette {silhouette:.4f}  restart ARI median {stability['median']:.3f} "
              f"min {stability['min']:.3f}  smallest group {shares.min():.1%}", flush=True)
    torch.cuda.synchronize()
    sweep_seconds = time.perf_counter() - started

    # Same code, same data, one configuration: measured CPU vs GPU time.
    timing_k = 10
    t0 = time.perf_counter()
    spherical_kmeans_torch(torch.from_numpy(x), timing_k, c["iterations"], 1, seed, "cpu")
    cpu_seconds = time.perf_counter() - t0
    torch.cuda.synchronize()
    t0 = time.perf_counter()
    spherical_kmeans_torch(xg, timing_k, c["iterations"], 1, seed, device)
    torch.cuda.synchronize()
    gpu_seconds = time.perf_counter() - t0

    chosen = choose_k(results, c["min_median_restart_ari"])
    out.mkdir(parents=True)
    for k, value in centroids.items():
        np.save(out / f"centroids_k{k}.npy", value.astype(np.float32))
    report = {"version": CONFIG["version"], "year": args.year, "created_at": datetime.now(timezone.utc).isoformat(),
              "gpu": torch.cuda.get_device_name(0), "torch": torch.__version__,
              "training_pixels": int(len(x)), "silhouette_sample": int(len(subsample)),
              "rule": f"best silhouette among k with median pairwise restart ARI >= {c['min_median_restart_ari']}",
              "selection": results, "chosen_k": chosen,
              "timing": {"sweep_gpu_seconds": round(sweep_seconds, 1), "k": timing_k, "iterations": c["iterations"],
                         "cpu_seconds": round(cpu_seconds, 2), "gpu_seconds": round(gpu_seconds, 3),
                         "measured_speedup": round(cpu_seconds / gpu_seconds, 1)},
              "embeddings_sha256": manifest["sha256"]}
    if chosen is not None:
        unit, valid_all = dequantise(np.asarray(flat), manifest["quantisation_scale"], manifest["nodata"])
        labels, fit = assign(unit, centroids[chosen])
        h, w = embeddings.shape[:2]
        np.save(out / "groups.npy", np.where(valid_all, labels, 255).astype(np.uint8).reshape(h, w))
        np.save(out / "fit.npy", fit.astype(np.float16).reshape(h, w))
        np.save(out / "valid.npy", valid_all.reshape(h, w))
        report["files"] = {name: digest(out / name) for name in ("groups.npy", "fit.npy", "valid.npy",
                                                                 f"centroids_k{chosen}.npy")}
    (out / "clusters.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps({k: v for k, v in report.items() if k not in ("selection",)}, indent=2))


if __name__ == "__main__":
    main()
