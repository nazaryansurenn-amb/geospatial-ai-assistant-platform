"""Validate and select a complete local runtime without mixing releases."""
from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path, PurePosixPath


def sha256(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def local_path(root: Path, relative: str) -> Path:
    if "\\" in relative or ":" in relative:
        raise ValueError("Expected a portable relative path")
    value = PurePosixPath(relative)
    if value.is_absolute() or ".." in value.parts:
        raise ValueError("Path leaves product root")
    path = (root / relative).resolve()
    if not path.is_relative_to(root.resolve()):
        raise ValueError("Path leaves product root")
    return path


def read_release(root: Path, name: str) -> tuple[Path, dict]:
    catalog = json.loads((root / "config/releases.json").read_text(encoding="utf-8"))
    if name not in catalog["releases"]:
        raise ValueError(f"Unknown release: {name}")
    directory = local_path(root, catalog["releases"][name]["directory"])
    manifest = json.loads((directory / "release.json").read_text(encoding="utf-8"))
    if sha256(directory / "release.json") != catalog["releases"][name]["manifest_sha256"]:
        raise ValueError("Release manifest changed")
    return directory, manifest


def verify_release(root: Path, name: str) -> dict:
    directory, manifest = read_release(root, name)
    expected = manifest["files"]
    actual = {p.relative_to(directory).as_posix() for p in directory.rglob("*")
              if p.is_file() and "__pycache__" not in p.parts and p.name != "release.json"}
    if actual != set(expected):
        raise ValueError("Release file allowlist mismatch")
    for relative, info in expected.items():
        path = local_path(directory, relative)
        if path.stat().st_size != info["bytes"] or sha256(path) != info["sha256"]:
            raise ValueError(f"Changed release file: {relative}")
    summary_path = local_path(directory, manifest["summary"])
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    if summary["delivery_version"] != manifest["delivery_version"]:
        raise ValueError("Summary belongs to another release")
    with sqlite3.connect(local_path(directory, manifest["index"]).as_uri() + "?mode=ro", uri=True) as con:
        metadata = dict(con.execute("SELECT key, value FROM metadata"))
        count, unique = con.execute("SELECT COUNT(*), COUNT(DISTINCT cadastre_code) FROM parcel_analytics").fetchone()
    if metadata["delivery_version"] != manifest["delivery_version"] or count != unique:
        raise ValueError("Parcel index mismatch")
    tile_url = summary["tile_delivery"]["url"]
    if tile_url != manifest["tile_url"]:
        raise ValueError("Tile delivery mismatch")
    tile_folder = local_path(directory, "dist/" + tile_url.lstrip("/").split("/{z}")[0])
    if not tile_folder.is_dir() or not list(tile_folder.rglob("*.pbf")):
        raise ValueError("Release tiles missing")
    return {"release": name, "delivery_version": manifest["delivery_version"],
            "verified_files": len(expected), "parcel_count": count,
            "analytical_approval": manifest["analytical_approval"]}


@contextmanager
def release_environment(profile: str):
    values = {"LAND_ANALYTICS_PROFILE": profile, "WORKING_PRODUCT_DIST": "dist"}
    previous = {key: os.environ.get(key) for key in values}
    os.environ.update(values)
    try:
        yield
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def load_runtime(root: Path, name: str):
    verify_release(root, name)
    directory, manifest = read_release(root, name)
    spec = importlib.util.spec_from_file_location("preserved_product_runtime", directory / "app.py")
    module = importlib.util.module_from_spec(spec)
    with release_environment(manifest["analytics_profile"]):
        spec.loader.exec_module(module)
    return module
