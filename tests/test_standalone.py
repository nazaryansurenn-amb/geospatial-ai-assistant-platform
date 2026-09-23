from __future__ import annotations

import ast
import hashlib
import importlib
import json
import sqlite3
import sys
import threading
import urllib.error
import urllib.request
from functools import partial
from pathlib import Path

import pytest

from release_tools import local_path, load_runtime, read_release, sha256, verify_release

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("name", ["working", "use_type_review"])
def test_complete_releases(name):
    result = verify_release(ROOT, name)
    assert result["parcel_count"] == 43984
    assert result["verified_files"] > 1000


@pytest.mark.parametrize("path", ["../legacy", "C:/legacy", "/legacy", "data/../../legacy", "..\\legacy"])
def test_paths_cannot_escape(path):
    with pytest.raises(ValueError):
        local_path(ROOT, path)


def test_copied_inputs_are_byte_identical():
    provenance = json.loads((ROOT / "config/isolation_provenance.json").read_text(encoding="utf-8"))
    for entry in provenance["copies"]:
        path = local_path(ROOT, entry["path"])
        assert sha256(path) == entry["sha256"], entry["path"]


def test_methods_are_unchanged():
    fingerprints = json.loads((ROOT / "config/preserved_method_fingerprints.json").read_text(encoding="utf-8"))
    for name, expected in fingerprints.items():
        definitions = {n.name: n for n in ast.parse((ROOT / name).read_text(encoding="utf-8")).body
                       if isinstance(n, ast.FunctionDef)}
        for function, digest in expected.items():
            assert hashlib.sha256(ast.dump(definitions[function]).encode()).hexdigest() == digest, (name, function)


def test_product_code_has_no_legacy_imports_or_paths():
    paths = list((ROOT / "scripts").glob("*.py")) + list((ROOT / "wp_core").glob("*.py"))
    for path in paths:
        text = path.read_text(encoding="utf-8")
        assert "PRODUCT_ROOT.parent" not in text, path
        assert "PROJECT_ROOT" not in text, path
        for node in ast.walk(ast.parse(text)):
            if isinstance(node, ast.ImportFrom):
                assert not (node.module or "").startswith("core."), path
        assert "C:\\Projects" not in text, path


@pytest.mark.parametrize("name", ["build_full_halo_parcel_eo", "prepare_current_parcel_universe",
                                  "prepare_land_activity_2026", "prepare_land_history_2021_2025",
                                  "prepare_lower_hrazdan_halos", "prepare_lower_hrazdan_zones"])
def test_preparation_input_paths_are_local(name):
    module = importlib.import_module(f"scripts.{name}")
    for key, value in vars(module).items():
        if isinstance(value, Path) and key.isupper():
            assert value.resolve().is_relative_to(ROOT), (name, key, value)


def test_existing_delivery_cannot_be_rebuilt(monkeypatch):
    from scripts import prepare_land_analytics_delivery as delivery
    def forbidden():
        pytest.fail("Existing release must be rejected before reading/recalculating data")
    monkeypatch.setattr(delivery, "load_delivery_frame", forbidden)
    with pytest.raises(FileExistsError):
        delivery.build()


@pytest.mark.parametrize("name", ["working", "use_type_review"])
def test_http_runtime_and_internal_files_not_served(name):
    module = load_runtime(ROOT, name)
    handler = partial(module.ProductRequestHandler, directory=str(module.DIST_ROOT))
    server = module.ProductServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}"
    try:
        with urllib.request.urlopen(base + "/api/land/delivery") as response:
            data = json.load(response)
        assert data == json.loads(module.LAND_ANALYTICS_SUMMARY.read_text(encoding="utf-8"))
        with urllib.request.urlopen(base + "/api/cadastre/search?code=04-055-0115-0011") as response:
            assert json.load(response)["cadastre_code"] == "04-055-0115-0011"
        with urllib.request.urlopen(base + "/api/land/parcel?code=04-055-0115-0011") as response:
            assert json.load(response)["history"] is not None
        for path in ["/app.py", "/server_data/cadastre_search.sqlite3", "/config/releases.json", "/data/source/cadastre/parcels_wua.gpkg", "/release.json"]:
            with pytest.raises(urllib.error.HTTPError) as error:
                urllib.request.urlopen(base + path)
            assert error.value.code == 404
        with urllib.request.urlopen(base + "/") as response:
            assert response.status == 200
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_local_environment_not_old_site_packages():
    import pandas
    import geopandas
    assert Path(pandas.__file__).resolve().is_relative_to(ROOT / ".venv")
    assert Path(geopandas.__file__).resolve().is_relative_to(ROOT / ".venv")


def test_environment_cannot_mix_profiles(monkeypatch):
    monkeypatch.setenv("LAND_ANALYTICS_PROFILE", "use_type_v2")
    monkeypatch.setenv("WORKING_PRODUCT_DIST", "../outside")
    module = load_runtime(ROOT, "working")
    assert module.ANALYTICS_PROFILE == "v2"
    assert module.DIST_ROOT.name == "dist"
    assert module.DIST_ROOT.is_relative_to(ROOT / "releases")


def test_modified_release_manifest_is_rejected(tmp_path):
    (tmp_path / "config").mkdir()
    (tmp_path / "preserved").mkdir()
    manifest = tmp_path / "preserved/release.json"
    manifest.write_text('{"version":1}', encoding="utf-8")
    catalog = {"releases": {"test": {"directory": "preserved", "manifest_sha256": sha256(manifest)}}}
    (tmp_path / "config/releases.json").write_text(json.dumps(catalog), encoding="utf-8")
    manifest.write_text('{"version":2}', encoding="utf-8")
    with pytest.raises(ValueError, match="manifest changed"):
        read_release(tmp_path, "test")


def test_modified_release_file_is_rejected(tmp_path):
    (tmp_path / "config").mkdir()
    directory = tmp_path / "preserved"
    directory.mkdir()
    asset = directory / "app.py"
    asset.write_text("original", encoding="utf-8")
    manifest = {"files": {"app.py": {"bytes": asset.stat().st_size, "sha256": sha256(asset)}}}
    (directory / "release.json").write_text(json.dumps(manifest), encoding="utf-8")
    catalog = {"releases": {"test": {"directory": "preserved", "manifest_sha256": sha256(directory / "release.json")}}}
    (tmp_path / "config/releases.json").write_text(json.dumps(catalog), encoding="utf-8")
    asset.write_text("modified", encoding="utf-8")
    with pytest.raises(ValueError, match="Changed release file"):
        verify_release(tmp_path, "test")
