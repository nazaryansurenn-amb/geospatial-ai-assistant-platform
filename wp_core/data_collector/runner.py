"""CLI orchestration; computation runs unattended, independently of map servers."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import numpy as np
import pandas as pd
from release_tools import local_path
from . import VERSION
from .storage import Store, atomic_json, atomic_parquet, digest, now
from .indices import metadata, FORMULAS
from .spatial import basis, load_weights
from . import eo, weather


def code_manifest():
    return {p.name: digest(p) for p in Path(__file__).parent.glob("*.py")}


def prepare(store):
    parcels, audit, scope = basis(store)
    specification = {"collector": VERSION, "config": store.config, "scope": scope,
                     "indices": metadata(), "code_sha256": code_manifest()}
    store.pin(specification)
    if not (store.base / "scope.parquet").exists():
        atomic_parquet(store.base / "scope.parquet", audit)
        atomic_parquet(store.base / "parcels.parquet", parcels.drop(columns="geometry"))
        atomic_json(store.base / "indices.json", metadata())
    manifest = eo.discover(store, scope)
    for scene in manifest["scenes"]:
        stamp = scene["properties"]["datetime"][:10]
        store.add(f"eo_{stamp}_{scene['id']}", "eo", scene)
    for job_id, payload in weather.jobs(store.config, scope["bounds_wgs84"]):
        store.add(job_id, "weather", payload)
    atomic_json(store.base / "schema.json", {
        "eo_primary_key": ["internal_parcel_id", "observation_time_utc", "scene_id"],
        "format": "parquet_zstd", "layout": "one scene per year partition; one logical dataset",
        "indices": list(FORMULAS), "statistics": ["mean", "median", "std", "p10", "p90", "count", "valid_fraction"],
        "null": "missing or mathematically undefined; never forward filled",
        "identity": "existing deterministic internal_parcel_id; cadastral code unchanged",
        "scene_policy": "all dates; C1 preferred per same acquisition/tile; partial footprints retained",
        "publication": "internal_only_no_map_or_public_API_change",
        "quality_fractions": "area weighted per native analysis grid, not a classification",
        "weather": "UTC hourly observations and Asia/Yerevan daily statistics linked by grid cell"})
    catalog(store)
    return parcels, scope


def catalog(store):
    result = store.status()
    result.update(path=store.base.relative_to(store.root).as_posix(), privacy="internal", classification_included=False,
                  eo_complete=bool(store.jobs("eo")) and all(j["state"] == "complete" for j in store.jobs("eo")),
                  weather_complete=bool(store.jobs("weather")) and all(j["state"] == "complete" for j in store.jobs("weather")))
    atomic_json(store.base / "catalog.json", result)
    path = store.root / "data/observations/catalog.json"
    previous = json.loads(path.read_text()) if path.exists() else {"versions": {}, "public_version": None}
    previous["versions"][store.version] = result
    previous["current_collection_version"] = store.version
    atomic_json(path, previous)
    return result


def validate_spec(store):
    spec = json.loads((store.base / "specification.json").read_text(encoding="utf-8"))
    parcels, _, scope = basis(store)
    if spec["config"] != store.config or spec["scope"] != scope or spec["code_sha256"] != code_manifest():
        raise ValueError("Pinned collector inputs or code changed; prepare a new version")
    return parcels, scope


def verify(store):
    parcels, scope = validate_spec(store)
    expected = set(parcels.internal_parcel_id)
    checked, total, bad = 0, 0, []
    for job in store.jobs():
        if job["state"] != "complete":
            continue
        path = local_path(store.root, job["output"])
        if not path.exists() or digest(path) != job["sha256"]:
            bad.append({"id": job["id"], "reason": "output_checksum"})
            continue
        data = pd.read_parquet(path)
        if len(data) != job["rows"]:
            raise ValueError("Stored row count mismatch")
        if job["kind"] == "eo":
            if set(data.internal_parcel_id) != expected or data.internal_parcel_id.duplicated().any():
                raise ValueError("Missing or duplicated parcel in scene")
            if set(data.geometry_version) != {scope["basis_sha256"]}:
                raise ValueError("Changed cadastral source")
            if not pd.to_datetime(data.observation_time_utc).dt.year.isin(store.config["years"]).all():
                raise ValueError("Unexpected EO year")
            for name in FORMULAS:
                f = data[name+"_valid_fraction"]
                if f.isna().any() or (f < -1e-6).any() or (f > 1+1e-6).any():
                    raise ValueError("Invalid spatial support")
                values = data[[name+"_mean", name+"_median", name+"_std"]].to_numpy()
                if np.isinf(values).any():
                    raise ValueError("Infinite index statistics")
                if not data.loc[data[name+"_count"].eq(0), name+"_mean"].isna().all():
                    raise ValueError("Invented value without pixel support")
            checked += 1
        total += len(data)
    result = {"verified_at": now(), "included_parcels": len(expected), "scenes_checked": checked,
              "rows_checked": total, "failures": bad, "classification_performed": False,
              "legacy_250m_dependency": False, "geometry_source_sha256": scope["basis_sha256"]}
    atomic_json(store.base / "verification.json", result)
    if bad:
        raise ValueError("Stored data checksum failure")
    return result


def run(store, kind="all", limit=None, retry=False):
    with store.lock():
        if not (store.base / "specification.json").exists():
            parcels, scope = prepare(store)
        else:
            parcels, scope = validate_spec(store)
        store.recover()
        if store.stop_file.exists():
            store.stop_file.unlink()
        if retry:
            for job in store.jobs():
                if job["state"] in ("failed", "blocked"):
                    store.transition(job["id"], "pending", attempts=0)
        pending_eo = [j for j in store.jobs("eo") if j["state"] != "complete"] if kind in ("all", "eo") else []
        # First results cover every year, then process all other observations.
        first = set()
        for year in store.config["years"]:
            candidates = [j for j in pending_eo if json.loads(j["payload"])["properties"]["datetime"].startswith(str(year))]
            if candidates:
                first.add(min(candidates, key=lambda j: (
                    float(json.loads(j["payload"])["properties"].get("eo:cloud_cover", 100)), j["id"]))["id"])
        pending_eo.sort(key=lambda j: (j["id"] not in first, j["id"]))
        sampling = {r: load_weights(store, parcels, r) for r in (10, 20)} if pending_eo else {}
        print(f"Ready: {len(parcels)} parcels; {len(pending_eo)} outstanding EO scenes", flush=True)
        processed = 0
        for job in pending_eo:
            if store.stop_file.exists() or (limit is not None and processed >= limit):
                break
            if job["attempts"] >= store.config["maximum_attempts"]:
                continue
            for attempt in range(job["attempts"], store.config["maximum_attempts"]):
                start = time.monotonic()
                store.transition(job["id"], "running", attempts=attempt+1)
                try:
                    output = eo.process_scene(store, json.loads(job["payload"]), parcels, sampling, scope)
                    store.transition(job["id"], "complete", elapsed=time.monotonic()-start, **output)
                    print(f"Complete {job['id']}: {output['rows']} rows in {time.monotonic()-start:.1f}s", flush=True)
                    break
                except KeyboardInterrupt:
                    store.transition(job["id"], "interrupted", "Stopped by operator")
                    raise
                except Exception as error:
                    message = str(error)[:250] if isinstance(error, (ValueError, RuntimeError)) else type(error).__name__
                    state = "blocked" if message in ("disk_free_limit", "cache_size_limit") else "failed"
                    store.transition(job["id"], state, message)
                    print(f"{state} {job['id']}: {message}", flush=True)
                    if state == "blocked":
                        catalog(store)
                        return catalog(store)
                    if attempt+1 < store.config["maximum_attempts"]:
                        time.sleep(2**attempt)
            processed += 1
            catalog(store)
        if kind in ("all", "weather") and not store.stop_file.exists():
            waiting = True
            while waiting and not store.stop_file.exists():
                waiting = False
                in_flight = sum(j["state"] == "waiting" for j in store.jobs("weather"))
                for job in store.jobs("weather"):
                    if store.stop_file.exists():
                        break
                    if job["state"] == "complete" or job["attempts"] >= store.config["maximum_attempts"]:
                        continue
                    if job["state"] == "blocked" and not weather.token(store):
                        continue
                    if not job["remote_id"] and in_flight >= 2:
                        waiting = True
                        continue
                    try:
                        state, error = weather.collect_job(store, job)
                        if state != "complete":
                            store.transition(job["id"], state, error)
                        waiting |= state == "waiting"
                        if state == "waiting" and not job["remote_id"]:
                            in_flight += 1
                        if state == "complete" and job["state"] == "waiting":
                            in_flight -= 1
                    except Exception as error:
                        # Never persist provider error bodies or authorization headers.
                        store.transition(job["id"], "failed", "CDS operation failed: "+type(error).__name__, attempts=job["attempts"]+1)
                catalog(store)
                if waiting:
                    for _ in range(30):
                        if store.stop_file.exists():
                            break
                        time.sleep(1)
            weather.assemble(store, parcels)
        return catalog(store)


def main(root):
    parser = argparse.ArgumentParser(description="Autonomous internal EO/weather collector. No AI API calls.")
    parser.add_argument("command", choices=["prepare", "run", "start", "status", "stop", "verify", "configure-cds"])
    parser.add_argument("--config", default="config/data_collector.json")
    parser.add_argument("--kind", choices=["eo", "weather", "all"], default="all")
    parser.add_argument("--limit-scenes", type=int)
    parser.add_argument("--retry-failed", action="store_true")
    args = parser.parse_args()
    if args.command == "configure-cds":
        from getpass import getpass
        key = getpass("CDS personal access token (hidden): ").strip()
        if not key:
            raise ValueError("Empty token was not saved")
        atomic_json(root / "server_data/collector/cds_credentials.json", {"key": key})
        print("CDS token saved locally. It is not printed or sent to an AI service.")
        return
    config = json.loads(local_path(root, args.config).read_text())
    if set(config["indices"]) != set(FORMULAS) or config["crs"] != "EPSG:32638":
        raise ValueError("Unsupported index catalog or analysis CRS")
    if config["years"] != [2021, 2022, 2023, 2024, 2025]:
        raise ValueError("Only approved completed years 2021-2025")
    store = Store(root, config)
    if args.command == "start":
        with store.lock():
            pass
        out = open(store.state/"worker.out.log", "ab", buffering=0)
        err = open(store.state/"worker.err.log", "ab", buffering=0)
        command = [sys.executable, "-B", str(root/"run_data_collector.py"), "run", "--config", args.config, "--kind", args.kind]
        if args.limit_scenes is not None:
            command += ["--limit-scenes", str(args.limit_scenes)]
        if args.retry_failed:
            command += ["--retry-failed"]
        options = {"creationflags": subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_BREAKAWAY_FROM_JOB} if os.name == "nt" else {"start_new_session": True}
        process = subprocess.Popen(command, cwd=root, stdin=subprocess.DEVNULL, stdout=out, stderr=err, **options)
        out.close()
        err.close()
        print(json.dumps({"started_pid": process.pid, "state_directory": str(store.state)}))
        return
    if args.command == "status":
        result = store.status()
    elif args.command == "stop":
        store.stop_file.write_text("stop after current job\n")
        result = {"stop_requested": True}
    elif args.command == "prepare":
        with store.lock():
            prepare(store)
            result = catalog(store)
    elif args.command == "verify":
        result = verify(store)
    else:
        result = run(store, args.kind, args.limit_scenes, args.retry_failed)
    print(json.dumps(result, indent=2))
