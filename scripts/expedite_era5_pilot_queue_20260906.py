"""One-time submission of four already-planned weather jobs, under collector lock.

Preserves pinned collector/configuration, completed files and existing CDS jobs.
Use only after a graceful collector stop. Never retry an ambiguous submission.
"""
import json
import logging
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from wp_core.data_collector.storage import Store, atomic_json, digest, now
from wp_core.data_collector.runner import validate_spec
from wp_core.data_collector.weather import token

TARGETS = [f"weather_2025_{m:02d}" for m in (6, 7, 8, 9)]
REVIEW = ROOT / "server_data/collector/observations_2021_2025_v1/expedite_20260906_v1"


def main():
    from ecmwf.datastores import Client
    logging.disable(logging.CRITICAL)
    cfg = json.loads((ROOT / "config/data_collector.json").read_text())
    store = Store(ROOT, cfg)
    if not store.stop_file.exists():
        raise RuntimeError("A graceful stop request is required before maintenance")
    REVIEW.mkdir(parents=True, exist_ok=True)
    with store.lock():
        validate_spec(store)
        if (REVIEW / "complete.json").exists():
            print(json.dumps({"status": "already_submitted_reuse_existing_requests"}))
            return
        if not (REVIEW / "jobs.before.sqlite3").exists():
            with store.connect() as source, sqlite3.connect(REVIEW / "jobs.before.sqlite3") as backup:
                source.backup(backup)
            atomic_json(REVIEW / "before.json", {"at": now(), "jobs": store.jobs("weather"),
                        "configuration_sha256": digest(ROOT / "config/data_collector.json"),
                        "script_sha256": digest(Path(__file__))})
        before = json.loads((REVIEW / "before.json").read_text())
        client = Client(url="https://cds.climate.copernicus.eu/api", key=token(store), timeout=30,
                        maximum_tries=1, cleanup=False, progress=False, log_callback=lambda *a, **k: None)
        results = []
        for name in TARGETS:
            job = next(j for j in store.jobs("weather") if j["id"] == name)
            if job["state"] == "complete":
                results.append({"month": name, "action": "already_complete"})
                continue
            receipt = REVIEW / (name + ".receipt.json")
            intent = REVIEW / (name + ".intent.json")
            if job["remote_id"]:
                rid, action = job["remote_id"], "reused_existing_request"
            elif receipt.exists():
                rid, action = json.loads(receipt.read_text())["remote_id"], "recovered_submission_receipt"
            else:
                if intent.exists():
                    raise RuntimeError("Submission outcome uncertain; reconcile CDS request before retrying " + name)
                payload = json.loads(job["payload"])
                assert payload["year"] == "2025" and int(payload["month"]) in (6, 7, 8, 9)
                atomic_json(intent, {"at": now(), "job": name, "payload_sha256": __import__('hashlib').sha256(job['payload'].encode()).hexdigest()})
                try:
                    remote = client.submit(cfg["weather"]["dataset"], payload)
                except Exception as exc:
                    atomic_json(REVIEW / "submission_error.json", {"at": now(), "job": name, "error_type": type(exc).__name__})
                    raise RuntimeError("CDS submission needs inspection; exception type " + type(exc).__name__) from None
                rid, action = remote.request_id, "submitted_planned_month"
                atomic_json(receipt, {"at": now(), "job": name, "remote_id": rid})
            # Persist identity immediately, before the optional status request.
            store.transition(name, "waiting", remote_id=rid)
            reply = client.get_remote(rid).json
            results.append({"month": name, "action": action, "remote_status": reply.get("status"),
                            "created": reply.get("created"), "started": reply.get("started")})
            print(json.dumps(results[-1]), flush=True)
        current = {j["id"]: j for j in store.jobs("weather")}
        for job in before["jobs"]:
            new = current[job["id"]]
            if job["state"] == "complete":
                assert new == job and digest(ROOT / job["output"]) == job["sha256"]
            if job["remote_id"]:
                assert new["remote_id"] == job["remote_id"]
            assert new["payload"] == job["payload"]
        assert digest(ROOT / "config/data_collector.json") == before["configuration_sha256"]
        atomic_json(REVIEW / "complete.json", {"at": now(), "results": results,
                    "previous_completed_files_verified": sum(j["state"] == "complete" for j in before["jobs"]),
                    "existing_remote_ids_preserved": True, "job_payloads_and_configuration_preserved": True})
        print(json.dumps({"status": "priority_months_submitted", "resume_existing_collector": True}))


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        # Never print provider response bodies, credentials or signed result URLs.
        print(json.dumps({"status": "maintenance_stopped", "error_type": type(exc).__name__}), flush=True)
        raise SystemExit(1) from None
