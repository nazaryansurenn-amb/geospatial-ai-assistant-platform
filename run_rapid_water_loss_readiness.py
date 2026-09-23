"""Check local evidence and attach review geography; does not infer soil loss from EO."""
from __future__ import annotations

import json
from pathlib import Path
from datetime import datetime, timezone
import geopandas as gpd
import pandas as pd
import pyarrow.parquet as pq
from release_tools import sha256

ROOT = Path(__file__).resolve().parent
VERSION = "rapid_water_loss_20260906_v1"
OUT = ROOT / "data/analysis/rapid_water_loss" / VERSION
CONFIG = ROOT / "config" / f"{VERSION}.json"


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def verify():
    manifest = json.loads((OUT / "complete.json").read_text(encoding="utf-8"))
    for relative, digest in manifest["files"].items():
        if sha256(ROOT / relative) != digest:
            raise ValueError("Readiness input/output changed: " + relative)
    parcels = pd.read_parquet(OUT / "parcels.parquet")
    scope = pd.read_parquet(ROOT / "data/observations/observations_2021_2025_v1/scope.parquet")
    identity = ["internal_parcel_id", "cadastre_code", "area_official_m2", "included", "household", "road_excluded"]
    pd.testing.assert_frame_equal(parcels[identity], scope[identity], check_dtype=False)
    assert len(parcels) == 43984 and parcels.internal_parcel_id.is_unique
    assert parcels.loc[parcels.included, "assessment_state"].eq("insufficient_evidence").all()
    assert parcels.loc[~parcels.included, "assessment_state"].eq("excluded").all()
    assert parcels.high_demand_candidate.isna().all()
    assert int(parcels.included.sum()) == 22802
    return json.loads((OUT / "report.json").read_text(encoding="utf-8"))


def main():
    if (OUT / "complete.json").exists():
        print(json.dumps(verify()["counts"], ensure_ascii=False))
        return
    if OUT.exists():
        raise ValueError("Incomplete output already exists; preserve it for inspection")
    cfg = json.loads(CONFIG.read_text(encoding="utf-8"))
    if cfg["classification_enabled"] or cfg["evidence_files"]:
        raise ValueError("This is a readiness runner, not a calibrated soil-loss classifier")
    scope_path = ROOT / "data/observations/observations_2021_2025_v1/scope.parquet"
    geom_path = ROOT / "data/source/cadastre/parcels_wua.gpkg"
    communities_path = ROOT / "public/data/communities.geojson"
    daily_path = ROOT / "data/analysis/observation_screening/transitions_area_20260906_v1/daily/2025.parquet"
    paths = [CONFIG, Path(__file__), scope_path, geom_path, communities_path]
    before = {p.relative_to(ROOT).as_posix(): sha256(p) for p in paths}
    scope = pd.read_parquet(scope_path)
    assert len(scope) == 43984 and scope.internal_parcel_id.is_unique
    assert scope.cadastre_code.is_unique and int(scope.included.sum()) == 22802
    assert (scope.included == ~(scope.household.astype(bool) | scope.road_excluded.astype(bool))).all()
    geometry = gpd.read_file(geom_path, layer="parcels", columns=["cadastre_code", "area_official_m2"])
    geometry = geometry.merge(scope[["internal_parcel_id", "cadastre_code", "area_official_m2"]],
                              on="cadastre_code", suffixes=("_geometry", ""), validate="one_to_one")
    assert len(geometry) == len(scope)
    assert geometry.area_official_m2.eq(geometry.area_official_m2_geometry).all()
    assert geometry.geometry.is_valid.all()
    communities = gpd.read_file(communities_path).to_crs(geometry.crs)
    assert communities.name_hy.is_unique and communities.geometry.is_valid.all()
    assert set(cfg["focus_communities"]) <= set(communities.name_hy)
    # A point-in-polygon association is review geography, not official membership.
    points = gpd.GeoDataFrame(geometry[["internal_parcel_id"]],
                             geometry=geometry.geometry.representative_point(), crs=geometry.crs)
    joined = gpd.sjoin(points, communities[["name_hy", "geometry"]], how="left", predicate="within")
    counts = joined.groupby("internal_parcel_id").name_hy.count()
    unambiguous = joined.loc[joined.internal_parcel_id.map(counts).eq(1)]
    names = unambiguous.set_index("internal_parcel_id").name_hy
    parcels = scope.copy()
    parcels["review_community_hy"] = parcels.internal_parcel_id.map(names)
    parcels["community_association"] = "representative_point_within_saved_boundary"
    parcels.loc[parcels.review_community_hy.isna(), "community_association"] = "unresolved"
    parcels["review_priority"] = "other"
    parcels.loc[parcels.review_community_hy.isin(cfg["focus_communities"]), "review_priority"] = "focus"
    parcels.loc[parcels.review_community_hy.isin(cfg["priority_communities"]), "review_priority"] = "priority"
    parcels["assessment_state"] = parcels.included.map({True: "insufficient_evidence", False: "excluded"})
    parcels["high_demand_candidate"] = pd.Series(pd.NA, index=parcels.index, dtype="boolean")
    counts_report = {"scope": len(parcels), "eligible": int(parcels.included.sum()),
                     "household": int(parcels.household.sum()), "roads": int(parcels.road_excluded.sum()),
                     "assessed": 0, "unassessed": int(parcels.included.sum()), "candidates": None}
    by_community = []
    for name in sorted(communities.name_hy):
        group = parcels.loc[parcels.review_community_hy.eq(name)]
        by_community.append({"name_hy": name, "scope_parcels": len(group),
                             "eligible_parcels": int(group.included.sum()),
                             "priority": name in cfg["priority_communities"],
                             "focus": name in cfg["focus_communities"]})
    report = {"version": VERSION, "run_type": "evidence_readiness_and_review_geography",
              "counts": counts_report, "communities": by_community,
              "community_unresolved": int(parcels.review_community_hy.isna().sum()),
              "missing_required_evidence": cfg["required_evidence"],
              "available_daily_eo_columns": pq.read_schema(daily_path).names,
              "evidence_search": "Configured local inputs and independent product input inventory; no parent project sources",
              "classification_executed": False, "bulk_llm_calls": 0,
              "eo_redownloaded": False, "coarse_250m_dataset_used": False}
    OUT.mkdir(parents=True)
    parcels.to_parquet(OUT / "parcels.parquet", index=False)
    write(OUT / "report.json", report)
    public_summary = {"title_hy": cfg["title_hy"], "status": "not_assessed", "fill_color": cfg["fill_color"],
                      "candidate_count": None, "scope_counts": {}}
    for name in ["lower_hrazdan", "stage_1", "stage_2"]:
        part = parcels if name == "lower_hrazdan" else parcels.loc[parcels.activity_stage.eq(name)]
        public_summary["scope_counts"][name] = {"eligible": int(part.included.sum()),
                                                "unassessed": int(part.included.sum())}
    write(OUT / "public_summary.json", public_summary)
    for relative, digest in before.items():
        if sha256(ROOT / relative) != digest:
            raise ValueError("Input changed during readiness pass")
    files = before | {p.relative_to(ROOT).as_posix(): sha256(p) for p in OUT.iterdir() if p.is_file()}
    write(OUT / "complete.json", {"version": VERSION, "finished_utc": datetime.now(timezone.utc).isoformat(), "files": files})
    print(json.dumps(verify()["counts"], ensure_ascii=False))


if __name__ == "__main__":
    main()
