"""Forecast archive v1: weather forecasts AS ISSUED, for the water delivery planner (owner approval 2026-11-02,
"Yes, download"; docs/OWNER_APPROVALS_2026_11_02_EN.md B2).

Source: Open-Meteo previous-runs API, model ECMWF IFS 0.25 deg. For every valid hour it gives the value forecast
1 to 7 days earlier (`<var>_previous_dayN`), so a simulation set to a past planning date can use only the forecast
that existed then. Free API for non-commercial use; data under CC BY 4.0 (credit below).
Points: the 6 ERA5 cells of the zone (data/source/era5_irrigation_normas). Seasons 2024-2026, 25 March to 31 October
(the archive starts in January 2024). Times in UTC.

  python -I scripts/fetch_forecast_archive_v1.py fetch    # downloads raw JSON (gzip) + receipts into a NEW folder
  python -I scripts/fetch_forecast_archive_v1.py build    # raw -> hourly.parquet + manifest.json (no network)
"""
from __future__ import annotations

import gzip
import hashlib
import json
import sys
import time
import urllib.request
from datetime import date, datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "data/source/forecast_archive_openmeteo_v1"
RAW = OUT / "raw"
API = "https://previous-runs-api.open-meteo.com/v1/forecast"
MODEL = "ecmwf_ifs025"
VARS = ["temperature_2m", "relative_humidity_2m", "wind_speed_10m", "shortwave_radiation", "precipitation"]
LEADS = range(1, 8)
POINTS = [(40.00, 44.00), (40.00, 44.25), (40.00, 44.50), (40.25, 44.00), (40.25, 44.25), (40.25, 44.50)]
SEASONS = {2024: ("2024-03-25", "2024-10-31"), 2025: ("2025-03-25", "2025-10-31"), 2026: ("2026-03-25", "2026-10-31")}
CREDIT = "Weather data by Open-Meteo.com (CC BY 4.0); forecasts from ECMWF IFS (CC BY 4.0)"
TERMS = "Open-Meteo free API: non-commercial use only (owner confirmed 2026-11-02: independent research)"


def url(lat, lon, d0, d1):
    hourly = ",".join(f"{v}_previous_day{n}" for v in VARS for n in LEADS)
    return (f"{API}?latitude={lat:.2f}&longitude={lon:.2f}&hourly={hourly}&models={MODEL}&start_date={d0}"
            f"&end_date={d1}&wind_speed_unit=ms&timezone=GMT")


def fetch():
    RAW.mkdir(parents=True, exist_ok=True)
    total = 0
    for year, (d0, d1) in SEASONS.items():
        for lat, lon in POINTS:
            name = f"{year}_{lat:.2f}_{lon:.2f}"
            if (RAW / f"{name}.json.gz").exists():
                continue
            u = url(lat, lon, d0, d1)
            with urllib.request.urlopen(u, timeout=120) as r:
                body, status = r.read(), r.status
            json.loads(body)                                            # must be JSON; never executed
            (RAW / f"{name}.json.gz").write_bytes(gzip.compress(body))
            (RAW / f"{name}.receipt.json").write_text(json.dumps({
                "url": u, "status": status, "bytes": len(body), "sha256": hashlib.sha256(body).hexdigest(),
                "fetched_utc": datetime.now(timezone.utc).isoformat(timespec="seconds")}, indent=1), encoding="utf-8")
            total += len(body)
            print(f"{name}: {len(body) / 1e6:.2f} MB", flush=True)
            time.sleep(1.0)
    print(f"downloaded {total / 1e6:.1f} MB")


def build():
    import pandas as pd
    frames, receipts = [], []
    for f in sorted(RAW.glob("*.json.gz")):
        d = json.loads(gzip.decompress(f.read_bytes()))
        year, lat, lon = f.name[:-8].split("_")
        h = d["hourly"]
        t = pd.to_datetime(h["time"])
        for n in LEADS:
            frames.append(pd.DataFrame({"point": f"{float(lat):.2f}_{float(lon):.2f}", "valid_utc": t, "lead_day": n,
                                        **{v: h[f"{v}_previous_day{n}"] for v in VARS}}))
        receipts.append(json.loads((RAW / f.name.replace(".json.gz", ".receipt.json")).read_text(encoding="utf-8")))
    df = pd.concat(frames, ignore_index=True).sort_values(["point", "lead_day", "valid_utc"])
    for v in VARS:
        df[v] = df[v].astype("float32")
    df.to_parquet(OUT / "hourly.parquet", index=False)
    nulls = {v: int(df[v].isna().sum()) for v in VARS}
    cover = {str(y): {"from": str(g.valid_utc.min()), "to": str(g.valid_utc.max()), "rows": len(g),
                      "null_rows": int(g[VARS].isna().any(axis=1).sum())}
             for y, g in df.groupby(df.valid_utc.dt.year)}
    man = {"version": "forecast_archive_openmeteo_v1", "source": API, "model": MODEL, "variables": VARS,
           "units": {"temperature_2m": "degC", "relative_humidity_2m": "%", "wind_speed_10m": "m/s",
                     "shortwave_radiation": "W/m2 (mean of the preceding hour)", "precipitation": "mm (preceding hour)"},
           "lead_days": list(LEADS), "points": [f"{a:.2f}_{b:.2f}" for a, b in POINTS], "times": "UTC",
           "meaning": "value at valid_utc as forecast lead_day x 24 h earlier (Open-Meteo previous_dayN)",
           "credit": CREDIT, "terms": TERMS, "rows": len(df), "nulls": nulls, "coverage": cover,
           "downloaded_mb": round(sum(r["bytes"] for r in receipts) / 1e6, 2),
           "raw_sha256": {r["url"].split("latitude=")[1][:24]: r["sha256"] for r in receipts},
           "hourly_parquet_sha256": hashlib.sha256((OUT / "hourly.parquet").read_bytes()).hexdigest(),
           "built": date.today().isoformat()}
    (OUT / "manifest.json").write_text(json.dumps(man, indent=1), encoding="utf-8")
    print(json.dumps({k: man[k] for k in ("rows", "nulls", "coverage", "downloaded_mb")}, indent=1))


if __name__ == "__main__":
    {"fetch": fetch, "build": build}[sys.argv[1]]()
