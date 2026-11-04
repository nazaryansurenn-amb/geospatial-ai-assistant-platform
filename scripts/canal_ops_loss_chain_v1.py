"""Loss chain v1 for the tree network v2 (owner, 2026-11-02): the main canal loses 15 % on average, the secondary
network (secondary and tertiary canals together) 30 % on average; field application efficiency (0.6) stays separate.

  canal_ops_loss_chain_v1.py build  -> config/canal_ops_losses_v1.json (calibrated rates, frozen)
                                       data/analysis/canal_ops_sim_v1/loss_chain_v1/ (loss at every point, summary, manifest)

How the averages are defined (the water actually carried decides the weights):
  - main canal: of the water released at the head for the offtakes, the share lost before it reaches its offtake,
    weighted by the head water each offtake takes;
  - secondary network: of the water entering a secondary canal at its main-canal gate, the share lost before it
    reaches the farm outlet, weighted by the water entering for each outlet.
Weights = each farm outlet's plan-time gross need summed over 2021-2024 (no 2025: no hindsight), as for the canal sizing.
Losses grow with distance: keep = (1 - rate)^km on every link, one rate for the main canal, and one factor on the
secondary (2 %/km) and tertiary (3 %/km) rates of config/canal_ops_network_v2.json so their 2 : 3 ratio is kept.
Each rate is found by bisection so the weighted average equals the owner's figure exactly. Simulation: assumed losses.
"""
from __future__ import annotations

import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from wp_core import canal_tree_allocation as A  # noqa: E402
from wp_core import read_guard  # noqa: E402

# outputs follow the network in use (config/canal_ops_network_active.json): v1 on network v2, v2 on network v3
CFG_OUT = ROOT / A.NETWORK["losses"]
OUT = ROOT / A.NETWORK["loss_chain_dir"]
NV2 = A.NV2
REF = ROOT / "data/analysis/canal_ops_sim_v1/demand/capacity_reference_2021_2024.parquet"
TARGET = {"main_canal": 0.15, "secondary_network": 0.30}
BASE = A.NET_CFG["sizing"]["loss_pct_per_km"]           # secondary 2, tertiary 3 (ratio kept)


def weights(nodes, pn, ref):
    """Gross plan-time need per farm outlet, 2021-2024 (m3)."""
    pn = pn[pn.canal_demand == "canal demand"]
    eff = A.ca.CFG["water_chain"]["field_efficiency"]["field"]
    r = ref[ref.year <= 2024].groupby("pid").net_m3_full_exp.sum() / eff
    w = pn.merge(r.rename("w"), left_on="internal_parcel_id", right_index=True).groupby("outlet").w.sum()
    return w[w > 0]


def chain(nodes, rates):
    t = A.build_tree(nodes, rates)
    return t


def averages(t, w):
    nd = t.nodes
    off = {o: next(n for n in t.path[o] if nd.at[n, "kind"] in ("main_gate", "main_pump")) for o in w.index}
    c_off = pd.Series({o: t.cost[off[o]] for o in w.index})
    c_out = pd.Series({o: t.cost[o] for o in w.index})
    head_w = w * c_out                                     # head water for each outlet's delivery
    main = float(((1 - 1 / c_off) * head_w).sum() / head_w.sum())
    off_w = w * c_out / c_off                              # water entering the secondary for each outlet
    sec = float(((1 - c_off / c_out) * off_w).sum() / off_w.sum())
    return main, sec


def solve(f, target, lo=0.0, hi=20.0):
    for _ in range(80):
        mid = (lo + hi) / 2
        if f(mid) < target:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2


def build():
    with read_guard.ReadGuard() as g:
        nodes = pd.read_parquet(NV2 / "nodes.parquet")
        pn = pd.read_parquet(NV2 / "parcel_node.parquet", columns=["internal_parcel_id", "outlet", "canal_demand"])
        ref = pd.read_parquet(REF, columns=["pid", "year", "net_m3_full_exp"])
    w = weights(nodes, pn, ref)
    # the two are coupled (the head water each outlet needs depends on the secondary losses): solve in turn until both
    # averages hold
    main_rate, factor = BASE["secondary"] / 4, 1.0
    for _ in range(8):
        main_rate = solve(lambda r: averages(chain(nodes, {"main": r, "secondary": BASE["secondary"] * factor,
                                                           "tertiary": BASE["tertiary"] * factor}), w)[0], TARGET["main_canal"])
        factor = solve(lambda k: averages(chain(nodes, {"main": main_rate, "secondary": BASE["secondary"] * k,
                                                        "tertiary": BASE["tertiary"] * k}), w)[1],
                       TARGET["secondary_network"], 0.0, 20.0)
    rates = {"main": round(main_rate, 6), "secondary": round(BASE["secondary"] * factor, 6),
             "tertiary": round(BASE["tertiary"] * factor, 6)}
    t = chain(nodes, rates)
    main, sec = averages(t, w)
    nd = t.nodes
    off_of = {n: next((x for x in t.path.get(n, []) if nd.at[x, "kind"] in ("main_gate", "main_pump")), None) for n in t.path}
    rows = []
    for n in nd.index:
        r = nd.loc[n]
        o = off_of.get(n) or (n if r.kind in ("main_gate", "main_pump") else None)
        loss_head = 1 - 1 / t.cost[n]
        rows.append({"node": n, "kind": r.kind, "level": int(r.level), "link_km": round(A.link_km(r), 3),
                     "link_rate_pct_per_km": A.link_rate(r, rates), "link_loss_pct": round(100 * (1 - r.keep), 3),
                     "loss_from_head_pct": round(100 * loss_head, 3),
                     "loss_from_offtake_pct": (round(100 * (1 - t.cost[o] / t.cost[n]), 3)
                                               if o and r.kind not in ("main_canal", "main_gate", "main_pump") else None)})
    table = pd.DataFrame(rows)
    out_rows = table[table.kind.isin(A.OUTLET_KINDS)]
    summary = {
        "version": OUT.name, "network": A.NETWORK["network_dir"], "targets": TARGET, "achieved": {"main_canal": round(main, 6), "secondary_network": round(sec, 6)},
        "rates_pct_per_km": rates, "factor_on_secondary_and_tertiary": round(factor, 6),
        "previous_rates_pct_per_km": {"main": A.NET_CFG["main_canal"]["loss_pct_per_km"], **BASE},
        "weights": "plan-time gross need per farm outlet, 2021-2024 (no 2025)",
        "outlets": int(len(out_rows)),
        "loss_from_offtake_pct_at_outlets": {k: round(float(v), 2) for k, v in
                                             out_rows.loss_from_offtake_pct.describe(percentiles=[.1, .5, .9]).items()
                                             if k in ("min", "10%", "50%", "90%", "max")},
        "loss_from_head_pct_at_offtakes": {r.node: r.loss_from_head_pct for r in table[table.kind.isin(["main_gate", "main_pump"])].itertuples()},
        "field_application_efficiency": A.ca.CFG["water_chain"]["field_efficiency"]["field"],
        "label": "assumed losses (owner averages, distance-proportional), simulation",
    }
    OUT.mkdir(parents=True, exist_ok=True)
    table.to_parquet(OUT / "nodes.parquet", index=False)
    (OUT / "summary.json").write_text(json.dumps(summary, indent=1, ensure_ascii=False), encoding="utf-8")
    cfg = {"version": CFG_OUT.stem, "network": A.NETWORK["network_dir"],
           "frozen": f"{datetime.now(timezone.utc):%Y-%m-%d}, owner: main canal loses 15 % and the secondary network "
                     "(secondary + tertiary) 30 % on average; field efficiency 0.6 separate. Change only as a new version.",
           "targets": TARGET, "rates_pct_per_km": rates, "definition": __doc__.split("How the averages")[1].strip()}
    CFG_OUT.write_text(json.dumps(cfg, indent=1, ensure_ascii=False), encoding="utf-8")
    man = {"files_read": g.files, "read_guard_violations": g.violations,
           "outputs": {f.name: hashlib.sha256(f.read_bytes()).hexdigest() for f in sorted(OUT.glob("*")) if f.name != "manifest.json"},
           "config": {CFG_OUT.name: hashlib.sha256(CFG_OUT.read_bytes()).hexdigest()},
           "script_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
           "not_used": "SQL archive and every archive-derived layer: not read"}
    (OUT / "manifest.json").write_text(json.dumps(man, indent=1), encoding="utf-8")
    print(json.dumps({k: summary[k] for k in ("achieved", "rates_pct_per_km", "loss_from_offtake_pct_at_outlets")}, indent=1))
    return summary


if __name__ == "__main__":
    if sys.argv[1:2] == ["build"]:
        build()
    else:
        sys.exit(__doc__)
