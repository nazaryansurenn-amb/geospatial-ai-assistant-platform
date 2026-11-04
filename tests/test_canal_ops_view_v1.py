"""Irrigation Assistant page server (8554): Host/Origin checks, approvals by token only, watermarked sheets, locked Excel."""
import json
import sys
import threading
import urllib.error
import urllib.request
import zipfile
import io
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import canal_ops_view as v  # noqa: E402


@pytest.fixture(scope="module")
def server(tmp_path_factory):
    app = v.App(tmp_path_factory.mktemp("l") / "ledger.sqlite", network="toy")
    srv = ThreadingHTTPServer(("127.0.0.1", 0), None)
    port = srv.server_address[1]
    srv.RequestHandlerClass = v.make_handler(app, port)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield app, port
    srv.shutdown()


def call(port, path, body=None, host=None, origin="same", ctype="application/json"):
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", data=None if body is None else json.dumps(body).encode(),
                                 method="POST" if body is not None else "GET")
    req.add_header("Host", host or f"127.0.0.1:{port}")
    if body is not None:
        req.add_header("Content-Type", ctype)
        if origin == "same":
            req.add_header("Origin", f"http://127.0.0.1:{port}")
        elif origin:
            req.add_header("Origin", origin)
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, r.read(), dict(r.headers)
    except urllib.error.HTTPError as e:
        return e.code, e.read(), dict(e.headers)


def test_page_and_state(server):
    _, port = server
    assert call(port, "/")[0] == 200
    s, b, _ = call(port, "/api/state")
    st = json.loads(b)
    assert s == 200 and st["mode"] == "fixture" and len(st["network"]["branches"]) == 6


def test_wrong_host_and_cross_origin_refused(server):
    _, port = server
    assert call(port, "/api/state", host="evil.example")[0] == 403
    assert call(port, "/api/chat", {"text": "hi"}, origin="http://evil.example")[0] == 403
    assert call(port, "/api/chat", {"text": "hi"}, ctype="text/plain")[0] == 403


def test_run_and_approval_flow(server):
    app, port = server
    s, b, _ = call(port, "/api/run", {"period": "2025-07-D2", "scenario": "short40"})
    run = json.loads(b)
    assert s == 200 and run["proposal"].startswith("P") and "{{" not in run["answer"]
    pid = run["proposal"]
    # sheet is watermarked and a draft before selection; Excel is locked
    s, sheet, _ = call(port, f"/api/sheet?proposal={pid}&lang=hy")
    assert s == 200 and "ոչ ջրի հրահանգ" in sheet.decode() and "ՆԱԽԱԳԻԾ" in sheet.decode()
    assert call(port, f"/api/excel?proposal={pid}")[0] == 403
    # a made-up token is refused; a card token works once
    assert call(port, "/api/decide", {"proposal": pid, "decision": "approve", "token": "yes"})[0] == 403
    tok = json.loads(call(port, "/api/card", {"proposal": pid})[1])["token"]
    assert call(port, "/api/decide", {"proposal": pid, "decision": "approve", "token": tok})[0] == 200
    assert call(port, "/api/decide", {"proposal": pid, "decision": "approve", "token": tok})[0] == 403
    s, x, h = call(port, f"/api/excel?proposal={pid}")
    assert s == 200 and "simulation" in h["Content-Disposition"]
    sheet1 = zipfile.ZipFile(io.BytesIO(x)).read("xl/worksheets/sheet1.xml").decode()
    assert "not a water order" in sheet1
    assert app.world.ledger.verify()


def test_future_period_refused(server):
    _, port = server
    app, _ = server
    app.world.as_of = "2025-07-D2"
    try:
        assert call(port, "/api/run", {"period": "2025-07-D3"})[0] == 400
    finally:
        app.world.as_of = "2025-07-D3"


def test_chat_refusal_and_size_limit(server):
    _, port = server
    r = json.loads(call(port, "/api/chat", {"text": "open gate 3", "lang": "en"})[1])
    assert r["kind"] == "refusal"
    assert call(port, "/api/chat", {"text": "x" * 30000})[0] == 413


def test_strings_have_all_languages():
    s = json.loads(v.STRINGS.read_text(encoding="utf-8"))
    keys = set(s["en"])
    assert keys == set(s["hy"]) == set(s["ru"])
