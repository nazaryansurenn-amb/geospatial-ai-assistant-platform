"""Private, versioned sensitivity test on existing parcel-level EO metrics."""
from __future__ import annotations
import argparse
from dataclasses import asdict
import json
from pathlib import Path
import shutil
import time

import pandas as pd

import run_land_consolidation as first
import run_land_consolidation_small_parcels as strict
from release_tools import local_path, sha256
from run_observation_analysis import now, run_lock, write_json, write_table
from wp_core.consolidation_phenology_sensitivity import Policy, connected_components, rescore_pair

ROOT = Path(__file__).resolve().parent
CONFIG = "config/consolidation_phenology_relaxed_20260906_v1.json"
OUT = ROOT / "data/analysis/land_consolidation/consolidation_phenology_relaxed_20260906_v1"
STRICT = Policy(minimum_years=3, minimum_correlation=.8, maximum_rmse=(.10, .08, .08, .10), minimum_phase_agreement=.8)


def analyze(source, policy):
    register = pd.read_parquet(source / "register.parquet")
    pairs = pd.read_parquet(source / "pair_checks.parquet")
    adjacency = pd.read_parquet(source / "adjacency.parquet")
    records = []
    for row in pairs.to_dict("records"):
        old = rescore_pair(row, STRICT)
        if old["support_year_mask"] != row["support_year_mask"]:
            raise ValueError("Strict metric replay differs from preserved decision")
        new = rescore_pair(row, policy)
        if new["support_year_mask"] & old["support_year_mask"] != old["support_year_mask"]:
            raise ValueError("Relaxation removed strict support")
        records.append({"screen": row["screen"], "a": row["a"], "b": row["b"],
            "strict_support_year_mask": old["support_year_mask"], "strict_similar": old["similar"],
            "relaxed_support_year_mask": new["support_year_mask"], "relaxed_similar": new["similar"],
            "state": new["state"], "year_results": json.dumps(new["year_results"], allow_nan=False)})
    comparison = pd.DataFrame(records)
    if comparison.duplicated(["screen", "a", "b"]).any() or adjacency.duplicated(["screen", "a", "b"]).any():
        raise ValueError("Duplicate pair")
    edges = adjacency.merge(comparison, on=["screen", "a", "b"], how="left", validate="1:1")
    if edges.state.isna().any():
        raise ValueError("Adjacency has no comparison")
    summaries, components = {}, []
    for screen, rows in edges.groupby("screen", sort=True):
        selected = register[register.screen.eq(screen)]
        ids = selected.internal_parcel_id.tolist()
        areas = dict(zip(ids, selected.area_official_m2))
        summary = {"eligible_parcels": len(ids), "adjacent_pairs": len(rows),
                   "shared_pixel_blocked_pairs": int(rows.state.eq("shared_pixel_review").sum()),
                   "strict_similar_adjacent_pairs": int(rows.strict_similar.sum()),
                   "relaxed_similar_adjacent_pairs": int(rows.relaxed_similar.sum())}
        for mode, allowed in (("strict", rows.strict_similar), ("relaxed", rows.relaxed_similar),
                              ("no_phenology_upper_bound", ~rows.state.eq("shared_pixel_review"))):
            grouping = connected_components(ids, list(rows.loc[allowed, ["a", "b"]].itertuples(index=False, name=None)), areas)
            summary[mode] = {"multi_parcel_components": len(grouping),
                "largest_connected_area_ha": grouping[0]["area_ha"] if grouping else 0.,
                "components_reaching_5ha": sum(g["area_ha"] >= 5 for g in grouping)}
            components.extend({"screen": screen, "mode": mode, "component_rank": n + 1,
                "parcel_count": g["parcel_count"], "area_ha": g["area_ha"],
                "member_ids": json.dumps(g["member_ids"]), "state": "connectivity_diagnostic_only", "accepted": False}
                for n, g in enumerate(grouping))
        summaries[screen] = summary
    unresolved = sum(v["relaxed"]["components_reaching_5ha"] for v in summaries.values())
    no_phenology = max((v["no_phenology_upper_bound"]["largest_connected_area_ha"] for v in summaries.values()), default=0.)
    report = {"eligible_parcels": sum(v["eligible_parcels"] for v in summaries.values()),
        "all_pair_checks": len(comparison), "adjacent_pairs": len(edges), "screens": summaries,
        "strict_similar_adjacent_pairs": int(edges.strict_similar.sum()),
        "relaxed_similar_adjacent_pairs": int(edges.relaxed_similar.sum()),
        "shared_pixel_blocked_adjacent_pairs": int(edges.state.eq("shared_pixel_review").sum()),
        "no_phenology_largest_connected_area_ha": no_phenology,
        "qualifying_group_count": 0 if unresolved == 0 else None,
        "unresolved_components_requiring_all_pair_verification": unresolved,
        "status": "no_qualifying_connected_area" if unresolved == 0 else "requires_all_pair_verification",
        "proof": "Any admissible connected group is contained in a connected component of the allowed adjacency graph. No component reaching 5 ha means no all-pair-similar group can reach 5 ha. Components themselves are never published as candidates.",
        "scope_limitation": "The two evidence-quality populations remain separate; cross-screen combinations were not tested."}
    return comparison, pd.DataFrame(components), report


def execute():
    config = first.read(ROOT / CONFIG)
    source = local_path(ROOT, config["source"])
    if source != strict.OUT or sha256(source / "complete.json") != config["source_complete_sha256"]:
        raise ValueError("Unexpected strict result")
    if config["publication"] != "internal_draft_only":
        raise ValueError("No publication authorized")
    policy = Policy(**config["policy"])
    strict.verify()
    OUT.mkdir(parents=True, exist_ok=True)
    with run_lock(OUT / "run.lock"):
        if (OUT / "complete.json").exists():
            verify(); return
        if (OUT / "manifest.json").exists():
            raise ValueError("Unsealed attempt exists; inspect before retry")
        started = time.perf_counter()
        parent = first.read(source / "manifest.json")
        inputs = dict(parent["inputs"])
        for filename in ("complete.json", "manifest.json", "pair_checks.parquet", "adjacency.parquet", "register.parquet"):
            path = source / filename
            inputs[path.relative_to(ROOT).as_posix()] = sha256(path)
        code = {**parent["code"], **{p: sha256(ROOT / p) for p in (
            CONFIG, "wp_core/consolidation_phenology_sensitivity.py", "run_consolidation_phenology_sensitivity.py")}}
        manifest = {"created_at": now(), "config": config, "policy": asdict(policy), "inputs": inputs,
                    "code": code, "accepted": False, "training_eligible": False,
                    "network_requests": 0, "raster_reads": 0, "raw_eo_reloads": 0}
        write_json(OUT / "manifest.json", manifest)
        for relative in code:
            destination = OUT / "source" / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(ROOT / relative, destination)
        comparison, components, report = analyze(source, policy)
        write_table(OUT / "pair_comparison.parquet", comparison)
        write_table(OUT / "connectivity_diagnostics.parquet", components)
        # An empty candidate file is justified only by the checked connectivity bound.
        if report["qualifying_group_count"] == 0:
            write_json(OUT / "candidate_blocks.geojson", {"type": "FeatureCollection", "features": []})
        report.update(version=config["version"], created_at=now(), elapsed_seconds=round(time.perf_counter() - started, 2),
                      accepted=False, training_eligible=False, publication="internal_draft_only",
                      network_requests=0, raster_reads=0, raw_eo_reloads=0,
                      limitations=["Exploratory thresholds, not calibrated accuracy or field validation.",
                                   "Only phenology thresholds changed; source cadence and all spatial/data-quality exclusions remain.",
                                   "Connectivity diagnostics are upper bounds, not complete-link similar groups or consolidation recommendations."])
        write_json(OUT / "report.json", report)
        first.check(inputs); first.check(code); strict.verify()
        write_json(OUT / "complete.json", {"outputs": {p.relative_to(OUT).as_posix(): sha256(p) for p in OUT.rglob("*")
            if p.is_file() and p.name not in ("complete.json", "run.lock")}})
        verify()
        print(json.dumps(report, indent=2))


def verify():
    manifest = first.read(OUT / "manifest.json")
    first.check(manifest["inputs"]); first.check(manifest["code"])
    strict.verify()
    for relative, digest in first.read(OUT / "complete.json")["outputs"].items():
        if sha256(OUT / relative) != digest:
            raise ValueError("Changed sensitivity output")
    source = local_path(ROOT, manifest["config"]["source"])
    comparison, components, report = analyze(source, Policy(**manifest["policy"]))
    pd.testing.assert_frame_equal(comparison, pd.read_parquet(OUT / "pair_comparison.parquet"))
    pd.testing.assert_frame_equal(components, pd.read_parquet(OUT / "connectivity_diagnostics.parquet"))
    saved = first.read(OUT / "report.json")
    for key, value in report.items():
        if saved[key] != value:
            raise ValueError("Report replay mismatch: " + key)
    if (OUT / "candidate_blocks.geojson").exists():
        if report["qualifying_group_count"] != 0 or first.read(OUT / "candidate_blocks.geojson")["features"]:
            raise ValueError("Unverified candidates")
    first.status("verified_phenology_sensitivity", comparisons=len(comparison), qualifying_groups=report["qualifying_group_count"])


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["run", "verify"], default="run", nargs="?")
    args = parser.parse_args()
    execute() if args.command == "run" else verify()
