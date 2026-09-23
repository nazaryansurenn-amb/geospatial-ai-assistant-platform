"""Apply the approved <=0.5 ha member rule before grouping, preserving both screens."""
from __future__ import annotations
import argparse
from itertools import combinations
import json
from pathlib import Path
import shutil
import time

import run_land_consolidation as first
import run_land_consolidation_10m as second
import geopandas as gpd
import numpy as np
import pandas as pd

from release_tools import local_path, sha256
from run_observation_analysis import now, run_lock, write_json, write_table
from wp_core.land_consolidation import Policy, block_id, connected_complete_link, compare_season, shared_pixels, union_geometry
from wp_core.land_consolidation_10m import Policy as TenPolicy, compare_season as compare_tenm, target_mask

ROOT = Path(__file__).resolve().parent
CONFIG = "config/consolidation_small_parcels_20260906_v1.json"
OUT = ROOT / "data/analysis/land_consolidation/consolidation_small_parcels_20260906_v1"


def small_parcels(areas, maximum_m2):
    return areas.notna() & np.isfinite(areas) & areas.gt(0) & areas.le(maximum_m2)


def execute():
    config = first.read(ROOT / CONFIG)
    second.verify()
    for source, key in ((first.OUT, "baseline_complete_sha256"), (second.OUT, "tenm_complete_sha256")):
        if sha256(source / "complete.json") != config[key]:
            raise ValueError("Changed source screen")
    if config["maximum_member_area_m2"] != 5000. or config["minimum_group_area_ha"] != 5. or config["publication"] != "internal_draft_only":
        raise ValueError("Unapproved size rule")
    OUT.mkdir(parents=True, exist_ok=True)
    with run_lock(OUT / "run.lock"):
        if (OUT / "complete.json").exists():
            verify(); return
        if (OUT / "manifest.json").exists():
            raise ValueError("Unsealed version exists; inspect without overwriting")
        started = time.perf_counter()
        original = first.read(first.OUT / "manifest.json")
        ten_manifest = first.read(second.OUT / "manifest.json")
        pins = {**ten_manifest["code"], **{p: sha256(ROOT / p) for p in (CONFIG, "run_land_consolidation_small_parcels.py")}}
        inputs = dict(ten_manifest["inputs"])
        for source in (first.OUT, second.OUT):
            for name in ("complete.json", "pair_checks.parquet", "manifest.json"):
                path = source / name
                inputs[path.relative_to(ROOT).as_posix()] = sha256(path)
        manifest = {"created_at": now(), "config": config, "inputs": inputs, "code": pins,
                    "accepted": False, "training_eligible": False, "network_requests": 0, "raster_reads": 0}
        write_json(OUT / "manifest.json", manifest)
        for relative in pins:
            destination = OUT / "source" / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(ROOT / relative, destination)
        register = pd.read_parquet(first.OUT / "active_register.parquet")
        small = small_parcels(register.area_official_m2, config["maximum_member_area_m2"])
        register["size_screening_state"] = "above_member_size_limit"
        register.loc[small, "size_screening_state"] = "spatial_or_mask_review"
        register["screen"] = "not_eligible"
        masks = {
            "four_index": register.screening_state.isin(["candidate", "no_qualifying_group", "similar_group_below_5ha"]) & small,
            "additional_10m": target_mask(register, TenPolicy(**ten_manifest["resolved_policy"])) & small,
        }
        topology = pd.read_parquet(first.OUT / "topology.parquet")
        basis = gpd.read_parquet(local_path(ROOT, original["config"]["basis"])).set_index("internal_parcel_id")
        groups_out, members_out, blocks_out, diagnostics, adjacency_rows, screen_counts = [], [], [], [], [], {}
        for name, selected in masks.items():
            target = register.loc[selected].sort_values("internal_parcel_id")
            ids = target.internal_parcel_id.tolist()
            positions = {int(old): new for new, old in enumerate(target.index)}
            usable = topology.state.eq("adjacent") & topology.a.isin(positions) & topology.b.isin(positions)
            edges = [(positions[int(r.a)], positions[int(r.b)], float(r.shared_boundary_m)) for r in topology.loc[usable].itertuples()]
            participants = sorted({i for a, b, _ in edges for i in (a, b)})
            register.loc[target.index, "screen"] = name
            register.loc[target.index, "size_screening_state"] = "no_eligible_neighbor"
            register.loc[target.index[participants], "size_screening_state"] = "no_qualifying_group"
            cached = pd.read_parquet((first.OUT if name == "four_index" else second.OUT) / "pair_checks.parquet")
            cache = {(r["a"], r["b"]): r for r in cached.to_dict("records")}
            policy = Policy(**original["config"]["policy"]) if name == "four_index" else TenPolicy(**ten_manifest["resolved_policy"])
            series, pixels, lookup, used, computed = None, None, None, {}, 0
            def compare(a, b):
                nonlocal series, pixels, lookup, computed
                a, b = sorted((a, b))
                key = (ids[a], ids[b])
                if key not in cache:
                    if series is None:
                        selected_ids = [ids[i] for i in participants]
                        loader = first.load_series if name == "four_index" else second.load_series
                        loaded = loader(original["config"], selected_ids, policy)
                        series = loaded if name == "four_index" else loaded[0]
                        pixels = first.load_pixels(original["config"], selected_ids)
                        lookup = {old: new for new, old in enumerate(participants)}
                    i, j = lookup[a], lookup[b]
                    shared = {r: shared_pixels(pixels[r][i], pixels[r][j]) for r in (10, 20)}
                    gate = max(shared.values()) if name == "four_index" else shared[10]
                    mask, details = 0, []
                    if gate <= policy.maximum_shared_pixel_fraction:
                        for bit, year in enumerate(original["config"]["years"]):
                            if name == "four_index":
                                days, values = series[year]
                                d = compare_season(days, values[i], values[j], year, policy)
                            else:
                                days, values, support = series[year]
                                d = compare_tenm(days, values[i], values[j], support[i], support[j], shared[10], year, policy)
                            details.append(d)
                            if d["similar"]:
                                mask |= 1 << bit
                    cache[key] = {"a": key[0], "b": key[1], "support_year_mask": mask,
                        "year_results": json.dumps(details, allow_nan=False), "reason": "shared_pixel_review" if not details else ("similar" if mask.bit_count() >= 3 else "not_repeatedly_similar")}
                    computed += 1
                used[key] = cache[key]
                return int(cache[key]["support_year_mask"])
            groups = connected_complete_link(ids, edges, compare, policy)
            for a, b, length in edges:
                compare(a, b)
                adjacency_rows.append({"screen": name, "a": ids[a], "b": ids[b], "shared_boundary_m": length})
            for indexes, mask in groups:
                if len(indexes) < 2:
                    continue
                group = target.iloc[indexes]
                area = float(group.area_official_m2.sum()) / 10000
                candidate = area >= config["minimum_group_area_ha"]
                state = "candidate_review" if candidate else "similar_group_below_5ha"
                register.loc[group.index, "size_screening_state"] = state
                bid = "small_" + block_id(group.internal_parcel_id.tolist())
                row = {"block_id": bid, "screen": name, "parcel_count": len(group), "area_ha": area,
                    "support_year_mask": mask, "state": state, "member_ids": json.dumps(group.internal_parcel_id.tolist()), "accepted": False}
                groups_out.append(row)
                if not candidate:
                    continue
                block = {**row, "geometry": union_geometry(basis.loc[group.internal_parcel_id].to_crs(32638).geometry.values)}
                blocks_out.append(block)
                for member in group.itertuples():
                    members_out.append({"block_id": bid, "screen": name, "internal_parcel_id": member.internal_parcel_id,
                        "cadastre_code": member.cadastre_code, "area_official_m2": float(member.area_official_m2), "stage": member.stage})
            diagnostics.extend({"screen": name, "a": r["a"], "b": r["b"], "support_year_mask": r["support_year_mask"],
                                "reason": r["reason"], "year_results": r["year_results"]} for r in used.values())
            screen_counts[name] = {"eligible_parcels": len(target), "neighboring_parcels": len(participants),
                                  "adjacent_pairs": len(edges), "all_pair_checks": len(used), "new_pair_calculations": computed}
            first.status("size_rule_screen_finished", screen=name, **screen_counts[name])
        group_cols = ["block_id", "screen", "parcel_count", "area_ha", "support_year_mask", "state", "member_ids", "accepted"]
        groups = pd.DataFrame(groups_out, columns=group_cols)
        blocks = gpd.GeoDataFrame(blocks_out, columns=group_cols + ["geometry"], geometry="geometry", crs=32638)
        members = pd.DataFrame(members_out, columns=["block_id", "screen", "internal_parcel_id", "cadastre_code", "area_official_m2", "stage"])
        for name, frame in (("register", register), ("groups", groups), ("blocks", blocks), ("members", members),
                            ("pair_checks", pd.DataFrame(diagnostics)), ("adjacency", pd.DataFrame(adjacency_rows))):
            write_table(OUT / f"{name}.parquet", frame)
        write_json(OUT / "blocks.geojson", json.loads(blocks.to_crs(4326).to_json(drop_id=True)))
        report = {"version": config["version"], "created_at": now(), "active_register": len(register),
            "member_size_limit_ha": .5, "group_minimum_ha": 5., "active_within_size_limit": int(small.sum()),
            "screen_counts": screen_counts, "register_states": register.size_screening_state.value_counts().to_dict(),
            "candidate_blocks": len(blocks), "candidate_parcels": len(members), "candidate_area_ha": float(blocks.area_ha.sum()),
            "similar_groups_below_5ha": int(groups.state.eq("similar_group_below_5ha").sum()),
            "largest_similar_group_ha": float(groups.area_ha.max()) if len(groups) else 0.,
            "prior_four_blocks_meet_member_size_rule": False,
            "accepted": False, "publication": "internal_draft_only", "network_requests": 0, "raster_reads": 0,
            "elapsed_seconds": round(time.perf_counter() - started, 2),
            "limitations": ["Separate evidence-quality screens retained; no cross-screen grouping is claimed.",
                "Zero qualifying groups is a result of this screening, not proof that consolidation is impossible.",
                "Rules are uncalibrated; no independent field accuracy, legal or operational feasibility is established."]}
        write_json(OUT / "report.json", report)
        first.check(inputs); first.check(pins); second.verify()
        write_json(OUT / "complete.json", {"outputs": {p.relative_to(OUT).as_posix(): sha256(p) for p in OUT.rglob("*") if p.is_file() and p.name not in ("complete.json", "run.lock")}})
        verify()
        first.status("completed_member_size_screen", **report)


def verify():
    manifest = first.read(OUT / "manifest.json")
    first.check(manifest["inputs"]); first.check(manifest["code"])
    second.verify()
    for relative, digest in first.read(OUT / "complete.json")["outputs"].items():
        if sha256(OUT / relative) != digest:
            raise ValueError("Changed size-screen output")
    register = pd.read_parquet(OUT / "register.parquet")
    original = pd.read_parquet(first.OUT / "active_register.parquet")
    pd.testing.assert_frame_equal(original, register[original.columns])
    for field in ("internal_parcel_id", "cadastre_code"):
        if register[field].isna().any() or not register[field].is_unique:
            raise ValueError("Cadastral identity failure")
    groups = pd.read_parquet(OUT / "groups.parquet")
    pairs = pd.read_parquet(OUT / "pair_checks.parquet")
    masks = {(r.screen, r.a, r.b): r.support_year_mask for r in pairs.itertuples()}
    adjacency = {(r.screen, *sorted((r.a, r.b))) for r in pd.read_parquet(OUT / "adjacency.parquet").itertuples()}
    seen = set()
    for group in groups.itertuples():
        ids = json.loads(group.member_ids)
        if seen & set(ids) or len(ids) < 2 or len(ids) != len(set(ids)):
            raise ValueError("Duplicate or singleton group")
        seen.update(ids)
        rows = register.set_index("internal_parcel_id").loc[ids]
        if not small_parcels(rows.area_official_m2, 5000.).all():
            raise ValueError("Member exceeds 0.5 ha")
        if rows.household_agriculture.any() or rows.road_excluded.any() or rows.mask_conflict.any() or not rows.screen.eq(group.screen).all():
            raise ValueError("Excluded or wrong-screen member")
        if not np.isclose(rows.area_official_m2.sum() / 10000, group.area_ha) or len(ids) != group.parcel_count:
            raise ValueError("Incorrect group area/count")
        mask = 31
        for a, b in combinations(sorted(ids), 2):
            mask &= int(masks.get((group.screen, a, b), 0))
        if mask != group.support_year_mask or mask.bit_count() < 3:
            raise ValueError("Group lacks three shared years of all-pair agreement")
        reached = {ids[0]}
        while True:
            new = reached | {b for b in ids if any((group.screen, *sorted((a, b))) in adjacency for a in reached)}
            if new == reached:
                break
            reached = new
        if reached != set(ids):
            raise ValueError("Disconnected group")
        if (group.state == "candidate_review") != (group.area_ha >= 5.):
            raise ValueError("Group area gate not applied")
    blocks = gpd.read_parquet(OUT / "blocks.parquet")
    members = pd.read_parquet(OUT / "members.parquet")
    if blocks.crs.to_epsg() != 32638 or not blocks.is_valid.all() or not members.internal_parcel_id.is_unique:
        raise ValueError("Invalid candidate output")
    if set(blocks.block_id) != set(groups.loc[groups.state.eq("candidate_review"), "block_id"]):
        raise ValueError("Missing candidate output")
    expected_members = {i for r in groups[groups.state.eq("candidate_review")].itertuples() for i in json.loads(r.member_ids)}
    if expected_members != set(members.internal_parcel_id):
        raise ValueError("Candidate member mismatch")
    basis_path = first.read(first.OUT / "manifest.json")["config"]["basis"]
    basis = gpd.read_parquet(local_path(ROOT, basis_path)).set_index("internal_parcel_id")
    for block in blocks.itertuples():
        m = members[members.block_id.eq(block.block_id)].sort_values("internal_parcel_id")
        source = basis.loc[m.internal_parcel_id]
        if not np.array_equal(source.cadastre_code.to_numpy(), m.cadastre_code) or not np.array_equal(source.area_official_m2.to_numpy(), m.area_official_m2):
            raise ValueError("Altered cadastral attributes")
        if not union_geometry(source.to_crs(32638).geometry.values).equals(block.geometry):
            raise ValueError("Altered source geometry")
    first.status("verified_member_size_rule", active_register=len(register), blocks=len(blocks), members=len(members))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["run", "verify"], default="run", nargs="?")
    args = parser.parse_args()
    execute() if args.command == "run" else verify()
