"""Plan v6 part 2: the map on page 8554 (checklist B7, plan section 1 display, section 4 click-to-ask).

The page draws data/analysis/canal_ops_sim_v1/map/layers.json: every layer file is read once under the read guard,
served gzip, and stripped of the internal parcel id; popups use only the layers.json popup fields; "Ask about this"
only fills the chat box. Since part 3b the live chat uses the v6 payload, refused until the owner approves it."""
import gzip
import json
import re
import sys
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import canal_ops_view as v  # noqa: E402
import irrigation_assistant as ia  # noqa: E402
from wp_core import read_guard as rg  # noqa: E402

HTML = v.PAGE.read_text(encoding="utf-8")
STR = json.loads(v.STRINGS.read_text(encoding="utf-8"))
PARCEL_CODES = re.compile(r"\b\d{2}-\d{3}-\d{4}-\d{4}\b|parcel_[0-9a-f]{16,}|\"f\d{5}\"")


@pytest.fixture(scope="module")
def manifest():
    if not (v.MAP_DIR / "layers.json").exists():
        pytest.skip("map layers not built")
    return json.loads((v.MAP_DIR / "layers.json").read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def server(tmp_path_factory, manifest):
    app = v.App(tmp_path_factory.mktemp("l") / "ledger.sqlite", network="toy", map_dir=v.MAP_DIR)
    srv = ThreadingHTTPServer(("127.0.0.1", 0), None)
    port = srv.server_address[1]
    srv.RequestHandlerClass = v.make_handler(app, port)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield app, port
    srv.shutdown()


def get(port, path, gz=True, host=None):
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}")
    req.add_header("Host", host or f"127.0.0.1:{port}")
    if gz:
        req.add_header("Accept-Encoding", "gzip")
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, r.read(), dict(r.headers)
    except urllib.error.HTTPError as e:
        return e.code, e.read(), dict(e.headers)


def body(raw, headers):
    return json.loads(gzip.decompress(raw) if headers.get("Content-Encoding") == "gzip" else raw)


# ---------------------------------------------------------------- serving
def test_every_layer_file_is_served_gzip(server, manifest):
    app, port = server
    files = {lay["file"] for lay in manifest["layers"]} | {"layers.json"}
    assert set(app.map.files) == files
    for f in files:
        s, raw, h = get(port, f"/map/{f}")
        assert s == 200 and h["Content-Encoding"] == "gzip", f
        assert body(raw, h)
    s, raw, h = get(port, "/map/parcels.geojson.gz", gz=False)            # a client without gzip still gets JSON
    assert s == 200 and "Content-Encoding" not in h and len(json.loads(raw)["features"]) == 22802


def test_only_listed_files_and_the_right_host(server):
    _, port = server
    for bad in ("/map/manifest.json", "/map/summary.json", "/map/../layers.json", "/map/%2e%2e/config.json",
                "/data/network.geojson"):
        assert get(port, bad)[0] == 404, bad
    assert get(port, "/map/layers.json", host="evil.example")[0] == 403


def test_layers_are_read_under_the_read_guard(server):
    app, _ = server
    assert app.map.read and all(f.startswith(v.MAP_DIR.relative_to(ROOT).as_posix() + "/") for f in app.map.read)
    assert all(rg.problem(ROOT / f) is None for f in app.map.read)


def test_no_map_without_built_layers(tmp_path):
    assert not v.MapLayers(tmp_path) and not v.MapLayers(None)


def test_state_says_whether_the_map_is_on(server):
    _, port = server
    s, raw, _ = get(port, "/api/state", gz=False)
    assert s == 200 and json.loads(raw)["map"] is True


# ---------------------------------------------------------------- no codes or internal parcel ids on the page
def test_no_parcel_id_or_cadastral_code_reaches_the_page(server):
    app, port = server
    parcels = body(*get(port, "/map/parcels.geojson.gz")[1:])
    assert len(parcels["features"]) == 22802
    assert not any("id" in f["properties"] or "id" in f for f in parcels["features"])
    for name, gz in app.map.files.items():
        assert not PARCEL_CODES.search(gzip.decompress(gz).decode("utf-8")), name


def test_popups_use_only_layers_json_fields_without_the_id(server, manifest):
    _, port = server
    served = {x["id"]: x for x in body(*get(port, "/map/layers.json")[1:])["layers"]}
    for lay in manifest["layers"]:
        if "popup" in lay:
            assert served[lay["id"]]["popup"] == [f for f in lay["popup"] if f != "id"], lay["id"]
            assert "id" not in served[lay["id"]]["popup"]
    assert served["parcels"]["popup"] == ["t", "ha", "com", "o", "ts"]


def test_served_layers_keep_everything_the_page_draws_by(server, manifest):
    app, _ = server
    data = {n: json.loads(gzip.decompress(b)) for n, b in app.map.files.items()}
    for lay in manifest["layers"]:
        props = set().union(*(f["properties"].keys() for f in data[lay["file"]]["features"]))
        need = set(lay.get("popup", [])) - {"id"} | set(lay.get("filter", {})) | {lay.get("color_field"), lay.get("label_field")} - {None}
        assert need <= props, (lay["id"], need - props)


# ---------------------------------------------------------------- the page
def test_page_draws_from_layers_json():
    assert 'fetch("/map/layers.json")' in HTML and "ML.basemap" in HTML and "b.attribution" in HTML
    assert "/data/network.geojson" not in HTML                        # the v1 schematic overlay is gone
    for k in ("minzoom", "label_minzoom", "color_field", "legend.groups", "l.popup", "l.filter"):
        assert k in HTML, k


def test_ask_about_this_only_fills_the_chat_box():
    fn = HTML[HTML.index("function askAbout("):]
    fn = fn[:fn.index("\n}\n")]
    assert '$("#q")' in fn and "q.value" in fn and "q.focus()" in fn
    assert "ask(" not in fn.replace("askAbout(", "") and "post(" not in fn and "fetch(" not in fn


def test_page_shows_no_internal_id_or_forbidden_wording():
    assert not re.search(r"command\s+area|irrigation pressure", HTML + json.dumps(STR, ensure_ascii=False), re.I)
    map_code = HTML[HTML.index("let MAP = null"):HTML.index("function drawSchematic(")]
    assert not re.search(r"\bp\.id\b|properties\.id|\[.id.\]|item_id", map_code)


def test_new_map_strings_in_all_three_languages(manifest):
    keys = set(STR["en"])
    assert keys == set(STR["hy"]) == set(STR["ru"])
    need = {"ask_about", "layers", "map_sim", "zoom_hint", "legend_source"}
    need |= {"L_" + x["id"] for x in manifest["layers"]}
    need |= {"G_" + g["key"] for g in manifest["legend"]["groups"]}
    need |= {"C_" + c["key"] for g in manifest["legend"]["groups"] for c in g["classes"]}
    need |= {"F_" + f for x in manifest["layers"] for f in x.get("popup", []) if f != "id"}
    assert need <= keys, need - keys
    assert STR["en"]["L_observed_zone"] == "Lower Hrazdan observed zone, stages I and II"
    for lang in ("hy", "ru"):                                         # translated, not copied
        assert STR[lang]["ask_about"] != STR["en"]["ask_about"] and STR[lang]["L_parcels"] != STR["en"]["L_parcels"]
    assert re.search("[Ա-֏]", STR["hy"]["map_sim"]) and STR["en"]["map_sim"].startswith("Simulation")


def test_page_defaults_to_armenian():
    assert '<html lang="hy">' in HTML and '|| "hy"' in HTML


# ---------------------------------------------------------------- live chat waits for the owner's new approval (part 3b)
def test_live_chat_is_refused_until_the_new_payload_is_approved(monkeypatch, tmp_path):
    stale = tmp_path / "approval.json"                                  # an approval for another schema
    stale.write_text(json.dumps({"schema_hash": "0" * 64}), encoding="utf-8")
    monkeypatch.setattr(ia, "APPROVAL", stale)
    monkeypatch.setenv("LIVE_ENABLED", "1")
    with pytest.raises(PermissionError):
        ia.guard_live("live")
