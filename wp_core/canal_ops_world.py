"""Real-canal world for the Irrigation Assistant (canal_ops_sim_v1): the synthetic network on the real Lower Hrazdan
canal, 2025 dekad demand (plan values, no hindsight) and demand-derived capacities.

Inputs (all built by fixed code, see their manifests):
  data/analysis/canal_ops_sim_v1/network/{branches,gates,parcel_gate}.parquet      synthetic network (6 km limit)
  data/analysis/canal_ops_sim_v1/demand/parcel_dekad_2025.parquet                 net demand per parcel and dekad
  data/analysis/canal_ops_sim_v1/demand/capacity_reference_2021_2024.parquet      2021-2024 only (capacities)

Groups for the sharing rules: perennial (orchard, vineyard, perennial), sensitive_field (stage weight >= 0.8 and not
ripening, decided by the demand step from data up to the plan date), other_green_field (other field land with demand).
Capacity per branch = P90 of 2021-2024 dekad gross demand x 1.10, clamped to 0.8-1.6 l/s per irrigated ha, at least
50 l/s, rounded to 10 l/s ("demand-derived estimate, not design capacity"). Normal head supply per dekad = sum of branch
capacities x 0.92 x monthly factor ("illustrative supply scenario").
"""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path

import numpy as np
import pandas as pd

from wp_core import canal_allocation as ca

ROOT = Path(__file__).resolve().parents[1]
NET = ROOT / "data/analysis/canal_ops_sim_v1/network"
DEM = ROOT / "data/analysis/canal_ops_sim_v1/demand"
PERENNIAL = {"orchard", "vineyard", "perennial"}


def available() -> bool:
    return (NET / "branches.parquet").exists() and (DEM / "parcel_dekad_2025.parquet").exists()


@lru_cache(maxsize=1)
def _network():
    b = pd.read_parquet(NET / "branches.parquet")
    g = pd.read_parquet(NET / "gates.parquet")
    pg = pd.read_parquet(NET / "parcel_gate.parquet")
    pg = pg[pg.status == "assigned"].copy()
    gl = dict(zip(g.gate, g.label))
    bl = dict(zip(b.branch, b.label))
    pg["gate_label"] = pg.gate.map(gl)
    pg["branch_label"] = pg.branch.map(bl)
    branches = pd.DataFrame({"branch": b.label, "stage": b.stage, "chainage_m": b.chainage_m,
                             "upstream_m": b.upstream_m.fillna(0), "offtake_type": b.offtake_type,
                             "community": b.community, "ha": b.ha, "lon": b.lon, "lat": b.lat})
    gates = pd.DataFrame({"gate": g.label, "branch": g.branch_label, "dist_km": g.dist_km, "ha": g.ha,
                          "community": g.community, "lon": g.lon, "lat": g.lat})
    return branches, gates, pg[["internal_parcel_id", "gate_label", "branch_label"]]


def _group(d: pd.DataFrame) -> pd.Series:
    sens = (d.stage_weight >= 0.8) & ~d.ripening.astype(bool)
    return np.where(d.ltype.isin(PERENNIAL), "perennial", np.where(sens, "sensitive_field", "other_green_field"))


@lru_cache(maxsize=1)
def _demand_2025():
    cols = ["pid", "dekad_id", "days", "ltype", "area_ha", "stage_weight", "ripening", "net_m3_full_exp", "net_m3_typical_exp"]
    d = pd.read_parquet(DEM / "parcel_dekad_2025.parquet", columns=cols)
    _, _, pg = _network()
    d = d.merge(pg, left_on="pid", right_on="internal_parcel_id", how="inner")
    d = d[d.ltype.isin(PERENNIAL | {"field"}) & (d.net_m3_full_exp > 0)].copy()
    d["group"] = _group(d)
    d["ltype_eff"] = np.where(d.ltype == "field", "field", d.ltype)
    d["w"] = d.area_ha
    agg = (d.assign(sw=d.stage_weight * d.w, rp=d.ripening.astype(float) * d.w)
             .groupby(["dekad_id", "gate_label", "group"], as_index=False)
             .agg(ha=("w", "sum"), net_m3=("net_m3_full_exp", "sum"), net_m3_typical=("net_m3_typical_exp", "sum"),
                  sw=("sw", "sum"), rp=("rp", "sum"), ltype=("ltype_eff", lambda s: s.mode().iat[0]), days=("days", "first")))
    agg["stage_weight"] = agg.sw / agg.ha
    agg["ripening"] = (agg.rp / agg.ha) > 0.5
    agg = agg.rename(columns={"gate_label": "gate"}).drop(columns=["sw", "rp"])
    return agg


@lru_cache(maxsize=1)
def capacities() -> pd.Series:
    """Branch capacity (l/s) from 2021-2024 demand only (no 2025: no hindsight)."""
    branches, gates, pg = _network()
    r = pd.read_parquet(DEM / "capacity_reference_2021_2024.parquet", columns=["pid", "year", "dekad_id", "days", "net_m3_full_exp"])
    r = r[(r.year <= 2024) & (r.net_m3_full_exp > 0)].merge(pg, left_on="pid", right_on="internal_parcel_id", how="inner")
    eff = ca.CFG["water_chain"]["field_efficiency"]["field"]
    per = r.groupby(["branch_label", "dekad_id"]).agg(m3=("net_m3_full_exp", "sum"), days=("days", "first"))
    per["lps"] = per.m3 / eff * 1000 / (per.days * 86400)
    p90 = per.groupby(level=0).lps.quantile(0.9) * ca.CFG["capacity"]["headroom"]
    ha = branches.set_index("branch").ha
    lo, hi = ca.CFG["capacity"]["clamp_lps_per_ha"]
    cap = p90.reindex(ha.index).fillna(0).clip(lower=lo * ha, upper=hi * ha)
    cap = cap.clip(lower=ca.CFG["capacity"]["branch_min_lps"])
    return (cap / ca.CFG["capacity"]["rounding_lps"]).round() * ca.CFG["capacity"]["rounding_lps"]


def capacity_basis() -> dict:
    """Share of branches whose capacity is set by the clamp or the minimum rather than by demand (G0.3 report)."""
    branches, _, pg = _network()
    cap = capacities()
    ha = branches.set_index("branch").ha
    lo, hi = ca.CFG["capacity"]["clamp_lps_per_ha"]
    by_clamp = ((cap <= lo * ha + 10) | (cap >= hi * ha - 10) | (cap <= ca.CFG["capacity"]["branch_min_lps"])).mean()
    return {"branches": int(len(cap)), "share_set_by_clamp_or_minimum": round(float(by_clamp), 3),
            "label": ca.CFG["labels"]["capacity"]}


SUPPLY_CFG = ROOT / "config/canal_ops_supply_v1_1.json"


@lru_cache(maxsize=1)
def normal_supply_p90() -> dict:
    """Supply v1.1: per calendar dekad ('07-D2'), the P90 over 2021-2024 of the zone's gross need at the head (m3)."""
    import json
    scfg = json.loads(SUPPLY_CFG.read_text(encoding="utf-8"))
    branches, gates, pg = _network()
    cost = dict(zip(gates.gate, ca.head_cost(gates, branches.assign(capacity_lps=0))))
    eff = ca.CFG["water_chain"]["field_efficiency"]["field"]
    r = pd.read_parquet(DEM / "capacity_reference_2021_2024.parquet", columns=["pid", "year", "dekad_id", "net_m3_full_exp"])
    r = r[r.year.isin(scfg["years"]) & (r.net_m3_full_exp > 0)].merge(pg, left_on="pid", right_on="internal_parcel_id")
    r["head_m3"] = r.net_m3_full_exp / eff * r.gate_label.map(cost)
    per = r.groupby(["year", "dekad_id"]).head_m3.sum().reset_index()
    per["cal"] = per.dekad_id.str[5:]
    return per.groupby("cal").head_m3.quantile(scfg["percentile"]).to_dict()


def real_world(ledger_path, as_of="2025-07-D3"):
    import json
    from wp_core.canal_ops_ledger import Ledger
    from wp_core.canal_ops_tools import World
    branches, gates, _ = _network()
    branches = branches.assign(capacity_lps=branches.branch.map(capacities()))
    d = _demand_2025()
    demand = {p: g.drop(columns=["dekad_id"]).reset_index(drop=True) for p, g in d.groupby("dekad_id")}
    days = {p: int(g.days.iat[0]) for p, g in d.groupby("dekad_id")}
    month = {p: int(p[5:7]) for p in demand}
    if SUPPLY_CFG.exists():                                   # supply v1.1 (owner): typical peak need of the zone
        shut = set(json.loads(SUPPLY_CFG.read_text(encoding="utf-8"))["shutdown_months"])
        p90 = normal_supply_p90()
        head = {p: 0.0 if month[p] in shut else float(p90.get(p[5:], 0.0)) for p in demand}
    else:                                                     # v1: sum of branch capacities x monthly factor
        total_lps = float(branches.capacity_lps.sum()) * 0.92
        head = {p: total_lps * 86.4 * days[p] * ca.CFG["supply"]["monthly_factor"][str(month[p])] for p in demand}
    return World(branches, gates, demand, days, head, Ledger(ledger_path), as_of)
