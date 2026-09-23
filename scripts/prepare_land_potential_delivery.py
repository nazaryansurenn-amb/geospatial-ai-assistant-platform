"""Extend a copied review UI and MVT delivery, preserving the sealed prior version."""
from __future__ import annotations
import json
import shutil
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import pandas as pd
from mapbox_vector_tile.Mapbox import vector_tile_pb2
from wp_core.land_potential import OUT, REVIEW, SLUG, read, write, sha, verify

BASE_SLUG = "transitions_area_20260906_v1"
BASE = ROOT / "output" / f"frontend_{BASE_SLUG}"
FRONTEND = ROOT / "output" / f"frontend_{SLUG}"
SOURCE = REVIEW / "frontend_source"
INDEX = ROOT / "server_data" / f"land_analytics_{SLUG}.sqlite3"
SUMMARY = ROOT / "server_data" / f"land_analytics_summary_{SLUG}.json"


def replace_once(text, old, new):
    if text.count(old) != 1:
        raise ValueError(f"Source anchor count {text.count(old)}: {old[:100]}")
    return text.replace(old, new, 1)


def prepare():
    verify()
    if INDEX.exists() or SOURCE.exists():
        raise ValueError("Preparation exists; preserve completed preparation")
    lock = read(ROOT / "config" / f"{BASE_SLUG}.review.lock.json")
    for name, record in lock["files"].items():
        assert sha(ROOT / name) == record["sha256"], name
    frame = pd.read_parquet(OUT / "all_parcels.parquet")
    shutil.copy2(ROOT / "server_data" / f"land_analytics_{BASE_SLUG}.sqlite3", INDEX)
    with sqlite3.connect(INDEX) as c:
        c.execute("ALTER TABLE parcel_analytics ADD COLUMN potential_class TEXT NOT NULL DEFAULT 'not_candidate'")
        c.executemany("UPDATE parcel_analytics SET potential_class=? WHERE cadastre_code=?", list(frame[["potential_class", "cadastre_code"]].itertuples(index=False, name=None)))
    summary = read(ROOT / "server_data" / f"land_analytics_summary_{BASE_SLUG}.json")
    summary["delivery_version"] = SLUG
    summary["tile_delivery"]["url"] = f"/data/land_analytics/{SLUG}/{{z}}/{{x}}/{{y}}.pbf"
    report = read(OUT / "report.json")
    summary["potential"] = {"analysis_version": SLUG, "years": [2021, 2022, 2023, 2024, 2025],
                            "current_observation_date": "2026-08-23", "status": "draft_owner_review",
                            "scope": "saved_lower_hrazdan_I_II_only", "expansion_assessed": False,
                            "summaries": report["summaries"]}
    write(SUMMARY, summary)
    tile_source = BASE / "data/land_analytics" / BASE_SLUG
    tile_output = REVIEW / "tiles"
    # Python int preserves the 53-bit public IDs on Windows (numpy int is 32-bit).
    lookup = {int(k): v for k, v in zip(frame.public_parcel_id, frame.potential_class)}
    tiles = 0
    for path in sorted(tile_source.rglob("*.pbf")):
        tile = vector_tile_pb2.tile()
        tile.ParseFromString(path.read_bytes())
        for layer in tile.layers:
            assert layer.name == "land_analytics"
            assert "potential_class" not in layer.keys
            key = len(layer.keys)
            layer.keys.append("potential_class")
            values = {}
            for name in sorted(set(lookup.values())):
                values[name] = len(layer.values)
                layer.values.add().string_value = name
            for feature in layer.features:
                feature.tags.extend([key, values[lookup[feature.id]]])
        dst = tile_output / path.relative_to(tile_source)
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_bytes(tile.SerializeToString())
        tiles += 1
    assert tiles == 370
    SOURCE.mkdir(parents=True)
    shutil.copytree(ROOT / "src", SOURCE / "src")
    shutil.copy2(ROOT / "index.html", SOURCE / "index.html")
    shutil.copy2(ROOT / "package.json", SOURCE / "package.json")
    src = (SOURCE / "src/App.jsx").read_text(encoding="utf-8")
    src = replace_once(src, 'import { LAND_MODES, LAND_SCOPES } from "./landResourcesSchema";',
        'import { LAND_MODES, LAND_SCOPES } from "./landResourcesSchema";\nimport PotentialView, { POTENTIAL_LABELS } from "./PotentialView";')
    src = replace_once(src, '  : "activity_2026";\nconst CADASTRE_LAYERS',
        '  : new URLSearchParams(window.location.search).get("view") === "potential" ? "potential" : "activity_2026";\nconst CADASTRE_LAYERS')
    src = replace_once(src, '  const [historyPayload, setHistoryPayload] = useState(null);',
        '  const [historyPayload, setHistoryPayload] = useState(null);\n  const [potentialPayload, setPotentialPayload] = useState(null);')
    src = replace_once(src, '        setHistoryPayload(payload.history);',
        '        setHistoryPayload(payload.history);\n        setPotentialPayload(payload.potential || null);')
    src = replace_once(src, '          cropType: analytics.cropType,',
        '          cropType: analytics.cropType,\n          potential: analytics.potential,')
    src = replace_once(src, '  } else if (parcel.mode === "history_2021_2025" && parcel.history) {', '''  } else if (parcel.mode === "potential" && parcel.potential) {
    const p = document.createElement("p");
    p.className = "parcel-popup-history-class";
    p.textContent = POTENTIAL_LABELS[parcel.potential.potentialClass] || "Ներուժի թեկնածու չի առանձնացվել";
    content.appendChild(p);
    const note = document.createElement("p");
    note.textContent = "Նախնական դիտարկում․ օգտագործման և ոռոգման հնարավորությունը ենթակա է տեղում ստուգման։";
    content.appendChild(note);
  } else if (parcel.mode === "history_2021_2025" && parcel.history) {''')
    src = replace_once(src, '(landMode === "history_2021_2025" && historyStatus === "ready"));',
        '(landMode === "history_2021_2025" && historyStatus === "ready") ||\n        (landMode === "potential" && Boolean(potentialPayload)));')
    src = replace_once(src, '}, [activityPreviewVisible, cropTypeStatus, historyStatus, landMode, landScope, mapReady]);',
        '}, [activityPreviewVisible, cropTypeStatus, historyStatus, landMode, landScope, mapReady, potentialPayload]);')
    src = replace_once(src, '              <div className="land-class-list" aria-label={activeLandMode.label}>',
        '''              <PotentialView map={mapRef.current} ready={mapReady} payload={potentialPayload} scope={landScope} active={landMode === "potential"} />
              <div className="land-class-list" aria-label={activeLandMode.label} style={landMode === "potential" ? {display: "none"} : undefined}>''')
    src = replace_once(src, '{landMode === "activity_2026"\n                  ? "Նախնական դիտարկում',
        '{landMode === "potential" ? "Նախնական թեկնածուներ․ ոռոգման հնարավորությունը դեռ հաստատված չէ" : landMode === "activity_2026"\n                  ? "Նախնական դիտարկում')
    (SOURCE / "src/App.jsx").write_text(src, encoding="utf-8")
    shutil.copy2(ROOT / "scripts/land_potential_PotentialView.jsx", SOURCE / "src/PotentialView.jsx")
    # No public source tree is copied. Existing allowlisted map assets are reused after build.
    config = '''import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
export default defineConfig({plugins:[react()], publicDir:false, build:{outDir:OUTPUT, emptyOutDir:false, rollupOptions:{output:{manualChunks:{"react-vendor":["react","react-dom"],"map-engine":["maplibre-gl"]}}}}});
'''.replace("OUTPUT", json.dumps(str(FRONTEND)))
    (SOURCE / "vite.config.mjs").write_text(config, encoding="utf-8")
    write(REVIEW / "preparation.json", {"tiles": tiles, "source": SOURCE.relative_to(ROOT).as_posix(), "status": "prepared_for_build"})
    print(json.dumps({"prepared_tiles": tiles, "frontend_source": str(SOURCE)}), flush=True)


def finish():
    assert (FRONTEND / "index.html").exists()
    if (FRONTEND / "data").exists():
        raise ValueError("Public delivery already copied")
    for child in (BASE / "data").iterdir():
        if child.name == "land_analytics":
            continue
        dst = FRONTEND / "data" / child.name
        dst.parent.mkdir(parents=True, exist_ok=True)
        if child.is_dir():
            shutil.copytree(child, dst)
        else:
            shutil.copy2(child, dst)
    shutil.copytree(REVIEW / "tiles", FRONTEND / "data/land_analytics" / SLUG)
    verify_delivery()
    paths = [ROOT / "app.py", ROOT / "run_land_potential_review.py", INDEX, SUMMARY,
             ROOT / "server_data/cadastre_search.sqlite3", *[p for p in FRONTEND.rglob("*") if p.is_file()]]
    write(ROOT / "config" / f"{SLUG}.review.lock.json", {"version": SLUG, "port": 8526,
        "files": {p.relative_to(ROOT).as_posix(): {"bytes": p.stat().st_size, "sha256": sha(p)} for p in paths}})


def verify_delivery():
    old_index = ROOT / "server_data" / f"land_analytics_{BASE_SLUG}.sqlite3"
    with sqlite3.connect(old_index.as_uri()+"?mode=ro", uri=True) as c:
        old = pd.read_sql_query("SELECT * FROM parcel_analytics ORDER BY cadastre_code", c)
    with sqlite3.connect(INDEX.as_uri()+"?mode=ro", uri=True) as c:
        new = pd.read_sql_query("SELECT * FROM parcel_analytics ORDER BY cadastre_code", c)
    pd.testing.assert_frame_equal(old, new[old.columns])
    summary, old_summary = read(SUMMARY), read(ROOT / "server_data" / f"land_analytics_summary_{BASE_SLUG}.json")
    for k in ["activity", "history", "land_use_type", "annual_cycles"]:
        assert summary[k] == old_summary[k]
    source = BASE / "data/land_analytics" / BASE_SLUG
    target = FRONTEND / "data/land_analytics" / SLUG
    count = 0
    for p in sorted(source.rglob("*.pbf")):
        a, b = vector_tile_pb2.tile(), vector_tile_pb2.tile()
        a.ParseFromString(p.read_bytes()); b.ParseFromString((target / p.relative_to(source)).read_bytes())
        assert len(a.layers) == len(b.layers)
        for la, lb in zip(a.layers, b.layers):
            assert list(lb.keys) == [*la.keys, "potential_class"]
            assert len(la.features) == len(lb.features)
            assert [x.SerializeToString() for x in la.values] == [x.SerializeToString() for x in lb.values[:len(la.values)]]
            for fa, fb in zip(la.features, lb.features):
                assert fa.id == fb.id and fa.type == fb.type and list(fa.geometry) == list(fb.geometry)
                assert list(fa.tags) == list(fb.tags)[:-2]
        count += 1
    assert count == 370
    lock = read(ROOT / "config" / f"{BASE_SLUG}.review.lock.json")
    for name, record in lock["files"].items():
        assert sha(ROOT / name) == record["sha256"], name
    write(REVIEW / "delivery_verification.json", {"status": "passed", "tiles": count,
         "all_prior_database_values_and_tile_geometry_preserved": True,
         "prior_review_seal_unchanged": True, "public_attribute_added": "potential_class"})


if __name__ == "__main__":
    {"prepare": prepare, "finish": finish, "verify": verify_delivery}[sys.argv[1]]()
