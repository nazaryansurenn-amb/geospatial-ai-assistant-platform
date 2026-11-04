"""Season weather for the 2026 replay (owner approved 2026-11-01): daily April-October 2024-2026 for the project's
9 coarse 0.1-degree points, two model sources (ECMWF IFS, as the stress layer uses, and ERA5) so the weather
source can be shown as a range. Only the 9 rounded coordinates are sent; no parcel, code or record.

  fetch_season_weather_v1.py        -> data/observations/season_weather_2024_2026_v3/ (March-October; owner added March)
v2 fetches HOURLY values and builds daily figures locally (v1 daily aggregates: ERA5-Land had no ET0/rain and
ECMWF lacked max/min temperature in 2024-2025).
Raw responses are kept with a sha256 receipt; a rerun verifies them and never refetches.
"""
from __future__ import annotations
import hashlib
import json
import time
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import requests

ROOT = Path(__file__).resolve().parents[1]
POINTS = ROOT / "data/observations/current_stress_weather_20260907_v1/points.parquet"
OUT = ROOT / "data/observations/season_weather_2024_2026_v3"
URL = "https://archive-api.open-meteo.com/v1/archive"
YEARS = (2024, 2025, 2026)
MODELS = ("ecmwf_ifs", "era5")  # era5_land returned no ET0 or rain
HOURLY = ["temperature_2m", "relative_humidity_2m", "precipitation", "et0_fao_evapotranspiration",
          "shortwave_radiation", "wind_speed_10m"]
DAILY = ["temperature_2m_max", "temperature_2m_min", "temperature_2m_mean", "precipitation_sum",
         "et0_fao_evapotranspiration", "relative_humidity_2m_mean"]


def sha(p):
    return hashlib.sha256(Path(p).read_bytes()).hexdigest()


def fetch(points, year, model):
    raw = OUT / f"raw_{model}_{year}.json"
    receipt = raw.with_suffix(".receipt.json")
    if raw.exists():
        assert sha(raw) == json.loads(receipt.read_text())["sha256"], raw
        return json.loads(raw.read_text())
    params = {"latitude": ",".join(map(str, points.latitude)), "longitude": ",".join(map(str, points.longitude)),
              "models": model, "start_date": f"{year}-03-01", "end_date": f"{year}-10-31",
              "hourly": ",".join(HOURLY), "timezone": "Asia/Yerevan", "wind_speed_unit": "ms"}
    for attempt in range(7):                      # the free archive rate-limits (429): wait and retry
        r = requests.get(URL, params=params, timeout=120)
        if r.status_code == 200:
            break
        if attempt == 6:
            r.raise_for_status()
        print(f"{model} {year}: HTTP {r.status_code}, retry in {60 * 2 ** attempt} s", flush=True)
        time.sleep(60 * 2 ** attempt)
    data = r.json()
    raw.write_text(json.dumps(data), encoding="utf-8")
    receipt.write_text(json.dumps({"params": params, "sha256": sha(raw), "fetched_utc": datetime.now(timezone.utc).isoformat(),
                                   "source": f"Open-Meteo historical archive, model {model}; model estimates, not station observations"},
                                  indent=1), encoding="utf-8")
    return data


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    points = pd.read_parquet(POINTS)
    frames = []
    for model in MODELS:
        for year in YEARS:
            data = fetch(points, year, model)
            assert isinstance(data, list) and len(data) == len(points), (model, year)
            for point, resp in zip(points.itertuples(), data):
                h = pd.DataFrame(resp["hourly"])
                for c in HOURLY:
                    h[c] = pd.to_numeric(h[c], errors="coerce")
                h["date"] = pd.to_datetime(h.pop("time")).dt.date
                g = h.groupby("date")
                f = pd.DataFrame({"temperature_2m_max": g.temperature_2m.max(), "temperature_2m_min": g.temperature_2m.min(),
                                  "temperature_2m_mean": g.temperature_2m.mean(),
                                  "precipitation_sum": g.precipitation.sum(min_count=24),
                                  "et0_fao_evapotranspiration": g.et0_fao_evapotranspiration.sum(min_count=24),
                                  "relative_humidity_2m_mean": g.relative_humidity_2m.mean(),
                                  "hours": g.temperature_2m.count()}).reset_index()
                f["point_id"], f["model"], f["year"] = point.point_id, model, year
                frames.append(f)
    d = pd.concat(frames, ignore_index=True)
    for c in DAILY:
        d[c] = pd.to_numeric(d[c], errors="coerce")
    d["complete"] = d[DAILY].notna().all(axis=1) & (d.hours == 24)
    d.to_parquet(OUT / "daily.parquet", index=False)
    summary = {m: {str(y): {"days": int(len(g) // len(points)), "first": str(g.date.min()), "last": str(g.date.max()), "complete_share": round(float(g.complete.mean()), 4),
                            "et0_apr_oct_mm_mean": round(float(g.groupby("point_id").et0_fao_evapotranspiration.sum().mean()), 1),
                            "rain_apr_oct_mm_mean": round(float(g.groupby("point_id").precipitation_sum.sum().mean()), 1)}
                   for y, g in dm.groupby("year")} for m, dm in d.groupby("model")}
    files = {p.name: sha(p) for p in sorted(OUT.glob("*")) if p.suffix in (".json", ".parquet") and p.name != "complete.json"}
    (OUT / "complete.json").write_text(json.dumps({"created": datetime.now(timezone.utc).isoformat(), "summary": summary,
                                                   "files": files}, indent=1), encoding="utf-8")
    print(json.dumps(summary, indent=1))


if __name__ == "__main__":
    main()
