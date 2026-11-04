"""AlphaEarth embedding groups: pure numeric helpers for the Earth Engine/GPU and parcel stages.

Only numpy is imported at module level, so the product and the GPU environment both load it.
Earth Engine requests built here carry grid numbers only, never cadastral codes or geometry.
"""
from __future__ import annotations
import itertools
import numpy as np


def quantise(values, scale=127):
    return np.clip(np.rint(np.asarray(values, dtype=np.float32) * scale), -scale, scale).astype(np.int8)


def dequantise(values, scale=127, nodata=-128):
    """int8 (n, 64) -> unit-length float32 rows and a validity mask; invalid rows stay zero."""
    values = np.asarray(values)
    valid = ~(values == nodata).any(axis=1)
    x = values.astype(np.float32) / scale
    norm = np.linalg.norm(x, axis=1)
    valid &= norm > 0
    x[valid] /= norm[valid, None]
    x[~valid] = 0
    return x, valid


def tile_requests(grid, tile_px):
    """computePixels grids covering the product 10 m grid; numbers only."""
    res, x0, y0 = grid["resolution_m"], grid["origin_x"], grid["origin_y"]
    for row in range(0, grid["height"], tile_px):
        for col in range(0, grid["width"], tile_px):
            width, height = min(tile_px, grid["width"] - col), min(tile_px, grid["height"] - row)
            yield {"row": row, "col": col, "width": width, "height": height,
                   "grid": {"dimensions": {"width": width, "height": height},
                            "affineTransform": {"scaleX": res, "shearX": 0, "translateX": x0 + col * res,
                                                "shearY": 0, "scaleY": -res, "translateY": y0 - row * res},
                            "crsCode": grid["crs"]}}


def training_pixels(parcel, pixel, area, min_area, cap, seed):
    """Pixels lying wholly inside one parcel, at most `cap` per parcel so large parcels don't dominate."""
    rng = np.random.default_rng(seed)
    pure = np.flatnonzero(area >= min_area)
    order = pure[rng.permutation(len(pure))]
    order = order[np.argsort(parcel[order], kind="stable")]
    owners = parcel[order]
    start = np.r_[0, np.flatnonzero(np.diff(owners)) + 1]
    rank = np.arange(len(order)) - np.repeat(start, np.diff(np.r_[start, len(order)]))
    keep = order[rank < cap]
    return pixel[keep], parcel[keep]


def spherical_kmeans_numpy(x, k, iterations=60, restarts=10, seed=0):
    rng = np.random.default_rng(seed)
    best, runs = None, []
    for _ in range(restarts):
        c = x[rng.choice(len(x), k, replace=False)].copy()
        for _ in range(iterations):
            labels = (x @ c.T).argmax(1)
            c = np.zeros_like(c)
            np.add.at(c, labels, x)
            norm = np.linalg.norm(c, axis=1)
            empty = norm < 1e-6
            if empty.any():
                c[empty] = x[rng.integers(0, len(x), int(empty.sum()))]
                norm[empty] = np.linalg.norm(c[empty], axis=1)
            c /= norm[:, None]
        sim = x @ c.T
        labels, score = sim.argmax(1), float(sim.max(1).mean())
        runs.append(labels)
        if best is None or score > best[0]:
            best = (score, c, labels)
    return {"score": best[0], "centroids": best[1], "labels": best[2], "restart_labels": runs}


def spherical_kmeans_torch(x, k, iterations=60, restarts=10, seed=0, device="cuda"):
    """Same algorithm as spherical_kmeans_numpy, on a torch device; x may already be a tensor."""
    import torch
    generator = torch.Generator(device=device).manual_seed(seed)
    x = torch.as_tensor(x, device=device)
    best, runs = None, []
    for _ in range(restarts):
        c = x[torch.randperm(x.shape[0], device=device, generator=generator)[:k]].clone()
        for _ in range(iterations):
            labels = (x @ c.T).argmax(1)
            c = torch.zeros_like(c).index_add_(0, labels, x)
            empty = c.norm(dim=1) < 1e-6
            if empty.any():
                c[empty] = x[torch.randint(0, x.shape[0], (int(empty.sum()),), device=device, generator=generator)]
            c = c / c.norm(dim=1, keepdim=True)
        sim = x @ c.T
        labels, score = sim.argmax(1), float(sim.max(1).values.mean())
        runs.append(labels.cpu().numpy())
        if best is None or score > best[0]:
            best = (score, c.cpu().numpy(), runs[-1])
    return {"score": best[0], "centroids": best[1], "labels": best[2], "restart_labels": runs}


def assign(x, centroids, chunk=500_000):
    labels = np.empty(len(x), dtype=np.int16)
    fit = np.empty(len(x), dtype=np.float32)
    for start in range(0, len(x), chunk):
        sim = x[start:start + chunk] @ centroids.T
        labels[start:start + chunk] = sim.argmax(1)
        fit[start:start + chunk] = sim.max(1)
    return labels, fit


def silhouette_cosine(x, labels, k, chunk=2000):
    """Mean silhouette with cosine distance; chunked so 20,000 points fit in memory."""
    onehot = np.zeros((len(x), k), dtype=np.float32)
    onehot[np.arange(len(x)), labels] = 1
    sizes = onehot.sum(0)
    scores = np.zeros(len(x), dtype=np.float64)
    for start in range(0, len(x), chunk):
        sums = (1 - x[start:start + chunk] @ x.T) @ onehot
        own = labels[start:start + chunk]
        rows = np.arange(len(own))
        size = sizes[own]
        a = np.divide(sums[rows, own], size - 1, out=np.zeros(len(own)), where=size > 1)
        other = np.divide(sums, sizes, out=np.full_like(sums, np.inf), where=sizes > 0)
        other[rows, own] = np.inf
        b = other.min(1)
        s = np.divide(b - a, np.maximum(a, b), out=np.zeros(len(own)), where=np.maximum(a, b) > 0)
        scores[start:start + chunk] = np.where(size > 1, s, 0)
    return float(scores.mean())


def adjusted_rand(a, b):
    _, a = np.unique(a, return_inverse=True)
    _, b = np.unique(b, return_inverse=True)
    table = np.zeros((a.max() + 1, b.max() + 1))
    np.add.at(table, (a, b), 1)
    comb = lambda v: v * (v - 1) / 2
    index, rows, cols, total = comb(table).sum(), comb(table.sum(1)).sum(), comb(table.sum(0)).sum(), comb(len(a))
    expected = rows * cols / total
    maximum = (rows + cols) / 2
    return 1.0 if maximum == expected else float((index - expected) / (maximum - expected))


def restart_stability(restart_labels):
    """Adjusted Rand index over every pair of restarts (the pilot compared only best vs first)."""
    values = [adjusted_rand(a, b) for a, b in itertools.combinations(restart_labels, 2)]
    return {"median": float(np.median(values)), "min": float(np.min(values)), "pairs": len(values)}


def choose_k(results, min_median_ari):
    """Best silhouette among stable configurations; None when no configuration is stable."""
    stable = [k for k, r in results.items() if r["restart_ari_median"] >= min_median_ari]
    return max(stable, key=lambda k: results[k]["silhouette"]) if stable else None


def allocate_sample(stratum_area, total, minimum):
    """Sample size per stratum: a floor for every stratum, the rest proportional to mapped area."""
    stratum_area = np.asarray(stratum_area, dtype=float)
    if minimum * len(stratum_area) > total:
        raise ValueError("Total sample is smaller than the per-stratum minimum")
    extra = (total - minimum * len(stratum_area)) * stratum_area / stratum_area.sum()
    counts = minimum + np.floor(extra).astype(int)
    for i in np.argsort(-(extra - np.floor(extra)))[:total - counts.sum()]:
        counts[i] += 1
    return counts


def area_accuracy(map_class, reference, area, stratum_area, stratum_units, classes):
    """Stratified estimates with strata = map classes, for sampling units of unequal area.

    Within each stratum the share of area with reference class j is a ratio estimate
    (sum of a*[ref=j] over the sum of a); strata are combined by mapped-area weights, following
    Olofsson et al. (2014) good practice with the ratio form of Stehman (2014) for unequal units.
    Returns area proportions with standard errors, overall, user's and producer's accuracy.
    """
    map_class, reference, area = np.asarray(map_class), np.asarray(reference), np.asarray(area, dtype=float)
    weights = np.asarray(stratum_area, dtype=float) / np.sum(stratum_area)
    k, m = len(weights), len(classes)
    ratio, variance = np.zeros((k, m)), np.zeros((k, m))
    for h in range(k):
        member = map_class == h
        n, a = member.sum(), area[member]
        if n < 2:
            raise ValueError(f"Stratum {h} needs at least two assessed units")
        fpc = 1 - n / stratum_units[h]
        for j, label in enumerate(classes):
            y = a * (reference[member] == label)
            ratio[h, j] = y.sum() / a.sum()
            residual = y - ratio[h, j] * a
            variance[h, j] = fpc * (residual @ residual) / (n - 1) / (n * a.mean() ** 2)
    matrix = weights[:, None] * ratio
    proportion = matrix.sum(0)
    se = np.sqrt((weights[:, None] ** 2 * variance).sum(0))
    diagonal = np.array([ratio[h, classes.index(h)] for h in range(k)])
    diagonal_var = np.array([variance[h, classes.index(h)] for h in range(k)])
    producers = np.array([matrix[h, classes.index(h)] / proportion[classes.index(h)]
                          if proportion[classes.index(h)] > 0 else np.nan for h in range(k)])
    return {"matrix": matrix, "proportion": proportion, "proportion_se": se,
            "overall": float((weights * diagonal).sum()), "overall_se": float(np.sqrt((weights ** 2 * diagonal_var).sum())),
            "users": diagonal, "users_se": np.sqrt(diagonal_var), "producers": producers}


def parcel_means(rows, valid, parcel, area, count):
    """Area-weighted mean embedding per parcel, renormalised to unit length; rows align with the weights."""
    w = np.where(valid, area, 0)
    total = np.zeros((count, rows.shape[1]))
    for band in range(rows.shape[1]):
        total[:, band] = np.bincount(parcel, weights=w * rows[:, band], minlength=count)
    norm = np.linalg.norm(total, axis=1)
    return np.divide(total, norm[:, None], out=np.zeros_like(total), where=norm[:, None] > 0)


def weighted_parcel_mean(values, valid, parcel, area, count):
    w = np.where(valid, area, 0)
    mass = np.bincount(parcel, weights=w, minlength=count)
    return np.divide(np.bincount(parcel, weights=w * values, minlength=count), mass, out=np.full(count, np.nan), where=mass > 0)


def parcel_groups(pixel_group, pixel_fit, pixel_valid, parcel, pixel, area, count, k):
    """Exact-area group composition per parcel from per-pixel groups on the product grid."""
    g = pixel_group.ravel()[pixel].astype(np.int64)
    usable = pixel_valid.ravel()[pixel]
    total = np.bincount(parcel, weights=area, minlength=count)
    mass = np.bincount(parcel[usable], weights=area[usable], minlength=count)
    shares = np.zeros((count, k))
    np.add.at(shares, (parcel[usable], g[usable]), area[usable])
    shares = np.divide(shares, mass[:, None], out=np.zeros_like(shares), where=mass[:, None] > 0)
    fit = np.divide(np.bincount(parcel[usable], weights=area[usable] * pixel_fit.ravel()[pixel][usable], minlength=count),
                    mass, out=np.full(count, np.nan), where=mass > 0)
    dominant = np.where(mass > 0, shares.argmax(1), -1)
    return {"shares": shares, "dominant": dominant, "dominant_share": shares.max(1),
            "valid_fraction": np.divide(mass, total, out=np.zeros(count), where=total > 0), "mean_fit": fit}
