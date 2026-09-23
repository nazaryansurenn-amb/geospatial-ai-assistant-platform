"""Prepare the approved area method as a separate local review delivery."""
from __future__ import annotations
import json
import os
from pathlib import Path
import shutil
import sqlite3
import sys
from datetime import datetime, timezone

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import geopandas as gpd
import numpy as np
import pandas as pd
import mapbox_vector_tile
from mapbox_vector_tile.Mapbox import vector_tile_pb2
from release_tools import verify_release, read_release, sha256

SLUG = "transitions_area_20260906_v1"
AREA = ROOT / "data/analysis/observation_screening" / SLUG
REVIEW = ROOT / "server_data/review" / SLUG
FRONTEND = ROOT / "output" / f"frontend_{SLUG}"
os.environ["LAND_ANALYTICS_PROFILE"] = "use_type_v2"
os.environ["LAND_ANALYTICS_DELIVERY_SLUG"] = SLUG
import prepare_land_analytics_delivery as delivery


def read(path):
    return json.loads(path.read_text(encoding="utf-8"))


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def summary_counts(frame, cycles=False):
    result = {}
    for scope in ("lower_hrazdan", "stage_1", "stage_2"):
        all_rows = frame if scope == "lower_hrazdan" else frame.loc[frame.stage.eq(scope)]
        rows = all_rows.loc[~all_rows.household_agriculture & ~all_rows.road_excluded]
        series = rows.annual_cycle if cycles else rows.crop_type_candidate
        classes = ["single_cycle", "two_cycle_recurring"] if cycles else ["annual", "perennial", "undetermined"]
        result[scope] = {"eligible_parcel_count": len(rows), "class_counts": {k: int(series.eq(k).sum()) for k in classes},
                         "household_count": int(all_rows.household_agriculture.sum()),
                         "road_excluded_count": int(all_rows.road_excluded.sum())}
    return result


def reuse_tile_geometry(frame, baseline_dir, baseline_manifest):
    """Change two MVT properties while preserving all geometry command bytes."""
    source = baseline_dir / "dist" / baseline_manifest["tile_url"].lstrip("/").split("/{z}")[0]
    lookup = {int(row.public_parcel_id): (str(row.crop_type_candidate), str(row.annual_cycle))
              for row in frame.itertuples(index=False)}
    delivery.TILE_OUTPUT.mkdir(parents=True, exist_ok=True)
    for number, path in enumerate(sorted(source.rglob("*.pbf")), 1):
        tile = vector_tile_pb2.tile()
        tile.ParseFromString(path.read_bytes())
        for layer in tile.layers:
            if layer.name != "land_analytics":
                raise ValueError("Unexpected baseline MVT layer")
            keys = {name: i for i, name in enumerate(layer.keys)}
            for name in ("crop_type", "annual_cycle"):
                if name not in keys:
                    keys[name] = len(layer.keys)
                    layer.keys.append(name)
            changed_keys = {keys["crop_type"], keys["annual_cycle"]}
            strings = {v.string_value: i for i, v in enumerate(layer.values) if v.HasField("string_value")}
            for value in ("", "annual", "perennial", "undetermined", "single_cycle", "two_cycle_recurring"):
                if value not in strings:
                    strings[value] = len(layer.values)
                    layer.values.add().string_value = value
            for feature in layer.features:
                kind, cycle = lookup[int(feature.id)]
                tags = list(feature.tags)
                kept = [item for offset in range(0, len(tags), 2) if tags[offset] not in changed_keys for item in tags[offset:offset+2]]
                kept.extend([keys["crop_type"], strings[kind], keys["annual_cycle"], strings[cycle]])
                del feature.tags[:]
                feature.tags.extend(kept)
        target = delivery.TILE_OUTPUT / path.relative_to(source)
        payload = tile.SerializeToString()
        if target.exists():
            if target.read_bytes() != payload:
                raise ValueError("Existing review tile differs; no overwrite")
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(payload)
        if number % 100 == 0:
            print(f"Updated classification attributes in {number}/370 preserved tiles", flush=True)
    return delivery.inspect_existing_tiles(frame.to_crs(3857))


def verify_delivery(baseline_dir, baseline_manifest, selected):
    def table(path):
        with sqlite3.connect(path.as_uri() + "?mode=ro", uri=True) as con:
            return pd.read_sql_query("SELECT * FROM parcel_analytics", con).set_index("cadastre_code").sort_index()
    old = table(baseline_dir / baseline_manifest["index"])
    new = table(delivery.INDEX_PATH)
    if len(new) != 43984 or not new.index.equals(old.index):
        raise ValueError("Delivered cadastral population changed")
    for name in old.columns:
        if not name.startswith("crop_") and not name.startswith("annual_cycle") and name != "cycle_assessed_year_count":
            pd.testing.assert_series_equal(old[name], new[name], check_dtype=False)
    excluded = new.household.astype(bool) | new.road_excluded.astype(bool)
    if int(new.household.sum()) != 20096 or int(new.road_excluded.sum()) != 1086 or not new.loc[excluded, "crop_type"].isna().all():
        raise ValueError("Delivered exclusions changed")
    fields = new.loc[~excluded]
    expected = selected.set_index("cadastre_code").loc[fields.index]
    if not fields.crop_type.eq(expected.crop_type_candidate).all():
        raise ValueError("Delivered type differs from selected area results")
    expected_cycle = expected.annual_cycle_candidate.map({"single_cycle": "single_cycle", "two_cycles": "two_cycle_recurring"})
    if not fields.annual_cycle.fillna("").eq(expected_cycle.fillna("")).all():
        raise ValueError("Delivered cycle differs from owner assignment")
    summary = read(delivery.SUMMARY_PATH)
    previous_summary = read(baseline_dir / baseline_manifest["summary"])
    for section in ("activity", "history"):
        if summary[section] != previous_summary[section]:
            raise ValueError("Unrelated summary changed")
    for scope, section in summary["land_use_type"]["summaries"].items():
        rows = fields if scope == "lower_hrazdan" else fields.loc[fields.activity_stage.eq(scope)]
        if section["class_counts"] != {k: int(rows.crop_type.eq(k).sum()) for k in ("annual", "perennial", "undetermined")}:
            raise ValueError("Public summary counts differ")
    old_root = baseline_dir / "dist" / baseline_manifest["tile_url"].lstrip("/").split("/{z}")[0]
    allowed = {"stage", "activity_class", "activity_fraction_bp", "activity_state", "household", "road_excluded", "history_class", "crop_type", "annual_cycle"}
    checked = 0
    for path in sorted(delivery.TILE_OUTPUT.rglob("*.pbf")):
        relative = path.relative_to(delivery.TILE_OUTPUT)
        previous = {f["id"]: f for f in mapbox_vector_tile.decode((old_root / relative).read_bytes())["land_analytics"]["features"]}
        features = mapbox_vector_tile.decode(path.read_bytes())["land_analytics"]["features"]
        if len(features) != len(previous):
            raise ValueError("Tile parcel count changed")
        for feature in features:
            original = previous[feature["id"]]
            if feature["geometry"] != original["geometry"] or set(feature["properties"]) != allowed:
                raise ValueError("Tile geometry or public schema changed")
            for key, value in feature["properties"].items():
                if key not in ("crop_type", "annual_cycle") and value != original["properties"][key]:
                    raise ValueError("Unrelated tile property changed")
        checked += 1
    if checked != 370:
        raise ValueError("Incomplete tile delivery")
    return {"status": "delivery_checks_passed", "parcels": len(new), "eligible": len(fields), "tiles_checked": checked,
            "geometry_codes_official_areas_and_other_attributes_preserved": True, "public_schema_checked": True,
            "working_release_unchanged": True, "visual_review": "pending"}


def main():
    if (ROOT / "config" / f"{SLUG}.review.lock.json").exists():
        raise ValueError("Review already sealed; no overwrite")
    complete = read(AREA / "complete.json")
    for relative, digest in complete["outputs"].items():
        if sha256(AREA / relative) != digest:
            raise ValueError("Completed area result changed")
    verify_release(ROOT, "working")
    baseline_dir, baseline_manifest = read_release(ROOT, "working")
    basis_path = ROOT / "data/analysis/parcel_eo/current_halo_v1/current_parcels.parquet"
    geo = gpd.read_parquet(basis_path)
    with sqlite3.connect((baseline_dir / baseline_manifest["index"]).as_uri() + "?mode=ro", uri=True) as con:
        old = pd.read_sql_query("SELECT * FROM parcel_analytics", con)
    old = old.drop(columns=[c for c in old if c.startswith("crop_") or c.startswith("annual_cycle") or c == "cycle_assessed_year_count"])
    frame = geo[["internal_parcel_id", "public_parcel_id", "cadastre_code", "area_official_m2", "geometry"]].merge(old, on="cadastre_code", validate="one_to_one")
    frame = frame.rename(columns={"activity_stage": "stage", "activity_state": "preview_state", "household": "household_agriculture"})
    for name in ("household_agriculture", "road_excluded"):
        frame[name] = frame[name].astype(bool)
    selected = pd.read_parquet(AREA / "parcels.parquet")
    lookup = selected.set_index("internal_parcel_id")
    excluded = frame.household_agriculture | frame.road_excluded
    if len(frame) != 43984 or set(frame.loc[~excluded, "internal_parcel_id"]) != set(lookup.index):
        raise ValueError("Area and preserved exclusions disagree")
    fields = frame.internal_parcel_id.map(lookup.crop_type_candidate).fillna("")
    frame["crop_type_candidate"] = fields
    frame["annual_cycle"] = frame.internal_parcel_id.map(lookup.annual_cycle_candidate).map({"single_cycle": "single_cycle", "two_cycles": "two_cycle_recurring"}).fillna("")
    frame["crop_profile_year_count"] = frame.internal_parcel_id.map(lookup.assessable_years).fillna(0).astype(int)
    frame["assessed_year_count"] = frame.crop_profile_year_count
    seasons = pd.concat([pd.read_parquet(AREA / "seasons" / f"{y}.parquet") for y in range(2021, 2026)])
    seasons = seasons.sort_values(["internal_parcel_id", "year"])
    year_types = seasons.assign(code=seasons.crop_type_candidate.map({"undetermined": 0, "annual": 1, "perennial": 2})).groupby("internal_parcel_id").code.apply(list)
    year_cycles = seasons.assign(code=seasons.annual_cycle_candidate.map({"single_cycle": 1, "two_cycles": 2}).fillna(0).astype(int)).groupby("internal_parcel_id").code.apply(list)
    frame["crop_type_year_codes"] = frame.internal_parcel_id.map(year_types).map(lambda x: x if isinstance(x, list) else [])
    frame["annual_cycle_year_codes"] = frame.internal_parcel_id.map(year_cycles).map(lambda x: x if isinstance(x, list) else [])
    frame["history_class"] = frame.history_class.fillna("")
    frame["annual_state_codes"] = frame.annual_state_codes.map(lambda x: json.loads(x) if x else [])
    for name in ("profile_year_count", "used_year_count", "source_year_count"):
        frame[name] = frame[name].fillna(0).astype(int)
    REVIEW.mkdir(parents=True, exist_ok=True)
    frame.drop(columns="geometry").to_parquet(REVIEW / "delivery_attributes.parquet", index=False)
    metrics = reuse_tile_geometry(frame, baseline_dir, baseline_manifest)
    if not delivery.INDEX_PATH.exists():
        delivery.build_index(frame)
    summary = read(baseline_dir / baseline_manifest["summary"])
    summary.update(delivery_version=delivery.DELIVERY_VERSION, generated_at_utc=datetime.now(timezone.utc).isoformat(),
                   tile_delivery={"url": f"/data/land_analytics/{SLUG}/{{z}}/{{x}}/{{y}}.pbf", "layer_name": "land_analytics", "min_zoom": 10, "max_zoom": 15, "bounds": metrics["bounds_wgs84"]},
                   land_use_type={"analysis_version": SLUG, "years": list(range(2021, 2026)), "status": "draft_owner_review", "summaries": summary_counts(frame)},
                   annual_cycles={"analysis_version": SLUG, "years": list(range(2021, 2026)), "summaries": summary_counts(frame, True)})
    write(delivery.SUMMARY_PATH, summary)
    result = verify_delivery(baseline_dir, baseline_manifest, selected)
    write(REVIEW / "delivery_verification.json", result)
    # Reuse the existing classification review UI byte-for-byte.
    existing_slug = "predominant_use_v2_20260905"
    existing = ROOT / "output" / f"frontend_{existing_slug}"
    old_lock = read(ROOT / "config" / f"{existing_slug}.lock.json")
    for relative, record in old_lock["files"].items():
        if sha256(ROOT / relative) != record["sha256"]:
            raise ValueError("Preserved review UI/runtime differs")
    if FRONTEND.exists():
        raise ValueError("New review frontend already exists; preserve it")
    FRONTEND.mkdir()
    for child in existing.iterdir():
        if child.name == "data":
            continue
        if child.is_dir():
            shutil.copytree(child, FRONTEND / child.name)
        else:
            shutil.copy2(child, FRONTEND / child.name)
    for child in (existing / "data").iterdir():
        if child.name == "land_analytics":
            continue
        target = FRONTEND / "data" / child.name
        target.parent.mkdir(parents=True, exist_ok=True)
        if child.is_dir():
            shutil.copytree(child, target)
        else:
            shutil.copy2(child, target)
    shutil.copytree(delivery.TILE_OUTPUT, FRONTEND / "data/land_analytics" / SLUG)
    required = [ROOT / "app.py", ROOT / "run_transitions_area_review.py", ROOT / "server_data/cadastre_search.sqlite3",
                delivery.INDEX_PATH, delivery.SUMMARY_PATH]
    files = set(required) | {p for p in FRONTEND.rglob("*") if p.is_file()}
    lock = {"version": SLUG, "port": 8526, "host": "127.0.0.1", "working_port": 8525,
            "approval": "owner_requested_area_application_local_visual_review", "files": {
                p.relative_to(ROOT).as_posix(): {"bytes": p.stat().st_size, "sha256": sha256(p)} for p in sorted(files)}}
    write(ROOT / "config" / f"{SLUG}.review.lock.json", lock)
    print(json.dumps(result, indent=2), flush=True)


if __name__ == "__main__":
    main()
