"""Parcel groups from the AlphaEarth pixel groups, comparisons and example parcels (product .venv).

Usage: alphaearth_parcels.py [--year 2025]
Exact-area composition per eligible parcel on the sealed 10 m grid; comparison with the live
classification is context only, not accuracy. Local only; writes new files and never overwrites.
"""
from __future__ import annotations
import argparse
import json
import sqlite3
import sys
from pathlib import Path

import geopandas as gpd
import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from release_tools import sha256
from wp_core.alphaearth import parcel_groups
from scripts.alphaearth_prepare_grid import load_sealed_grid

CONFIG = json.loads((ROOT / "config/alphaearth_groups_20261030_v1.json").read_text())
DATA = ROOT / CONFIG["outputs"]["data"]
LIVE_INDEX = ROOT / "server_data/agent_v7/parcels.sqlite3"
DAILY = ROOT / "data/analysis/observation_screening/transitions_area_20260906_v1/daily"
COMPARE = ["crop_type", "annual_cycle", "activity_class", "history_class"]
EXAMPLES = 5


def load_groups(year):
    clusters = DATA / f"clusters_{year}"
    report = json.loads((clusters / "clusters.json").read_text())
    for name, value in report["files"].items():
        if sha256(clusters / name) != value:
            raise ValueError(f"{name} changed since clustering")
    return report, np.load(clusters / "groups.npy"), np.load(clusters / "fit.npy").astype(np.float32), np.load(clusters / "valid.npy")


def monthly_profiles(table, year, index):
    """Median monthly index per parcel from the existing daily parcel observations, then per group."""
    daily = pd.read_parquet(DAILY / f"{year}.parquet",
                            columns=["internal_parcel_id", "observation_date", f"{index}_median", f"{index}_valid_fraction"])
    daily = daily[daily[f"{index}_valid_fraction"] >= .8]
    daily["month"] = pd.to_datetime(daily.observation_date).dt.month
    parcel = daily.groupby(["internal_parcel_id", "month"])[f"{index}_median"].median().rename("value").reset_index()
    parcel = parcel.merge(table[["internal_parcel_id", "group"]], on="internal_parcel_id")
    curve = parcel.groupby(["group", "month"]).value.median().unstack().reindex(columns=range(1, 13))
    return {int(g): [None if np.isnan(v) else round(float(v), 3) for v in row] for g, row in curve.iterrows()}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--year", type=int, default=CONFIG["years_initial"][0])
    args = parser.parse_args()
    out = DATA / f"parcels_{args.year}"
    if out.exists():
        raise SystemExit(f"{out.relative_to(ROOT)} already exists; it is never overwritten.")
    report, groups, fit, valid = load_groups(args.year)
    k = report["chosen_k"]
    weights = load_sealed_grid(CONFIG)
    grid_report = json.loads((DATA / "grid/grid.json").read_text())
    if sha256(DATA / "grid/parcels.parquet") != grid_report["files"]["parcels.parquet"]:
        raise ValueError("Parcel order table changed")
    table = pd.read_parquet(DATA / "grid/parcels.parquet")
    r = parcel_groups(groups, fit, valid, weights["parcel"], weights["pixel"], weights["area"], len(table), k)
    p = CONFIG["parcel"]
    pure = np.bincount(weights["parcel"][weights["area"] >= CONFIG["training"]["pure_pixel_min_area_m2"]], minlength=len(table))
    table["group"] = r["dominant"]
    table["group_share"] = r["dominant_share"].round(4)
    table["valid_fraction"] = r["valid_fraction"].round(4)
    table["mean_fit"] = r["mean_fit"].round(4)
    table["mixed"] = r["dominant_share"] < p["mixed_if_dominant_share_below"]
    table["small"] = table.pixel_equivalents < p["small_if_pixel_equivalents_below"]
    table["has_whole_pixel"] = pure > 0
    for g in range(k):
        table[f"share_g{g}"] = r["shares"][:, g].round(4)
    if (table.group < 0).any():
        raise ValueError("A parcel has no valid AlphaEarth pixel")

    with sqlite3.connect(LIVE_INDEX.as_uri() + "?mode=ro", uri=True) as con:
        live = pd.read_sql_query("SELECT cadastre_code, community, " + ", ".join(COMPARE) + " FROM parcels", con)
    table = table.merge(live, on="cadastre_code", how="left", validate="one_to_one")
    if table.community.isna().all():
        raise ValueError("Live index did not join")

    geometry = gpd.read_parquet(ROOT / json.loads((ROOT / "data/observations/observations_2021_2025_v1/specification.json")
                                                  .read_text())["config"]["basis"]).set_index("internal_parcel_id")
    geometry = geometry.loc[table.internal_parcel_id].to_crs(CONFIG["grid"]["crs"])
    area_m2 = geometry.area.to_numpy()
    covered_ha = r["shares"] * area_m2[:, None] / 1e4
    lonlat = geometry.geometry.centroid.to_crs(4326)

    rectangle = np.bincount(groups[valid].ravel(), minlength=k)
    ndvi = monthly_profiles(table, args.year, "ndvi")
    ndmi = monthly_profiles(table, args.year, "ndmi")
    summary = {"version": CONFIG["version"], "year": args.year, "k": k, "credit": CONFIG["credit"],
               "parcels": len(table), "mixed_parcels": int(table.mixed.sum()), "small_parcels": int(table.small.sum()),
               "parcels_without_whole_pixel": int((~table.has_whole_pixel).sum()), "groups": []}
    examples = {}
    for g in range(k):
        member = table.group == g
        clear = member & ~table.mixed
        facts = {"group": g, "parcels": int(member.sum()), "parcels_clear": int(clear.sum()),
                 "parcels_mixed": int((member & table.mixed).sum()), "parcels_small": int((member & table.small).sum()),
                 "official_ha": round(float(table.area_official_m2[member].sum() / 1e4), 2),
                 "covered_ha_all_parcels": round(float(covered_ha[:, g].sum()), 2),
                 "rectangle_km2": round(float(rectangle[g] * 100 / 1e6), 2),
                 "mean_fit": round(float(table.mean_fit[member].mean()), 4) if member.any() else None,
                 "median_parcel_ha": round(float(table.area_official_m2[member].median() / 1e4), 3) if member.any() else None,
                 "ndvi_monthly": ndvi.get(g), "ndmi_monthly": ndmi.get(g),
                 "compare": {c: {str(key): int(v) for key, v in table.loc[member, c].fillna("none").value_counts().items()}
                             for c in COMPARE},
                 "communities_top": {str(key): int(v) for key, v in table.loc[member, "community"].value_counts().head(6).items()}}
        summary["groups"].append(facts)
        candidates = table[clear & ~table.small & table.has_whole_pixel].copy()
        candidates = candidates.sort_values(["group_share", "area_official_m2", "internal_parcel_id"], ascending=[False, False, True])
        chosen, seen = [], set()
        for index, row in candidates.iterrows():
            if row.community in seen:
                continue
            seen.add(row.community)
            shape = geometry.geometry.iloc[index]
            chosen.append({"cadastre_code": row.cadastre_code, "community": row.community,
                           "official_ha": round(row.area_official_m2 / 1e4, 3), "group_share": float(row.group_share),
                           "centre_utm": [round(shape.centroid.x, 1), round(shape.centroid.y, 1)],
                           "centre_lonlat": [round(lonlat.iloc[index].x, 6), round(lonlat.iloc[index].y, 6)],
                           "outline_utm": [[round(x, 1), round(y, 1)] for x, y in
                                           (shape.exterior.coords if shape.geom_type == "Polygon" else
                                            max(shape.geoms, key=lambda s: s.area).exterior.coords)]})
            if len(chosen) == EXAMPLES:
                break
        examples[str(g)] = chosen

    out.mkdir(parents=True)
    table.to_parquet(out / "parcels.parquet", index=False)
    (out / "summary.json").write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")
    (out / "examples.json").write_text(json.dumps(examples, indent=1, ensure_ascii=False), encoding="utf-8")
    sys.stdout.reconfigure(encoding="utf-8")
    print(json.dumps({key: v for key, v in summary.items() if key != "groups"}, indent=2))
    for facts in summary["groups"]:
        print(json.dumps({key: facts[key] for key in ("group", "parcels", "parcels_clear", "official_ha", "rectangle_km2",
                                                      "median_parcel_ha", "ndvi_monthly", "compare")}, ensure_ascii=False))


if __name__ == "__main__":
    main()
