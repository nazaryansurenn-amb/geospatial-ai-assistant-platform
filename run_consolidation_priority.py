"""Versioned <=1 ha / >=3 ha consolidation screen with a narrow-strip filter."""
from __future__ import annotations
import argparse
from dataclasses import asdict
from itertools import combinations
import json
from pathlib import Path
import shutil
import time

import run_land_consolidation as base
import run_land_consolidation_10m as ten
import run_consolidation_joint_eo as joint
import geopandas as gpd
import numpy as np
import pandas as pd

from release_tools import local_path, sha256
from run_observation_analysis import now, run_lock, write_json, write_table
from wp_core.land_consolidation import block_id, connected_complete_link, negligible_overlap, shared_pixels, union_geometry
from wp_core.land_consolidation_10m import Policy
from wp_core.consolidation_joint_eo import compare_joint_season, eligible_mask
from wp_core.consolidation_priority import StripPolicy, group_priority, qualifies_group, shape_metrics

ROOT = Path(__file__).resolve().parent
CONFIG = "config/consolidation_strip_priority_20260906_v1.json"
OUT = ROOT / "data/analysis/land_consolidation/consolidation_strip_priority_20260906_v1"


def execute():
    config = base.read(ROOT / CONFIG)
    source = local_path(ROOT, config["source"])
    if source != joint.OUT or sha256(source / "complete.json") != config["source_complete_sha256"]:
        raise ValueError("Unexpected previous joint screen")
    if config["maximum_member_area_m2"] != 10000. or config["minimum_group_area_ha"] != 3. or config["publication"] != "internal_draft_only":
        raise ValueError("Unapproved size or publication rule")
    joint.verify()
    previous = base.read(source / "manifest.json")
    policy = Policy(**{**previous["policy"], "minimum_area_ha": config["minimum_group_area_ha"]})
    shape_policy = StripPolicy(**config["strip_policy"])
    OUT.mkdir(parents=True, exist_ok=True)
    with run_lock(OUT / "run.lock"):
        if (OUT / "complete.json").exists():
            verify(); return
        if (OUT / "manifest.json").exists():
            raise ValueError("Unsealed attempt exists; do not overwrite")
        started = time.perf_counter()
        original = pd.read_parquet(base.OUT / "active_register.parquet")
        selected = eligible_mask(original, config["maximum_member_area_m2"])
        register = original.loc[selected].sort_values("internal_parcel_id").copy()
        register["baseline_row"] = register.index
        register = register.reset_index(drop=True)
        if len(register) != config["expected_eligible_parcels"]:
            raise ValueError("Unexpected population")
        ids = register.internal_parcel_id.tolist()
        remap = dict(zip(register.baseline_row, register.index))
        topology = pd.read_parquet(base.OUT / "topology.parquet")
        kept = topology.state.eq("adjacent") & topology.a.isin(remap) & topology.b.isin(remap)
        edges = [(remap[int(r.a)], remap[int(r.b)], float(r.shared_boundary_m)) for r in topology.loc[kept].itertuples()]
        participants = sorted({i for a, b, _ in edges for i in (a, b)})
        index = {i: j for j, i in enumerate(participants)}
        inputs = dict(previous["inputs"])
        for name in ("complete.json", "manifest.json", "pair_checks.parquet", "groups.parquet"):
            p = source / name
            inputs[p.relative_to(ROOT).as_posix()] = sha256(p)
        code = {**previous["code"], **{p: sha256(ROOT / p) for p in (CONFIG, "run_consolidation_priority.py", "wp_core/consolidation_priority.py")}}
        manifest = {"created_at": now(), "config": config, "policy": asdict(policy), "source_config": previous["source_config"],
                    "inputs": inputs, "code": code, "accepted": False, "training_eligible": False,
                    "network_requests": 0, "raster_reads": 0}
        write_json(OUT / "manifest.json", manifest)
        for relative in code:
            dst = OUT / "source" / relative
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(ROOT / relative, dst)
        basis = gpd.read_parquet(local_path(ROOT, previous["source_config"]["basis"])).set_index("internal_parcel_id")
        metric = basis.loc[original.internal_parcel_id].to_crs(32638)
        if metric.crs.to_epsg() != 32638:
            raise ValueError("Shape metrics require the local metric CRS")
        shape = pd.DataFrame([{"internal_parcel_id": pid, **shape_metrics(g, shape_policy)} for pid, g in zip(metric.index, metric.geometry)])
        register = register.merge(shape, on="internal_parcel_id", how="left", validate="1:1", sort=False)
        scope = original.merge(shape, on="internal_parcel_id", how="left", validate="1:1", sort=False)
        scope["within_member_size_limit"] = scope.area_official_m2.gt(0) & scope.area_official_m2.le(config["maximum_member_area_m2"])
        scope["priority_screen_eligible"] = selected.to_numpy()
        register["priority_screening_state"] = "no_eligible_neighbor"
        register.loc[participants, "priority_screening_state"] = "no_qualifying_group"
        cached = pd.read_parquet(source / "pair_checks.parquet")
        cache = {(r["a"], r["b"]): r for r in cached.to_dict("records")}
        used, series, pixels, observation_counts, computed = {}, None, None, {}, 0
        base.status("starting_strip_priority", eligible_parcels=len(ids), strip_parcels=int(register.long_narrow.sum()), adjacent_pairs=len(edges))
        def compare(a, b):
            nonlocal series, pixels, computed, observation_counts
            a, b = sorted((a, b))
            key = (ids[a], ids[b])
            if key not in cache:
                if series is None:
                    selected_ids = [ids[i] for i in participants]
                    series, observation_counts = ten.load_series(previous["source_config"], selected_ids, policy)
                    pixels = base.load_pixels(previous["source_config"], selected_ids)
                i, j = index[a], index[b]
                shared = {r: shared_pixels(pixels[r][i], pixels[r][j]) for r in (10, 20)}
                details, mask = [], 0
                for bit, year in enumerate(previous["source_config"]["years"]):
                    days, values, support = series[year]
                    d = compare_joint_season(days, values[i], values[j], support[i], support[j], year, policy)
                    details.append(d)
                    if d["similar"]:
                        mask |= 1 << bit
                cache[key] = {"a": ids[a], "b": ids[b], "support_year_mask": mask,
                    "shared_10m_fraction": shared[10], "shared_20m_fraction": shared[20],
                    "year_results": json.dumps(details, allow_nan=False),
                    "state": "joint_profile_match" if mask.bit_count() >= policy.minimum_years else "not_repeatedly_similar"}
                computed += 1
                if computed % 3000 == 0:
                    base.status("new_priority_pair_checks", computed=computed, elapsed_seconds=round(time.perf_counter() - started, 1))
            used[key] = cache[key]
            return int(cache[key]["support_year_mask"])
        groups = connected_complete_link(ids, edges, compare, policy)
        for a, b, _ in edges:
            compare(a, b)
        group_rows, block_rows, member_rows = [], [], []
        for members, mask in groups:
            if len(members) < 2:
                continue
            rows = register.iloc[members]
            group_ids = rows.internal_parcel_id.tolist()
            area = float(rows.area_official_m2.sum()) / 10000
            candidate = qualifies_group(rows.area_official_m2, config["maximum_member_area_m2"], config["minimum_group_area_ha"])
            state = "joint_eo_candidate_review" if candidate else "joint_group_below_3ha"
            register.loc[members, "priority_screening_state"] = state
            bid = "strip_" + block_id(group_ids)
            row = {"block_id": bid, "parcel_count": len(rows), "area_ha": area, "support_year_mask": mask,
                "support_years": ",".join(str(y) for bit, y in enumerate(range(2021, 2026)) if mask & (1 << bit)),
                "state": state, "member_ids": json.dumps(group_ids), "accepted": False,
                **group_priority(rows.area_official_m2, rows.long_narrow, shape_policy)}
            group_rows.append(row)
            if not candidate:
                continue
            pair_rows = [used[a, b] for a, b in combinations(group_ids, 2)]
            conflicts = any(v["state"] == "different_mixed_context" for p in pair_rows for d in json.loads(p["year_results"])
                if mask & (1 << (d["year"] - 2021)) for v in d["context_20m"].values())
            block_rows.append({**row, "stage": ",".join(sorted(set(rows.stage))),
                "evidence_mode": config["evidence_mode"], "maximum_shared_10m_fraction": max(p["shared_10m_fraction"] for p in pair_rows),
                "context_20m_differs": conflicts, "geometry": union_geometry(metric.loc[group_ids].geometry.values)})
            for i in members:
                r = register.iloc[i]
                member_rows.append({"block_id": bid, "internal_parcel_id": ids[i], "cadastre_code": r.cadastre_code,
                    "area_official_m2": float(r.area_official_m2), "stage": r.stage, "long_narrow": bool(r.long_narrow),
                    "strip_length_m": float(r.strip_length_m), "strip_width_m": float(r.strip_width_m), "strip_elongation": float(r.strip_elongation)})
        group_cols = ["block_id", "parcel_count", "area_ha", "support_year_mask", "support_years", "state", "member_ids", "accepted",
            "strip_parcel_count", "strip_area_ha", "strip_area_fraction", "has_strip_members", "all_members_strips", "strip_priority"]
        block_cols = group_cols + ["stage", "evidence_mode", "maximum_shared_10m_fraction", "context_20m_differs", "geometry"]
        blocks = gpd.GeoDataFrame(block_rows, columns=block_cols, geometry="geometry", crs=32638)
        priority = blocks[blocks.strip_priority.astype(bool)].copy()
        members = pd.DataFrame(member_rows, columns=["block_id", "internal_parcel_id", "cadastre_code", "area_official_m2", "stage", "long_narrow", "strip_length_m", "strip_width_m", "strip_elongation"])
        group_frame = pd.DataFrame(group_rows, columns=group_cols)
        for name, frame in (("register", register), ("scope", scope), ("groups", group_frame), ("blocks", blocks),
            ("priority_blocks", priority), ("members", members), ("pair_checks", pd.DataFrame(used.values())),
            ("adjacency", pd.DataFrame([(ids[a], ids[b], length) for a, b, length in edges], columns=["a", "b", "shared_boundary_m"]))):
            write_table(OUT / f"{name}.parquet", frame)
        for name, frame in (("blocks", blocks), ("priority_blocks", priority)):
            write_json(OUT / f"{name}.geojson", json.loads(frame.drop(columns="member_ids").to_crs(4326).to_json(drop_id=True)))
        example = scope[scope.cadastre_code.eq("04-087-0119-0005")].iloc[0]
        report = {"version": config["version"], "created_at": now(), "active_scope": len(scope), "eligible_parcels": len(register),
            "eligible_strip_parcels": int(register.long_narrow.sum()), "neighboring_parcels": len(participants),
            "adjacent_pairs": len(edges), "all_pair_checks": len(used), "new_pair_calculations": computed,
            "candidate_blocks": len(blocks), "candidate_parcels": len(members), "candidate_area_ha": float(blocks.area_ha.sum()),
            "priority_blocks": len(priority), "priority_parcels": int(priority.parcel_count.sum()), "priority_area_ha": float(priority.area_ha.sum()),
            "priority_strip_parcels": int(priority.strip_parcel_count.sum()), "all_strip_blocks": int(blocks.all_members_strips.sum()),
            "blocks_with_any_strip": int(blocks.has_strip_members.sum()), "largest_candidate_area_ha": float(blocks.area_ha.max()) if len(blocks) else 0.,
            "register_states": register.priority_screening_state.value_counts().to_dict(), "observation_counts": observation_counts,
            "example": {"cadastre_code": example.cadastre_code, "area_ha": float(example.area_official_m2) / 10000,
                "within_member_size_limit": bool(example.within_member_size_limit), "long_narrow": bool(example.long_narrow),
                "screen_eligible": bool(example.priority_screen_eligible), "remaining_review_state": example.screening_state,
                "estimated_length_m": float(example.strip_length_m), "estimated_width_m": float(example.strip_width_m)},
            "accepted": False, "training_eligible": False, "publication": "internal_draft_only", "network_requests": 0, "raster_reads": 0,
            "elapsed_seconds": round(time.perf_counter() - started, 2),
            "limitations": ["Shape priority is a configurable screening choice, not a crop, ownership or feasibility claim.",
                "Shape dimensions are analytical oriented-envelope estimates, not cadastral survey measurements.",
                "Shared 10 m pixels are joint EO evidence, never independent parcel confirmation.",
                "Existing mapped-barrier review remains; the selected example is still held by its road intersection.",
                "Greedy disjoint grouping can change membership as scope expands and is not a globally optimal allocation."]}
        write_json(OUT / "report.json", report)
        base.check(inputs); base.check(code); joint.verify()
        write_json(OUT / "complete.json", {"outputs": {p.relative_to(OUT).as_posix(): sha256(p) for p in OUT.rglob("*") if p.is_file() and p.name not in ("complete.json", "run.lock")}})
        verify()
        print(json.dumps(report, indent=2))


def verify():
    manifest = base.read(OUT / "manifest.json")
    base.check(manifest["inputs"]); base.check(manifest["code"]); joint.verify()
    for name, digest in base.read(OUT / "complete.json")["outputs"].items():
        if sha256(OUT / name) != digest:
            raise ValueError("Changed priority output")
    config = manifest["config"]
    shape_policy = StripPolicy(**config["strip_policy"])
    original = pd.read_parquet(base.OUT / "active_register.parquet")
    scope = pd.read_parquet(OUT / "scope.parquet")
    pd.testing.assert_frame_equal(original, scope[original.columns])
    register = pd.read_parquet(OUT / "register.parquet")
    expected = original[eligible_mask(original, config["maximum_member_area_m2"])].sort_values("internal_parcel_id").reset_index(drop=True)
    pd.testing.assert_frame_equal(expected, register[original.columns])
    if not register.internal_parcel_id.is_unique or not register.cadastre_code.is_unique:
        raise ValueError("Nonunique cadastral identity")
    groups = pd.read_parquet(OUT / "groups.parquet")
    members = pd.read_parquet(OUT / "members.parquet")
    blocks = gpd.read_parquet(OUT / "blocks.parquet")
    priority = gpd.read_parquet(OUT / "priority_blocks.parquet")
    pd.testing.assert_frame_equal(blocks[blocks.strip_priority.astype(bool)].reset_index(drop=True), priority.reset_index(drop=True))
    if blocks.crs.to_epsg() != 32638 or not blocks.is_valid.all() or blocks.is_empty.any() or blocks.accepted.any() or not members.internal_parcel_id.is_unique:
        raise ValueError("Invalid candidate geometry, publication or membership")
    pairs = pd.read_parquet(OUT / "pair_checks.parquet")
    if pairs.duplicated(["a", "b"]).any():
        raise ValueError("Duplicate pair")
    masks = {(r.a, r.b): int(r.support_year_mask) for r in pairs.itertuples()}
    adjacency = {tuple(sorted((r.a, r.b))) for r in pd.read_parquet(OUT / "adjacency.parquet").itertuples()}
    index, seen = register.set_index("internal_parcel_id"), set()
    for group in groups.itertuples():
        ids = json.loads(group.member_ids)
        if len(ids) != len(set(ids)) or len(ids) < 2 or len(ids) != group.parcel_count or seen & set(ids):
            raise ValueError("Invalid group members")
        seen.update(ids)
        rows = index.loc[ids]
        if not np.isclose(rows.area_official_m2.sum() / 10000, group.area_ha):
            raise ValueError("Incorrect official area total")
        for name, value in group_priority(rows.area_official_m2, rows.long_narrow, shape_policy).items():
            if getattr(group, name) != value:
                raise ValueError("Incorrect priority aggregation")
        mask = 31
        for pair in combinations(sorted(ids), 2):
            mask &= masks.get(pair, 0)
        if mask != group.support_year_mask or mask.bit_count() < manifest["policy"]["minimum_years"]:
            raise ValueError("Chained or inconsistent-year group")
        reached = {ids[0]}
        while True:
            new = reached | {b for b in ids if any(tuple(sorted((a, b))) in adjacency for a in reached)}
            if new == reached:
                break
            reached = new
        if reached != set(ids):
            raise ValueError("Disconnected group")
        if (group.state == "joint_eo_candidate_review") != qualifies_group(rows.area_official_m2, config["maximum_member_area_m2"], config["minimum_group_area_ha"]):
            raise ValueError("Candidate size gate failure")
    candidate_ids = set(register.loc[register.priority_screening_state.eq("joint_eo_candidate_review"), "internal_parcel_id"])
    if candidate_ids != set(members.internal_parcel_id) or set(blocks.block_id) != set(groups.loc[groups.state.eq("joint_eo_candidate_review"), "block_id"]):
        raise ValueError("Candidate output/register mismatch")
    basis = gpd.read_parquet(local_path(ROOT, manifest["source_config"]["basis"])).set_index("internal_parcel_id")
    for block in blocks.itertuples():
        m = members[members.block_id.eq(block.block_id)].sort_values("internal_parcel_id")
        src = basis.loc[m.internal_parcel_id]
        if not qualifies_group(m.area_official_m2, 10000., 3.) or len(m) != block.parcel_count:
            raise ValueError("1 ha member / 3 ha group rule failure")
        if not np.array_equal(src.cadastre_code.to_numpy(), m.cadastre_code) or not np.array_equal(src.area_official_m2.to_numpy(), m.area_official_m2):
            raise ValueError("Changed cadastral attributes")
        if not union_geometry(src.to_crs(32638).geometry.values).equals(block.geometry):
            raise ValueError("Changed cadastral union")
    policy = Policy(**manifest["policy"])
    for i, a in enumerate(blocks.geometry):
        for b in blocks.geometry.iloc[i + 1:]:
            if a.intersects(b) and not negligible_overlap(a, b, policy):
                raise ValueError("Overlapping candidate groups")
    base.status("verified_strip_priority", eligible_parcels=len(register), candidates=len(blocks), priority_blocks=len(priority), members=len(members))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["run", "verify"], default="run", nargs="?")
    args = parser.parse_args()
    execute() if args.command == "run" else verify()
