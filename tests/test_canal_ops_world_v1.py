"""Real-canal world (canal_ops_sim_v1): synthetic network + 2025 dekad demand + demand-derived capacities."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from wp_core import canal_allocation as ca  # noqa: E402
from wp_core import canal_ops_tools as T  # noqa: E402
from wp_core import canal_ops_world as cw  # noqa: E402

pytestmark = pytest.mark.skipif(not cw.available(), reason="network/demand outputs not built")


@pytest.fixture(scope="module")
def world(tmp_path_factory):
    return cw.real_world(tmp_path_factory.mktemp("w") / "l.sqlite", as_of="2025-07-D2")


def test_world_shape(world):
    assert len(world.branches) == 75 and len(world.gates) == 426
    assert set(world.gates.branch) <= set(world.branches.branch)
    assert all(p.startswith("2025-") for p in world.demand)


def test_capacities_rules():
    cap = cw.capacities()
    assert (cap >= ca.CFG["capacity"]["branch_min_lps"]).all()
    assert (cap % ca.CFG["capacity"]["rounding_lps"] == 0).all()
    assert 0 <= cw.capacity_basis()["share_set_by_clamp_or_minimum"] <= 1


def test_demand_groups_and_no_zero_rows(world):
    d = world.demand["2025-07-D2"]
    assert set(d.group) <= set(ca.GROUPS) and (d.net_m3 > 0).all()
    assert set(d.gate) <= set(world.gates.gate)


def test_march_supply_is_shutdown(world):
    assert world.head_capacity_m3["2025-03-D1"] == 0


def test_all_rules_hold_invariants_on_real_network(world):
    days = world.days["2025-07-D2"]
    d = world.demand["2025-07-D2"]
    caps = ca.branch_volume_cap(world.branches, days).to_dict()
    for f in (1.0, 0.3, 0.1):
        s = world.head_capacity_m3["2025-07-D2"] * f
        for p in (ca.rule_A(d, world.gates, world.branches, s, days), ca.rule_B(d, world.gates, world.branches, s, days)):
            assert ca.invariants(p, caps) == []


def test_run_dekad_real_passes_checker(world):
    import irrigation_assistant as ia
    out = ia.run_dekad(world, "2025-07-D2", "short40")
    assert out["checker"] == [] and "{{" not in out["answer"]
    with pytest.raises(T.ToolRefused):
        T.compare_rules(world, "2025-07-D3")          # after the planning date


def test_supply_v1_1_is_typical_peak_need(world):
    """Supply v1.1: normal head supply follows 2021-2024 P90 need, so shortage scenarios really bite."""
    p = "2025-07-D2"
    d, days = world.demand[p], world.days[p]
    cost = dict(zip(world.gates.gate, ca.head_cost(world.gates, world.branches)))
    head_need = float((d.net_m3 / 0.6 * d.gate.map(cost)).sum())
    assert 0.7 * head_need < world.head_capacity_m3[p] < 1.5 * head_need
    m40 = ca.metrics(ca.rule_A(d, world.gates, world.branches, world.head_capacity_m3[p] * 0.6, days))
    assert m40["need_met_pct"] < 80


def test_all_floor_gives_every_gate_water(world):
    p = "2025-07-D2"
    d, days = world.demand[p], world.days[p]
    s = world.head_capacity_m3[p] * 0.6
    plan = ca.simulate_plan(d, world.gates, world.branches, s, days, all_floor=0.3)
    g = plan.gate_totals()
    assert ((g.alloc / g.need) >= 0.3 - 1e-6).all() and ca.invariants(plan) == []
