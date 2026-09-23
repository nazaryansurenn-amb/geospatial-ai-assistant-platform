"""Offline second consolidation screen. Writes only a separate private version."""
from __future__ import annotations
import argparse
from dataclasses import asdict
from itertools import combinations
import json
from pathlib import Path
import shutil
import time

import run_land_consolidation as baseline
import geopandas as gpd
import numpy as np
import pandas as pd
import shapely

from release_tools import local_path, sha256
from run_observation_analysis import now, run_lock, write_json, write_table
from wp_core.land_consolidation import INDICES, block_id, connected_complete_link, negligible_overlap, shared_pixels, union_geometry
from wp_core.land_consolidation_10m import Policy, compare_season, mask_observations, target_mask
from wp_core.observation_screening import Policy as ScreeningPolicy, isolated_excursions

ROOT = Path(__file__).resolve().parent
CONFIG = "config/consolidation_10m_20260906_v1.json"
OUT = ROOT / "data/analysis/land_consolidation/consolidation_10m_20260906_v1"
read, status = baseline.read, baseline.status


def setup():
    config = read(ROOT / CONFIG)
    base = local_path(ROOT, config["baseline"])
    if base != baseline.OUT or sha256(base / "complete.json") != config["baseline_complete_sha256"]:
        raise ValueError("Unexpected first-screen version")
    baseline.verify()
    original = read(base / "manifest.json")
    policy = Policy(**{**original["config"]["policy"], **config["policy"]})
    if config["years"] != original["config"]["years"] or config["years"] != list(range(2021, 2026)):
        raise ValueError("Only completed 2021-2025 seasons are allowed")
    if config["publication"] != "internal_draft_only":
        raise ValueError("Publication is not authorized")
    return config, base, original, policy


def population(base, config, policy):
    register = pd.read_parquet(base / "active_register.parquet")
    selected = target_mask(register, policy)
    target = register.loc[selected].copy()
    target["baseline_row"] = target.index
    target = target.sort_values("internal_parcel_id").reset_index(drop=True)
    if len(target) != config["expected_target_count"] or not target.internal_parcel_id.is_unique or not target.cadastre_code.is_unique:
        raise ValueError("Changed or duplicate target population")
    old_members = pd.read_parquet(base / "members.parquet")
    if set(target.internal_parcel_id) & set(old_members.internal_parcel_id):
        raise ValueError("First-screen members entered the separate screen")
    topology = pd.read_parquet(base / "topology.parquet")
    positions = dict(zip(target.baseline_row, target.index))
    adjacent = topology.state.eq("adjacent") & topology.a.isin(positions) & topology.b.isin(positions)
    edges = [(positions[int(r.a)], positions[int(r.b)], float(r.shared_boundary_m)) for r in topology.loc[adjacent].itertuples()]
    target["baseline_screening_state"] = target.screening_state
    target["screening_state"] = "no_target_neighbor"
    participants = sorted({i for a, b, _ in edges for i in (a, b)})
    target.loc[participants, "screening_state"] = "no_qualifying_group"
    return target, edges, participants


def load_series(source_config, ids, policy):
    eo = local_path(ROOT, source_config["eo"])
    columns = ["internal_parcel_id", "observation_date"]
    columns += [k + "_median" for k in INDICES] + [k + "_valid_fraction" for k in INDICES]
    result, counts = {}, {}
    for year in source_config["years"]:
        frame = pd.read_parquet(eo / "daily" / f"{year}.parquet", columns=columns,
                                filters=[("internal_parcel_id", "in", list(ids))])
        if frame.duplicated(["internal_parcel_id", "observation_date"]).any():
            raise ValueError("Duplicate parcel/date")
        dates = pd.to_datetime(frame.observation_date)
        if dates.isna().any() or not dates.dt.year.eq(year).all():
            raise ValueError("Missing or incorrect observation date")
        days = np.sort(dates.dt.dayofyear.unique())
        pi = pd.Index(ids).get_indexer(frame.internal_parcel_id)
        di = pd.Index(days).get_indexer(dates.dt.dayofyear)
        if (pi < 0).any():
            raise ValueError("Unexpected parcel in EO input")
        raw = frame[[k + "_median" for k in INDICES]].to_numpy(np.float32)
        fractions = frame[[k + "_valid_fraction" for k in INDICES]].to_numpy(np.float32)
        values = np.full((len(ids), len(days), 4), np.nan, dtype=np.float32)
        support = np.full((len(ids), len(days)), np.nan, dtype=np.float32)
        values[pi, di] = mask_observations(raw, fractions, policy)
        support[pi, di] = np.min(fractions[:, :2], axis=1)
        excursions = 0
        for i in range(len(ids)):
            a = values[i]
            good = np.isfinite(a[:, :2]).all(axis=1)
            flagged = isolated_excursions(days, a[:, 0], a[:, 1], good, ScreeningPolicy())
            excursions += int(np.sum(flagged))
            a[flagged, :2] = np.nan
        result[year] = (days, values, support)
        counts[year] = {"rows": len(frame), "parcels_with_rows": int(frame.internal_parcel_id.nunique()),
                        "distinct_dates": len(days), "isolated_excursions": excursions,
                        "primary_valid_rows": int(np.isfinite(values[:, :, :2]).all(axis=2).sum())}
        status("loaded_cached_10m_profiles", year=year, **counts[year])
    return result, counts


def qualifies(count, area_ha, year_mask, policy):
    return count >= 2 and area_ha >= policy.minimum_area_ha and int(year_mask).bit_count() >= policy.minimum_years


def execute():
    config, base, original, policy = setup()
    OUT.mkdir(parents=True, exist_ok=True)
    with run_lock(OUT / "run.lock"):
        if (OUT / "complete.json").exists():
            verify(); return
        if (OUT / "manifest.json").exists():
            raise ValueError("Unsealed attempt exists; preserve and inspect before any retry")
        started = time.perf_counter()
        register, edges, participants = population(base, config, policy)
        ids = register.internal_parcel_id.tolist()
        selected_ids = [ids[i] for i in participants]
        code = [CONFIG, "run_land_consolidation_10m.py", "wp_core/land_consolidation_10m.py"]
        pins = {**original["code"], **{p: sha256(ROOT / p) for p in code}}
        hashes = {**original["inputs"]}
        for name in ("complete.json", "manifest.json", "active_register.parquet", "topology.parquet", "members.parquet"):
            path = base / name
            hashes[path.relative_to(ROOT).as_posix()] = sha256(path)
        manifest = {"created_at": now(), "config": config, "resolved_policy": asdict(policy),
                    "source_config": original["config"], "inputs": hashes, "code": pins,
                    "accepted": False, "training_eligible": False, "publication": "internal_draft_only",
                    "network_requests": 0, "raster_reads": 0}
        write_json(OUT / "manifest.json", manifest)
        for relative in pins:
            destination = OUT / "source" / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(ROOT / relative, destination)
        status("starting_separate_10m_screen", target_parcels=len(ids), participants=len(participants), adjacent_pairs=len(edges))
        series, observation_counts = load_series(original["config"], selected_ids, policy) if participants else ({}, {})
        pixels = baseline.load_pixels(original["config"], selected_ids) if participants else {}
        index = {i: j for j, i in enumerate(participants)}
        cache, diagnostics = {}, []
        def compare(a, b):
            a, b = sorted((a, b))
            if (a, b) in cache:
                return cache[a, b]
            i, j = index[a], index[b]
            shared = {r: shared_pixels(pixels[r][i], pixels[r][j]) for r in (10, 20)}
            details, mask = [], 0
            if shared[10] <= policy.maximum_shared_pixel_fraction:
                for bit, year in enumerate(config["years"]):
                    days, values, support = series[year]
                    d = compare_season(days, values[i], values[j], support[i], support[j], shared[10], year, policy)
                    details.append(d)
                    if d["similar"]:
                        mask |= 1 << bit
            reason = "shared_10m_pixel_review" if not details else ("similar" if mask.bit_count() >= policy.minimum_years else "not_repeatedly_similar")
            diagnostics.append({"a": ids[a], "b": ids[b], "support_year_mask": mask,
                "shared_10m_fraction": shared[10], "shared_20m_fraction": shared[20],
                "year_results": json.dumps(details, allow_nan=False), "reason": reason})
            cache[a, b] = mask
            return mask
        groups = connected_complete_link(ids, edges, compare, policy)
        # Materialize every adjacency decision, even if the greedy partition skipped it.
        for a, b, _ in edges:
            compare(a, b)
        status("pair_comparisons_finished", checks=len(diagnostics))
        basis = gpd.read_parquet(local_path(ROOT, original["config"]["basis"])).set_index("internal_parcel_id")
        metric = basis.loc[ids].to_crs(32638)
        block_rows, member_rows, group_rows = [], [], []
        diag = {(r["a"], r["b"]): r for r in diagnostics}
        for members, mask in groups:
            if len(members) < 2:
                continue
            group_ids = [ids[i] for i in members]
            bid = "tenm_" + block_id(group_ids)
            area = float(register.iloc[members].area_official_m2.sum()) / 10000
            candidate = qualifies(len(members), area, mask, policy)
            state = "candidate_review" if candidate else "similar_group_below_5ha"
            register.loc[members, "screening_state"] = state
            context_differs = any(v["state"] == "different_mixed_context"
                for a, b in combinations(group_ids, 2)
                for d in json.loads(diag[a, b]["year_results"])
                if mask & (1 << (d["year"] - 2021))
                for v in d["context_20m"].values())
            group_rows.append({"block_id": bid, "parcel_count": len(members), "area_ha": area,
                               "support_year_mask": mask, "state": state, "context_20m_differs": context_differs,
                               "member_ids": json.dumps(group_ids)})
            if not candidate:
                continue
            years = [y for bit, y in enumerate(config["years"]) if mask & (1 << bit)]
            block_rows.append({"block_id": bid, "parcel_count": len(members), "area_ha": area,
                "stage": ",".join(sorted(set(register.iloc[members].stage))),
                "support_years": ",".join(map(str, years)), "support_year_mask": mask,
                "context_20m_differs": context_differs, "state": state, "accepted": False,
                "geometry": union_geometry(metric.loc[group_ids].geometry.values)})
            for i in members:
                row = register.iloc[i]
                member_rows.append({"block_id": bid, "internal_parcel_id": ids[i], "cadastre_code": row.cadastre_code,
                    "area_official_m2": float(row.area_official_m2), "stage": row.stage})
        block_cols = ["block_id", "parcel_count", "area_ha", "stage", "support_years", "support_year_mask", "context_20m_differs", "state", "accepted", "geometry"]
        blocks = gpd.GeoDataFrame(block_rows, columns=block_cols, geometry="geometry", crs=32638)
        members = pd.DataFrame(member_rows, columns=["block_id", "internal_parcel_id", "cadastre_code", "area_official_m2", "stage"])
        group_frame = pd.DataFrame(group_rows, columns=["block_id", "parcel_count", "area_ha", "support_year_mask", "state", "context_20m_differs", "member_ids"])
        pairs = pd.DataFrame(diagnostics, columns=["a", "b", "support_year_mask", "shared_10m_fraction", "shared_20m_fraction", "year_results", "reason"])
        topology = pd.DataFrame([(ids[a], ids[b], length) for a, b, length in edges], columns=["a", "b", "shared_boundary_m"])
        for name, frame in (("register", register), ("members", members), ("groups", group_frame), ("pair_checks", pairs), ("adjacency", topology), ("blocks", blocks)):
            write_table(OUT / f"{name}.parquet", frame)
        write_json(OUT / "blocks.geojson", json.loads(blocks.to_crs(4326).to_json(drop_id=True)))
        details = [d for row in diagnostics for d in json.loads(row["year_results"])]
        summary = {"version": config["version"], "created_at": now(), "target_parcels": len(register),
            "target_stages": register.stage.value_counts().to_dict(), "neighboring_parcels": len(participants),
            "adjacent_pairs_tested": len(edges), "all_pair_checks": len(pairs),
            "pair_states": pairs.reason.value_counts().to_dict(),
            "pair_year_states": pd.Series([d["reason"] for d in details], dtype=str).value_counts().to_dict(),
            "register_states": register.screening_state.value_counts().to_dict(),
            "candidate_blocks": len(blocks), "candidate_parcels": len(members),
            "candidate_area_ha": float(blocks.area_ha.sum()), "similar_groups": len(group_frame),
            "largest_similar_group_ha": float(group_frame.area_ha.max()) if len(group_frame) else 0.,
            "observation_counts": observation_counts, "candidate_context_conflicts": int(blocks.context_20m_differs.sum()),
            "baseline_blocks_preserved": read(base / "report.json")["candidate_blocks"],
            "accepted": False, "training_eligible": False, "publication": "internal_draft_only",
            "network_requests": 0, "raster_reads": 0, "elapsed_seconds": round(time.perf_counter() - started, 2),
            "limitations": ["10 m review candidates are weaker evidence than the first four-index screen.",
                "NDVI and EVI2 share red/NIR bands; they are not independent confirmations.",
                "No fully contained 20 m pixel: NDMI/BSI are mixed context only, with differences retained.",
                "Only the 4875 additional parcels are grouped; original blocks and other populations are not joined.",
                "Shared-pixel checks bound contamination but cannot remove sensor point-spread or geolocation effects.",
                "March-November window and uncalibrated thresholds; no independent accuracy estimate.",
                "Cached boundaries exclude mapped roads/canal and substantive overlaps, not every unmapped barrier.",
                "Greedy groups are disjoint and deterministic, not an optimal land allocation.",
                "No ownership, common management, willingness, cost or hydraulic feasibility is established."]}
        write_json(OUT / "report.json", summary)
        baseline.check(hashes); baseline.check(pins); baseline.verify()
        outputs = {p.relative_to(OUT).as_posix(): sha256(p) for p in OUT.rglob("*") if p.is_file() and p.name not in ("complete.json", "run.lock")}
        write_json(OUT / "complete.json", {"outputs": outputs})
        verify()
        status("completed_private_10m_screen", **summary)


def verify():
    config, base, original, policy = setup()
    manifest = read(OUT / "manifest.json")
    baseline.check(manifest["inputs"]); baseline.check(manifest["code"])
    for relative, digest in read(OUT / "complete.json")["outputs"].items():
        if sha256(OUT / relative) != digest:
            raise ValueError("Changed second-screen output: " + relative)
    expected, edges, _ = population(base, config, policy)
    register = pd.read_parquet(OUT / "register.parquet")
    pd.testing.assert_frame_equal(expected.drop(columns="screening_state"), register.drop(columns="screening_state"))
    members = pd.read_parquet(OUT / "members.parquet")
    blocks = gpd.read_parquet(OUT / "blocks.parquet")
    if not members.internal_parcel_id.is_unique or set(members.internal_parcel_id) != set(register.loc[register.screening_state.eq("candidate_review"), "internal_parcel_id"]):
        raise ValueError("Candidate membership mismatch")
    if blocks.crs.to_epsg() != 32638 or not blocks.is_valid.all() or blocks.is_empty.any():
        raise ValueError("Invalid analytical geometry")
    if register.accepted.any() or blocks.accepted.any():
        raise ValueError("Unapproved publication state")
    basis = gpd.read_parquet(local_path(ROOT, original["config"]["basis"])).set_index("internal_parcel_id")
    pairs = pd.read_parquet(OUT / "pair_checks.parquet")
    if pairs.duplicated(["a", "b"]).any():
        raise ValueError("Duplicate pair checks")
    masks = {(r.a, r.b): r.support_year_mask for r in pairs.itertuples()}
    ids = register.internal_parcel_id.tolist()
    adjacency = {tuple(sorted((ids[a], ids[b]))) for a, b, _ in edges}
    for block in blocks.itertuples():
        m = members[members.block_id.eq(block.block_id)].sort_values("internal_parcel_id")
        group_ids = m.internal_parcel_id.tolist()
        source = basis.loc[group_ids]
        if not np.array_equal(source.cadastre_code.to_numpy(), m.cadastre_code) or not np.array_equal(source.area_official_m2.to_numpy(), m.area_official_m2):
            raise ValueError("Changed cadastral code/area")
        mask = 31
        for pair in combinations(group_ids, 2):
            mask &= masks.get(pair, 0)
        if mask != block.support_year_mask or not qualifies(len(m), float(m.area_official_m2.sum()) / 10000, mask, policy):
            raise ValueError("Group does not meet area or same-year all-pair criteria")
        if len(m) != block.parcel_count or not np.isclose(m.area_official_m2.sum() / 10000, block.area_ha):
            raise ValueError("Group total mismatch")
        reached = {group_ids[0]}
        while True:
            new = reached | {b for b in group_ids if any(tuple(sorted((a, b))) in adjacency for a in reached)}
            if new == reached:
                break
            reached = new
        if reached != set(group_ids):
            raise ValueError("Disconnected group")
        if not union_geometry(source.to_crs(32638).geometry.values).equals(block.geometry):
            raise ValueError("Source geometry changed")
    if len(blocks):
        intersections = shapely.STRtree(blocks.geometry.values).query(blocks.geometry.values, predicate="intersects")
        for i, j in zip(*intersections):
            if i < j and not negligible_overlap(blocks.geometry.iloc[i], blocks.geometry.iloc[j], policy):
                raise ValueError("Groups overlap")
    status("verified_separate_10m_screen", target_parcels=len(register), candidate_blocks=len(blocks), candidate_parcels=len(members))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["run", "verify"], nargs="?", default="run")
    args = parser.parse_args()
    execute() if args.command == "run" else verify()
