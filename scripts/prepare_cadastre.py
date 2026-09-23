from __future__ import annotations

import argparse
import hashlib
import os
import sqlite3
from pathlib import Path

import geopandas as gpd


PRODUCT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT = PRODUCT_ROOT / "server_data" / "cadastre_search.sqlite3"
EXPECTED_COUNT = 111_428


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def build_index(source_path: Path, output_path: Path) -> None:
    parcels = gpd.read_file(
        source_path,
        columns=["cadastre_code", "area_official_m2", "code_verified", "geometry"],
    )
    if len(parcels) != EXPECTED_COUNT:
        raise ValueError(f"Expected {EXPECTED_COUNT} parcels, found {len(parcels)}")
    if parcels.crs is None or parcels.crs.to_epsg() != 4326:
        raise ValueError(f"Expected EPSG:4326, found {parcels.crs}")
    if parcels["cadastre_code"].isna().any() or parcels["cadastre_code"].duplicated().any():
        raise ValueError("Cadastre codes must be complete and unique")
    if not parcels["code_verified"].fillna(False).all():
        raise ValueError("The public cadastre index may contain only verified codes")
    if parcels["area_official_m2"].isna().any():
        raise ValueError("Official cadastral area is missing")
    if parcels.geometry.isna().any() or parcels.geometry.is_empty.any():
        raise ValueError("Cadastre geometry is missing or empty")
    if not parcels.geometry.is_valid.all():
        raise ValueError("Cadastre geometry contains invalid polygons")

    bounds = parcels.geometry.bounds
    rows = zip(
        parcels["cadastre_code"].astype(str),
        parcels["area_official_m2"].astype(float) / 10_000.0,
        bounds.minx.astype(float),
        bounds.miny.astype(float),
        bounds.maxx.astype(float),
        bounds.maxy.astype(float),
        ((bounds.minx + bounds.maxx) / 2).astype(float),
        ((bounds.miny + bounds.maxy) / 2).astype(float),
        strict=True,
    )

    output_path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = output_path.with_suffix(".tmp")
    if temporary_path.exists():
        temporary_path.unlink()
    connection = sqlite3.connect(temporary_path)
    try:
        connection.executescript(
            """
            PRAGMA journal_mode=OFF;
            PRAGMA synchronous=OFF;
            CREATE TABLE parcels (
                cadastre_code TEXT PRIMARY KEY,
                area_ha REAL NOT NULL,
                minx REAL NOT NULL,
                miny REAL NOT NULL,
                maxx REAL NOT NULL,
                maxy REAL NOT NULL,
                center_x REAL NOT NULL,
                center_y REAL NOT NULL
            ) WITHOUT ROWID;
            CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL) WITHOUT ROWID;
            """
        )
        connection.executemany(
            "INSERT INTO parcels VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            rows,
        )
        connection.executemany(
            "INSERT INTO metadata VALUES (?, ?)",
            [
                ("feature_count", str(len(parcels))),
                ("crs", "EPSG:4326"),
                ("area_field", "area_official_m2"),
                ("source_sha256", sha256(source_path)),
            ],
        )
        connection.commit()
    finally:
        connection.close()
    os.replace(temporary_path, output_path)
    print(f"Prepared cadastral search index: {len(parcels)} verified parcels")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("source", type=Path)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    build_index(args.source.resolve(), args.output.resolve())


if __name__ == "__main__":
    main()
