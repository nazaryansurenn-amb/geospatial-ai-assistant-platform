"""Map layers v2: the satellite map over the terrain-routed network v3 (owner-approved design section 5).

Same layers and checks as scripts/canal_ops_map_layers_v1.py (parcels, communities, WUA outline, observed zone, real main
canal, network, pumping areas, connectors), built from config/canal_ops_map_layers_v2.json into
data/analysis/canal_ops_sim_v1/map_v2/. What changes:
  - canals are merged lines drawn downstream (flow arrows from zoom 14), with a casing so they stand out on imagery;
  - colour-blind safe colours by level and by supply: main canal blue with a white halo, gravity secondaries and
    tertiaries lighter blues, canals below a pump station orange, rising mains red-orange dashed, canals of
    terrain-uncertain units with a grey dashed overlay;
  - one pump station symbol per lifted land cluster (owner): the gates below it are ordinary gates in pump orange;
  - draw order parcels, areas, tertiary, secondary, main canal, structures; parcel fills at 30 %.
v1 and its outputs are not changed.

  canal_ops_map_layers_v2.py build   -> data/analysis/canal_ops_sim_v1/map_v2/
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pandas as pd
import shapely

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))
import canal_ops_map_layers_v1 as m1  # noqa: E402
from wp_core.read_guard import ReadGuard  # noqa: E402

CFG_PATH = ROOT / "config/canal_ops_map_layers_v2.json"
CFG = json.loads(CFG_PATH.read_text(encoding="utf-8"))
OUT = ROOT / CFG["output"]
ORDER = {"fill": 0, "area": 1, "tertiary": 2, "secondary": 3, "main": 4, "structure": 5}


def pumped_canals(nodes):
    """Canals that carry pumped water: the secondary below a main-canal pump, the tertiary below a secondary pump."""
    nd = nodes.set_index("node")
    out = set()
    for n, r in nd.iterrows():
        if r.kind in ("secondary_canal", "tertiary_canal") and isinstance(r.parent, str) and r.parent in nd.index \
                and nd.at[r.parent, "kind"] in ("main_pump", "secondary_pump"):
            out.add(n)
    for n, r in nd.iterrows():                                   # tertiaries on a pumped secondary are pumped too
        if r.kind == "tertiary_canal" and isinstance(r.parent, str) and nd.at[r.parent, "parent"] in out:
            out.add(n)
    return out


def merged(g):
    """Segments of one canal merged into downstream lines (direction kept)."""
    segs = shapely.linestrings([[[a, b], [c, d]] for a, b, c, d in zip(g.lon0, g.lat0, g.lon1, g.lat1)])
    m = shapely.line_merge(shapely.multilinestrings(segs), directed=True)
    parts = list(m.geoms) if m.geom_type == "MultiLineString" else [m]
    return [[[round(x, 6), round(y, 6)] for x, y in p.coords] for p in parts]


def network_layer(nodes, edges, cfg=CFG):
    feats = []
    nd = nodes.set_index("node")
    pumped = pumped_canals(nodes)
    for (canal, tier), g in edges.groupby(["canal", "tier"], sort=True):
        r = nd.loc[canal]
        kind = "rising_main" if tier == "rising_main" else f"{tier}_canal"
        feats.append({"type": "Feature", "properties": {
            "kind": kind, "node": canal, "label": r.label, "community": r.community, "ha": round(float(r.ha), 2),
            "capacity_lps": None if pd.isna(r.capacity_lps) else float(r.capacity_lps),
            "terrain_state": r.terrain_state if isinstance(r.terrain_state, str) else None, "section": r.section,
            "pumped": canal in pumped, "synthetic": True},
            "geometry": {"type": "MultiLineString", "coordinates": merged(g)}})
    pts = nodes[~nodes.kind.isin(["main_canal", "secondary_canal", "tertiary_canal"])]
    pump_fed = {n for n in nodes.node if any(n.startswith(c + "-") for c in pumped)}
    for r in pts.itertuples():
        props = {"kind": r.kind, "node": r.node, "label": r.label, "community": r.community, "ha": round(float(r.ha), 2),
                 "parent": r.parent, "terrain_state": r.terrain_state if isinstance(r.terrain_state, str) else None,
                 "capacity_lps": None if pd.isna(r.capacity_lps) else float(r.capacity_lps), "pump": bool(r.pump),
                 "pumped": r.node in pump_fed, "section": r.section, "synthetic": True}
        if bool(r.pump):
            props.update({"pump_units": None if pd.isna(r.pump_units) else int(r.pump_units),
                          "run_windows": r.run_windows, "pump_basis": r.pump_basis})
        feats.append({"type": "Feature", "properties": props,
                      "geometry": {"type": "Point", "coordinates": [round(float(r.lon), 6), round(float(r.lat), 6)]}})
    return m1.fc(feats, name="network_v3", label="Synthetic terrain-routed network on the real main canal", synthetic=True)


def canal_layer(net_summary, cfg=CFG):
    """The real canal stages; v3's terrain covers the whole canal, so there is no 'terrain not covered' stretch."""
    raw = json.loads(m1.inp("canal", cfg).read_text(encoding="utf-8"))["features"]
    feats, lengths = [], {}
    for f in raw:
        st = f["properties"]["stage"]
        g = shapely.geometry.shape(f["geometry"])
        gm = m1.to_m([g], cfg).iloc[0]
        lab = "I" if st == "stage_1" else "II"
        lengths[lab] = round(gm.length, 1)
        line = g if lab == "I" else shapely.LineString(list(g.coords)[::-1])      # drawn downstream (arrows)
        props = {"kind": "main_canal", "node": lab, "stage": lab, "name_en": f["properties"].get("display_name_en"),
                 "name_hy": f["properties"].get("display_name_hy"), "length_m": round(gm.length, 1), "basis": "real main canal line"}
        if lab == "I":
            props["capacity_note"] = cfg["labels"]["main_capacity"]
        feats.append({"type": "Feature", "properties": props, "geometry": m1.rounded(shapely.geometry.mapping(line))})
    return m1.fc(feats, name="canal"), lengths


def layers_manifest(cfg=CFG):
    z, s = cfg["zoom"], cfg["styles"]
    pop_parcel = ["id", "t", "ha", "com", "o", "ts"]
    pop_node = ["label", "kind", "community", "ha", "capacity_lps", "terrain_state", "section"]
    net = "network.geojson.gz"
    L = [
        {"id": "wua_outline", "file": "wua_outline.geojson", "title": "Outline of the 44 communities", "type": "line", "visible": True,
         "style": s["wua_outline"], "order": ORDER["area"]},
        {"id": "communities", "file": "communities.geojson", "title": "Communities", "type": "line", "visible": True, "style": s["community"],
         "label_field": "name", "label_minzoom": z["labels_min"], "popup": ["name", "zone_parcels", "zone_ha"], "order": ORDER["area"]},
        {"id": "parcels", "file": "parcels.geojson.gz", "gzip": True, "title": "Parcels by land type (v1.5)", "type": "fill", "visible": True,
         "minzoom": z["parcels_min"], "color_field": "t", "legend": "legend", "popup": pop_parcel,
         "fill_opacity": cfg["parcels"]["fill_opacity"], "order": ORDER["fill"]},
        {"id": "observed_zone", "file": "observed_zone.geojson", "title": cfg["labels"]["zone"], "type": "line", "visible": True,
         "style": s["observed_zone"], "order": ORDER["area"]},
        {"id": "pumping_areas", "file": "pumping_areas.geojson", "filter": {"kind": "pumping_area"}, "title": "Pumping areas (terrain screening, synthetic)",
         "type": "fill", "visible": True, "style": s["pumping_area"], "popup": ["label", "ha", "parcels", "pump_units", "basis"], "order": ORDER["area"]},
        {"id": "terrain_uncertain", "file": "pumping_areas.geojson", "filter": {"kind": "terrain_uncertain_area"}, "title": "Terrain uncertain (+-3 m)",
         "type": "fill", "visible": False, "style": s["terrain_uncertain_area"], "popup": ["label", "ha", "parcels", "basis"], "order": ORDER["area"]},
        {"id": "tertiary_canals", "file": net, "gzip": True, "filter": {"kind": "tertiary_canal", "pumped": False}, "title": "Tertiary canals, gravity (synthetic)",
         "type": "line", "visible": True, "minzoom": z["tertiary_min"], "style": s["tertiary_canal"], "popup": pop_node, "order": ORDER["tertiary"]},
        {"id": "pumped_tertiary_canals", "file": net, "gzip": True, "filter": {"kind": "tertiary_canal", "pumped": True}, "title": "Tertiary canals below a pump (synthetic)",
         "type": "line", "visible": True, "minzoom": z["tertiary_min"], "style": s["pumped_tertiary_canal"], "popup": pop_node, "order": ORDER["tertiary"]},
        {"id": "uncertain_canals", "file": net, "gzip": True, "filter": {"kind": "tertiary_canal", "terrain_state": "terrain uncertain"},
         "title": "Canals on uncertain terrain (+-3 m)", "type": "line", "visible": True, "minzoom": z["tertiary_min"], "style": s["uncertain_canal"],
         "order": ORDER["tertiary"] + 0.5},
        {"id": "laterals", "file": "laterals.geojson.gz", "gzip": True, "title": "Outlet to parcel connectors", "type": "line", "visible": True,
         "minzoom": z["laterals_min"], "style": s["lateral"], "order": ORDER["tertiary"] - 0.5},
        {"id": "secondary_canals", "file": net, "gzip": True, "filter": {"kind": "secondary_canal", "pumped": False}, "title": "Secondary canals, gravity (synthetic)",
         "type": "line", "visible": True, "style": s["secondary_canal"], "popup": pop_node, "order": ORDER["secondary"]},
        {"id": "pumped_secondary_canals", "file": net, "gzip": True, "filter": {"kind": "secondary_canal", "pumped": True}, "title": "Secondary canals below a pump (synthetic)",
         "type": "line", "visible": True, "style": s["pumped_secondary_canal"], "popup": pop_node, "order": ORDER["secondary"]},
        {"id": "rising_mains", "file": net, "gzip": True, "filter": {"kind": "rising_main"}, "title": "Pump rising mains (synthetic)",
         "type": "line", "visible": True, "style": s["rising_main"], "popup": pop_node, "order": ORDER["secondary"] + 0.5},
        {"id": "main_canal", "file": "canal.geojson", "filter": {"kind": "main_canal"}, "title": "Lower Hrazdan canal, stages I and II (real line)",
         "type": "line", "visible": True, "style": s["main_canal"], "popup": ["name_en", "name_hy", "length_m", "capacity_note"], "order": ORDER["main"]},
        {"id": "outlets", "file": net, "gzip": True, "filter": {"kind": ["tertiary_gate", "direct_outlet"], "pumped": False}, "title": "Tertiary gates and direct outlets",
         "type": "symbol", "visible": True, "minzoom": z["outlets_min"], "style": s["tertiary_gate"], "popup": pop_node, "order": ORDER["structure"]},
        {"id": "pumped_outlets", "file": net, "gzip": True, "filter": {"kind": ["tertiary_gate", "direct_outlet"], "pumped": True}, "title": "Gates below a pump",
         "type": "symbol", "visible": True, "minzoom": z["outlets_min"], "style": s["pumped_gate"], "popup": pop_node, "order": ORDER["structure"]},
        {"id": "secondary_gates", "file": net, "gzip": True, "filter": {"kind": ["secondary_gate"]}, "title": "Secondary-canal gates",
         "type": "symbol", "visible": True, "minzoom": z["tertiary_min"], "style": s["secondary_gate"], "popup": pop_node, "order": ORDER["structure"] + 0.1},
        {"id": "tail_escapes", "file": net, "gzip": True, "filter": {"kind": ["tail_escape"]}, "title": "Tail escapes",
         "type": "symbol", "visible": True, "minzoom": z["tertiary_min"], "style": s["tail_escape"], "popup": ["label"], "order": ORDER["structure"] + 0.1},
        {"id": "main_gates", "file": net, "gzip": True, "filter": {"kind": ["main_gate"]}, "title": "Main-canal gates",
         "type": "symbol", "visible": True, "style": s["main_gate"], "label_field": "node", "popup": pop_node, "order": ORDER["structure"] + 0.2},
        {"id": "pump_stations", "file": net, "gzip": True, "filter": {"pump": True}, "title": "Pump stations, one per lifted area (whole units, night windows)",
         "type": "symbol", "visible": True, "style": s["pump_station"], "label_field": "node", "popup": pop_node + ["pump_units", "run_windows", "pump_basis"],
         "order": ORDER["structure"] + 0.3},
    ]
    return {"version": cfg["version"], "basemap": cfg["basemap"], "layers": L, "legend": cfg["legend"],
            "labels": cfg["labels"], "zoom": z, "draw_order": cfg["draw_order"],
            "strings_note": "English titles here; the page translates them (scripts/canal_ops_strings.json, native review pending)"}


def build(out=OUT, cfg=CFG):
    import geopandas as gpd
    out = Path(out)
    with ReadGuard() as guard:
        p = m1.load_parcels(cfg)
        comm = gpd.read_file(m1.inp("communities", cfg))
        cad = gpd.read_file(m1.inp("cadastre", cfg), columns=["cadastre_code", "area_official_m2", "centroid_in_wua"])
        nodes = pd.read_parquet(m1.inp("network_nodes", cfg))
        edges = pd.read_parquet(m1.inp("network_edges", cfg))
        net_summary = json.loads(m1.inp("network_summary", cfg).read_text(encoding="utf-8"))
        canal, lengths = canal_layer(net_summary, cfg)
    out.mkdir(parents=True, exist_ok=True)
    p_m = p.to_crs(cfg["crs_metric"])
    cad_m = cad.to_crs(cfg["crs_metric"])
    layers = {"parcels.geojson.gz": m1.parcels_layer(p, cfg), "communities.geojson": m1.communities_layer(comm, p, cfg)}
    layers["wua_outline.geojson"], wua = m1.wua_outline_layer(comm, cad_m, p_m, cfg)
    layers["observed_zone.geojson"], zone = m1.zone_layer(p_m, cfg)
    layers["canal.geojson"] = canal
    layers["network.geojson.gz"] = network_layer(nodes, edges, cfg)
    layers["pumping_areas.geojson"], station = m1.areas_layer(p_m, nodes, cfg)
    layers["laterals.geojson.gz"] = m1.laterals_layer(p, nodes)
    layers["layers.json"] = layers_manifest(cfg)
    for name, obj in layers.items():
        (m1.write_gz if name.endswith(".gz") else m1.write_json)(out / name, obj)
    summary = m1.summarize(p, comm, nodes, layers, wua, zone, lengths, station, cfg)
    ch = summary["checks"]
    ch.pop("canal_two_stages_and_not_covered")
    ch["canal_two_stages"] = sorted(f["properties"]["node"] for f in canal["features"]) == ["I", "II"]
    net = layers["network.geojson.gz"]["features"]
    pumps = [f for f in net if f["properties"].get("pump")]
    ch["one_pump_symbol_per_station"] = all(f["properties"]["kind"] in ("main_pump", "secondary_pump") for f in pumps)
    ch["pumping_areas_one_per_station"] = len([f for f in layers["pumping_areas.geojson"]["features"]
                                               if f["properties"]["kind"] == "pumping_area"]) <= len(pumps)
    ch["every_canal_style_distinct"] = len({json.dumps(cfg["styles"][k]["color"]) for k in
                                            ("main_canal", "secondary_canal", "tertiary_canal", "pumped_secondary_canal",
                                             "pumped_tertiary_canal", "rising_main")}) == 6
    summary["checks"]["no_codes_ids_or_forbidden_wording"] = not (bad := m1.public_text_problems(out))
    summary["public_text_problems"] = bad
    summary["pump_stations"] = len(pumps)
    summary["read_guard"] = {"violations": guard.violations, "files_read": len(guard.files)}
    summary["all_checks_pass"] = all(summary["checks"].values())
    m1.write_json(out / "summary.json", summary)
    m1.write_json(out / "manifest.json", {
        "version": cfg["version"], "script": "scripts/canal_ops_map_layers_v2.py", "script_sha256": m1.sha(Path(__file__)),
        "config": "config/canal_ops_map_layers_v2.json", "config_sha256": m1.sha(CFG_PATH),
        "helpers": {"scripts/canal_ops_map_layers_v1.py": m1.sha(ROOT / "scripts/canal_ops_map_layers_v1.py"),
                    "wp_core/read_guard.py": m1.sha(ROOT / "wp_core/read_guard.py")},
        "inputs": {k: m1.sha(ROOT / v) for k, v in cfg["inputs"].items() if k != "not_used"},
        "outputs": {f.name: m1.sha(f) for f in sorted(out.iterdir()) if f.name != "manifest.json"},
        "files_read": guard.files, "read_guard_violations": guard.violations, "not_used": cfg["inputs"]["not_used"]})
    return summary


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "build":
        s = build()
        print(json.dumps({k: s[k] for k in ("parcels", "network", "pumping_areas", "pump_stations", "checks", "all_checks_pass")},
                         indent=1, ensure_ascii=False))
    else:
        print(__doc__)
