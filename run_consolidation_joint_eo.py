"""Offline unified small-parcel screen; shared pixels are joint evidence, not vetoes."""
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
import geopandas as gpd
import numpy as np
import pandas as pd

from release_tools import local_path, sha256
from run_observation_analysis import now, run_lock, write_json, write_table
from wp_core.land_consolidation import block_id, connected_complete_link, negligible_overlap, shared_pixels, union_geometry
from wp_core.land_consolidation_10m import Policy
from wp_core.consolidation_joint_eo import compare_joint_season, eligible_mask, sampling_footprint

ROOT = Path(__file__).resolve().parent
CONFIG = "config/consolidation_joint_eo_20260906_v1.json"
OUT = ROOT / "data/analysis/land_consolidation/consolidation_joint_eo_20260906_v1"


def execute():
    config = base.read(ROOT / CONFIG)
    if sha256(base.OUT / "complete.json") != config["baseline_complete_sha256"]:
        raise ValueError("First-screen source changed")
    if config["publication"] != "internal_draft_only" or config["maximum_member_area_m2"] != 5000. or config["minimum_group_area_ha"] != 5.:
        raise ValueError("Unapproved scope or size rule")
    ten.verify()
    original = base.read(base.OUT / "manifest.json")
    ten_manifest = base.read(ten.OUT / "manifest.json")
    policy = Policy(**{**ten_manifest["resolved_policy"], **config["policy"]})
    OUT.mkdir(parents=True, exist_ok=True)
    with run_lock(OUT / "run.lock"):
        if (OUT / "complete.json").exists():
            verify(); return
        if (OUT / "manifest.json").exists():
            raise ValueError("Unsealed run exists; do not overwrite it")
        started = time.perf_counter()
        source = pd.read_parquet(base.OUT / "active_register.parquet")
        selection = eligible_mask(source, config["maximum_member_area_m2"])
        register = source.loc[selection].sort_values("internal_parcel_id").copy()
        register["baseline_row"] = register.index
        register = register.reset_index(drop=True)
        if len(register) != config["expected_eligible_parcels"]:
            raise ValueError("Unexpected target population")
        ids = register.internal_parcel_id.tolist()
        old_to_new = dict(zip(register.baseline_row, register.index))
        topology = pd.read_parquet(base.OUT / "topology.parquet")
        allowed = topology.state.eq("adjacent") & topology.a.isin(old_to_new) & topology.b.isin(old_to_new)
        edges = [(old_to_new[int(r.a)], old_to_new[int(r.b)], float(r.shared_boundary_m)) for r in topology.loc[allowed].itertuples()]
        participants = sorted({i for a, b, _ in edges for i in (a, b)})
        index = {i: n for n, i in enumerate(participants)}
        selected_ids = [ids[i] for i in participants]
        register["joint_screening_state"] = "no_eligible_neighbor"
        register.loc[participants, "joint_screening_state"] = "no_qualifying_group"
        register["evidence_mode"] = config["evidence_mode"]
        inputs = dict(ten_manifest["inputs"])
        for p in (base.OUT / "complete.json", base.OUT / "manifest.json", base.OUT / "active_register.parquet", base.OUT / "topology.parquet"):
            inputs[p.relative_to(ROOT).as_posix()] = sha256(p)
        code = {**ten_manifest["code"], **{p: sha256(ROOT / p) for p in (CONFIG, "run_consolidation_joint_eo.py", "wp_core/consolidation_joint_eo.py")}}
        manifest = {"created_at": now(), "config": config, "source_config": original["config"], "policy": asdict(policy),
                    "inputs": inputs, "code": code, "accepted": False, "training_eligible": False,
                    "network_requests": 0, "raster_reads": 0}
        write_json(OUT / "manifest.json", manifest)
        for relative in code:
            destination = OUT / "source" / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(ROOT / relative, destination)
        base.status("starting_joint_pixel_screen", eligible_parcels=len(ids), neighboring_parcels=len(participants), adjacent_pairs=len(edges))
        series, observation_counts = ten.load_series(original["config"], selected_ids, policy)
        pixels = base.load_pixels(original["config"], selected_ids)
        cache, diagnostics = {}, []
        def compare(a, b):
            a, b = sorted((a, b))
            if (a, b) in cache:
                return cache[a, b]
            i, j = index[a], index[b]
            shared = {r: shared_pixels(pixels[r][i], pixels[r][j]) for r in (10, 20)}
            mask, details = 0, []
            for bit, year in enumerate(original["config"]["years"]):
                days, values, support = series[year]
                result = compare_joint_season(days, values[i], values[j], support[i], support[j], year, policy)
                details.append(result)
                if result["similar"]:
                    mask |= 1 << bit
            diagnostics.append({"a": ids[a], "b": ids[b], "support_year_mask": mask,
                "shared_10m_fraction": shared[10], "shared_20m_fraction": shared[20],
                "year_results": json.dumps(details, allow_nan=False),
                "state": "joint_profile_match" if mask.bit_count() >= policy.minimum_years else "not_repeatedly_similar"})
            cache[a, b] = mask
            if len(cache) % 2000 == 0:
                base.status("joint_pair_comparisons", completed=len(cache), elapsed_seconds=round(time.perf_counter() - started, 1))
            return mask
        groups = connected_complete_link(ids, edges, compare, policy)
        for a, b, _ in edges:
            compare(a, b)
        lookup = {(d["a"], d["b"]): d for d in diagnostics}
        basis = gpd.read_parquet(local_path(ROOT, original["config"]["basis"])).set_index("internal_parcel_id")
        group_rows, block_rows, member_rows = [], [], []
        for members, mask in groups:
            if len(members) < 2:
                continue
            group_ids = [ids[i] for i in members]
            area = float(register.iloc[members].area_official_m2.sum()) / 10000
            candidate = area >= config["minimum_group_area_ha"]
            state = "joint_eo_candidate_review" if candidate else "joint_group_below_5ha"
            register.loc[members, "joint_screening_state"] = state
            bid = "joint_" + block_id(group_ids)
            row = {"block_id": bid, "parcel_count": len(members), "area_ha": area, "support_year_mask": mask,
                   "state": state, "member_ids": json.dumps(group_ids), "accepted": False}
            group_rows.append(row)
            if not candidate:
                continue
            pair_rows = [lookup[a, b] for a, b in combinations(group_ids, 2)]
            common = [y for bit, y in enumerate(original["config"]["years"]) if mask & (1 << bit)]
            context_differs = any(v["state"] == "different_mixed_context" for pair in pair_rows
                for d in json.loads(pair["year_results"]) if d["year"] in common for v in d["context_20m"].values())
            footprint = sampling_footprint([index[i] for i in members], pixels[10])
            block_rows.append({**row, **footprint, "stage": ",".join(sorted(set(register.iloc[members].stage))),
                "support_years": ",".join(map(str, common)), "context_20m_differs": context_differs,
                "pairs_sharing_10m_pixels": sum(d["shared_10m_fraction"] > 0 for d in pair_rows),
                "maximum_shared_10m_fraction": max(d["shared_10m_fraction"] for d in pair_rows),
                "evidence_mode": config["evidence_mode"],
                "geometry": union_geometry(basis.loc[group_ids].to_crs(32638).geometry.values)})
            for i in members:
                r = register.iloc[i]
                member_rows.append({"block_id": bid, "internal_parcel_id": ids[i], "cadastre_code": r.cadastre_code,
                    "area_official_m2": float(r.area_official_m2), "stage": r.stage})
        group_cols = ["block_id", "parcel_count", "area_ha", "support_year_mask", "state", "member_ids", "accepted"]
        block_cols = group_cols + ["unique_10m_footprint_pixels", "parcel_pixel_memberships", "pixels_shared_by_members", "stage", "support_years", "context_20m_differs", "pairs_sharing_10m_pixels", "maximum_shared_10m_fraction", "evidence_mode", "geometry"]
        blocks = gpd.GeoDataFrame(block_rows, columns=block_cols, geometry="geometry", crs=32638)
        members = pd.DataFrame(member_rows, columns=["block_id", "internal_parcel_id", "cadastre_code", "area_official_m2", "stage"])
        for name, frame in (("register", register), ("groups", pd.DataFrame(group_rows, columns=group_cols)),
            ("blocks", blocks), ("members", members), ("pair_checks", pd.DataFrame(diagnostics)),
            ("adjacency", pd.DataFrame([(ids[a], ids[b], length) for a, b, length in edges], columns=["a", "b", "shared_boundary_m"]))):
            write_table(OUT / f"{name}.parquet", frame)
        write_json(OUT / "blocks.geojson", json.loads(blocks.drop(columns="member_ids").to_crs(4326).to_json(drop_id=True)))
        summary = {"version": config["version"], "created_at": now(), "eligible_parcels": len(register),
            "neighboring_parcels": len(participants), "adjacent_pairs": len(edges), "all_pair_checks": len(diagnostics),
            "below_10m_width": int(register.minimum_width_m.lt(10).sum()),
            "fewer_than_three_pure_10m_pixels": int(register.pure_pixels_10m.lt(3).sum()),
            "register_states": register.joint_screening_state.value_counts().to_dict(),
            "candidate_blocks": len(blocks), "candidate_parcels": len(members), "candidate_area_ha": float(blocks.area_ha.sum()),
            "largest_candidate_ha": float(blocks.area_ha.max()) if len(blocks) else 0.,
            "similar_groups_below_5ha": sum(g["state"] == "joint_group_below_5ha" for g in group_rows),
            "observation_counts": observation_counts, "accepted": False, "training_eligible": False,
            "publication": "internal_draft_only", "network_requests": 0, "raster_reads": 0,
            "elapsed_seconds": round(time.perf_counter() - started, 2),
            "limitations": ["Shared pixels are joint evidence, not independent confirmation for each parcel.",
                "All-pair temporal consistency does not prove independent observations, ownership or common management.",
                "NDVI/EVI2 use the same red/NIR bands; mixed NDMI/BSI remain context, with disagreements preserved.",
                "Unique footprint pixel counts are spatial diagnostics, not counts of independent clear observations.",
                "Narrow parcels may contain mostly neighbor signal; candidates require spatial and temporal review.",
                "Known roads/canal and overlap protections remain, but unmapped barriers may exist.",
                "Greedy disjoint grouping is deterministic, not an optimal consolidation layout.",
                "No legal, ownership, willingness, cost, crop identity or irrigation feasibility conclusion."]}
        write_json(OUT / "report.json", summary)
        base.check(inputs); base.check(code); ten.verify()
        write_json(OUT / "complete.json", {"outputs": {p.relative_to(OUT).as_posix(): sha256(p) for p in OUT.rglob("*") if p.is_file() and p.name not in ("complete.json", "run.lock")}})
        verify()
        print(json.dumps(summary, indent=2))


def verify():
    manifest = base.read(OUT / "manifest.json")
    base.check(manifest["inputs"]); base.check(manifest["code"]); ten.verify()
    for name, digest in base.read(OUT / "complete.json")["outputs"].items():
        if sha256(OUT / name) != digest:
            raise ValueError("Changed joint-screen output")
    original = pd.read_parquet(base.OUT / "active_register.parquet")
    expected = original[eligible_mask(original)].sort_values("internal_parcel_id").reset_index(drop=True)
    register = pd.read_parquet(OUT / "register.parquet")
    pd.testing.assert_frame_equal(expected, register[original.columns])
    for column in ("internal_parcel_id", "cadastre_code"):
        if register[column].isna().any() or not register[column].is_unique:
            raise ValueError("Duplicate or null cadastral identity")
    members = pd.read_parquet(OUT / "members.parquet")
    blocks = gpd.read_parquet(OUT / "blocks.parquet")
    groups = pd.read_parquet(OUT / "groups.parquet")
    if not members.internal_parcel_id.is_unique or (members.area_official_m2 > 5000).any() or (members.area_official_m2 <= 0).any():
        raise ValueError("Member-size or membership failure")
    if set(members.internal_parcel_id) != set(register.loc[register.joint_screening_state.eq("joint_eo_candidate_review"), "internal_parcel_id"]):
        raise ValueError("Candidate register mismatch")
    if blocks.crs.to_epsg() != 32638 or not blocks.is_valid.all() or blocks.is_empty.any() or blocks.accepted.any():
        raise ValueError("Invalid/unapproved block geometry")
    pairs = pd.read_parquet(OUT / "pair_checks.parquet")
    if pairs.duplicated(["a", "b"]).any():
        raise ValueError("Duplicate pair")
    masks = {(r.a, r.b): int(r.support_year_mask) for r in pairs.itertuples()}
    adjacency = {tuple(sorted((r.a, r.b))) for r in pd.read_parquet(OUT / "adjacency.parquet").itertuples()}
    seen = set()
    rindex = register.set_index("internal_parcel_id")
    for group in groups.itertuples():
        ids = json.loads(group.member_ids)
        if len(ids) < 2 or len(ids) != group.parcel_count or len(ids) != len(set(ids)) or seen & set(ids):
            raise ValueError("Invalid group membership")
        seen.update(ids)
        rows = rindex.loc[ids]
        if not np.isclose(rows.area_official_m2.sum() / 10000, group.area_ha):
            raise ValueError("Incorrect group area")
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
        if (group.state == "joint_eo_candidate_review") != (group.area_ha >= 5.):
            raise ValueError("Group area gate failed")
    if set(blocks.block_id) != set(groups.loc[groups.state.eq("joint_eo_candidate_review"), "block_id"]):
        raise ValueError("Missing candidate blocks")
    basis = gpd.read_parquet(local_path(ROOT, manifest["source_config"]["basis"])).set_index("internal_parcel_id")
    for block in blocks.itertuples():
        m = members[members.block_id.eq(block.block_id)].sort_values("internal_parcel_id")
        src = basis.loc[m.internal_parcel_id]
        if not np.array_equal(src.cadastre_code.to_numpy(), m.cadastre_code) or not np.array_equal(src.area_official_m2.to_numpy(), m.area_official_m2):
            raise ValueError("Cadastral attributes changed")
        if len(m) != block.parcel_count or len(m) < 10 or not np.isclose(m.area_official_m2.sum() / 10000, block.area_ha):
            raise ValueError("Candidate size/count mismatch")
        if not union_geometry(src.to_crs(32638).geometry.values).equals(block.geometry):
            raise ValueError("Cadastral union changed")
        if block.unique_10m_footprint_pixels > block.parcel_pixel_memberships:
            raise ValueError("Duplicate sampling counted as unique")
    policy = Policy(**manifest["policy"])
    for i, a in enumerate(blocks.geometry):
        for b in blocks.geometry.iloc[i + 1:]:
            if a.intersects(b) and not negligible_overlap(a, b, policy):
                raise ValueError("Candidate blocks overlap")
    base.status("verified_joint_pixel_screen", eligible_parcels=len(register), candidates=len(blocks), members=len(members))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["run", "verify"], default="run", nargs="?")
    args = parser.parse_args()
    execute() if args.command == "run" else verify()
