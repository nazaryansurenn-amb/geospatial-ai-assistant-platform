"""Atomic files, a pinned run specification and crash-recoverable job state."""
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import time

from release_tools import local_path


def now():
    return datetime.now(timezone.utc).isoformat()


def digest(path):
    with open(path, "rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def json_hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".partial")
    temp.write_text(json.dumps(value, indent=2, ensure_ascii=False, allow_nan=False)+"\n", encoding="utf-8")
    os.replace(temp, path)


def atomic_parquet(path, frame):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(".partial.parquet")
    frame.to_parquet(temp, index=False, compression="zstd")
    os.replace(temp, path)
    return {"path": str(path), "sha256": digest(path), "bytes": path.stat().st_size, "rows": len(frame)}


class Store:
    def __init__(self, root, config):
        self.root, self.config = Path(root).resolve(), config
        self.version = config["version"]
        self.base = local_path(self.root, "data/observations/"+self.version)
        self.state = local_path(self.root, "server_data/collector/"+self.version)
        self.state.mkdir(parents=True, exist_ok=True)
        self.base.mkdir(parents=True, exist_ok=True)
        self.db = self.state / "jobs.sqlite3"
        self.stop_file = self.state / "stop.request"
        self.lock_file = self.state / "worker.lock"
        with self.connect() as con:
            con.executescript('''
                CREATE TABLE IF NOT EXISTS jobs (
                    id TEXT PRIMARY KEY, kind TEXT NOT NULL, payload TEXT NOT NULL,
                    state TEXT NOT NULL DEFAULT 'pending', attempts INTEGER NOT NULL DEFAULT 0,
                    updated TEXT, error TEXT, output TEXT, sha256 TEXT, rows INTEGER,
                    elapsed REAL, remote_id TEXT);
                CREATE TABLE IF NOT EXISTS events (
                    at TEXT, job_id TEXT, state TEXT, message TEXT);
            ''')

    def connect(self):
        con = sqlite3.connect(self.db, timeout=30)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA journal_mode=WAL")
        return con

    @contextmanager
    def lock(self):
        stream = self.lock_file.open("a+b")
        if os.fstat(stream.fileno()).st_size == 0:
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
        except (OSError, BlockingIOError):
            stream.close()
            raise RuntimeError("Another collector process owns this version") from None
        try:
            atomic_json(self.state / "worker.json", {"pid": os.getpid(), "started": now()})
            yield
        finally:
            stream.close()

    def pin(self, specification):
        path = self.base / "specification.json"
        if path.exists():
            if json.loads(path.read_text(encoding="utf-8")) != specification:
                raise ValueError("Inputs/config/code changed: create a new collector version")
        else:
            atomic_json(path, specification)

    def add(self, job_id, kind, payload):
        encoded = json.dumps(payload, sort_keys=True)
        with self.connect() as con:
            old = con.execute("SELECT payload,kind FROM jobs WHERE id=?", (job_id,)).fetchone()
            if old and (old["payload"] != encoded or old["kind"] != kind):
                raise ValueError("Cannot change an existing job")
            con.execute("INSERT OR IGNORE INTO jobs(id,kind,payload,updated) VALUES(?,?,?,?)",
                        (job_id, kind, encoded, now()))

    def transition(self, job_id, state, error=None, **values):
        allowed = {"output", "sha256", "rows", "elapsed", "remote_id", "attempts"}
        if set(values)-allowed:
            raise ValueError("Unknown job update")
        values.update(state=state, updated=now(), error=error)
        with self.connect() as con:
            con.execute("UPDATE jobs SET "+",".join(f"{key}=?" for key in values)+" WHERE id=?",
                        (*values.values(), job_id))
            con.execute("INSERT INTO events VALUES(?,?,?,?)", (now(), job_id, state, error))

    def jobs(self, kind=None):
        with self.connect() as con:
            sql = "SELECT * FROM jobs" + (" WHERE kind=?" if kind else "") + " ORDER BY id"
            return [dict(row) for row in con.execute(sql, (kind,) if kind else ())]

    def recover(self):
        # Only called while holding the OS lock; a dead process cannot own it.
        for job in self.jobs():
            if job["state"] in ("running", "interrupted"):
                self.transition(job["id"], "pending", "Resume after interrupted worker")

    def status(self):
        jobs = self.jobs()
        counts = {}
        for job in jobs:
            key = job["kind"]+":"+job["state"]
            counts[key] = counts.get(key, 0)+1
        result = {"version": self.version, "updated": now(), "jobs": counts,
                  "complete": bool(jobs) and all(j["state"] == "complete" for j in jobs),
                  "rows": sum(j["rows"] or 0 for j in jobs if j["state"] == "complete"),
                  "errors": [{"id": j["id"], "state": j["state"], "message": j["error"]}
                             for j in jobs if j["error"] and j["state"] != "complete"]}
        times = [j["elapsed"] for j in jobs if j["kind"] == "eo" and j["state"] == "complete" and j["elapsed"]]
        if times:
            result["eo_mean_seconds_per_scene"] = sum(times)/len(times)
            result["eo_remaining_estimate_seconds"] = sum(j["state"] != "complete" for j in jobs if j["kind"] == "eo")*sum(times)/len(times)
        atomic_json(self.base / "status.json", result)
        return result
