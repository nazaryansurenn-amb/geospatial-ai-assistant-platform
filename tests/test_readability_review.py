from html.parser import HTMLParser
from io import BytesIO
from pathlib import Path
import pytest
from run_readability_review import ASSETS, ROOT, install_handler


class AssetsParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.assets = []

    def handle_starttag(self, tag, attrs):
        values = dict(attrs)
        if tag == "script" and values.get("src"):
            self.assets.append(values["src"])
        if tag == "link" and values.get("href"):
            self.assets.append(values["href"])


def test_original_bundle_is_reused_with_one_local_stylesheet():
    old, new = AssetsParser(), AssetsParser()
    old.feed((ROOT / "output/frontend_activity_change_area50_20260907_v1/index.html").read_text(encoding="utf-8"))
    new.feed((ROOT / "ui/readability-20260907-v1.html").read_text(encoding="utf-8"))
    assert new.assets == old.assets + ["/ui/readability-20260907-v1.css"]
    css = (ROOT / "ui/readability-20260907-v1.css").read_text()
    assert "url(" not in css and "@import" not in css and "!important" not in css
    assert ".agent-message { margin-bottom: 22px; font-size: 16px;" in css
    assert "max-height: calc(100dvh - 106px)" in css
    assert ".map" not in css and ".parcel-popup" not in css


class Parent:
    def __init__(self, path):
        self.path, self.wfile, self.headers, self.delegated = path, BytesIO(), {}, None

    def send_response(self, status):
        self.status = status

    def send_header(self, key, value):
        self.headers[key] = value

    def end_headers(self):
        pass

    def do_GET(self):
        self.delegated = "GET"

    def do_HEAD(self):
        self.delegated = "HEAD"


@pytest.mark.parametrize("path", list(ASSETS))
def test_exact_public_assets_and_head(path):
    for method in ("GET", "HEAD"):
        handler = install_handler(Parent)(path + "?review=readability")
        getattr(handler, "do_" + method)()
        name, mime = ASSETS[path]
        body = (ROOT / "ui" / name).read_bytes()
        assert handler.status == 200 and handler.delegated is None
        assert handler.headers["Content-Type"] == mime
        assert handler.headers["Content-Length"] == str(len(body))
        assert handler.wfile.getvalue() == (body if method == "GET" else b"")


@pytest.mark.parametrize("path", ["/api/land/activity-change", "/api/land/degradation", "/api/agent/session",
    "/ui/../../.env", "/ui/readability-20260907-v1.html", "/ui/unknown.css", "/data/private.parquet"])
def test_non_allowlisted_routes_are_delegated_unchanged(path):
    handler = install_handler(Parent)(path)
    handler.do_GET()
    assert handler.delegated == "GET" and not handler.wfile.getvalue()
