"""Map layers for the map-centred Irrigation Assistant (locked plan v6 section 1; 2c coverage and layers).

Builds every layer of the one satellite map the page and the agents share, from files inside the product only (the
SQL archive is not read; the build runs under wp_core.read_guard and lists every file it read in the manifest):

  parcels.geojson.gz        22,802 observed-zone cadastral parcels (geometry from the cadastre, simplified ~0.25 m),
                            coloured by land types v1.5 (two-level legend), with network outlet and terrain state.
                            Item ids only: cadastral codes are used for the join and never written.
  communities.geojson       the 44 community outlines, with observed-zone parcels and hectares per community
  wua_outline.geojson       outline of the 44 communities (slivers closed, small holes filled, large holes explained)
  observed_zone.geojson     "Lower Hrazdan observed zone, stages I and II", buffered in metres, area reported
  canal.geojson             the real main canal (stages I and II) and its "terrain not covered" stretch
  network.geojson.gz        synthetic tree network v2: secondary and tertiary canals, gates, pump stations, outlets,
                            tail escapes (labels with community names)
  pumping_areas.geojson     dissolved parcels served by each pump station (separate tint), and terrain-uncertain areas
  laterals.geojson.gz       outlet -> parcel connectors (zoom 13 and closer)
  layers.json               basemap, draw order, legend, zoom limits, popup fields, labels for the page
  summary.json (QA: reconciles to 22,802 parcels, 44 communities, 10,339.3 ha), manifest.json

  canal_ops_map_layers_v1.py build   -> data/analysis/canal_ops_sim_v1/map/
"""
from __future__ import annotations

import gzip
import hashlib
import json
import re
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import shapely
from shapely.geometry import mapping, shape

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from wp_core.read_guard import ReadGuard  # noqa: E402

CFG_PATH = ROOT / "config/canal_ops_map_layers_v1.json"
CFG = json.loads(CFG_PATH.read_text(encoding="utf-8"))
OUT = ROOT / "data/analysis/canal_ops_sim_v1/map"
CADASTRE_CODE = re.compile(r"\b\d{2}-\d{3}-\d{4}-\d{4}\b")
PARCEL_ID = re.compile(r"parcel_[0-9a-f]{16,}")
STATE_KEY = {"gravity": "g", "pumping": "p", "terrain uncertain": "u", "terrain not covered": "n"}


def sha(p):
    return hashlib.sha256(Path(p).read_bytes()).hexdigest()


def inp(key, cfg=CFG):
    return ROOT / cfg["inputs"][key]


def rounded(g, nd=6):
    """GeoJSON geometry with coordinates rounded to 6 decimals (about 10 cm)."""
    def r(c):
        return [round(c[0], nd), round(c[1], nd)] if isinstance(c[0], float) else [r(x) for x in c]
    return {"type": g["type"], "coordinates": r(g["coordinates"])}


def fc(features, **props):
    return {"type": "FeatureCollection", **props, "features": features}


def to_m(geoms, cfg=CFG):
    import geopandas as gpd
    return gpd.GeoSeries(geoms, crs="EPSG:4326").to_crs(cfg["crs_metric"])


def to_wgs(geoms, cfg=CFG):
    import geopandas as gpd
    return gpd.GeoSeries(geoms, crs=cfg["crs_metric"]).to_crs("EPSG:4326")


# ---------------------------------------------------------------- inputs
def load_parcels(cfg=CFG):
    """22,802 parcels: v1.5 class, official ha, item id, cadastral geometry, network v2 outlet and terrain state."""
    import geopandas as gpd
    lt = pd.read_parquet(inp("land_types", cfg), columns=["internal_parcel_id", "item_id", "type6", "area_ha"])
    ids = pd.read_parquet(inp("parcel_ids", cfg), columns=["internal_parcel_id", "cadastre_code"])
    cad = gpd.read_file(inp("cadastre", cfg), columns=["cadastre_code"])
    geo = cad.merge(ids, on="cadastre_code", how="inner").drop(columns="cadastre_code").set_index("internal_parcel_id")
    net = pd.read_parquet(inp("network_parcels", cfg), columns=["internal_parcel_id", "community", "status", "canal_demand",
                                                                 "main", "secondary", "unit", "outlet", "terrain_state", "lon", "lat", "loc_source"])
    d = lt.merge(net, on="internal_parcel_id", how="left").set_index("internal_parcel_id")
    d["geometry"] = geo.geometry.reindex(d.index)
    d["t"] = d.type6.fillna("NCI")
    return gpd.GeoDataFrame(d.drop(columns="type6"), geometry="geometry", crs="EPSG:4326")


def legend_classes(cfg=CFG):
    return {c["key"]: (g["key"], c) for g in cfg["legend"]["groups"] for c in g["classes"]}


# ---------------------------------------------------------------- layers
def parcels_layer(p, cfg=CFG):
    cls = legend_classes(cfg)
    simp = p.geometry.simplify(cfg["parcels"]["simplify_deg"], preserve_topology=True)
    feats = []
    for (pid, r), g in zip(p.iterrows(), simp):
        props = {"id": r.item_id, "t": r.t, "g": cls[r.t][0], "ha": round(float(r.area_ha), 3),
                 "com": r.community if isinstance(r.community, str) else "",
                 "r": r.status == "reached", "dem": r.canal_demand == "canal demand",
                 "o": r.outlet if isinstance(r.outlet, str) else None,
                 "u": r.unit if isinstance(r.unit, str) else None,
                 "ts": STATE_KEY.get(r.terrain_state) if isinstance(r.terrain_state, str) else None}
        feats.append({"type": "Feature", "properties": props, "geometry": rounded(mapping(g))})
    return fc(feats, name="parcels", source=cfg["legend"]["source"])


def communities_layer(comm, p, cfg=CFG):
    m = to_m(comm.geometry, cfg)
    by = p.groupby("community").area_ha.agg(["size", "sum"])
    feats = []
    for (_, r), gm in zip(comm.iterrows(), m):
        n = by.loc[r.name_hy] if r.name_hy in by.index else None
        feats.append({"type": "Feature", "properties": {
            "name": r.name_hy, "area_ha": round(gm.area / 1e4, 1),
            "zone_parcels": int(n["size"]) if n is not None else 0, "zone_ha": round(float(n["sum"]), 2) if n is not None else 0.0},
            "geometry": rounded(mapping(r.geometry))})
    return fc(feats, name="communities")


def holes_of(g):
    polys = g.geoms if g.geom_type == "MultiPolygon" else [g]
    return [shapely.Polygon(r) for poly in polys for r in poly.interiors]


def fill_small_holes(g, min_area):
    def fix(poly):
        keep = [r for r in poly.interiors if shapely.Polygon(r).area >= min_area]
        return shapely.Polygon(poly.exterior, keep)
    if g.geom_type == "MultiPolygon":
        return shapely.MultiPolygon([fix(q) for q in g.geoms])
    return fix(g)


def wua_outline_layer(comm, cad_m, study_m, cfg=CFG):
    """Outline of the 44 communities, with every remaining hole explained from the data."""
    w = cfg["wua_outline"]
    cm = to_m(comm.geometry, cfg)
    raw = shapely.union_all(cm.values)
    closed = raw.buffer(w["close_m"], join_style="mitre").buffer(-w["close_m"], join_style="mitre")
    raw_holes = holes_of(raw)
    out = fill_small_holes(closed, w["fill_holes_below_ha"] * 1e4)
    holes = []
    rep = shapely.point_on_surface(cad_m.geometry.values)
    srep = shapely.point_on_surface(study_m.geometry.values)
    for h in sorted(holes_of(out), key=lambda h: -h.area):
        inside = shapely.contains(h, rep)
        around = sorted(comm.name_hy[cm.buffer(1.0).intersects(h.exterior)].tolist())
        c = to_wgs([h.centroid], cfg).iloc[0]
        n_in, ha_in = int(inside.sum()), float(cad_m.area_official_m2[inside].sum() / 1e4)
        outside_flag = int((~cad_m.centroid_in_wua[inside]).sum())
        n_study = int(shapely.contains(h, srep).sum())
        holes.append({"area_ha": round(h.area / 1e4, 1), "centroid_lon": round(c.x, 5), "centroid_lat": round(c.y, 5),
                      "surrounded_by": around, "cadastral_parcels_inside": n_in, "cadastral_ha_inside": round(ha_in, 1),
                      "cadastre_flags_outside_wua": outside_flag, "observed_zone_parcels_inside": n_study,
                      "explanation": (f"Land between {' and '.join(around)} that lies in none of the 44 community outlines. "
                                      f"The cadastre holds {n_in} parcels there ({ha_in:.1f} ha), {outside_flag} of them marked as "
                                      f"outside the WUA; {n_study} observed-zone parcels lie in it. It is left open, not filled.")})
    small = [round(h.area / 1e4, 2) for h in raw_holes if h.area < w["fill_holes_below_ha"] * 1e4]
    g = to_wgs([out], cfg).iloc[0]
    props = {"label": w["label"], "area_ha": round(out.area / 1e4, 1), "communities": int(len(comm)),
             "sum_of_community_areas_ha": round(float(cm.area.sum() / 1e4), 1), "union_area_ha": round(raw.area / 1e4, 1),
             "slivers_closed_m": w["close_m"], "small_holes_filled": len(small), "small_holes_filled_ha": round(sum(small), 2),
             "holes": holes, "rule": w["rule"]}
    return fc([{"type": "Feature", "properties": props, "geometry": rounded(mapping(g))}], name="wua_outline"), props


def zone_layer(study_m, cfg=CFG):
    z = cfg["observed_zone"]
    u = shapely.union_all(shapely.buffer(study_m.geometry.values, z["close_m"] / 2.0))
    u = u.buffer(-z["close_m"] / 2.0).buffer(z["buffer_m"]).simplify(z["simplify_m"], preserve_topology=True)
    g = to_wgs([u], cfg).iloc[0]
    parts = len(u.geoms) if u.geom_type == "MultiPolygon" else 1
    props = {"label": z["label"], "buffer_m": z["buffer_m"], "close_m": z["close_m"], "area_ha": round(u.area / 1e4, 1),
             "parts": parts, "parcels": int(len(study_m)), "parcel_ha_official": round(float(study_m.area_ha.sum()), 2),
             "rule": z["rule"]}
    return fc([{"type": "Feature", "properties": props, "geometry": rounded(mapping(g))}], name="observed_zone"), props


def canal_layer(net_summary, cfg=CFG):
    """Real canal stages, with the stage I stretch outside the terrain grid marked 'terrain not covered'."""
    from shapely.ops import substring
    raw = json.loads(inp("canal", cfg).read_text(encoding="utf-8"))["features"]
    feats, lengths = [], {}
    for f in raw:
        st = f["properties"]["stage"]
        g = shape(f["geometry"])
        gm = to_m([g], cfg).iloc[0]
        lab = "I" if st == "stage_1" else "II"
        lengths[lab] = round(gm.length, 1)
        props = {"kind": "main_canal", "node": lab, "stage": lab, "name_en": f["properties"].get("display_name_en"),
                 "name_hy": f["properties"].get("display_name_hy"), "length_m": round(gm.length, 1),
                 "basis": "real main canal line"}
        if lab == "I":
            props["capacity_note"] = cfg["labels"]["main_capacity"]
        feats.append({"type": "Feature", "properties": props, "geometry": rounded(mapping(g))})
        if lab == "I":                                       # stage I runs from its first vertex (network v1/v2 orientation)
            to = float(net_summary["terrain"]["stage_1_terrain_not_covered_to_m"])
            seg = to_wgs([substring(gm, 0.0, to)], cfg).iloc[0]
            feats.append({"type": "Feature", "properties": {"kind": "terrain_not_covered", "stage": "I", "from_m": 0.0, "to_m": to,
                          "label": "terrain not covered (outside the terrain grid)"}, "geometry": rounded(mapping(seg))})
    return fc(feats, name="canal"), lengths


def network_layer(nodes, edges, cfg=CFG):
    feats = []
    nd = nodes.set_index("node")
    for (canal, tier), g in edges.groupby(["canal", "tier"], sort=True):
        r = nd.loc[canal]
        feats.append({"type": "Feature", "properties": {
            "kind": f"{tier}_canal", "node": canal, "label": r.label, "community": r.community, "ha": round(float(r.ha), 2),
            "capacity_lps": None if pd.isna(r.capacity_lps) else float(r.capacity_lps), "terrain_state": r.terrain_state
            if isinstance(r.terrain_state, str) else None, "section": r.section, "synthetic": True},
            "geometry": {"type": "MultiLineString", "coordinates": [[[round(a, 6), round(b, 6)], [round(c, 6), round(d, 6)]]
                                                                    for a, b, c, d in zip(g.lon0, g.lat0, g.lon1, g.lat1)]}})
    pts = nodes[~nodes.kind.isin(["main_canal", "secondary_canal", "tertiary_canal"])]
    for r in pts.itertuples():
        props = {"kind": r.kind, "node": r.node, "label": r.label, "community": r.community, "ha": round(float(r.ha), 2),
                 "parent": r.parent, "terrain_state": r.terrain_state if isinstance(r.terrain_state, str) else None,
                 "capacity_lps": None if pd.isna(r.capacity_lps) else float(r.capacity_lps), "pump": bool(r.pump),
                 "section": r.section, "synthetic": True}
        if bool(r.pump):
            props.update({"pump_units": None if pd.isna(r.pump_units) else int(r.pump_units),
                          "run_windows": r.run_windows, "pump_basis": r.pump_basis})
        feats.append({"type": "Feature", "properties": props,
                      "geometry": {"type": "Point", "coordinates": [round(float(r.lon), 6), round(float(r.lat), 6)]}})
    return fc(feats, name="network_v2", label="Synthetic tree network on the real main canal", synthetic=True)


def pump_station_of(p, nodes):
    """Pump station serving each pumping parcel: a main pump, a secondary pump or a direct-outlet pump."""
    nd = nodes.set_index("node")
    st = pd.Series(None, index=p.index, dtype=object)
    main_pump = p.main.map(lambda m: m if isinstance(m, str) and nd.at[m, "kind"] == "main_pump" else None)
    unit_gate = p.unit.map(lambda u: nd.at[u, "parent"] if isinstance(u, str) and nd.at[u, "kind"] == "tertiary_canal" else None)
    sec_pump = unit_gate.map(lambda g: g if isinstance(g, str) and nd.at[g, "kind"] == "secondary_pump" else None)
    out_pump = p.unit.map(lambda u: u if isinstance(u, str) and nd.at[u, "kind"] == "direct_outlet" and bool(nd.at[u, "pump"]) else None)
    st = main_pump.fillna(sec_pump).fillna(out_pump)
    return st.where(p.terrain_state == "pumping")


def areas_layer(p_m, nodes, cfg=CFG):
    """Pumping areas per pump station and terrain-uncertain areas per unit, dissolved (buffer out, then in, simplified)."""
    nd = nodes.set_index("node")
    a = cfg["terrain_areas"]
    dissolve = lambda g: shapely.union_all(shapely.buffer(g.geometry.values, a["dissolve_out_m"])).buffer(  # noqa: E731
        -a["dissolve_in_m"]).simplify(a["simplify_m"], preserve_topology=True)
    feats = []
    station = pump_station_of(p_m, nodes)
    for sid, g in p_m[station.notna()].groupby(station[station.notna()]):
        u = dissolve(g)
        r = nd.loc[sid]
        feats.append({"type": "Feature", "properties": {"kind": "pumping_area", "station": sid, "station_kind": r.kind,
                      "label": r.label, "ha": round(float(g.area_ha.sum()), 2), "parcels": int(len(g)),
                      "pump_units": None if pd.isna(r.pump_units) else int(r.pump_units), "basis": cfg["labels"]["terrain"]},
                      "geometry": rounded(mapping(to_wgs([u], cfg).iloc[0]))})
    unc = p_m[p_m.terrain_state == "terrain uncertain"]
    for uid, g in unc.groupby("unit"):
        u = dissolve(g)
        feats.append({"type": "Feature", "properties": {"kind": "terrain_uncertain_area", "unit": uid, "label": nd.at[uid, "label"],
                      "ha": round(float(g.area_ha.sum()), 2), "parcels": int(len(g)), "basis": cfg["labels"]["terrain"]},
                      "geometry": rounded(mapping(to_wgs([u], cfg).iloc[0]))})
    return fc(feats, name="terrain_areas", basis=cfg["labels"]["terrain"]), station


def laterals_layer(p, nodes):
    nd = nodes.set_index("node")
    r = p[p.outlet.notna()]
    feats = [{"type": "Feature", "properties": {"o": o},
              "geometry": {"type": "LineString", "coordinates": [[round(float(nd.at[o, "lon"]), 6), round(float(nd.at[o, "lat"]), 6)],
                                                                 [round(float(x), 6), round(float(y), 6)]]}}
             for o, x, y in zip(r.outlet, r.lon, r.lat)]
    return fc(feats, name="laterals", note="display connectors from each outlet to the parcels it serves")


def layers_manifest(cfg=CFG):
    z, s = cfg["zoom"], cfg["styles"]
    pop_parcel = ["id", "t", "ha", "com", "o", "ts"]
    pop_node = ["label", "kind", "community", "ha", "capacity_lps", "terrain_state", "section"]
    L = [
        {"id": "wua_outline", "file": "wua_outline.geojson", "title": "Outline of the 44 communities", "type": "line", "visible": True, "style": s["wua_outline"]},
        {"id": "communities", "file": "communities.geojson", "title": "Communities", "type": "line", "visible": True, "style": s["community"],
         "label_field": "name", "label_minzoom": z["labels_min"], "popup": ["name", "zone_parcels", "zone_ha"]},
        {"id": "parcels", "file": "parcels.geojson.gz", "gzip": True, "title": "Parcels by land type (v1.5)", "type": "fill", "visible": True,
         "minzoom": z["parcels_min"], "color_field": "t", "legend": "legend", "popup": pop_parcel},
        {"id": "observed_zone", "file": "observed_zone.geojson", "title": cfg["labels"]["zone"], "type": "line", "visible": True, "style": s["observed_zone"]},
        {"id": "pumping_areas", "file": "pumping_areas.geojson", "filter": {"kind": "pumping_area"}, "title": "Pumping areas (terrain screening, synthetic)",
         "type": "fill", "visible": True, "style": s["pumping_area"], "popup": ["label", "ha", "parcels", "pump_units", "basis"]},
        {"id": "terrain_uncertain", "file": "pumping_areas.geojson", "filter": {"kind": "terrain_uncertain_area"}, "title": "Terrain uncertain (+-3 m)",
         "type": "fill", "visible": False, "style": s["terrain_uncertain_area"], "popup": ["label", "ha", "parcels", "basis"]},
        {"id": "main_canal", "file": "canal.geojson", "filter": {"kind": "main_canal"}, "title": "Lower Hrazdan canal, stages I and II (real line)",
         "type": "line", "visible": True, "style": s["main_canal"], "popup": ["name_en", "name_hy", "length_m", "capacity_note"]},
        {"id": "terrain_not_covered", "file": "canal.geojson", "filter": {"kind": "terrain_not_covered"}, "title": "Terrain not covered",
         "type": "line", "visible": True, "style": s["terrain_not_covered"]},
        {"id": "secondary_canals", "file": "network.geojson.gz", "gzip": True, "filter": {"kind": "secondary_canal"}, "title": "Secondary canals (synthetic)",
         "type": "line", "visible": True, "style": s["secondary_canal"], "popup": pop_node},
        {"id": "tertiary_canals", "file": "network.geojson.gz", "gzip": True, "filter": {"kind": "tertiary_canal"}, "title": "Tertiary canals (synthetic)",
         "type": "line", "visible": True, "minzoom": z["tertiary_min"], "style": s["tertiary_canal"], "popup": pop_node},
        {"id": "laterals", "file": "laterals.geojson.gz", "gzip": True, "title": "Outlet to parcel connectors", "type": "line", "visible": True,
         "minzoom": z["laterals_min"], "style": s["lateral"]},
        {"id": "main_gates", "file": "network.geojson.gz", "gzip": True, "filter": {"kind": ["main_gate"]}, "title": "Main-canal gates",
         "type": "symbol", "visible": True, "style": s["main_gate"], "label_field": "node", "popup": pop_node},
        {"id": "pump_stations", "file": "network.geojson.gz", "gzip": True, "filter": {"pump": True}, "title": "Pump stations (whole units, night windows)",
         "type": "symbol", "visible": True, "style": s["pump_station"], "popup": pop_node + ["pump_units", "run_windows", "pump_basis"]},
        {"id": "secondary_gates", "file": "network.geojson.gz", "gzip": True, "filter": {"kind": ["secondary_gate"]}, "title": "Secondary-canal gates",
         "type": "symbol", "visible": True, "minzoom": z["tertiary_min"], "style": s["secondary_gate"], "popup": pop_node},
        {"id": "outlets", "file": "network.geojson.gz", "gzip": True, "filter": {"kind": ["tertiary_gate", "direct_outlet"]}, "title": "Tertiary gates and direct outlets",
         "type": "symbol", "visible": True, "minzoom": z["outlets_min"], "style": s["tertiary_gate"], "popup": pop_node},
        {"id": "tail_escapes", "file": "network.geojson.gz", "gzip": True, "filter": {"kind": ["tail_escape"]}, "title": "Tail escapes",
         "type": "symbol", "visible": True, "minzoom": z["tertiary_min"], "style": s["tail_escape"], "popup": ["label"]},
    ]
    return {"version": cfg["version"], "basemap": cfg["basemap"], "layers": L, "legend": cfg["legend"],
            "labels": cfg["labels"], "zoom": z,
            "strings_note": "English titles here; the page translates them (scripts/canal_ops_strings.json, native review pending)"}


# ---------------------------------------------------------------- QA and build
def write_json(path, obj):
    path.write_text(json.dumps(obj, indent=1, ensure_ascii=False, allow_nan=False) + "\n", encoding="utf-8")


def write_gz(path, obj):
    path.write_bytes(gzip.compress(json.dumps(obj, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode("utf-8"), mtime=0))


def read_layer(path):
    raw = path.read_bytes()
    return json.loads(gzip.decompress(raw) if path.suffix == ".gz" else raw)


def public_text_problems(out):
    """Cadastral codes, internal parcel ids or the forbidden zone wording in any written layer."""
    bad = {}
    for f in sorted(out.iterdir()):
        if f.suffix not in (".json", ".geojson", ".gz") or f.name == "manifest.json":
            continue
        raw = f.read_bytes()
        text = (gzip.decompress(raw) if f.suffix == ".gz" else raw).decode("utf-8")
        hits = [k for k, rx in (("cadastral code", CADASTRE_CODE), ("internal parcel id", PARCEL_ID)) if rx.search(text)]
        if re.search(r"command\s+area", text, re.I):
            hits.append("forbidden zone wording")
        if hits:
            bad[f.name] = hits
    return bad


def build(out=OUT, cfg=CFG):
    import geopandas as gpd
    out = Path(out)
    with ReadGuard() as guard:
        p = load_parcels(cfg)
        comm = gpd.read_file(inp("communities", cfg))
        cad = gpd.read_file(inp("cadastre", cfg), columns=["cadastre_code", "area_official_m2", "centroid_in_wua"])
        nodes = pd.read_parquet(inp("network_nodes", cfg))
        edges = pd.read_parquet(inp("network_edges", cfg))
        net_summary = json.loads(inp("network_summary", cfg).read_text(encoding="utf-8"))
        canal, lengths = canal_layer(net_summary, cfg)
    out.mkdir(parents=True, exist_ok=True)
    p_m = p.to_crs(cfg["crs_metric"])
    cad_m = cad.to_crs(cfg["crs_metric"])
    layers = {"parcels.geojson.gz": parcels_layer(p, cfg), "communities.geojson": communities_layer(comm, p, cfg)}
    layers["wua_outline.geojson"], wua = wua_outline_layer(comm, cad_m, p_m, cfg)
    layers["observed_zone.geojson"], zone = zone_layer(p_m, cfg)
    layers["canal.geojson"] = canal
    layers["network.geojson.gz"] = network_layer(nodes, edges, cfg)
    layers["pumping_areas.geojson"], station = areas_layer(p_m, nodes, cfg)
    layers["laterals.geojson.gz"] = laterals_layer(p, nodes)
    layers["layers.json"] = layers_manifest(cfg)
    for name, obj in layers.items():
        (write_gz if name.endswith(".gz") else write_json)(out / name, obj)
    summary = summarize(p, comm, nodes, layers, wua, zone, lengths, station, cfg)
    summary["checks"]["no_codes_ids_or_forbidden_wording"] = not (bad := public_text_problems(out))
    summary["public_text_problems"] = bad
    summary["read_guard"] = {"violations": guard.violations, "files_read": len(guard.files)}
    summary["all_checks_pass"] = all(summary["checks"].values())
    write_json(out / "summary.json", summary)
    write_json(out / "manifest.json", {
        "version": cfg["version"], "script": "scripts/canal_ops_map_layers_v1.py", "script_sha256": sha(Path(__file__)),
        "config": "config/canal_ops_map_layers_v1.json", "config_sha256": sha(CFG_PATH),
        "helpers": {"wp_core/read_guard.py": sha(ROOT / "wp_core/read_guard.py")},
        "inputs": {k: sha(ROOT / v) for k, v in cfg["inputs"].items() if k != "not_used"},
        "outputs": {f.name: sha(f) for f in sorted(out.iterdir()) if f.name != "manifest.json"},
        "files_read": guard.files, "read_guard_violations": guard.violations, "not_used": cfg["inputs"]["not_used"]})
    return summary


def summarize(p, comm, nodes, layers, wua, zone, lengths, station, cfg=CFG):
    feats = layers["parcels.geojson.gz"]["features"]
    by_cls = p.groupby("t").agg(parcels=("area_ha", "size"), ha=("area_ha", "sum"))
    leg = legend_classes(cfg)
    net = layers["network.geojson.gz"]["features"]
    node_ids = set(nodes.node)
    outlets = {f["properties"]["o"] for f in feats if f["properties"]["o"]}
    kinds = pd.Series([f["properties"]["kind"] for f in net]).value_counts()
    pump_feats = [f for f in layers["pumping_areas.geojson"]["features"] if f["properties"]["kind"] == "pumping_area"]
    checks = {
        "parcels_22802": len(feats) == 22802 and len({f["properties"]["id"] for f in feats}) == 22802,
        "every_parcel_has_geometry": all(f["geometry"]["coordinates"] for f in feats),
        "communities_44": len(layers["communities.geojson"]["features"]) == 44,
        "ha_reconcile_10339_3": abs(float(p.area_ha.sum()) - 10339.3) <= 0.01,
        "every_class_in_legend": set(by_cls.index) <= set(leg),
        "two_level_legend": [g["key"] for g in cfg["legend"]["groups"]][:2] == ["field_land", "perennial"],
        "zone_label_exact": layers["observed_zone.geojson"]["features"][0]["properties"]["label"] == "Lower Hrazdan observed zone, stages I and II",
        "zone_area_reported": zone["area_ha"] > 0,
        "wua_outline_holes_explained": all(h["explanation"] for h in wua["holes"]),
        "canal_two_stages_and_not_covered": {f["properties"]["kind"] for f in layers["canal.geojson"]["features"]} == {"main_canal", "terrain_not_covered"},
        "parcel_outlets_exist_in_network": outlets <= node_ids,
        "farm_parcels_all_linked": bool(p.loc[p.status == "reached", "outlet"].notna().all()),
        "pumping_areas_have_stations": bool(station[p.terrain_state == "pumping"].notna().all()) and len(pump_feats) > 0,
        "laterals_one_per_reached_parcel": len(layers["laterals.geojson.gz"]["features"]) == int((p.status == "reached").sum()),
        "basemap_satellite_with_attribution": bool(cfg["basemap"]["tiles"]) and "Esri" in cfg["basemap"]["attribution"],
        "layer_files_exist": all(l["file"] in layers for l in layers["layers.json"]["layers"]),
    }
    return {
        "version": cfg["version"], "labels": cfg["labels"],
        "parcels": {"features": len(feats), "ha": round(float(p.area_ha.sum()), 2),
                    "by_class": {k: {"parcels": int(r.parcels), "ha": round(float(r.ha), 2), "group": leg[k][0]} for k, r in by_cls.iterrows()},
                    "location_filled_from_cadastral_geometry": int((p.loc_source != "land types v1.5").sum()),
                    "reached": int((p.status == "reached").sum())},
        "communities": {"features": len(layers["communities.geojson"]["features"])},
        "wua_outline": wua, "observed_zone": zone, "canal_length_m": lengths,
        "network": {k: int(v) for k, v in kinds.items()},
        "pumping_areas": {"stations": len(pump_feats), "ha": round(sum(f["properties"]["ha"] for f in pump_feats), 2)},
        "layers": [l["id"] for l in layers["layers.json"]["layers"]],
        "checks": checks,
    }


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "build":
        s = build()
        print(json.dumps({k: s[k] for k in ("parcels", "wua_outline", "observed_zone", "network", "pumping_areas", "checks", "all_checks_pass")},
                         indent=1, ensure_ascii=False))
    else:
        print(__doc__)
