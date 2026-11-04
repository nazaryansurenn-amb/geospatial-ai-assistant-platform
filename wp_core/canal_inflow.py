"""Flow at the Lower Hrazdan head, capped at the main canal capacity (locked plan v6, owner decision 1 and 2c hydraulics).

The main canal carries at most 12 m3/s (assumed design value, owner 2026-11-02; config/canal_ops_network_v2.json). An
owner or operator flow above it is accepted only up to the capacity; the excess is shown as spill, never delivered.
"""
from __future__ import annotations

import json
import math
from pathlib import Path

CFG = json.loads((Path(__file__).resolve().parents[1] / "config/canal_ops_network_v2.json").read_text(encoding="utf-8"))
MAIN_CAPACITY_M3S = CFG["main_canal"]["capacity_lps"] / 1000.0


def cap_inflow(q_m3s, capacity_m3s: float = MAIN_CAPACITY_M3S) -> dict:
    """Requested head flow -> accepted flow and spill (m3/s). Refuses negative or non-numeric flows."""
    if isinstance(q_m3s, bool) or not isinstance(q_m3s, (int, float)) or not math.isfinite(q_m3s) or q_m3s < 0:
        raise ValueError("head flow must be a finite number of m3/s, zero or more")
    accepted = min(float(q_m3s), capacity_m3s)
    spill = float(q_m3s) - accepted
    return {"requested_m3s": float(q_m3s), "accepted_m3s": round(accepted, 6), "spill_m3s": round(spill, 6),
            "capped": spill > 0, "capacity_m3s": capacity_m3s, "capacity_basis": CFG["main_canal"]["capacity_basis"]}


def dekad_volume_m3(q_m3s: float, days: int) -> float:
    """Volume of a constant flow over a period: Q x seconds."""
    return q_m3s * days * 86400.0
