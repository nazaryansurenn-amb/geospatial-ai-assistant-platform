"""Append-only, hash-chained state for the Irrigation Assistant (canal_ops_sim_v1).

One SQLite table of events; every event stores the previous event's hash, so any edit breaks the chain (`verify`).
Writes go through one lock (single writer) in WAL mode. Kinds:
  run, result, proposal, decision, constraint, period, event
Approval rules: a decision is valid only with a token issued by the loopback UI for that proposal, and it lapses if
the proposal's input hash no longer matches. A "yes" typed in chat never reaches this table.
"""
from __future__ import annotations

import hashlib
import json
import secrets
import sqlite3
import threading
import time
from pathlib import Path

GENESIS = "0" * 64


def _h(prev: str, kind: str, payload: str, ts: float) -> str:
    return hashlib.sha256(f"{prev}|{kind}|{payload}|{ts:.6f}".encode()).hexdigest()


class Ledger:
    def __init__(self, path: str | Path):
        self.path = str(path)
        self._lock = threading.Lock()
        self._tokens: dict[str, tuple[str, float]] = {}
        with self._con() as c:
            c.execute("PRAGMA journal_mode=WAL")
            c.execute("CREATE TABLE IF NOT EXISTS events (id INTEGER PRIMARY KEY, ts REAL, kind TEXT, payload TEXT, "
                      "prev TEXT, hash TEXT)")

    def _con(self):
        return sqlite3.connect(self.path, timeout=30)

    # ---------------------------------------------------------- writing
    def append(self, kind: str, payload: dict) -> dict:
        body = json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str)
        with self._lock, self._con() as c:
            row = c.execute("SELECT hash FROM events ORDER BY id DESC LIMIT 1").fetchone()
            prev = row[0] if row else GENESIS
            ts = time.time()
            h = _h(prev, kind, body, ts)
            cur = c.execute("INSERT INTO events (ts, kind, payload, prev, hash) VALUES (?,?,?,?,?)", (ts, kind, body, prev, h))
            return {"id": cur.lastrowid, "hash": h}

    def events(self, kind: str | None = None) -> list[dict]:
        with self._con() as c:
            q = "SELECT id, ts, kind, payload, hash FROM events" + (" WHERE kind=?" if kind else "") + " ORDER BY id"
            rows = c.execute(q, (kind,) if kind else ()).fetchall()
        return [{"id": i, "ts": ts, "kind": k, "hash": h, **json.loads(p)} for i, ts, k, p, h in rows]

    def verify(self) -> bool:
        prev = GENESIS
        with self._con() as c:
            for ts, k, p, pv, h in c.execute("SELECT ts, kind, payload, prev, hash FROM events ORDER BY id"):
                if pv != prev or _h(prev, k, p, ts) != h:
                    return False
                prev = h
        return True

    # ---------------------------------------------------------- proposals and approvals
    def save_proposal(self, sim_run_id: str, input_hash: str, summary: dict, author: str) -> str:
        pid = f"P{secrets.token_hex(4)}"
        fp = hashlib.sha256(json.dumps(summary, sort_keys=True, default=str).encode()).hexdigest()[:16]
        for r in self.events("decision"):
            if r.get("fingerprint") == fp and r["decision"] == "reject":
                raise ValueError("an identical proposal was rejected before; cite new evidence")
        self.append("proposal", {"proposal": pid, "sim_run_id": sim_run_id, "input_hash": input_hash,
                                 "summary": summary, "fingerprint": fp, "author": author, "status": "draft"})
        return pid

    def issue_token(self, proposal: str, ttl_s: int = 900) -> str:
        """Called only by the loopback UI when it renders an approval card."""
        tok = secrets.token_urlsafe(16)
        self._tokens[tok] = (proposal, time.time() + ttl_s)
        return tok

    def use_token(self, subject: str, token: str) -> None:
        """Spend a one-time token issued for this subject (e.g. a supply card); raises when invalid, used or expired."""
        p_tok = self._tokens.pop(token, None)
        if not p_tok or p_tok[0] != subject or p_tok[1] < time.time():
            raise PermissionError("invalid, used or expired approval token")

    def decide(self, proposal: str, decision: str, token: str, current_input_hash: str, by: str, reason: str = "") -> dict:
        p_tok = self._tokens.pop(token, None)                     # single use
        if not p_tok or p_tok[0] != proposal or p_tok[1] < time.time():
            raise PermissionError("invalid, used or expired approval token")
        prop = next((r for r in self.events("proposal") if r["proposal"] == proposal), None)
        if prop is None:
            raise KeyError(proposal)
        if decision == "approve" and prop["input_hash"] != current_input_hash:
            raise PermissionError("inputs changed since the proposal; re-simulate before approving")
        return self.append("decision", {"proposal": proposal, "decision": decision, "by": by, "reason": reason,
                                        "fingerprint": prop["fingerprint"], "sim_run_id": prop["sim_run_id"]})

    # ---------------------------------------------------------- standing constraints and periods
    def add_constraint(self, constraint: dict, by: str) -> dict:
        n = 1 + sum(1 for r in self.events("constraint") if r.get("key") == constraint["key"])
        return self.append("constraint", {**constraint, "version": n, "by": by})

    def constraints(self, as_of: str) -> dict:
        out = {}
        for r in self.events("constraint"):
            if r.get("valid_from", "") <= as_of <= r.get("valid_to", "9999") and not r.get("revoked"):
                out[r["key"]] = r
        return out

    def record_period(self, period: str, rows: list[dict]) -> dict:
        return self.append("period", {"period": period, "rows": rows})

    def history(self, as_of: str) -> list[dict]:
        """Past periods only; later periods are refused by construction."""
        return [r for r in self.events("period") if r["period"] < as_of]

    def zero_last(self, as_of: str) -> set:
        """(gate, group) pairs that were green and got nothing in the latest recorded period before as_of."""
        h = self.history(as_of)
        if not h:
            return set()
        return {(r["gate"], r["group"]) for r in h[-1]["rows"] if r.get("green") and r.get("alloc", 0) <= 0}
