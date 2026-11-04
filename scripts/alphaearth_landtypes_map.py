"""Owner-only local map of the frozen land types v1.1 on satellite photos (owner request 2026-10-31).

  alphaearth_landtypes_map.py serve     -> http://127.0.0.1:8548  (loopback only, read-only)

Reads releases/landtypes_v1_1 (never writes to it) and the parcel outlines; writes its derived GeoJSON to
map_landtypes_v1_1/ beside the releases. Cadastral codes are not included. Outlines are simplified to about 0.25 m
for display (hectares shown are the official ones). The owner's answers on the check page v11_final are a layer
(/api/check, read on every refresh). Owner decision: check parcels show their type like all others (no withholding);
looking a parcel up before answering it anchors that answer.
Not 8526, not the public link: an AI-interpreted, not field-verified candidate.
"""
from __future__ import annotations
import gzip
import json
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent))
from alphaearth_landtypes_v11 import OUT, ROOT  # noqa: E402

PORT = 8548
RELEASE = OUT / "releases/landtypes_v1_4"   # v1.3 = v1.2 + owner labels (2026-11-01); v1.1 and v1.2 stay frozen beside it
CACHE = OUT / "map_landtypes_v1_1"
PAGE = Path(__file__).with_name("alphaearth_landtypes_map.html")
CHECK = "v12_combined"   # the combined owner-first page (v11_final is superseded, never answered)
OLD_PHOTO = "structure seen 2021-22, current use not confirmed"


def short_source(r):
    s = str(r.source_v11 or "")
    if isinstance(r.owner_label, str):
        return "your label"
    if s.startswith("v1.2 rule"):
        return {"v1.2 rule C": "v1.2: alfalfa or annual crop", "v1.2 rule D": "v1.2: tree plot (orchard)",
                "v1.2 rule A1": "v1.2: neighbours agree", "v1.2 rule A2": "v1.2: neighbours agree",
                "v1.2 rule P": "v1.2: broader class", "v1.2 rule K": "v1.2: broader class",
                "v1.2 rule W": "v1.2: block review"}.get(s, s)
    if s.startswith("rule road"):
        return "shape rule: likely road / track / canal"
    if s.startswith("unclassified"):
        return s
    if s.startswith("thin strip"):
        return "thin strip rule"
    if s.startswith("second pass"):
        return "AI second pass"
    if s.startswith("rule"):
        return s
    return "AI review"


def check_items():
    key = json.loads((OUT / "verify" / f"{CHECK}.key.json").read_text(encoding="utf-8"))["items"]
    return {k["item"]: vid for vid, k in key.items()}


def features():
    """GeoJSON of every parcel with its v1.1 type, source, confidence, notes and hint; check parcels carry their vid."""
    import geopandas as gpd
    import pandas as pd
    d = pd.read_parquet(RELEASE / "parcels.parquet")
    spec = json.loads((ROOT / "data/observations/observations_2021_2025_v1/specification.json").read_text())["config"]["basis"]
    from shapely.geometry import mapping
    geom = gpd.read_parquet(ROOT / spec)[["internal_parcel_id", "geometry"]].set_index("internal_parcel_id")
    geom = geom[geom.index.isin(d.internal_parcel_id)].geometry.simplify(2.5e-6, preserve_topology=True)
    check = check_items()
    # v1.2 candidates (not yet checked), shown as their own layer: the full overlay when built, else the rule groups
    cand = None
    if (OUT / "v12/overlay.parquet").exists():
        cand = pd.read_parquet(OUT / "v12/overlay.parquet").rename(columns={"v12_type": "v12_candidate"}).set_index("item_id")
        v12 = OUT / "releases/landtypes_v1_2/summary.json"
        if v12.exists():                                # after the v1.2 freeze only the groups NOT adopted stay candidates
            adopted = json.loads(v12.read_text(encoding="utf-8")).get("adopted_groups", [])
            cand = cand[~cand.group.isin(adopted)]
    elif (OUT / "v12/candidates.parquet").exists():
        cand = pd.read_parquet(OUT / "v12/candidates.parquet").set_index("item_id")
    out = []
    for r in d.itertuples():
        shape = geom.get(r.internal_parcel_id)
        if shape is None:
            continue
        props = {"id": r.item_id, "t": r.type6 if isinstance(r.type6, str) else "NCI",
                 "src": "too small or narrow for imagery" if r.status == "fixed_nci" else short_source(r),
                 "conf": r.confidence if isinstance(r.confidence, str) else None,
                 "notes": r.attrs_v11 if isinstance(r.attrs_v11, str) else "",
                 "hint": r.hint if isinstance(r.hint, str) else None,
                 "ha": round(float(r.area_ha), 3), "com": r.community if isinstance(r.community, str) else "",
                 "photo": r.photo_date if isinstance(r.photo_date, str) else None,
                 "old": bool(isinstance(r.attrs_v11, str) and OLD_PHOTO in r.attrs_v11),
                 "owner": isinstance(r.owner_label, str)}
        if r.item_id in check:
            props["vid"] = check[r.item_id]
        if cand is not None and r.item_id in cand.index:
            props["c12"], props["g12"] = cand.at[r.item_id, "v12_candidate"], cand.at[r.item_id, "group"]
        out.append({"type": "Feature", "properties": props, "geometry": rounded(mapping(shape))})
    return {"type": "FeatureCollection", "features": out}


def rounded(g):
    """GeoJSON geometry with coordinates rounded to 6 decimals (about 10 cm)."""
    def r(c):
        return [round(c[0], 6), round(c[1], 6)] if isinstance(c[0], float) else [r(x) for x in c]
    return {"type": g["type"], "coordinates": r(g["coordinates"])}


def build():
    CACHE.mkdir(exist_ok=True)
    data = features()
    (CACHE / "parcels.geojson.gz").write_bytes(gzip.compress(json.dumps(data, separators=(",", ":")).encode()))
    (CACHE / "source.json").write_text(json.dumps({"release_parcels_sha256": (RELEASE / "parcels.parquet.sha256").read_text()
                                                   if (RELEASE / "parcels.parquet.sha256").exists() else
                                                   json.loads((RELEASE / "manifest.json").read_text())["parcels_sha256"]}),
                                       encoding="utf-8")
    print(f"map data: {len(data['features'])} parcels")


def answered():
    """item_id -> the owner's answer on the check page (verdict, owner's type, comment)."""
    path = OUT / "answers" / f"verify_{CHECK}__owner.json"
    ans = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    by_vid = {v: k for k, v in check_items().items()}
    return {by_vid[vid]: {"verdict": a["verdict"], "owner_type": a.get("owner_type"), "comment": a.get("note", "")}
            for vid, a in ans.items() if vid in by_vid}


def serve():
    if not (CACHE / "parcels.geojson.gz").exists():
        build()
    geo = (CACHE / "parcels.geojson.gz").read_bytes()
    page = PAGE.read_text(encoding="utf-8").encode()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _send(self, code, body, ctype, gz=False):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            if gz:
                self.send_header("Content-Encoding", "gzip")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if self.headers.get("Host") != f"127.0.0.1:{PORT}":
                return self._send(403, b'{"error":"forbidden"}', "application/json")
            path = urlparse(self.path).path
            if path == "/":
                self._send(200, page, "text/html; charset=utf-8")
            elif path == "/data/parcels.geojson":
                self._send(200, geo, "application/json", gz=True)
            elif path == "/api/check":
                self._send(200, json.dumps(answered()).encode(), "application/json")
            else:
                self._send(404, b'{"error":"not found"}', "application/json")

    print(f"land types v1.1 map: http://127.0.0.1:{PORT}")
    ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()


if __name__ == "__main__":
    {"serve": serve, "build": build}.get(sys.argv[1] if len(sys.argv) > 1 else "", lambda: sys.exit(__doc__))()
