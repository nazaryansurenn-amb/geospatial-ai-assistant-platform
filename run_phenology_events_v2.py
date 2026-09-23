"""Private control experiment: independent event rules over cached measurements."""
from __future__ import annotations

import os

# Each process uses one numerical thread; avoid multiplying BLAS threads by workers.
for _name in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS", "NUMEXPR_NUM_THREADS"):
    os.environ[_name] = "1"

import argparse
from concurrent.futures import ProcessPoolExecutor
from contextlib import contextmanager
from dataclasses import asdict
from datetime import datetime, timezone
import hashlib
import importlib.metadata
import json
import multiprocessing
from pathlib import Path
import platform
import shutil
import time

import numpy as np
import pandas as pd

from wp_core.phenology_events_v2 import (
    EventPolicy, REQUIRED_DAILY_COLUMNS, REQUIRED_SPATIAL_COLUMNS, classify_year, predominant,
)

ROOT = Path(__file__).resolve().parent
SOURCE = "data/analysis/observation_screening/screening_20260906_v2"
COMPARISON = "data/analysis/observation_screening/transitions_20260906_v3"
PREVIOUS_EVENT = "data/analysis/observation_screening/events_20260906_v1"
OUTPUT = "data/analysis/observation_screening/events_20260906_v2"
CONFIG = "config/phenology_events_v2.json"
IDENTITY = ["internal_parcel_id", "cadastre_code", "area_official_m2", "household", "road_excluded"]
CODE = ["run_phenology_events_v2.py", "verify_phenology_events_v2.py", "wp_core/phenology_events_v2.py"]
FORBIDDEN_MODULES = {"wp_core.observation_rules", "wp_core.observation_screening", "wp_core.phenology_transitions", "wp_core.phenology_events"}


def local(relative):
    path = (ROOT / relative).resolve()
    if not path.is_relative_to(ROOT) or Path(relative).is_absolute():
        raise ValueError("Path must remain inside the independent product")
    return path


def digest(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def now():
    return datetime.now(timezone.utc).isoformat()


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".partial")
    tmp.write_text(json.dumps(value, indent=2, ensure_ascii=False, allow_nan=False) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def write_table(path, frame):
    tmp = path.with_suffix(".partial.parquet")
    frame.to_parquet(tmp, index=False, compression="zstd")
    os.replace(tmp, path)


@contextmanager
def run_lock(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    stream = path.open("a+b")
    if not path.stat().st_size:
        stream.write(b"0")
        stream.flush()
    stream.seek(0)
    try:
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        stream.close()
        raise RuntimeError("Another process owns this event experiment") from None
    try:
        yield
    finally:
        stream.close()


def check_hashes(hashes):
    for relative, expected in hashes.items():
        if digest(local(relative)) != expected:
            raise ValueError("Preserved file changed: " + relative)


def protected_hashes():
    """Hash historical bytes for preservation; never interpret old labels/policies."""
    hashes = {}
    for version in ("screening_20260906_v2", "transitions_20260906_v1", "transitions_20260906_v2", "transitions_20260906_v3", "events_20260906_v1"):
        prefix = "data/analysis/observation_screening/" + version
        completion = local(prefix + "/complete.json")
        hashes[prefix + "/complete.json"] = digest(completion)
        for relative, expected in read_json(completion)["outputs"].items():
            hashes[prefix + "/" + relative] = expected
    # Includes cadastral basis, exclusions, completed EO metadata and collector code.
    previous = read_json(local(SOURCE + "/manifest.json"))
    hashes.update(previous["input_sha256"])
    hashes.update(previous["code_sha256"])
    event_manifest = read_json(local(PREVIOUS_EVENT + "/manifest.json"))
    hashes.update(event_manifest["code_sha256"])
    hashes["config/phenology_events.json"] = event_manifest["config_sha256"]
    for relative in ("tests/test_phenology_events.py", "docs/PHENOLOGY_EVENTS_EN.md"):
        hashes[relative] = digest(local(relative))
    for relative in ("run_product.py", "run_use_type_candidate.py", "config/releases.json", "config/data_collector.json"):
        hashes[relative] = digest(local(relative))
    check_hashes(hashes)
    return hashes


def load_inputs():
    """Only explicitly listed measurement and identity fields enter numerical tasks."""
    source = local(SOURCE)
    sample = pd.read_parquet(source / "selection.parquet", columns=IDENTITY)
    static = pd.read_parquet(source / "spatial.parquet", columns=["internal_parcel_id", *REQUIRED_SPATIAL_COLUMNS])
    columns = list(dict.fromkeys(["internal_parcel_id", "cadastre_code", "geometry_version", *REQUIRED_DAILY_COLUMNS]))
    daily = pd.read_parquet(source / "daily.parquet", columns=columns)
    if len(sample) != 120 or not sample.internal_parcel_id.is_unique or not sample.cadastre_code.is_unique:
        raise ValueError("Only the fixed 120 unique control parcels are authorized")
    if sample[["household", "road_excluded"]].isna().any().any() or sample[["household", "road_excluded"]].any().any():
        raise ValueError("An excluded parcel entered the control")
    if sample.area_official_m2.isna().any() or (sample.area_official_m2 <= 0).any():
        raise ValueError("Missing official parcel area")
    if static.internal_parcel_id.tolist() != sample.internal_parcel_id.tolist():
        raise ValueError("Spatial/selection order differs from packed weight order")
    if set(daily.internal_parcel_id) != set(sample.internal_parcel_id) or daily.duplicated(["internal_parcel_id", "observation_date"]).any():
        raise ValueError("Missing parcel or duplicate observation date")
    identity = daily[["internal_parcel_id", "cadastre_code"]].drop_duplicates()
    joined = identity.merge(sample[["internal_parcel_id", "cadastre_code"]], on="internal_parcel_id", validate="one_to_one", suffixes=("", "_source"))
    if not joined.cadastre_code.eq(joined.cadastre_code_source).all():
        raise ValueError("Cadastral identity mismatch")
    spec = read_json(local("data/observations/observations_2021_2025_v1/specification.json"))
    if set(daily.geometry_version) != {spec["scope"]["basis_sha256"]}:
        raise ValueError("Cadastral geometry version mismatch")
    dates = pd.to_datetime(daily.observation_date)
    if not dates.dt.year.isin(range(2021, 2026)).all():
        raise ValueError("Only completed 2021-2025 seasons are allowed")
    weights = {}
    for resolution in (10, 20):
        with np.load(source / f"weights_{resolution}m.npz", allow_pickle=False) as z:
            values = dict(z)
        if int(values["count"]) != 120 or set(values["parcel"]) != set(range(120)):
            raise ValueError("Wrong spatial weight population")
        weights[resolution] = values
    return sample, static, daily, weights


def tasks_from_inputs(sample, static, daily, weights, policy):
    grouped = {identifier: frame for identifier, frame in daily.groupby("internal_parcel_id", sort=False)}
    tasks = []
    for i, row in enumerate(sample.itertuples(index=False)):
        frame = grouped[row.internal_parcel_id][list(REQUIRED_DAILY_COLUMNS)].copy()
        if sorted(pd.to_datetime(frame.observation_date).dt.year.unique()) != list(range(2021, 2026)):
            raise ValueError("Each control parcel must retain all five years")
        spatial = static.iloc[i][list(REQUIRED_SPATIAL_COLUMNS)].to_dict()
        w10, w20 = [weights[r]["area"][weights[r]["parcel"] == i].copy() for r in (10, 20)]
        tasks.append((row.internal_parcel_id, frame, spatial, w10, w20, policy))
    return tasks


def analyze_parcel(task):
    # Identity is attached AFTER classification and never passed to the engine.
    identifier, frame, spatial, w10, w20, policy_values = task
    policy = EventPolicy(**policy_values)
    years = pd.to_datetime(frame.observation_date).dt.year
    seasons, probes, events = [], [], []
    for year in range(2021, 2026):
        observed = frame.loc[years.eq(year)].reset_index(drop=True)
        full, found = classify_year(observed, spatial, w10, w20, policy)
        seasons.append({"internal_parcel_id": identifier, **full})
        for number, event in enumerate(found):
            events.append({"internal_parcel_id": identifier, "year": year, "episode_number": number + 1, **event})
        for phase in (0, 1):
            probe, _ = classify_year(observed, spatial, w10, w20, policy, drop_alternate=phase)
            changed = (full["crop_type_candidate"], full["annual_cycle_candidate"]) != (probe["crop_type_candidate"], probe["annual_cycle_candidate"])
            probes.append({"internal_parcel_id": identifier, "year": year, "phase": phase,
                           "covered": probe["covered"], "comparable": full["covered"] and probe["covered"],
                           "full_type": full["crop_type_candidate"], "full_cycle": full["annual_cycle_candidate"],
                           "crop_type": probe["crop_type_candidate"], "cycle": probe["annual_cycle_candidate"],
                           "reason": probe["reason"], "changed": changed})
    return {"seasons": seasons, "events": events, "probes": probes}


def stable_json(value):
    def scalar(item):
        if isinstance(item, np.generic):
            return item.item()
        raise TypeError(f"Unsupported result value: {type(item).__name__}")
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False, default=scalar)


def resource_limit():
    available_gb = None
    if os.name == "nt":
        import ctypes
        class MemoryStatus(ctypes.Structure):
            _fields_ = [("length", ctypes.c_ulong), ("load", ctypes.c_ulong),
                        *[(n, ctypes.c_ulonglong) for n in ("total", "available", "page_total", "page_available", "virtual_total", "virtual_available", "extended")]]
        status = MemoryStatus()
        status.length = ctypes.sizeof(status)
        if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
            available_gb = status.available / 2**30
    logical = os.cpu_count() or 1
    limit = min(8, max(1, logical - 2))
    if available_gb is not None:
        limit = min(limit, max(1, int(available_gb - 2)))
    return limit, {"logical_cpus": logical, "available_memory_gb": None if available_gb is None else round(available_gb, 2),
                   "worker_cap": limit, "numerical_threads_per_worker": 1}


def calculate(tasks, worker_choice):
    limit, resources = resource_limit()
    requested = limit if worker_choice == "auto" else int(worker_choice)
    if requested < 1 or requested > limit:
        raise ValueError(f"Worker count must be between 1 and {limit} on this machine")
    benchmark = {}
    start = time.perf_counter()
    if requested == 1:
        results = []
        for i, task in enumerate(tasks):
            results.append(analyze_parcel(task))
            if (i + 1) % 20 == 0:
                print(f"Calculated {i + 1}/120 parcels", flush=True)
        selected = 1
    else:
        pool = ProcessPoolExecutor(max_workers=requested, mp_context=multiprocessing.get_context("spawn"))
        try:
            # Real-data replay checks determinism and chooses the faster execution path.
            sample_positions = np.linspace(0, len(tasks) - 1, requested, dtype=int).tolist()
            sample_tasks = [tasks[i] for i in sample_positions]
            begin = time.perf_counter()
            serial = [analyze_parcel(task) for task in sample_tasks]
            benchmark["serial_sample_seconds"] = time.perf_counter() - begin
            begin = time.perf_counter()
            parallel = list(pool.map(analyze_parcel, sample_tasks, chunksize=1))
            benchmark["parallel_cold_sample_seconds"] = time.perf_counter() - begin
            if stable_json(serial) != stable_json(parallel):
                raise ValueError("Serial and parallel numerical results differ")
            # A warm batch separates Windows process startup from calculation time.
            begin = time.perf_counter()
            replay = list(pool.map(analyze_parcel, sample_tasks, chunksize=1))
            benchmark["parallel_warm_sample_seconds"] = time.perf_counter() - begin
            if stable_json(serial) != stable_json(replay):
                raise ValueError("Repeated parallel calculation differs")
            selected = requested if worker_choice != "auto" or benchmark["parallel_warm_sample_seconds"] < benchmark["serial_sample_seconds"] else 1
            benchmark.update(sample_parcels=len(sample_tasks), serial_parallel_identical=True)
            print(f"Selected {selected} worker(s); serial/parallel replay identical", flush=True)
            results = [None] * len(tasks)
            for i, result in zip(sample_positions, serial):
                results[i] = result
            remaining = [(i, task) for i, task in enumerate(tasks) if results[i] is None]
            iterator = pool.map(analyze_parcel, [t for _, t in remaining], chunksize=1) if selected > 1 else map(analyze_parcel, [t for _, t in remaining])
            for done, ((i, _), result) in enumerate(zip(remaining, iterator), start=len(sample_tasks) + 1):
                results[i] = result
                if done % 20 == 0:
                    print(f"Calculated {done}/120 parcels", flush=True)
        finally:
            pool.shutdown(wait=True, cancel_futures=True)
    return results, {**resources, "selected_workers": selected, "benchmark": benchmark,
                     "calculation_seconds": round(time.perf_counter() - start, 3)}


def compile_frames(results, sample):
    seasons = pd.DataFrame([r for p in results for r in p["seasons"]])
    probes = pd.DataFrame([r for p in results for r in p["probes"]])
    events = pd.DataFrame([r for p in results for r in p["events"]])
    parcel_rows = []
    for row in sample.itertuples(index=False):
        values = predominant(seasons.loc[seasons.internal_parcel_id.eq(row.internal_parcel_id)], minimum_years=3)
        parcel_rows.append({"internal_parcel_id": row.internal_parcel_id, "cadastre_code": row.cadastre_code,
                            "area_official_m2": row.area_official_m2, **values})
    return seasons, pd.DataFrame(parcel_rows), probes, events


def compare_after_prediction(seasons, parcels):
    """Historical class tables are opened only after new predictions are saved."""
    old_s = pd.read_parquet(local(COMPARISON + "/seasons.parquet"), columns=["internal_parcel_id", "year", "crop_type_candidate", "annual_cycle_candidate", "reason", "transition_rule_changed_result"])
    old_p = pd.read_parquet(local(COMPARISON + "/parcels.parquet"), columns=["internal_parcel_id", "crop_type_candidate", "annual_cycle_candidate"])
    compared_s = seasons.merge(old_s, on=["internal_parcel_id", "year"], suffixes=("", "_v3"), validate="one_to_one")
    compared_p = parcels.merge(old_p, on="internal_parcel_id", suffixes=("", "_v3"), validate="one_to_one")
    for frame in (compared_s, compared_p):
        frame["changed_from_v3"] = frame.crop_type_candidate.ne(frame.crop_type_candidate_v3) | frame.annual_cycle_candidate.ne(frame.annual_cycle_candidate_v3)
    return compared_s, compared_p


def compare_previous_event_after_prediction(seasons, parcels, out):
    changes = {}
    for name, current, keys in (("seasons", seasons, ["internal_parcel_id", "year"]),
                                ("parcels", parcels, ["internal_parcel_id"])):
        previous = pd.read_parquet(local(PREVIOUS_EVENT + "/" + name + ".parquet"),
                                   columns=[*keys, "crop_type_candidate", "annual_cycle_candidate"])
        compared = current.merge(previous, on=keys, suffixes=("", "_events_v1"), validate="one_to_one")
        compared["changed_from_events_v1"] = (compared.crop_type_candidate.ne(compared.crop_type_candidate_events_v1)
                                               | compared.annual_cycle_candidate.ne(compared.annual_cycle_candidate_events_v1))
        write_table(out / ("comparison_events_v1_" + name + ".parquet"), compared)
        changes[name] = int(compared.changed_from_events_v1.sum())
    return changes


def make_report(seasons, parcels, probes, events, comparisons, performance):
    cs, cp = comparisons
    comparable = probes.loc[probes.comparable]
    resolved = lambda kind, cycle: kind.eq("perennial") | (kind.eq("annual") & cycle.isin(["single_cycle", "two_cycles"]))
    full = resolved(comparable.full_type, comparable.full_cycle)
    thin = resolved(comparable.crop_type, comparable.cycle)
    concrete = lambda kind, cycle: kind.where(kind.ne("annual"), cycle)
    disagree = full & thin & concrete(comparable.full_type, comparable.full_cycle).ne(concrete(comparable.crop_type, comparable.cycle))
    return {"status": "control_complete_not_accepted", "parcels": len(parcels), "parcel_seasons": len(seasons),
            "types": parcels.crop_type_candidate.value_counts().to_dict(),
            "annual_cycles": parcels.loc[parcels.crop_type_candidate.eq("annual"), "annual_cycle_candidate"].value_counts().to_dict(),
            "cycle_history": parcels.cycle_history_status.value_counts().to_dict(),
            "season_reasons": seasons.reason.value_counts().to_dict(), "covered_seasons": int(seasons.covered.sum()),
            "episodes": len(events), "changed_parcels_from_v3": int(cp.changed_from_v3.sum()),
            "changed_seasons_from_v3": int(cs.changed_from_v3.sum()),
            "temporal_checks": {"probes": len(probes), "comparable": len(comparable), "all_state_changes": int(comparable.changed.sum()),
                                "jointly_resolved": int((full & thin).sum()), "resolved_disagreements": int(disagree.sum()),
                                "resolved_to_unresolved": int((full & ~thin).sum()), "unresolved_to_resolved": int((~full & thin).sum())},
            "performance": performance, "independent_previous_classifier_calls": 0, "previous_labels_as_predictors": False,
            "previous_binary_features_as_predictors": False, "sample_selected_previously": True,
            "cached_SCL_4_5_preprocessing_retained": True, "accuracy_measured": False, "accepted": False,
            "training_eligible": False, "classification_performed_only_on_control120": True,
            "raster_reads": 0, "network_requests": 0, "llm_calculation_calls": 0, "public_release_changed": False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workers", default="auto", help="auto benchmarks serial and parallel; or an explicit bounded worker count")
    args = parser.parse_args()
    begin = time.perf_counter()
    config = read_json(local(CONFIG))
    if config["version"] != "events_20260906_v2" or config["years"] != list(range(2021, 2026)) or config["scope"] != "control_120":
        raise ValueError("Unauthorized version, years or scope")
    policy = asdict(EventPolicy(**config["policy"]))
    if policy != config["policy"]:
        raise ValueError("Every effective parameter must be explicit in the new configuration")
    protected = protected_hashes()
    input_paths = [SOURCE + "/" + name for name in ("daily.parquet", "spatial.parquet", "selection.parquet", "weights_10m.npz", "weights_20m.npz")]
    manifest = {"version": config["version"], "source": SOURCE, "comparison_only_after_prediction": [COMPARISON, PREVIOUS_EVENT],
                "config": config, "input_sha256": {p: digest(local(p)) for p in input_paths},
                "code_sha256": {p: digest(local(p)) for p in CODE}, "config_sha256": digest(local(CONFIG)),
                "protected_sha256": protected, "daily_predictors": list(REQUIRED_DAILY_COLUMNS), "spatial_predictors": list(REQUIRED_SPATIAL_COLUMNS),
                "environment": {"python": platform.python_version(), "executable": str(Path(os.sys.executable).resolve()),
                                "numpy": importlib.metadata.version("numpy"), "pandas": importlib.metadata.version("pandas")},
                "independence": "No predecessor classifier imports/calls/config loading; labels opened only after saved predictions. Existing sample and SCL4/5 measurements retained.",
                "privacy": "internal_only", "accepted": False}
    out = local(OUTPUT)
    with run_lock(out / "run.lock"):
        if (out / "manifest.json").exists() and read_json(out / "manifest.json") != manifest:
            raise ValueError("Experiment inputs/code changed; preserve this version and choose a separately authorized version")
        if (out / "complete.json").exists():
            check_hashes({OUTPUT + "/" + p: h for p, h in read_json(out / "complete.json")["outputs"].items()})
            print("Completed event experiment verified; no files rewritten")
            return
        write_json(out / "manifest.json", manifest)
        for relative in [*CODE, CONFIG]:
            destination = out / "source" / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(local(relative), destination)
        sample, static, daily, weights = load_inputs()
        tasks = tasks_from_inputs(sample, static, daily, weights, policy)
        print(f"Loaded {len(daily):,} cached observations; 120 parcels, 600 seasons; no old class inputs", flush=True)
        results, performance = calculate(tasks, args.workers)
        seasons, parcels, probes, events = compile_frames(results, sample)
        if FORBIDDEN_MODULES.intersection(os.sys.modules):
            raise ValueError("A predecessor analytical module was imported")
        for name, frame in (("seasons", seasons), ("parcels", parcels), ("probes", probes), ("events", events)):
            write_table(out / (name + ".parquet"), frame)
        comparisons = compare_after_prediction(seasons, parcels)
        event_changes = compare_previous_event_after_prediction(seasons, parcels, out)
        write_table(out / "comparison_seasons.parquet", comparisons[0])
        write_table(out / "comparison_parcels.parquet", comparisons[1])
        focus = comparisons[0].loc[comparisons[0].transition_rule_changed_result]
        if len(focus) != 18:
            raise ValueError("Historical focus set no longer contains 18 seasons")
        write_table(out / "focus18.parquet", focus)
        performance["total_seconds_before_final_verification"] = round(time.perf_counter() - begin, 3)
        report = make_report(seasons, parcels, probes, events, comparisons, performance)
        report["changes_from_events_v1"] = event_changes
        comparable = probes.loc[probes.comparable]
        report["annual_perennial_type_switches"] = int((comparable.full_type.isin(["annual", "perennial"])
                                                        & comparable.crop_type.isin(["annual", "perennial"])
                                                        & comparable.full_type.ne(comparable.crop_type)).sum())
        write_json(out / "report.json", report)
        check_hashes(protected)
        check_hashes(manifest["input_sha256"])
        check_hashes(manifest["code_sha256"])
        if digest(local(CONFIG)) != manifest["config_sha256"]:
            raise ValueError("Configuration changed during run")
        hashes = {p.relative_to(out).as_posix(): digest(p) for p in out.rglob("*") if p.is_file() and p.name not in ("run.lock", "complete.json")}
        write_json(out / "complete.json", {"created_at": now(), "outputs": hashes, "accepted": False})
        print(json.dumps(report, indent=2), flush=True)


if __name__ == "__main__":
    main()
