"""canal_ops_sim_v1 core: sharing rules, invariants, turns, soil water, ledger approvals and the Checker."""
import numpy as np
import pytest

from wp_core import canal_allocation as ca
from wp_core.canal_ops_checker import check
from wp_core.canal_ops_ledger import Ledger
from wp_core.canal_toy_network import toy_network

DAYS = 11


@pytest.fixture(scope="module")
def net():
    return toy_network()


def need_head(d):
    return float((d.net_m3 / 0.6).sum()) * 1.1


def caps(b):
    return ca.branch_volume_cap(b, DAYS).to_dict()


@pytest.mark.parametrize("factor", [1.2, 0.9, 0.6, 0.3, 0.05])
def test_all_rules_hold_invariants(net, factor):
    b, g, d = net
    s = need_head(d) * factor
    for p in (ca.rule_A(d, g, b, s, DAYS), ca.rule_B(d, g, b, s, DAYS),
              ca.simulate_plan(d, g, b, s, DAYS, group_weights={"other_green_field": 0.5}, branch_floors={"B6": 0.6})):
        assert ca.invariants(p, caps(b)) == []
        assert p.used <= s * (1 + 1e-6) + 1


def test_efficiency_applied_once(net):
    b, g, d = net
    p = ca.rule_B(d, g, b, 1e12, DAYS)
    assert np.allclose(p.alloc.need, p.alloc.net_m3 / 0.6)


def test_shortage_never_gives_more_than_normal(net):
    b, g, d = net
    s = need_head(d) * 0.9
    for rule in (ca.rule_A, ca.rule_B):
        hi = rule(d, g, b, s, DAYS).gate_totals().alloc
        lo = rule(d, g, b, s * 0.6, DAYS).gate_totals().alloc
        assert (lo <= hi + 1e-6).all()


def test_level_order_rule_A(net):
    b, g, d = net
    p = ca.rule_A(d, g, b, need_head(d) * 0.4, DAYS)
    m = ca.metrics(p)["need_met_by_group_pct"]
    assert m["young_perennial"] >= m["perennial"] >= m.get("other_green_field", 0)


def test_floors_not_met_is_reported_not_blocked(net):
    b, g, d = net
    p = ca.rule_A(d, g, b, need_head(d) * 0.05, DAYS)
    assert not p.floors_met and p.notes and ca.invariants(p, caps(b)) == []


def test_zero_streak_floor(net):
    b, g, d = net
    s = need_head(d) * 0.5
    p0 = ca.rule_A(d, g, b, s, DAYS)
    zero = {(r.gate, r.group) for r in p0.alloc.itertuples() if r.alloc <= 1e-6}
    assert zero                                            # at -40% some green groups get nothing ...
    p1 = ca.rule_A(d, g, b, s, DAYS, zero_last=zero)
    got = p1.alloc.set_index(["gate", "group"]).alloc
    assert all(got[k] > 0 for k in zero)                   # ... so the next dekad gives them a floor first


def test_branch_floor_respected(net):
    b, g, d = net
    s = need_head(d) * 0.6
    p = ca.simulate_plan(d, g, b, s, DAYS, branch_floors={"B6": 0.6})
    t = p.alloc[p.alloc.branch == "B6"]
    assert t.alloc.sum() >= 0.6 * t.need.sum() - 1


def test_turns_hours_formula(net):
    b, g, d = net
    p = ca.rule_B(d, g, b, need_head(d) * 0.5, DAYS)
    t = ca.build_turns(p, b, DAYS)
    gt = p.gate_totals().alloc
    r = t.iloc[0]
    expect = gt[r.gate] / (0.8 * min(60, b.set_index("branch").capacity_lps[r.branch]) * 3.6) + 2
    assert abs(r.hours - expect) < 0.1
    assert {"fits_dekad", "night", "section_turns"} <= set(t.columns)


def test_dekad_days():
    assert ca.dekad_days(2025, 7, 3) == 11 and ca.dekad_days(2025, 2, 3) == 8 and ca.dekad_days(2025, 6, 3) == 10


def test_bucket_balance():
    dr, eta, ks = ca.bucket_update(40, 80, 0.45, 70, 5, 30, days=10)
    assert 0 <= dr <= 80 and eta <= 70 and 0 <= ks <= 1
    assert abs((40 + eta - 5 - 30) - dr) < 1e-9 or dr in (0, 80)


def test_yield_proxy_band():
    y = ca.yield_proxy(400, 500)
    assert y["low"] < y["loss"] < y["high"] and ca.yield_proxy(0, 0) is None


# ---------------------------------------------------------------- ledger
def test_ledger_chain_and_tamper(tmp_path):
    L = Ledger(tmp_path / "l.sqlite")
    L.append("run", {"a": 1})
    L.append("run", {"a": 2})
    assert L.verify()
    import sqlite3
    with sqlite3.connect(tmp_path / "l.sqlite") as c:
        c.execute("UPDATE events SET payload='{\"a\": 9}' WHERE id=1")
    assert not L.verify()


def test_approval_needs_token_and_current_inputs(tmp_path):
    L = Ledger(tmp_path / "l.sqlite")
    pid = L.save_proposal("S1", "h1", {"rule": "A"}, "agent")
    with pytest.raises(PermissionError):
        L.decide(pid, "approve", "made-up-token", "h1", "owner")          # a 'yes' without a UI token
    tok = L.issue_token(pid)
    with pytest.raises(PermissionError):
        L.decide(pid, "approve", tok, "h2", "owner")                      # inputs changed -> lapses
    tok = L.issue_token(pid)
    L.decide(pid, "approve", tok, "h1", "owner")
    with pytest.raises(PermissionError):
        L.decide(pid, "approve", tok, "h1", "owner")                      # token replay


def test_rejected_proposal_not_reproposed(tmp_path):
    L = Ledger(tmp_path / "l.sqlite")
    pid = L.save_proposal("S1", "h1", {"rule": "A", "B3": 0.5}, "agent")
    L.decide(pid, "reject", L.issue_token(pid), "h1", "owner", "tail end starved")
    with pytest.raises(ValueError):
        L.save_proposal("S2", "h1", {"rule": "A", "B3": 0.5}, "agent")


def test_history_and_zero_last(tmp_path):
    L = Ledger(tmp_path / "l.sqlite")
    L.record_period("2025-07-D1", [{"gate": "B1.1", "group": "other_green_field", "green": True, "alloc": 0}])
    L.record_period("2025-07-D3", [{"gate": "B1.1", "group": "other_green_field", "green": True, "alloc": 5}])
    assert [h["period"] for h in L.history("2025-07-D2")] == ["2025-07-D1"]
    assert L.zero_last("2025-07-D2") == {("B1.1", "other_green_field")}


def test_constraints_versioned(tmp_path):
    L = Ledger(tmp_path / "l.sqlite")
    L.add_constraint({"key": "B7_floor", "value": 0.6, "valid_from": "2025-07"}, "owner")
    L.add_constraint({"key": "B7_floor", "value": 0.5, "valid_from": "2025-07"}, "owner")
    c = L.constraints("2025-07-D2")
    assert c["B7_floor"]["version"] == 2 and c["B7_floor"]["value"] == 0.5


# ---------------------------------------------------------------- Checker
RES = {"R1": {"B3": {"pct": 62.5, "m3": 12000}, "B7": {"pct": 48.0}}}


def test_checker_fills_and_passes():
    v = check("B3 receives {{R1.B3.pct}} % of need, more than B7 at {{R1.B7.pct}} %.", RES)
    assert v.ok and "62.5" in v.text


def test_checker_rejects_untraced_digit():
    assert not check("B3 receives 70 % of need.", RES).ok


def test_checker_allows_ids():
    assert check("Gate B3.2 in stage II, dekad D2: see {{R1.B3.pct}}.", RES).ok


def test_checker_comparison_must_match_values():
    assert not check("{{R1.B7.pct}} is more than {{R1.B3.pct}}.", RES).ok
    assert check("{{R1.B3.pct}} is more than {{R1.B7.pct}}.", RES).ok


@pytest.mark.parametrize("bad", ["Please open gate B3 now.", "Water owed to B3.", "This is the command area.",
                                 "Ձեր հերթն է։", "Ваша очередь завтра.", "Parcel 04-002-9199-0012 is short.",
                                 "Built from the SQL archive.", "Point 40.123456 is dry."])
def test_checker_blocks_wording_and_restricted(bad):
    assert not check(bad, RES).ok


def test_checker_missing_placeholder_and_labels():
    assert not check("See {{R9.x}}.", RES).ok
    assert not check("Plan ready.", RES, profile="plan").ok
    assert check("Plan ready. Simulation. Illustrative sharing rule, not legal priority.", RES, profile="plan").ok


def test_checker_echo_and_data_prefix():
    r = {"R2": {"supply_m3": 100, "need_m3": 200}}
    assert check("Supply {{R2.data.supply_m3}} m3 at a 40% cut in 2025-07-D2 (R2).", r, echo="drop 40% 2025-07-D2").ok
    assert not check("Need is 70 percent.", r, echo="drop 40%").ok          # 70 was not in the question
