"""A small invented 6-branch network for building and testing the canal core and the agents before the synthetic
network of the real canal exists. Deterministic; no real names, places or data."""
from __future__ import annotations

import numpy as np
import pandas as pd

LTYPE = {"perennial": "orchard", "young_perennial": "orchard", "sensitive_field": "field", "other_green_field": "field"}


def toy_network(seed: int = 7):
    rng = np.random.default_rng(seed)
    branches = pd.DataFrame({
        "branch": [f"B{i}" for i in range(1, 7)],
        "stage": ["I", "I", "I", "II", "II", "II"],
        "chainage_m": [1500, 5200, 9800, 1200, 4600, 8300],
        "upstream_m": [0, 0, 0, 12000, 12000, 12000],
        "offtake_type": ["gate", "gate", "pump", "gate", "gate", "pump"],
    })
    gates, demand = [], []
    for b in branches.itertuples():
        for k in range(1, 4):
            gid = f"{b.branch}.{k}"
            ha = float(rng.integers(12, 30))
            gates.append({"gate": gid, "branch": b.branch, "dist_km": round(0.6 * k + rng.random(), 2), "ha": ha})
            shares = {"perennial": 0.3, "young_perennial": 0.05, "sensitive_field": 0.35, "other_green_field": 0.3}
            for grp, s in shares.items():
                demand.append({"gate": gid, "group": grp, "ltype": LTYPE[grp], "ha": ha * s,
                               "net_m3": ha * s * 10 * float(rng.uniform(45, 70)),   # 45-70 mm net per dekad
                               "stage_weight": 1.0, "ripening": False})
    gates = pd.DataFrame(gates)
    demand = pd.DataFrame(demand)
    # capacity from demand: peak gross at 1.2 l/s/ha, min 50 l/s (toy stand-in for the P90 rule)
    ha_b = gates.groupby("branch").ha.sum()
    branches["capacity_lps"] = branches.branch.map(lambda b: max(50.0, round(1.2 * ha_b[b], -1)))
    return branches, gates, demand
