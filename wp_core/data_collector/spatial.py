"""Immutable parcel population and reusable exact pixel-area sampling weights."""
import json
import math
import sqlite3
import numpy as np
import pandas as pd
import geopandas as gpd
import shapely
from rasterio.transform import from_origin
from release_tools import read_release, local_path
from .storage import digest, atomic_parquet, atomic_json


def basis(store):
    path = local_path(store.root, store.config["basis"])
    parcels = gpd.read_parquet(path)
    release, manifest = read_release(store.root, store.config["working_release"])
    with sqlite3.connect((release/manifest["index"]).as_uri()+"?mode=ro", uri=True) as con:
        masks = pd.read_sql_query("SELECT cadastre_code,household,road_excluded,activity_stage FROM parcel_analytics", con)
    if len(parcels) != store.config["expected_population"] or parcels.cadastre_code.duplicated().any():
        raise ValueError("Unexpected cadastral population")
    if parcels.internal_parcel_id.duplicated().any() or not parcels.is_valid.all() or parcels.crs is None:
        raise ValueError("Invalid immutable geometry/identity/CRS")
    if set(parcels.cadastre_code) != set(masks.cadastre_code):
        raise ValueError("Missing mask membership; refusing silent exclusions")
    columns = ["internal_parcel_id", "cadastre_code", "area_official_m2", "geometry"]
    joined = parcels[columns].merge(masks, on="cadastre_code", validate="one_to_one")
    if joined[["household", "road_excluded", "activity_stage"]].isna().any().any():
        raise ValueError("Missing exclusion or stage")
    if set(joined.activity_stage) != {"stage_1", "stage_2"}:
        raise ValueError("Unexpected area scope")
    selected = joined.loc[~joined.household.astype(bool) & ~joined.road_excluded.astype(bool)].copy()
    selected = selected.sort_values("internal_parcel_id").reset_index(drop=True)
    if len(selected) != store.config["expected_eligible"]:
        raise ValueError("Scope count changed; requires explicit new version")
    audit = joined.drop(columns="geometry").copy()
    audit["included"] = ~audit.household.astype(bool) & ~audit.road_excluded.astype(bool)
    audit["exclusion"] = np.select([audit.road_excluded.astype(bool), audit.household.astype(bool)],
                                   ["road", "household"], default="")
    scope = {"basis_sha256": digest(path), "mask_sha256": digest(release/manifest["index"]),
             "geometry_crs": str(parcels.crs), "population": len(joined), "included": len(selected),
             "household": int(joined.household.sum()), "roads": int(joined.road_excluded.sum()),
             "stage_counts": selected.activity_stage.value_counts().to_dict(),
             "bounds_wgs84": selected.to_crs(4326).total_bounds.tolist()}
    return selected, audit, scope


def grid(bounds, resolution):
    left, bottom, right, top = bounds
    left, bottom = math.floor(left/resolution)*resolution, math.floor(bottom/resolution)*resolution
    right, top = math.ceil(right/resolution)*resolution, math.ceil(top/resolution)*resolution
    return from_origin(left, top, resolution, resolution), int(round((right-left)/resolution)), int(round((top-bottom)/resolution))


def build_weights(parcels, resolution, crs):
    metric = parcels.to_crs(crs)
    transform, width, height = grid(metric.total_bounds, resolution)
    indices, pixels, weights = [], [], []
    for index, geom in enumerate(metric.geometry):
        xmin, ymin, xmax, ymax = geom.bounds
        c0 = max(0, math.floor((xmin-transform.c)/resolution))
        c1 = min(width, math.ceil((xmax-transform.c)/resolution))
        r0 = max(0, math.floor((transform.f-ymax)/resolution))
        r1 = min(height, math.ceil((transform.f-ymin)/resolution))
        rows, cols = np.meshgrid(np.arange(r0, r1), np.arange(c0, c1), indexing="ij")
        rows, cols = rows.ravel(), cols.ravel()
        x, y = transform.c+cols*resolution, transform.f-rows*resolution
        cells = shapely.box(x, y-resolution, x+resolution, y)
        areas = shapely.area(shapely.intersection(cells, geom))
        keep = areas > 1e-8
        if not np.isclose(areas.sum(), geom.area, rtol=1e-7, atol=.01):
            raise ValueError("Incomplete pixel coverage of parcel")
        indices.append(np.full(keep.sum(), index, dtype=np.int32))
        pixels.append((rows[keep]*width+cols[keep]).astype(np.int32))
        weights.append(areas[keep].astype(np.float64))
    return {"parcel": np.concatenate(indices), "pixel": np.concatenate(pixels),
            "area": np.concatenate(weights), "transform": np.array(tuple(transform)),
            "width": np.array(width), "height": np.array(height), "count": np.array(len(parcels))}


def load_weights(store, parcels, resolution):
    path = store.base / "sampling" / f"exact_area_{resolution}m.npz"
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        weights = build_weights(parcels, resolution, store.config["crs"])
        temp = path.with_suffix(".partial.npz")
        np.savez_compressed(temp, **weights)
        temp.replace(path)
        atomic_json(path.with_suffix(".json"), {"sha256": digest(path), "method": "exact_intersection",
                    "resolution_m": resolution, "parcel_count": len(parcels)})
    if digest(path) != json.loads(path.with_suffix(".json").read_text())["sha256"]:
        raise ValueError("Sampling weights checksum mismatch")
    with np.load(path) as arrays:
        return {key: arrays[key] for key in arrays.files}


def summarize(values, valid, weights):
    p, pixel, area = (weights[k] for k in ("parcel", "pixel", "area"))
    count = int(weights["count"])
    total = np.bincount(p, weights=area, minlength=count)
    sampled = values.ravel()[pixel]
    usable = valid.ravel()[pixel] & np.isfinite(sampled)
    groups, w, v = p[usable], area[usable], sampled[usable]
    mass = np.bincount(groups, weights=w, minlength=count)
    n = np.bincount(groups, minlength=count)
    mean = np.divide(np.bincount(groups, weights=w*v, minlength=count), mass,
                     out=np.full(count, np.nan), where=mass > 0)
    variance = np.divide(np.bincount(groups, weights=w*(v-mean[groups])**2, minlength=count), mass,
                        out=np.full(count, np.nan), where=mass > 0)
    result = {"mean": mean, "std": np.sqrt(variance), "count": n,
              "valid_fraction": np.divide(mass, total, out=np.zeros(count), where=total > 0)}
    if len(v):
        order = np.lexsort((v, groups))
        sv, sw, sg = v[order], w[order], groups[order]
        start = np.r_[0, np.flatnonzero(np.diff(sg))+1]
        cumulative = np.cumsum(sw)
        base = np.where(start > 0, cumulative[np.maximum(start-1, 0)], 0)
        for q, name in ((.1, "p10"), (.5, "median"), (.9, "p90")):
            output = np.full(count, np.nan)
            ix = np.searchsorted(cumulative, base+mass[sg[start]]*q, side="left")
            output[sg[start]] = sv[np.minimum(ix, len(sv)-1)]
            result[name] = output
    else:
        result.update({name: np.full(count, np.nan) for name in ("p10", "median", "p90")})
    return result
