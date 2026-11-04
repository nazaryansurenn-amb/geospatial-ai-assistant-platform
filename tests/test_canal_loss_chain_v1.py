"""Loss chain v1 (owner, 2026-11-02): the main canal loses 15 % and the secondary network (secondary + tertiary canals)
30 % on average, distance-proportional; field application efficiency 0.6 stays separate."""
import json
import sys
from pathlib import Path

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))
from wp_core import canal_tree_allocation as A  # noqa: E402
from wp_core import read_guard as rg  # noqa: E402

OUT = ROOT / A.NETWORK["loss_chain_dir"]                 # the loss chain of the network in use
pytestmark = pytest.mark.skipif(not (OUT / "summary.json").exists(), reason="loss chain not built")


@pytest.fixture(scope="module")
def summary():
    return json.loads((OUT / "summary.json").read_text(encoding="utf-8"))


def test_owner_averages_are_met_exactly(summary):
    assert summary["targets"] == {"main_canal": 0.15, "secondary_network": 0.30}
    assert summary["achieved"]["main_canal"] == pytest.approx(0.15, abs=1e-4)
    assert summary["achieved"]["secondary_network"] == pytest.approx(0.30, abs=1e-4)


def test_the_averages_recompute_from_the_tree_in_use(summary):
    import canal_ops_loss_chain_v1 as L
    nodes = pd.read_parquet(L.NV2 / "nodes.parquet")
    pn = pd.read_parquet(L.NV2 / "parcel_node.parquet", columns=["internal_parcel_id", "outlet", "canal_demand"])
    ref = pd.read_parquet(L.REF, columns=["pid", "year", "net_m3_full_exp"])
    main, sec = L.averages(A.build_tree(nodes), L.weights(nodes, pn, ref))     # default rates = the frozen config
    assert main == pytest.approx(0.15, abs=1e-4) and sec == pytest.approx(0.30, abs=1e-4)
    assert A.LOSS_PCT_PER_KM == summary["rates_pct_per_km"]


def test_secondary_to_tertiary_ratio_kept_and_field_efficiency_separate(summary):
    r = summary["rates_pct_per_km"]
    assert r["tertiary"] / r["secondary"] == pytest.approx(3 / 2, rel=1e-4)
    assert summary["field_application_efficiency"] == 0.6 == A.ca.CFG["water_chain"]["field_efficiency"]["field"]


def test_loss_at_every_point_grows_downstream():
    t = pd.read_parquet(OUT / "nodes.parquet").set_index("node")
    tree = A.tree()
    for n, r in t.iterrows():
        par = tree.nodes.at[n, "parent"]
        if par is not None:
            assert r.loss_from_head_pct >= t.at[par, "loss_from_head_pct"] - 1e-9
        assert 0 <= r.loss_from_head_pct < 100
    offt = t[t.kind.isin(["main_gate", "main_pump"])]
    assert offt.loss_from_head_pct.min() > 0 and offt.loss_from_head_pct.max() < 30


def test_built_under_the_read_guard():
    man = json.loads((OUT / "manifest.json").read_text(encoding="utf-8"))
    assert man["read_guard_violations"] == [] and man["files_read"]
    assert all(rg.problem(ROOT / f) is None for f in man["files_read"])


def test_head_to_outlet_share_is_about_0_6():
    """0.85 (main canal) x 0.70 (secondary network): about 60 % of the head water reaches the farm outlets."""
    from wp_core import canal_ops_tools_v6 as V
    import tempfile
    w = V.v6_world(Path(tempfile.mkdtemp()) / "l.sqlite", "2025-07-D3")
    per = "2025-07-D2"
    sup, _ = A.head_supply(w.head_capacity_m3[per] * 0.8, w.days[per])
    m = A.metrics(A.rule_A(w.demand[per], sup, w.days[per], w.maps.tree))
    assert 55 <= m["delivered_share_of_head_pct"] <= 65
