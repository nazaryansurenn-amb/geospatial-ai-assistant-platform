"""ERA5 daily weather for the 2026 season replay (owner 2026-11-01: "download weather ... also March"; "in other
project I have ERA5 data already").

Source 1, read-only: the owner's IRRIGATION NORMAS project, ERA5 via the Open-Meteo archive, 1961-2025, daily,
models=era5, cell_selection=nearest, elevation=nan (raw 0.25-degree grid values). The six cells over the study area
are copied into data/source/era5_irrigation_normas/ with their sha256.
Source 2: the same request for 2026-01-01..2026-10-31 for the same six cells (only cell-centre coordinates are sent).

  build_season_weather_era5_v1.py   -> data/observations/season_weather_era5_v1/
     daily.parquet    cell, date, the ten daily variables, year, source
     normals.parquet  cell, day of year, 1991-2020 mean ET0, rain, Tmax (7-day centred smoothing)
     complete.json    sha256 of every input and output, coverage per year
"""
from __future__ import annotations
import gzip
import hashlib
import json
import os
import shutil
import time
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd
import requests

ROOT = Path(__file__).resolve().parents[1]
NORMAS = Path(os.environ.get("ERA5_NORMAS_DIR", "data/source/era5_normas_downloads"))
SRC = ROOT / "data/source/era5_irrigation_normas"
OUT = ROOT / "data/observations/season_weather_era5_v1"
CELLS = [(40.00, 44.00), (40.00, 44.25), (40.00, 44.50), (40.25, 44.00), (40.25, 44.25), (40.25, 44.50)]
URL = "https://archive-api.open-meteo.com/v1/archive"
DAILY = ["et0_fao_evapotranspiration", "precipitation_sum", "temperature_2m_max", "temperature_2m_min",
         "temperature_2m_mean", "relative_humidity_2m_min", "wind_speed_10m_mean", "shortwave_radiation_sum",
         "relative_humidity_2m_max", "dew_point_2m_mean"]
YEAR_2026 = ("2026-01-01", "2026-10-31")


def sha(p):
    return hashlib.sha256(Path(p).read_bytes()).hexdigest()


def cell_name(lat, lon):
    return f"{lat:.2f}_{lon:.2f}"


def copy_normas():
    SRC.mkdir(parents=True, exist_ok=True)
    out = {}
    for lat, lon in CELLS:
        f = NORMAS / f"cell_{cell_name(lat, lon)}.json.gz"
        dst = SRC / f.name
        if not dst.exists():
            shutil.copyfile(f, dst)
        assert sha(dst) == sha(f), f
        out[f.name] = sha(dst)
    return out


def wait_for(r):
    """Seconds to wait after a 429 (same policy as the owner's downloader)."""
    reason = (r.text or "").lower()
    return 65 if "minutely" in reason else 3600 if "daily" in reason else 600


def fetch_2026(lat, lon):
    raw = OUT / f"raw_2026_{cell_name(lat, lon)}.json"
    receipt = raw.with_suffix(".receipt.json")
    if raw.exists():
        assert sha(raw) == json.loads(receipt.read_text())["sha256"], raw
        return json.loads(raw.read_text())
    params = dict(latitude=lat, longitude=lon, start_date=YEAR_2026[0], end_date=YEAR_2026[1], daily=",".join(DAILY),
                  models="era5", cell_selection="nearest", elevation="nan", timezone="Asia/Yerevan", wind_speed_unit="ms")
    for attempt in range(8):
        r = requests.get(URL, params=params, timeout=300)
        if r.status_code == 200:
            break
        if r.status_code != 429 or attempt == 7:
            r.raise_for_status()
        s = wait_for(r)
        print(f"{cell_name(lat, lon)}: rate limited, waiting {s} s", flush=True)
        time.sleep(s)
    data = r.json()
    raw.write_text(json.dumps(data), encoding="utf-8")
    receipt.write_text(json.dumps({"params": params, "sha256": sha(raw), "fetched_utc": datetime.now(timezone.utc).isoformat(),
                                   "source": "Open-Meteo archive, models=era5 (ERA5 reanalysis, raw grid values)"},
                                  indent=1), encoding="utf-8")
    return data


def frame(js, cell, source):
    d = pd.DataFrame(js["daily"])
    d["date"] = pd.to_datetime(d.pop("time")).dt.date
    for c in DAILY:
        d[c] = pd.to_numeric(d[c], errors="coerce")
    d["cell"], d["source"] = cell, source
    d["grid_elevation_m"] = js.get("elevation")
    return d


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    inputs = copy_normas()
    frames = []
    for lat, lon in CELLS:
        name = cell_name(lat, lon)
        old = json.load(gzip.open(SRC / f"cell_{name}.json.gz"))
        frames.append(frame(old, name, "IRRIGATION NORMAS ERA5 1961-2025"))
        frames.append(frame(fetch_2026(lat, lon), name, "ERA5 2026 fetch"))
    d = pd.concat(frames, ignore_index=True).sort_values(["cell", "date"])
    assert not d.duplicated(["cell", "date"]).any()
    d["year"] = pd.to_datetime(d.date).dt.year
    d["complete"] = d[["et0_fao_evapotranspiration", "precipitation_sum", "temperature_2m_max"]].notna().all(axis=1)
    d.to_parquet(OUT / "daily.parquet", index=False)
    n = d[(d.year >= 1991) & (d.year <= 2020)].copy()
    n["doy"] = np.minimum(pd.to_datetime(n.date).dt.dayofyear, 365)
    norm = n.groupby(["cell", "doy"])[["et0_fao_evapotranspiration", "precipitation_sum", "temperature_2m_max"]].mean()
    norm = norm.groupby(level="cell", group_keys=False).apply(
        lambda g: g.rolling(7, center=True, min_periods=1).mean()).reset_index()
    norm.to_parquet(OUT / "normals.parquet", index=False)
    season = d[(pd.to_datetime(d.date).dt.month >= 3) & (pd.to_datetime(d.date).dt.month <= 10)]
    cover = {str(y): {"days": int(g.date.nunique()), "complete_share": round(float(g.complete.mean()), 4),
                      "last_complete_day": str(g[g.complete].date.max()),
                      "et0_mar_oct_mm": round(float(g.groupby("cell").et0_fao_evapotranspiration.sum().mean()), 1),
                      "rain_mar_oct_mm": round(float(g.groupby("cell").precipitation_sum.sum().mean()), 1)}
             for y, g in season[season.year >= 2021].groupby("year")}
    files = {p.name: sha(p) for p in sorted(OUT.glob("*")) if p.suffix in (".json", ".parquet") and p.name != "complete.json"}
    (OUT / "complete.json").write_text(json.dumps({"created": datetime.now(timezone.utc).isoformat(), "cells": [cell_name(*c) for c in CELLS],
                                                   "inputs_copied": inputs, "coverage_mar_oct": cover, "files": files}, indent=1),
                                       encoding="utf-8")
    print(json.dumps(cover, indent=1))


if __name__ == "__main__":
    main()
