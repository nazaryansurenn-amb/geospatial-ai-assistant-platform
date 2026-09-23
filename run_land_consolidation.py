"""Offline active-land consolidation screening; no changes to map releases."""
from __future__ import annotations
import os
for _key in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS"):
    os.environ[_key] = "1"
import argparse
import json
from pathlib import Path
import shutil
import sqlite3
import time

import geopandas as gpd
import numpy as np
import pandas as pd
import shapely

from release_tools import local_path, sha256
from run_observation_analysis import now, run_lock, write_json, write_table
from wp_core.land_consolidation import (INDICES, Policy, block_id, compare_season,
    connected_complete_link, negligible_overlap, shared_boundary, shared_pixels, union_geometry)
from wp_core.observation_screening import Policy as ScreeningPolicy, isolated_excursions

ROOT = Path(__file__).resolve().parent
CONFIG = "config/consolidation_active_20260906_v1.json"
OUT = ROOT / "data/analysis/land_consolidation/consolidation_active_20260906_v1"


def read(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def status(message, **values):
    print(json.dumps({"stage": message, **values}), flush=True)


def paths(config):
    rel = local_path(ROOT, config["activity_release"])
    release = read(rel / "release.json")
    eo = local_path(ROOT, config["eo"])
    obs = local_path(ROOT, config["observations"])
    return rel, release, eo, obs


def input_files(config):
    rel, release, eo, obs = paths(config)
    files = [local_path(ROOT, config[k]) for k in ("basis", "roads", "canal")]
    files += [rel / "release.json", rel / release["index"], eo / "complete.json",
              eo / "spatial.parquet", eo / "selection.parquet", obs / "parcels.parquet"]
    files += [eo / "daily" / f"{y}.parquet" for y in config["years"]]
    files += [obs / "sampling" / f"exact_area_{r}m.npz" for r in (10, 20)]
    files += [obs / "sampling" / f"exact_area_{r}m.json" for r in (10, 20)]
    # Protected pointers and runtimes are hashed, never modified.
    files += [ROOT / "config/releases.json", rel / "app.py", ROOT / "app.py"]
    return files


def check(hashes):
    for relative, expected in hashes.items():
        if sha256(local_path(ROOT, relative)) != expected:
            raise ValueError("Pinned file changed: " + relative)


def load_population(config, policy):
    rel, release, eo, _ = paths(config)
    with sqlite3.connect((rel / release["index"]).as_uri() + "?mode=ro", uri=True) as con:
        activity = pd.read_sql_query("SELECT cadastre_code, activity_class, activity_state, household, road_excluded FROM parcel_analytics", con)
    basis = gpd.read_parquet(local_path(ROOT, config["basis"]))
    if basis.crs.to_epsg() != 4326 or not basis.is_valid.all() or basis.is_empty.any():
        raise ValueError("Invalid immutable cadastral basis")
    for column in ("internal_parcel_id", "cadastre_code"):
        if basis[column].isna().any() or not basis[column].is_unique:
            raise ValueError("Missing or duplicate cadastral identity")
    if set(basis.cadastre_code) != set(activity.cadastre_code):
        raise ValueError("Working activity / cadastral coverage mismatch")
    source = basis.merge(activity, on="cadastre_code", validate="1:1", suffixes=("", "_release"))
    source["mask_conflict"] = (source.road_excluded != source.road_excluded_release.astype(bool)) | (source.household_agriculture != source.household.astype(bool))
    active = source.activity_class.eq(config["activity_class"]) & source.activity_state.eq(config["activity_state"])
    active &= ~source.household.astype(bool) & ~source.road_excluded_release.astype(bool)
    active_geo = source.loc[active].sort_values("internal_parcel_id").reset_index(drop=True)
    spatial = pd.read_parquet(eo / "spatial.parquet")
    active_geo = active_geo.merge(spatial.drop(columns=["cadastre_code", "area_official_m2", "activity_stage"]), on="internal_parcel_id", how="left", validate="1:1")
    metric = active_geo.to_crs(32638)
    supported = ((metric.minimum_width_m >= policy.minimum_width_m)
        & (metric.pure_pixels_10m >= policy.minimum_pure_pixels_10m)
        & (metric.pure_pixels_20m >= policy.minimum_pure_pixels_20m))
    metric["screening_state"] = np.where(supported, "pending", "spatial_support_review")
    metric.loc[metric.mask_conflict, "screening_state"] = "household_road_mask_conflict_review"
    roads = gpd.read_file(local_path(ROOT, config["roads"])).to_crs(32638)
    canal = gpd.read_file(local_path(ROOT, config["canal"])).to_crs(32638)
    buffers = [g.buffer(float(w)) for g, w in zip(roads.geometry, roads.buffer_width_m)]
    buffers += list(canal.buffer(policy.canal_buffer_m))
    barrier_tree = shapely.STRtree(buffers)
    line_tree = shapely.STRtree(list(roads.geometry) + list(canal.geometry))
    interiors = np.asarray(metric.geometry.buffer(-policy.interior_buffer_m).values)
    crossing = line_tree.query(interiors, predicate="intersects")
    if crossing.size:
        affected = np.unique(crossing[0])
        affected = [i for i in affected if not metric.mask_conflict.iloc[i]]
        metric.loc[affected, "screening_state"] = "mapped_barrier_review"
    # Treat only small boundary slivers as adjacency, never repair input polygons.
    tree = shapely.STRtree(metric.geometry.values)
    pairs = tree.query(metric.geometry.values, predicate="dwithin", distance=policy.topology_tolerance_m)
    bands = metric.geometry.boundary.buffer(policy.topology_tolerance_m).values
    edges, audit, overlaps = [], [], set()
    status("checking_local_boundaries", pairs=int((pairs[0] < pairs[1]).sum()))
    for a, b in zip(*pairs):
        if a >= b:
            continue
        length, reason = shared_boundary(metric.geometry.iloc[a], metric.geometry.iloc[b], barrier_tree, policy, bands[a], bands[b])
        if reason == "overlapping_geometry":
            overlaps.update((int(a), int(b)))
        audit.append((int(a), int(b), length, reason))
    if overlaps:
        metric.loc[[i for i in sorted(overlaps) if not metric.mask_conflict.iloc[i]], "screening_state"] = "overlap_review"
    for a, b, length, reason in audit:
        if reason == "adjacent" and metric.screening_state.iloc[a] == metric.screening_state.iloc[b] == "pending":
            edges.append((a, b, length))
    return basis, metric, edges, audit


def load_pixels(config, ids):
    _, _, _, obs = paths(config)
    order = pd.read_parquet(obs / "parcels.parquet").sort_values("internal_parcel_id").internal_parcel_id
    positions = pd.Index(order).get_indexer(ids)
    if (positions < 0).any():
        raise ValueError("Active parcel missing from observation population")
    result = {}
    for resolution in (10, 20):
        with np.load(obs / "sampling" / f"exact_area_{resolution}m.npz", allow_pickle=False) as z:
            p, px, area = z["parcel"], z["pixel"], z["area"]
        order_idx = np.argsort(p, kind="stable")
        groups = np.split(order_idx, np.cumsum(np.bincount(p, minlength=len(order)))[:-1])
        result[resolution] = [(px[groups[k]], area[groups[k]]) for k in positions]
    return result


def load_series(config, ids, policy):
    _, _, eo, _ = paths(config)
    result = {}
    columns = ["internal_parcel_id", "observation_date", "support", "finite"] + [k + "_median" for k in INDICES]
    for year in config["years"]:
        frame = pd.read_parquet(eo / "daily" / f"{year}.parquet", columns=columns,
                                filters=[("internal_parcel_id", "in", list(ids))])
        if frame.duplicated(["internal_parcel_id", "observation_date"]).any():
            raise ValueError("Duplicate parcel-date observations")
        dates = pd.to_datetime(frame.observation_date)
        if not dates.dt.year.eq(year).all():
            raise ValueError("Observation year mismatch")
        days = np.sort(dates.dt.dayofyear.unique())
        pi = pd.Index(ids).get_indexer(frame.internal_parcel_id)
        di = pd.Index(days).get_indexer(dates.dt.dayofyear)
        values = np.full((len(ids), len(days), 4), np.nan, dtype=np.float32)
        good = (frame.support >= policy.minimum_valid_fraction) & frame.finite
        values[pi[good], di[good]] = frame.loc[good, [k + "_median" for k in INDICES]].to_numpy(np.float32)
        for i in range(len(ids)):
            a = values[i]
            mask = np.isfinite(a).all(axis=1)
            flagged = isolated_excursions(days, a[:, 0], a[:, 1], mask, ScreeningPolicy())
            a[flagged] = np.nan
        result[year] = (days, values)
        status("loaded_existing_eo", year=year, parcels=len(ids), rows=len(frame))
    return result


def draw_preview(metric, members, blocks):
    from PIL import Image, ImageDraw, ImageFont
    image = Image.new("RGB", (1600, 1000), "white")
    draw = ImageDraw.Draw(image)
    xmin, ymin, xmax, ymax = metric.total_bounds
    scale = min(1520 / (xmax - xmin), 800 / (ymax - ymin))
    def coordinates(ring):
        return [(40 + (x - xmin) * scale, 910 - (y - ymin) * scale) for x, y in ring.coords]
    def paint(geometry, fill, outline=None):
        polygons = [geometry] if geometry.geom_type == "Polygon" else geometry.geoms
        for polygon in polygons:
            draw.polygon(coordinates(polygon.exterior), fill=fill, outline=outline)
            for ring in polygon.interiors:
                draw.polygon(coordinates(ring), fill="white")
    for geometry in metric.geometry:
        paint(geometry, "#d5dadd")
    colors = ["#007c83", "#dc8b10", "#bd4054", "#4271b5"]
    for n, block in enumerate(blocks.itertuples()):
        paint(block.geometry, colors[n % len(colors)], "#172725")
    font = ImageFont.truetype("C:/Windows/Fonts/arial.ttf", 28)
    small = ImageFont.truetype("C:/Windows/Fonts/arial.ttf", 21)
    draw.text((40, 22), "Potential consolidation blocks: active land only", font=font, fill="#172725")
    draw.text((40, 62), f"{len(blocks)} candidate blocks | {len(members)} parcels | private, not approved", font=small, fill="#172725")
    draw.text((40, 950), "Gray: active parcels. Colors distinguish candidate groups, not suitability levels.", font=small, fill="#172725")
    image.save(OUT / "overview.png")


def execute():
    config = read(ROOT / CONFIG)
    policy = Policy(**config["policy"])
    if config["years"] != list(range(2021, 2026)) or config["publication"] != "internal_draft_only":
        raise ValueError("Unsupported period/publication")
    OUT.mkdir(parents=True, exist_ok=True)
    with run_lock(OUT / "run.lock"):
        if (OUT / "complete.json").exists():
            verify(); return
        if (OUT / "manifest.json").exists():
            raise ValueError("Unsealed attempt exists; inspect it before creating a new version")
        started = time.perf_counter()
        code = [CONFIG, "run_land_consolidation.py", "wp_core/land_consolidation.py",
                "release_tools.py", "run_observation_analysis.py", "wp_core/observation_rules.py", "wp_core/observation_screening.py"]
        hashes = {p.relative_to(ROOT).as_posix(): sha256(p) for p in input_files(config)}
        pins = {p: sha256(ROOT / p) for p in code}
        _, _, eo, obs = paths(config)
        complete = read(eo / "complete.json")["outputs"]
        for relative in ["spatial.parquet", "selection.parquet"] + [f"daily/{y}.parquet" for y in config["years"]]:
            if hashes[(eo / relative).relative_to(ROOT).as_posix()] != complete[relative]:
                raise ValueError("Unverified EO input: " + relative)
        for r in (10, 20):
            path = obs / "sampling" / f"exact_area_{r}m.npz"
            if hashes[path.relative_to(ROOT).as_posix()] != read(path.with_suffix(".json"))["sha256"]:
                raise ValueError("Spatial sampling input changed")
        manifest = {"created_at": now(), "config": config, "inputs": hashes, "code": pins,
                    "accepted": False, "training_eligible": False, "activity_year": 2026,
                    "phenology_years": config["years"], "network_requests": 0, "raster_reads": 0}
        write_json(OUT / "manifest.json", manifest)
        for relative in code:
            destination = OUT / "source" / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(ROOT / relative, destination)
        basis, metric, edges, topology = load_population(config, policy)
        ids = metric.internal_parcel_id.tolist()
        status("spatial_screening", active_parcels=len(metric), neighboring_pairs=len(edges), states=metric.screening_state.value_counts().to_dict())
        participants = sorted({i for a, b, _ in edges for i in (a, b)})
        selected_ids = [ids[i] for i in participants]
        index = {i: j for j, i in enumerate(participants)}
        series = load_series(config, selected_ids, policy) if participants else {}
        pixels = load_pixels(config, selected_ids) if participants else {}
        pair_cache, diagnostics = {}, []
        def compare(a, b):
            a, b = sorted((a, b))
            if (a, b) in pair_cache:
                return pair_cache[a, b]
            i, j = index[a], index[b]
            shared = max(shared_pixels(pixels[r][i], pixels[r][j]) for r in (10, 20))
            details, mask = [], 0
            if shared <= policy.maximum_shared_pixel_fraction:
                for bit, year in enumerate(config["years"]):
                    days, values = series[year]
                    d = compare_season(days, values[i], values[j], year, policy)
                    details.append(d)
                    if d["similar"]:
                        mask |= 1 << bit
            diagnostics.append({"a": ids[a], "b": ids[b], "support_year_mask": mask,
                "shared_pixel_fraction": shared, "year_results": json.dumps(details),
                "reason": "shared_pixel_review" if not details else ("similar" if mask.bit_count() >= policy.minimum_years else "not_repeatedly_similar")})
            pair_cache[a, b] = mask
            return mask
        groups = connected_complete_link(ids, edges, compare, policy)
        member_rows, block_rows = [], []
        for members, mask in groups:
            area = float(metric.iloc[members].area_official_m2.sum()) / 10000
            if len(members) < 2:
                i = members[0]
                if metric.screening_state.iloc[i] == "pending":
                    metric.loc[i, "screening_state"] = "no_qualifying_group"
                continue
            bid = block_id([ids[i] for i in members])
            qualifies = area >= policy.minimum_area_ha
            state = "candidate" if qualifies else "similar_group_below_5ha"
            metric.loc[members, "screening_state"] = state
            if not qualifies:
                continue
            years = [y for bit, y in enumerate(config["years"]) if mask & (1 << bit)]
            stages = sorted(set(metric.iloc[members].stage))
            block_rows.append({"block_id": bid, "parcel_count": len(members), "area_ha": area,
                "stage": ",".join(stages), "support_years": ",".join(map(str, years)), "support_year_mask": mask,
                "state": "candidate", "accepted": False, "geometry": union_geometry(metric.geometry.iloc[members].values)})
            for i in members:
                row = metric.iloc[i]
                member_rows.append({"block_id": bid, "internal_parcel_id": ids[i], "cadastre_code": row.cadastre_code,
                    "area_official_m2": float(row.area_official_m2), "stage": row.stage})
        cols = ["block_id", "parcel_count", "area_ha", "stage", "support_years", "support_year_mask", "state", "accepted", "geometry"]
        blocks = gpd.GeoDataFrame(block_rows, columns=cols, geometry="geometry", crs=32638)
        members = pd.DataFrame(member_rows, columns=["block_id", "internal_parcel_id", "cadastre_code", "area_official_m2", "stage"])
        register = pd.DataFrame(metric.drop(columns="geometry"))
        register["accepted"] = False
        write_table(OUT / "active_register.parquet", register)
        write_table(OUT / "members.parquet", members)
        write_table(OUT / "pair_checks.parquet", pd.DataFrame(diagnostics))
        write_table(OUT / "topology.parquet", pd.DataFrame(topology, columns=["a", "b", "shared_boundary_m", "state"]))
        write_table(OUT / "blocks.parquet", blocks)
        public_fields = ["block_id", "parcel_count", "area_ha", "stage", "support_years", "state", "geometry"]
        (OUT / "blocks.geojson").write_text(blocks[public_fields].to_crs(4326).to_json(drop_id=True), encoding="utf-8")
        draw_preview(metric, members, blocks)
        summary = {"version": config["version"], "created_at": now(), "active_parcels": len(metric),
            "classified_activity_year": 2026, "phenology_years": config["years"], "candidate_blocks": len(blocks),
            "candidate_parcels": len(members), "candidate_area_ha": float(blocks.area_ha.sum()),
            "adjacent_pairs_tested": len(edges), "all_pair_checks": len(diagnostics),
            "register_states": register.screening_state.value_counts().to_dict(),
            "accepted": False, "training_eligible": False, "publication": "internal_draft_only",
            "network_requests": 0, "raster_reads": 0, "elapsed_seconds": round(time.perf_counter() - started, 2),
            "limitations": ["Uncalibrated screening thresholds; no independent accuracy estimate.",
                "Known roads and Lower Hrazdan canal only: smaller unmapped barriers, buildings and water need visual review.",
                "Disjoint greedy grouping is deterministic, not a globally optimal consolidation layout.",
                "Adjacency permits <=25 cm boundary slivers of <=1% of the smaller parcel; source geometry is unchanged.",
                "2026 active population screened against 2021-2025 history, not evidence of current common management.",
                "No ownership, willingness, access, cost or hydraulic feasibility is assessed."]}
        write_json(OUT / "report.json", summary)
        check(hashes); check(pins)
        outputs = {p.relative_to(OUT).as_posix(): sha256(p) for p in OUT.rglob("*") if p.is_file() and p.name not in ("complete.json", "run.lock")}
        write_json(OUT / "complete.json", {"outputs": outputs})
        verify()
        status("completed_private_screening", **summary)


def verify():
    manifest = read(OUT / "manifest.json")
    check(manifest["inputs"]); check(manifest["code"])
    for name, digest in read(OUT / "complete.json")["outputs"].items():
        if sha256(OUT / name) != digest:
            raise ValueError("Changed output: " + name)
    register = pd.read_parquet(OUT / "active_register.parquet")
    members = pd.read_parquet(OUT / "members.parquet")
    blocks = gpd.read_parquet(OUT / "blocks.parquet")
    if not register.internal_parcel_id.is_unique or not register.cadastre_code.is_unique or register.screening_state.eq("pending").any():
        raise ValueError("Incomplete or duplicate register")
    if register.household.astype(bool).any() or register.road_excluded_release.astype(bool).any() or not register.activity_class.eq("active").all() or not register.activity_state.eq("ready").all():
        raise ValueError("Inactive, road or household entered population")
    candidates = register[register.screening_state.eq("candidate")]
    if candidates.household_agriculture.any() or candidates.road_excluded.any() or candidates.mask_conflict.any():
        raise ValueError("Candidate contains excluded or conflicting parcel")
    if not members.internal_parcel_id.is_unique or not set(members.internal_parcel_id) <= set(register.internal_parcel_id):
        raise ValueError("Overlapping group membership")
    if len(members) != int(register.screening_state.eq("candidate").sum()):
        raise ValueError("Candidate register mismatch")
    if blocks.crs.to_epsg() != 32638 or not blocks.is_valid.all() or blocks.is_empty.any():
        raise ValueError("Invalid analytical block geometry")
    policy = Policy(**manifest["config"]["policy"])
    basis = gpd.read_parquet(local_path(ROOT, manifest["config"]["basis"])).set_index("internal_parcel_id")
    pairs = pd.read_parquet(OUT / "pair_checks.parquet")
    masks = {(r.a, r.b): r.support_year_mask for r in pairs.itertuples()} if len(pairs) else {}
    topology = pd.read_parquet(OUT / "topology.parquet")
    id_order = register.internal_parcel_id.tolist()
    adjacency = {tuple(sorted((id_order[int(r.a)], id_order[int(r.b)]))) for r in topology.itertuples() if r.state == "adjacent"}
    from itertools import combinations
    for block in blocks.itertuples():
        m = members[members.block_id.eq(block.block_id)].sort_values("internal_parcel_id")
        ids = m.internal_parcel_id.tolist()
        original = basis.loc[ids]
        if not np.array_equal(original.cadastre_code.to_numpy(), m.cadastre_code) or not np.array_equal(original.area_official_m2.to_numpy(), m.area_official_m2):
            raise ValueError("Changed cadastral attributes")
        if len(m) < 2 or len(m) != block.parcel_count or m.area_official_m2.sum() / 10000 < policy.minimum_area_ha or not np.isclose(m.area_official_m2.sum() / 10000, block.area_ha):
            raise ValueError("Area/count threshold failed")
        mask = 31
        for a, b in combinations(ids, 2):
            mask &= masks.get((a, b), 0)
        if mask.bit_count() < policy.minimum_years or mask != block.support_year_mask:
            raise ValueError("Chained phenology or inconsistent support years")
        reached = {ids[0]}
        while True:
            extended = reached | {b for b in ids if any(tuple(sorted((a, b))) in adjacency for a in reached)}
            if extended == reached:
                break
            reached = extended
        if reached != set(ids):
            raise ValueError("Disconnected block")
        expected = union_geometry(original.to_crs(32638).geometry.values)
        if not expected.equals(block.geometry):
            raise ValueError("Analytical geometry differs from original parcel union")
    if len(blocks):
        intersections = shapely.STRtree(blocks.geometry.values).query(blocks.geometry.values, predicate="intersects")
        for i, j in zip(*intersections):
            if i < j and not negligible_overlap(blocks.geometry.iloc[i], blocks.geometry.iloc[j], policy):
                raise ValueError("Candidate blocks overlap")
    status("verification_passed", active_parcels=len(register), blocks=len(blocks), candidate_parcels=len(members))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["run", "verify"], default="run", nargs="?")
    args = parser.parse_args()
    execute() if args.command == "run" else verify()
