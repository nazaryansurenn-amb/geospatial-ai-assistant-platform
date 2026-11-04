"""Water delivery planner, step 3a: the weather the system knew on the planning date (owner approval 2026-11-02,
docs/OWNER_APPROVALS_2026_11_02_EN.md B2 and B3). Fixed code, never a model.

For a planning date D (a simulated "today"), every day of a window gets reference crop water use (ET0, mm) and rain
(mm) from the newest source that existed on D:
  - days up to D-6: ERA5 actual weather (ERA5 is complete only to about six days back);
  - days D-5 .. D-1: the forecast issued the day before each day (lead 1);
  - days D+1 .. D+7: the forecast issued on D (lead = days after D); day D itself: the forecast issued on D-1;
  - later days: a band, never called a forecast: "normal" (1991-2020 normals), "optimistic" (a cool or wet season:
    the 10th-percentile season of 1991-2020 by water need) or "pessimistic" (the 90th-percentile season), each with
    that real year's day-by-day pattern.
A forecast value is only used when it was issued on or before D (lead >= days after D); a missing value falls back to
an older forecast (longer lead), then to the band. Forecasts come from the Open-Meteo archive of ECMWF IFS
(data/source/forecast_archive_openmeteo_v1, seasons 2024-2026); earlier years have no archived forecast and use the
band. Forecast ET0 is computed here with FAO-56 Penman-Monteith from daily aggregates (Armenian local days).
Water need is a simulation estimate; a forecast is never a fact. Weather cells cover the zone coarsely: 97 % of the
hectares with need sit in one cell, so weather scales the total need and cannot rank outlets.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, timedelta
from functools import lru_cache
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
FORECAST = ROOT / "data/source/forecast_archive_openmeteo_v1/hourly.parquet"
ERA5 = ROOT / "data/observations/season_weather_era5_v1/daily.parquet"
NORMALS = ROOT / "data/observations/season_weather_era5_v1/normals.parquet"
LOCAL_UTC_H = 4                                          # Armenia, no daylight saving
ERA5_LAG = 6
LEADS = range(1, 8)
BAND_YEARS = (1991, 2020)
SEASON = ((4, 1), (10, 31))
MAIN_CELL = "40.25_44.25"                                # 97 % of the hectares with need
BANDS = ("normal", "optimistic", "pessimistic")
CREDIT = "Weather: ERA5 (Copernicus); forecasts by Open-Meteo.com from ECMWF IFS (CC BY 4.0)"


def fao56_daily(tmin, tmax, rhmin, rhmax, u10, rs_mj, lat_deg, doy, elev_m):
    """FAO-56 Penman-Monteith reference ET (mm/day) from daily values (eq. 6, 17, 21-40, 47)."""
    tmean = (tmin + tmax) / 2
    svp = lambda t: 0.6108 * np.exp(17.27 * t / (t + 237.3))
    es = (svp(tmin) + svp(tmax)) / 2
    ea = (svp(tmin) * rhmax / 100 + svp(tmax) * rhmin / 100) / 2
    delta = 4098 * svp(tmean) / (tmean + 237.3) ** 2
    pressure = 101.3 * ((293 - 0.0065 * elev_m) / 293) ** 5.26
    gamma = 0.000665 * pressure
    u2 = u10 * 4.87 / np.log(67.8 * 10 - 5.42)
    lat = np.deg2rad(lat_deg)
    dr = 1 + 0.033 * np.cos(2 * np.pi * doy / 365)
    dec = 0.409 * np.sin(2 * np.pi * doy / 365 - 1.39)
    ws = np.arccos(np.clip(-np.tan(lat) * np.tan(dec), -1, 1))
    ra = 24 * 60 / np.pi * 0.0820 * dr * (ws * np.sin(lat) * np.sin(dec) + np.cos(lat) * np.cos(dec) * np.sin(ws))
    rso = (0.75 + 2e-5 * elev_m) * ra
    rnl = 4.903e-9 * ((tmax + 273.16) ** 4 + (tmin + 273.16) ** 4) / 2 * (0.34 - 0.14 * np.sqrt(np.maximum(ea, 0))) \
        * (1.35 * np.clip(rs_mj / rso, 0.3, 1) - 0.35)
    rn = 0.77 * rs_mj - rnl
    et0 = (0.408 * delta * rn + gamma * 900 / (tmean + 273) * u2 * np.maximum(es - ea, 0)) / (delta + gamma * (1 + 0.34 * u2))
    return np.maximum(et0, 0.0)


def forecast_daily(hourly: pd.DataFrame, elev: dict) -> pd.DataFrame:
    """Hourly forecasts (UTC) -> one row per point, lead and Armenian local day; days without 24 hours are dropped."""
    h = hourly.copy()
    h["day"] = (h.valid_utc + pd.Timedelta(hours=LOCAL_UTC_H) - pd.Timedelta(minutes=1)).dt.date   # hour ending
    g = h.groupby(["point", "lead_day", "day"])
    d = g.agg(n=("temperature_2m", "count"), tmin=("temperature_2m", "min"), tmax=("temperature_2m", "max"),
              rhmin=("relative_humidity_2m", "min"), rhmax=("relative_humidity_2m", "max"),
              n_rh=("relative_humidity_2m", "count"), u10=("wind_speed_10m", "mean"),
              rs=("shortwave_radiation", "sum"), rain=("precipitation", "sum"), n_rain=("precipitation", "count"))
    d = d[(d.n == 24) & (d.n_rh == 24) & (d.n_rain == 24)].reset_index()
    lat = d.point.str[:5].astype(float)
    doy = pd.to_datetime(d.day).dt.dayofyear
    d["et0"] = fao56_daily(d.tmin, d.tmax, d.rhmin, d.rhmax, d.u10, d.rs * 3600 / 1e6, lat, doy,
                           d.point.map(elev).astype(float))
    return d.rename(columns={"point": "cell", "lead_day": "lead"})[["cell", "lead", "day", "et0", "rain"]]


@dataclass
class Sources:
    era5: pd.DataFrame                                   # cell, date, et0, rain (complete days)
    normals: pd.DataFrame                                # cell, doy, et0, rain
    forecast: pd.DataFrame                               # cell, lead, day, et0, rain


@lru_cache(maxsize=1)
def load() -> Sources:
    e = pd.read_parquet(ERA5)
    elev = e.groupby("cell").grid_elevation_m.first().to_dict()
    era5 = e[e.complete.astype(bool)].rename(columns={"et0_fao_evapotranspiration": "et0", "precipitation_sum": "rain"})
    era5 = era5[["cell", "date", "et0", "rain"]].dropna()
    n = pd.read_parquet(NORMALS).rename(columns={"et0_fao_evapotranspiration": "et0", "precipitation_sum": "rain"})
    fc = forecast_daily(pd.read_parquet(FORECAST), elev) if FORECAST.exists() else \
        pd.DataFrame(columns=["cell", "lead", "day", "et0", "rain"])
    return Sources(era5, n[["cell", "doy", "et0", "rain"]], fc)


def season_need(era5: pd.DataFrame, year: int, cell: str = MAIN_CELL) -> float:
    """Season water need index (mm): ET0 minus rain, 1 April - 31 October, on the main cell."""
    (m0, d0), (m1, d1) = SEASON
    x = era5[(era5.cell == cell) & (era5.date >= date(year, m0, d0)) & (era5.date <= date(year, m1, d1))]
    return float((x.et0 - x.rain).sum()) if len(x) >= 200 else float("nan")


def band_years(src: Sources) -> dict:
    """Optimistic = 10th-percentile season of 1991-2020 by water need (3rd lowest of 30), pessimistic = 90th (3rd
    highest); each keeps that year's real day-by-day weather."""
    need = {y: season_need(src.era5, y) for y in range(BAND_YEARS[0], BAND_YEARS[1] + 1)}
    order = sorted((v, y) for y, v in need.items() if v == v)
    k = max(0, round(0.1 * len(order)) - 1)
    return {"optimistic": order[k][1], "pessimistic": order[-1 - k][1],
            "need_mm": {"optimistic": round(order[k][0], 1), "pessimistic": round(order[-1 - k][0], 1)},
            "years_ranked": len(order)}


def _band_value(src: Sources, cell: str, day: date, band: str, years: dict):
    if band == "normal":
        doy = min(day.timetuple().tm_yday, 365)
        r = src.normals[(src.normals.cell == cell) & (src.normals.doy == doy)]
        return (float(r.et0.iloc[0]), float(r.rain.iloc[0]), None) if len(r) else (np.nan, np.nan, None)
    y = years[band]
    d = date(y, day.month, min(day.day, 28) if day.month == 2 else day.day)
    r = src.era5[(src.era5.cell == cell) & (src.era5.date == d)]
    return (float(r.et0.iloc[0]), float(r.rain.iloc[0]), y) if len(r) else (np.nan, np.nan, y)


def window(planning: date, first: date, last: date, band: str = "normal", cells=None, src: Sources | None = None) -> pd.DataFrame:
    """Daily ET0 and rain for first..last as known on the planning date. Columns: cell, day, et0, rain, source
    (actual / forecast / band name), issued (forecast issue day), lead, band_year."""
    if band not in BANDS:
        raise ValueError(f"band must be one of {BANDS}")
    src = src or load()
    years = band_years(src) if band != "normal" else {}
    cells = cells or sorted(src.normals.cell.unique())
    era5 = src.era5.set_index(["cell", "date"])
    fc = src.forecast.set_index(["cell", "lead", "day"])
    rows = []
    for cell in cells:
        d = first
        while d <= last:
            after = (d - planning).days                  # days after the planning date (negative = before)
            row = None
            if after <= -ERA5_LAG and (cell, d) in era5.index:
                v = era5.loc[(cell, d)]
                row = (float(v.et0), float(v.rain), "actual", None, None, None)
            elif after <= LEADS[-1]:
                for lead in range(max(1, after), LEADS[-1] + 1):     # issued on or before the planning date only
                    if (cell, lead, d) in fc.index:
                        v = fc.loc[(cell, lead, d)]
                        row = (float(v.et0), float(v.rain), "forecast", d - timedelta(days=lead), lead, None)
                        break
            if row is None:
                et0, rain, y = _band_value(src, cell, d, band, years)
                row = (et0, rain, band, None, None, y)
            rows.append((cell, d, *row))
            d += timedelta(days=1)
    return pd.DataFrame(rows, columns=["cell", "day", "et0", "rain", "source", "issued", "lead", "band_year"])


def forecast_check(src: Sources | None = None) -> dict:
    """How the archived 1-day forecast compares with ERA5 on the same days (mean ET0 and rain), per year."""
    src = src or load()
    f = src.forecast[src.forecast.lead == 1].rename(columns={"day": "date"})
    m = f.merge(src.era5, on=["cell", "date"], suffixes=("_fc", "_era5"))
    out = {}
    for y, g in m.groupby(pd.to_datetime(m.date).dt.year):
        out[int(y)] = {"days": int(len(g)), "et0_fc_mm": round(float(g.et0_fc.mean()), 2),
                       "et0_era5_mm": round(float(g.et0_era5.mean()), 2),
                       "et0_ratio": round(float(g.et0_fc.sum() / g.et0_era5.sum()), 3),
                       "rain_fc_mm": round(float(g.rain_fc.sum() / g.cell.nunique()), 1),
                       "rain_era5_mm": round(float(g.rain_era5.sum() / g.cell.nunique()), 1)}
    return out


if __name__ == "__main__":
    print(json.dumps({"bands": band_years(load()), "forecast_vs_era5": forecast_check()}, indent=1, default=str))
