"""Add a sanitized consolidation layer to a copied, preserved 8526 frontend."""
import json
from pathlib import Path
import shutil
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import geopandas as gpd
import pandas as pd
import mapbox_vector_tile
from mapbox_vector_tile.encoder import on_invalid_geometry_make_valid
from shapely.geometry import box
from prepare_land_analytics_delivery import tile_range, tile_bounds_3857, tile_width_m
import run_consolidation_priority as analysis
from release_tools import sha256
from run_observation_analysis import write_json
from wp_core.consolidation_delivery import project, PROPERTIES

SLUG = "consolidation_review_20260906_v1"
BASE_SLUG = "land_potential_1km_review_20260906_v2"
BASE = ROOT / "output" / f"frontend_{BASE_SLUG}"
REVIEW = ROOT / "server_data/review" / SLUG
SOURCE = REVIEW / "frontend_source"
FRONTEND = ROOT / "output" / f"frontend_{SLUG}"
PUBLIC_DIR = "data/consolidation/consolidation_strip_priority_20260906_v1"


def verify_previous():
    for name, record in json.loads((ROOT / "config" / f"{BASE_SLUG}.review.lock.json").read_text())["files"].items():
        assert sha256(ROOT / name) == record["sha256"], name
    analysis.verify()


def prepare():
    verify_previous()
    if SOURCE.exists() or FRONTEND.exists():
        raise ValueError("Preserve existing preparation")
    shutil.copytree(ROOT / "server_data/review" / BASE_SLUG / "frontend_source", SOURCE)
    shutil.copytree(BASE / "data", FRONTEND / "data")
    tiles()
    write_json(REVIEW / "preparation.json", {"analysis_seal": sha256(analysis.OUT / "complete.json"),
        "base_review_seal": sha256(ROOT / "config" / f"{BASE_SLUG}.review.lock.json"),
        "approved_fields": sorted(PROPERTIES), "new_features": 31, "member_parcels": 262})
    print(json.dumps({"source": str(SOURCE)}))


def tiles():
    if (ROOT / "config" / f"{SLUG}.review.lock.json").exists():
        raise ValueError("Sealed review cannot be rebuilt")
    collection, summary, lookup = project(gpd.read_parquet(analysis.OUT / "blocks.parquet"), pd.read_parquet(analysis.OUT / "members.parquet"))
    groups = gpd.GeoDataFrame.from_features(collection, crs=4326)
    summary["groups"] = [{**r, "bbox": list(geom.bounds)} for r, geom in zip(groups.drop(columns="geometry").to_dict("records"), groups.geometry)]
    summary["tile_delivery"] = {"url": f"/{PUBLIC_DIR}/{{z}}/{{x}}/{{y}}.pbf", "layer_name": "consolidation", "min_zoom": 8, "max_zoom": 15, "bounds": list(groups.total_bounds)}
    metric = groups.to_crs(3857)
    target = FRONTEND / PUBLIC_DIR
    if target.exists():
        raise ValueError("Tile preparation already exists")
    seen, total_bytes, count = set(), 0, 0
    for z in range(8, 16):
        xs, ys = tile_range(tuple(metric.total_bounds), z)
        for x in xs:
            for y in ys:
                bounds = tile_bounds_3857(z, x, y)
                buffer = tile_width_m(z) * 8 / 4096
                clip = box(bounds[0] - buffer, bounds[1] - buffer, bounds[2] + buffer, bounds[3] + buffer)
                features = []
                for r in metric.itertuples():
                    if not r.geometry.intersects(clip):
                        continue
                    display = r.geometry.intersection(clip).simplify(tile_width_m(z) / 4096 * .25, preserve_topology=True)
                    if display.is_empty:
                        continue
                    props = {k: getattr(r, k) for k in PROPERTIES}
                    features.append({"id": int(r.group_number), "geometry": display, "properties": props})
                payload = mapbox_vector_tile.encode({"name": "consolidation", "features": features}, default_options={"quantize_bounds": bounds, "extents": 4096, "on_invalid_geometry": on_invalid_geometry_make_valid})
                for f in mapbox_vector_tile.decode(payload)["consolidation"]["features"]:
                    assert set(f["properties"]) == PROPERTIES
                    seen.add(f["id"])
                p = target / str(z) / str(x) / f"{y}.pbf"
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_bytes(payload)
                total_bytes += len(payload)
                count += 1
    assert seen == set(range(1, 32))
    write_json(REVIEW / "summary.json", summary)
    write_json(REVIEW / "parcel_lookup.json", lookup)
    write_json(REVIEW / "tile_verification.json", {"groups": len(seen), "tiles": count, "bytes": total_bytes, "display_only_quarter_tile_unit_simplification": True})
    print(json.dumps({"tiles": count, "bytes": total_bytes, "groups": len(seen)}))


def finish():
    verify_previous()
    assert (FRONTEND / "index.html").exists()
    for p in (BASE / "data").rglob("*"):
        if p.is_file():
            assert sha256(p) == sha256(FRONTEND / p.relative_to(BASE)), str(p)
    expected, summary, lookup = project(gpd.read_parquet(analysis.OUT / "blocks.parquet"), pd.read_parquet(analysis.OUT / "members.parquet"))
    saved_summary = json.loads((REVIEW / "summary.json").read_text())
    assert all(saved_summary[k] == v for k, v in summary.items())
    assert lookup == json.loads((REVIEW / "parcel_lookup.json").read_text())
    expected_props = {f["id"]: f["properties"] for f in expected["features"]}
    seen = set()
    for p in (FRONTEND / PUBLIC_DIR).rglob("*.pbf"):
        for f in mapbox_vector_tile.decode(p.read_bytes())["consolidation"]["features"]:
            assert f["properties"] == expected_props[f["id"]]
            seen.add(f["id"])
    assert seen == set(expected_props)
    prior_names = {p.relative_to(BASE).as_posix() for p in (BASE / "data").rglob("*") if p.is_file()}
    delivered = {p.relative_to(FRONTEND).as_posix() for p in FRONTEND.rglob("*") if p.is_file()}
    assert all(n in prior_names or (n.startswith(PUBLIC_DIR + "/") and n.endswith(".pbf")) or n == "index.html" or (n.startswith("assets/") and n.endswith((".js", ".css"))) for n in delivered)
    files = [ROOT / "app.py", ROOT / "run_consolidation_review.py", ROOT / "wp_core/consolidation_delivery.py",
        REVIEW / "summary.json", REVIEW / "parcel_lookup.json", *[p for p in SOURCE.rglob("*") if p.is_file()],
        *[p for p in FRONTEND.rglob("*") if p.is_file()]]
    write_json(ROOT / "config" / f"{SLUG}.review.lock.json", {"version": SLUG, "base": BASE_SLUG, "port": 8526,
        "files": {p.relative_to(ROOT).as_posix(): {"bytes": p.stat().st_size, "sha256": sha256(p)} for p in files}})
    write_json(REVIEW / "delivery_verification.json", {"passed": True, "features": len(seen), "members": len(lookup),
        "all_prior_map_data_unchanged": True,
        "prior_analysis_and_review_seals_unchanged": True, "new_public_properties": sorted(PROPERTIES)})
    print("Delivery verified and sealed")


if __name__ == "__main__":
    {"prepare": prepare, "tiles": tiles, "finish": finish}[sys.argv[1]]()
