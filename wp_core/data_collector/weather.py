"""ERA5-Land requests and UTC-hour/local-day observations; no forecast mixing."""
import calendar
import json
import os
import zipfile
import numpy as np
import pandas as pd
from .storage import atomic_json, atomic_parquet, digest

VARIABLES = ["2m_temperature", "2m_dewpoint_temperature", "10m_u_component_of_wind",
             "10m_v_component_of_wind", "surface_pressure", "total_precipitation",
             "surface_solar_radiation_downwards"]


def jobs(config, bounds):
    west, south, east, north = bounds
    area = [float(np.ceil(north*10)/10+.1), float(np.floor(west*10)/10-.1),
            float(np.floor(south*10)/10-.1), float(np.ceil(east*10)/10+.1)]
    # December context supplies the previous UTC hours of January 1 Armenia time.
    months = [(min(config["years"])-1, 12)] + [(y, m) for y in config["years"] for m in range(1, 13)]
    return [(f"weather_{year}_{month:02d}", {"year": str(year), "month": f"{month:02d}",
              "day": [f"{d:02d}" for d in range(1, calendar.monthrange(year, month)[1]+1)],
              "time": [f"{h:02d}:00" for h in range(24)], "variable": VARIABLES,
              "area": area, "data_format": "netcdf", "download_format": "unarchived"}) for year, month in months]


def token(store):
    value = os.getenv(store.config["weather"]["credential_env"])
    path = store.root / "server_data/collector/cds_credentials.json"
    if not value and path.exists():
        value = json.loads(path.read_text(encoding="utf-8")).get("key")
    return value


def collect_job(store, job):
    key = token(store)
    if not key:
        return "blocked", "CDS account/token and dataset terms required"
    from ecmwf.datastores import Client
    client = Client(url="https://cds.climate.copernicus.eu/api", key=key, timeout=60,
                    progress=False, maximum_tries=3, sleep_max=30, cleanup=False,
                    log_callback=lambda *args, **kwargs: None)
    raw = store.base / "weather/raw" / (job["id"]+".nc")
    raw.parent.mkdir(parents=True, exist_ok=True)
    if not raw.exists():
        if job["remote_id"]:
            remote = client.get_remote(job["remote_id"])
        else:
            remote = client.submit(store.config["weather"]["dataset"], json.loads(job["payload"]))
            store.transition(job["id"], "waiting", remote_id=remote.request_id)
        state = remote.status
        if state not in ("successful", "completed"):
            if state in ("failed", "dismissed"):
                raise RuntimeError("CDS remote job failed; inspect account request status")
            return "waiting", None
        temp = raw.with_suffix(".partial.nc")
        remote.download(str(temp))
        temp.replace(raw)
        atomic_json(raw.with_suffix(".json"), {"sha256": digest(raw), "dataset": store.config["weather"]["dataset"],
                                              "request": json.loads(job["payload"])})
    if digest(raw) != json.loads(raw.with_suffix(".json").read_text())["sha256"]:
        raise ValueError("Weather source checksum mismatch")
    frame = read_netcdf(raw)
    path = store.base / "weather/raw_tables" / (job["id"]+".parquet")
    info = atomic_parquet(path, frame)
    store.transition(job["id"], "complete", output=path.relative_to(store.root).as_posix(),
                     sha256=info["sha256"], rows=len(frame))
    return "complete", None


def read_netcdf(path):
    import xarray as xr
    frames = []
    def read(source):
        with xr.open_dataset(source, engine="netcdf4") as ds:
            rename = {k: v for k, v in {"time": "valid_time", "lat": "latitude", "lon": "longitude"}.items() if k in ds.dims}
            ds = ds.rename(rename)
            if "expver" in ds.dims:
                if ds.sizes["expver"] != 1:
                    raise ValueError("Multiple weather experiment versions require reconciliation")
                ds = ds.squeeze("expver", drop=True)
            frame = ds.to_dataframe().reset_index()
            if not {"valid_time", "latitude", "longitude"} <= set(frame):
                raise ValueError("Unexpected CDS time/space schema")
            required_units = {"t2m": "K", "d2m": "K", "sp": "Pa", "tp": "m"}
            for var, unit in required_units.items():
                if var in ds and ds[var].attrs.get("units") != unit:
                    raise ValueError("Unexpected weather units for "+var)
            return frame
    if zipfile.is_zipfile(path):
        with zipfile.ZipFile(path) as archive:
            for name in archive.namelist():
                if not name.endswith(".nc"):
                    continue
                # Flatten names and never extract provider paths outside the cache.
                destination = path.parent / (path.stem+"_"+str(len(frames))+".nc")
                if not destination.exists():
                    destination.write_bytes(archive.read(name))
                frames.append(read(destination))
    else:
        frames = [read(path)]
    if not frames:
        raise ValueError("CDS download contains no NetCDF data")
    keys = ["valid_time", "latitude", "longitude"]
    frame = frames[0]
    for extra in frames[1:]:
        overlap = [c for c in extra if c in frame and c not in keys]
        frame = frame.merge(extra.drop(columns=overlap), on=keys, how="outer", validate="one_to_one")
    cols = ["t2m", "d2m", "u10", "v10", "sp", "tp", "ssrd"]
    if not set(cols) <= set(frame):
        raise ValueError("Missing weather variables")
    frame = frame[keys+cols].copy()
    frame["valid_time"] = pd.to_datetime(frame.valid_time, utc=True)
    if frame.duplicated(keys).any():
        raise ValueError("Duplicate weather grid observations")
    return frame


def hourly_observations(raw, timezone):
    frame = raw.sort_values(["latitude", "longitude", "valid_time"]).copy()
    if frame.duplicated(["latitude", "longitude", "valid_time"]).any():
        raise ValueError("Duplicate weather times")
    frame["cell_id"] = [f"era5land_{lat:.1f}_{lon:.1f}" for lat, lon in zip(frame.latitude, frame.longitude)]
    group = frame.groupby("cell_id", sort=False)
    gap = group.valid_time.diff().dt.total_seconds().eq(3600)
    for var, name, scale in (("tp", "precipitation_mm", 1000.), ("ssrd", "solar_radiation_mj_m2", 1e-6)):
        # Standard ERA5-Land accumulates from forecast 00 UTC: 01 is step 1,
        # 00 of the following day is step 24. It is not an hourly total already.
        increment = group[var].diff().where(gap)
        increment = increment.where(frame.valid_time.dt.hour.ne(1), frame[var])
        frame[name] = (increment*scale).where(increment >= 0)
    frame["temperature_c"] = frame.t2m-273.15
    frame["dewpoint_c"] = frame.d2m-273.15
    es = .6108*np.exp(17.27*frame.temperature_c/(frame.temperature_c+237.3))
    ea = .6108*np.exp(17.27*frame.dewpoint_c/(frame.dewpoint_c+237.3))
    frame["humidity_pct"] = (100*ea/es).clip(0, 100)
    frame["wind_10m_m_s"] = np.hypot(frame.u10, frame.v10)
    frame["pressure_kpa"] = frame.sp/1000
    frame["time_local"] = frame.valid_time.dt.tz_convert(timezone)
    frame["date_local"] = frame.time_local.dt.strftime("%Y-%m-%d")
    # Accumulations belong to the preceding hour, unlike instantaneous values.
    frame["interval_date_local"] = (frame.time_local-pd.Timedelta(minutes=30)).dt.strftime("%Y-%m-%d")
    return frame


def daily_observations(hourly):
    records = []
    continuous = ["temperature_c", "humidity_pct", "wind_10m_m_s", "pressure_kpa", "dewpoint_c"]
    days = sorted(set(hourly.date_local) | set(hourly.interval_date_local))
    for cell, data in hourly.groupby("cell_id", sort=True):
        instant = {d: g for d, g in data.groupby("date_local")}
        intervals = {d: g for d, g in data.groupby("interval_date_local")}
        for day in days:
            a, b = instant.get(day, data.iloc[:0]), intervals.get(day, data.iloc[:0])
            row = {"cell_id": cell, "date": day, "latitude": data.latitude.iloc[0],
                   "longitude": data.longitude.iloc[0], "source": "ERA5-Land_reanalysis",
                   "temperature_hours": int(a.temperature_c.notna().sum()),
                   "precipitation_hours": int(b.precipitation_mm.notna().sum())}
            for name in continuous:
                row[name+"_mean"] = a[name].mean() if a[name].notna().sum() == 24 else np.nan
            for agg in ("min", "max"):
                row["temperature_c_"+agg] = getattr(a.temperature_c, agg)() if a.temperature_c.notna().sum() == 24 else np.nan
            for name in ("precipitation_mm", "solar_radiation_mj_m2"):
                row[name] = b[name].sum(min_count=24) if len(b) == 24 else np.nan
            row["et0_mm"] = et0(row)
            row["quality"] = "complete" if row["temperature_hours"] == 24 and row["precipitation_hours"] == 24 else "incomplete"
            records.append(row)
    return pd.DataFrame(records)


def et0(row):
    names = ["temperature_c_mean", "temperature_c_min", "temperature_c_max", "dewpoint_c_mean",
             "pressure_kpa_mean", "wind_10m_m_s_mean", "solar_radiation_mj_m2"]
    if not all(np.isfinite(row.get(n, np.nan)) for n in names):
        return np.nan
    t, low, high, dew, pressure, wind, rs = (row[n] for n in names)
    if pressure <= 0 or rs < 0:
        return np.nan
    svp = lambda x: .6108*np.exp(17.27*x/(x+237.3))
    es, ea = (svp(low)+svp(high))/2, svp(dew)
    slope = 4098*svp(t)/(t+237.3)**2
    gamma = .000665*pressure
    u2 = wind*4.87/np.log(67.8*10-5.42)
    day = pd.Timestamp(row["date"]).dayofyear
    latitude = np.deg2rad(row["latitude"])
    dr = 1+.033*np.cos(2*np.pi*day/365)
    declination = .409*np.sin(2*np.pi*day/365-1.39)
    sunset = np.arccos(np.clip(-np.tan(latitude)*np.tan(declination), -1, 1))
    ra = 24*60/np.pi*.0820*dr*(sunset*np.sin(latitude)*np.sin(declination)+np.cos(latitude)*np.cos(declination)*np.sin(sunset))
    # Approximate elevation from model pressure, explicitly not cadastral terrain.
    elevation = (1-(pressure/101.3)**(1/5.26))*293/.0065
    rso = (.75+2e-5*elevation)*ra
    if rso <= 0:
        return np.nan
    rnl = 4.903e-9*((low+273.16)**4+(high+273.16)**4)/2*(.34-.14*np.sqrt(max(ea, 0)))*(1.35*np.clip(rs/rso, .3, 1)-.35)
    rn = .77*rs-rnl
    return max(0., (.408*slope*rn+gamma*900/(t+273)*u2*max(es-ea, 0))/(slope+gamma*(1+.34*u2)))


def assemble(store, parcels):
    completed = [j for j in store.jobs("weather") if j["state"] == "complete"]
    if not completed:
        return
    raw = pd.concat([pd.read_parquet(store.root/j["output"]) for j in completed], ignore_index=True)
    hourly = hourly_observations(raw, store.config["weather"]["timezone"])
    daily = daily_observations(hourly)
    hourly = hourly.loc[hourly.time_local.dt.year.isin(store.config["years"])]
    daily = daily.loc[pd.to_datetime(daily.date).dt.year.isin(store.config["years"])]
    for year in store.config["years"]:
        atomic_parquet(store.base / "weather/hourly" / f"{year}.parquet", hourly.loc[hourly.time_local.dt.year.eq(year)])
        atomic_parquet(store.base / "weather/daily" / f"{year}.parquet", daily.loc[pd.to_datetime(daily.date).dt.year.eq(year)])
    cells = raw[["latitude", "longitude"]].drop_duplicates().reset_index(drop=True)
    points = parcels.to_crs(32638).geometry.centroid.to_crs(4326)
    distance = ((points.x.to_numpy()[:, None]-cells.longitude.to_numpy())*np.cos(np.deg2rad(points.y.to_numpy()[:, None])))**2+(points.y.to_numpy()[:, None]-cells.latitude.to_numpy())**2
    nearest = cells.iloc[np.argmin(distance, axis=1)].reset_index(drop=True)
    links = parcels[["internal_parcel_id", "cadastre_code"]].copy()
    links["cell_id"] = [f"era5land_{lat:.1f}_{lon:.1f}" for lat, lon in zip(nearest.latitude, nearest.longitude)]
    links["method"] = "nearest_0.1_degree_grid_centre_not_parcel_measurement"
    atomic_parquet(store.base / "weather/parcel_cells.parquet", links)
    atomic_json(store.base / "weather/metadata.json", {"provider": "Copernicus CDS ERA5-Land",
        "type": "historical_reanalysis_not_forecast", "native_resolution_m": 9000,
        "grid_degrees": .1, "timezone": store.config["weather"]["timezone"],
        "accumulation": "deaccumulated from 00 UTC forecast; daily sums require 24 complete intervals",
        "et0": "FAO56 daily; elevation approximated from model surface pressure; not actual ET or irrigation volume",
        "reference": "https://www.fao.org/4/X0490E/x0490e06.htm"})
