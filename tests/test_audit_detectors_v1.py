"""Data-mistake auditor v1: detectors on synthetic fixtures, evidence, ledger, review server and the v1.5 apply job."""
from __future__ import annotations

import copy
import http.client
import json
import re
import threading
from http.server import ThreadingHTTPServer

import geopandas as gpd
import numpy as np
import pandas as pd
import pytest
from shapely.geometry import box

from wp_core import audit_detectors as ad
import audit_review_v1 as arv

CODE = re.compile(r"\d{2}-\d{3}-\d{4}-\d{4}")
YEARS = [2021, 2022, 2023, 2024, 2025, 2026]


def cfg_for(n_parcels):
    cfg = copy.deepcopy(ad.load_config())
    cfg["expected_parcels"] = n_parcels
    cfg["checks"]["AUD02_CLOUD_LEAK_DATE"]["min_parcels"] = 2
    return cfg


PARCELS = {  # pid: type6, width, area, owner_label
    "parcel_nocrop": ("NO_CROP", 30.0, 1.0, None),
    "parcel_nocrop_owner": ("NO_CROP", 30.0, 1.0, "NO_CROP"),
    "parcel_nonfarm_wide": ("NON_FARM", 25.0, 0.8, None),
    "parcel_nonfarm_strip": ("NON_FARM", 8.0, 0.2, None),
    "parcel_field_idle": ("FIELD_CROP", 22.0, 0.9, None),
    "parcel_orchard_brown": ("ORCHARD", 21.0, 0.7, None),
    "parcel_vine_cycles": ("VINEYARD", 15.0, 0.4, None),
    "parcel_greenhouse": ("GREENHOUSE", 12.0, 0.1, None),
    "parcel_sliver": (None, 3.0, 0.004, None),
    "parcel_ok": ("FIELD_CROP", 40.0, 2.0, None),
}


def make_ctx(daily=None):
    p = pd.DataFrame([{"internal_parcel_id": k, "type6": t, "mean_width_m": w, "area_ha": a, "owner_label": o,
                       "item_id": f"f{n:05d}", "community": "C1", "photo_date": "2025-10-01", "photo_current": True,
                       "main_class": None, "source_v11": "owner label (labelling page)" if o else "v1"}
                      for n, (k, (t, w, a, o)) in enumerate(PARCELS.items(), 1)]).set_index("internal_parcel_id")
    state = {k: {y: "unclear" for y in YEARS} for k in PARCELS}
    cycles = {k: {y: 0 for y in YEARS} for k in PARCELS}
    mx = {k: {y: 0.7 for y in YEARS} for k in PARCELS}
    state["parcel_nocrop"][2025] = state["parcel_nocrop_owner"][2025] = "cultivated"
    state["parcel_nonfarm_wide"][2024] = state["parcel_nonfarm_wide"][2026] = "cultivated"
    state["parcel_nonfarm_strip"][2025] = state["parcel_nonfarm_strip"][2026] = "cultivated"
    for y in (2021, 2022, 2023):
        state["parcel_field_idle"][y] = "idle"
    state["parcel_ok"][2025] = "cultivated"
    mx["parcel_orchard_brown"][2025] = mx["parcel_orchard_brown"][2026] = 0.2
    cycles["parcel_vine_cycles"][2024] = cycles["parcel_vine_cycles"][2026] = 1
    cycles["parcel_greenhouse"][2026] = 1
    years = {y: pd.DataFrame({"state": {k: state[k][y] for k in PARCELS}, "peak_ndvi": 0.8, "n_green": 8,
                              "green_span_days": 40, "max_ndvi_jun_oct": {k: mx[k][y] for k in PARCELS},
                              "pipeline_cycles": {k: cycles[k][y] for k in PARCELS}}) for y in YEARS}
    spatial = pd.DataFrame({"area_official_m2": p.area_ha * 1e4, "geometry_area_m2": p.area_ha * 1e4})
    spatial.loc["parcel_ok", "geometry_area_m2"] = 2e4 * 1.3
    geoms = {k: box(n * 1000, 0, n * 1000 + 50, 50) for n, k in enumerate(PARCELS)}
    geoms["parcel_greenhouse"] = geoms["parcel_vine_cycles"]                      # identical pair
    geoms["parcel_sliver"] = box(1000 * 9 + 5, 5, 1000 * 9 + 20, 20)              # nested in parcel_ok
    geometry = gpd.GeoDataFrame({"internal_parcel_id": list(geoms)}, geometry=list(geoms.values()), crs="EPSG:32638")
    stress = pd.DataFrame({"state": ["candidate", "candidate", "not_flagged"]},
                          index=["parcel_nonfarm_wide", "parcel_ok", "parcel_nocrop"])
    v3 = pd.DataFrame({"crop_type_candidate": ["annual", "perennial"]}, index=["parcel_vine_cycles", "parcel_orchard_brown"])
    days = pd.date_range("2026-03-01", "2026-10-31")
    weather = pd.DataFrame({"date": days.strftime("%Y-%m-%d"), "cell": "c1", "et0_fao_evapotranspiration": 5.0,
                            "precipitation_sum": 0.0, "complete": True, "year": 2026})
    weather.loc[10, "precipitation_sum"] = -1.0
    weather = weather.drop(index=20)
    cfg = cfg_for(len(PARCELS))
    series = ad.SeriesAgg()
    ad.aggregate_year(series, daily if daily is not None else make_daily(), 2026, cfg)
    return cfg, ad.Context(parcels=p, years=years, series=series, spatial=spatial, geometry=geometry, stress=stress,
                           v3=v3, weather=weather)


def make_daily():
    dates = pd.date_range("2026-04-01", "2026-10-31", freq="5D")
    rows = []
    for k in PARCELS:
        for d in dates:
            if k == "parcel_vine_cycles" and pd.Timestamp("2026-06-01") <= d <= pd.Timestamp("2026-07-10"):
                continue                                   # a 40+ day gap without a clear view
            nd = 0.6
            rows.append({"internal_parcel_id": k, "observation_date": d.strftime("%Y-%m-%d"), "ndvi_median": nd,
                         "ndmi_median": 0.1, "bsi_median": -0.1, "ndvi_valid_fraction": 1.0, "ndvi_p10": 0.5,
                         "ndvi_p90": 0.7, "ndvi_inner5_mean": nd, "ndvi_inner5_valid_fraction": 1.0})
    d = pd.DataFrame(rows)
    dip = (d.observation_date == "2026-05-16") & d.internal_parcel_id.isin(["parcel_ok", "parcel_nocrop", "parcel_field_idle"])
    d.loc[dip, "ndvi_median"] = 0.2                       # cloud leak: three parcels dip on one date and recover
    d.loc[(d.internal_parcel_id == "parcel_ok") & (d.observation_date == "2026-04-06"), "ndvi_median"] = 1.7
    edge = (d.internal_parcel_id == "parcel_orchard_brown") & d.observation_date.isin(["2026-04-11", "2026-04-21", "2026-05-01"])
    d.loc[edge, "ndvi_inner5_mean"] = 0.95
    return d


@pytest.fixture(scope="module")
def run():
    cfg, ctx = make_ctx()
    hits, summary = ad.run_detectors(cfg, ctx)
    return cfg, ctx, hits, summary


def by_rule(hits, rule):
    return {h["_pid"]: h for h in hits if h["rule_id"] == rule and h.get("_pid")}


def test_land_type_checks_hit_planted_parcels(run):
    cfg, ctx, hits, summary = run
    assert set(by_rule(hits, "AUD03_NO_CROP_CULTIVATED")) == {"parcel_nocrop", "parcel_nocrop_owner"}
    assert set(by_rule(hits, "AUD04_NON_FARM_CULTIVATED")) == {"parcel_nonfarm_wide", "parcel_nonfarm_strip"}
    assert set(by_rule(hits, "AUD05_FIELD_CROP_NEVER_CULTIVATED")) == {"parcel_field_idle"}
    assert set(by_rule(hits, "AUD06_PERENNIAL_NO_GREEN")) == {"parcel_orchard_brown"}
    assert set(by_rule(hits, "AUD07_PERENNIAL_CROP_CYCLES")) == {"parcel_vine_cycles"}
    assert set(by_rule(hits, "AUD08_GREENHOUSE_FIELD_CURVE")) == {"parcel_greenhouse"}
    assert set(by_rule(hits, "AUD13_STRESS_ON_NON_FARM")) == {"parcel_nonfarm_wide"}
    assert set(by_rule(hits, "AUD14_TYPE_VS_TRANSITIONS_V3")) == {"parcel_vine_cycles"}
    assert len(summary["checks"]) == 16 and summary["parcels_reconcile"]


def test_proposal_scope_rules(run):
    cfg, ctx, hits, _ = run
    h3 = by_rule(hits, "AUD03_NO_CROP_CULTIVATED")
    assert h3["parcel_nocrop"]["proposal"] == {"field": "type6", "from": "NO_CROP", "suggested": "FIELD_CROP",
                                               "question": h3["parcel_nocrop"]["proposal"]["question"]}
    assert h3["parcel_nocrop_owner"]["report_only"] and "owner label wins" in h3["parcel_nocrop_owner"]["report_reason"]
    h4 = by_rule(hits, "AUD04_NON_FARM_CULTIVATED")
    assert not h4["parcel_nonfarm_wide"]["report_only"] and h4["parcel_nonfarm_strip"]["report_only"]
    assert by_rule(hits, "AUD06_PERENNIAL_NO_GREEN")["parcel_orchard_brown"]["proposal"]["suggested"] is None
    for h in hits:
        if "proposal" in h:
            assert h["proposal"]["field"] == "type6" and not cfg["checks"][h["rule_id"]]["report_only"]


def test_geometry_area_and_hygiene_checks_are_report_only(run):
    cfg, ctx, hits, summary = run
    assert set(by_rule(hits, "AUD09_SLIVER")) == {"parcel_sliver"}
    pairs = {(h["_pid"], h["evidence"]["relation"]) for h in hits if h["rule_id"] == "AUD10_DUPLICATE_NESTED"}
    assert ("parcel_vine_cycles", "identical") in pairs and len(pairs) == 2
    assert set(by_rule(hits, "AUD11_AREA_MISMATCH")) == {"parcel_ok"}
    report = {"AUD01_OUT_OF_RANGE", "AUD02_CLOUD_LEAK_DATE", "AUD09_SLIVER", "AUD10_DUPLICATE_NESTED",
              "AUD11_AREA_MISMATCH", "AUD15_STALE_THIN_SERIES", "AUD16_WEATHER_GAPS"}
    for h in hits:
        if h["rule_id"] in report:
            assert h["report_only"] and "proposal" not in h
    assert summary["checks"]["AUD10_DUPLICATE_NESTED"]["identical_pairs"] == 1


def test_series_checks(run):
    cfg, ctx, hits, summary = run
    assert set(by_rule(hits, "AUD01_OUT_OF_RANGE")) == {"parcel_ok"}
    assert summary["checks"]["AUD01_OUT_OF_RANGE"]["by_year"]["2026"]["ndvi_rows"] == 1
    leak = [h for h in hits if h["rule_id"] == "AUD02_CLOUD_LEAK_DATE"]
    assert [(h["date"], h["evidence"]["parcels_dipping"]) for h in leak] == [("2026-05-16", 3)]
    assert set(by_rule(hits, "AUD12_EDGE_INTERIOR")) == {"parcel_orchard_brown"}
    stale = by_rule(hits, "AUD15_STALE_THIN_SERIES")
    assert set(stale) == {"parcel_vine_cycles"} and stale["parcel_vine_cycles"]["evidence"]["flags"] == ["gap"]


def test_weather_gaps(run):
    _, _, hits, summary = run
    w = summary["checks"]["AUD16_WEATHER_GAPS"]
    assert w["negative_rain"] == 1 and w["missing_days"] == 1
    assert all(h["kind"] == "weather" for h in hits if h["rule_id"] == "AUD16_WEATHER_GAPS")


def test_hits_and_evidence_carry_no_codes_or_ids(run):
    cfg, ctx, hits, _ = run
    text = "\n".join(ad.canonical(ad.public_hit(h)).decode() for h in hits)
    packets = [ad.evidence_packet(cfg, ctx, h) for h in hits if h.get("_pid")]
    text += "\n".join(ad.canonical(p).decode() for p in packets)
    assert "parcel_" not in text and not CODE.search(text)
    assert all(re.fullmatch(r"p[0-9a-f]{12}", h["handle"]) for h in hits if h.get("_pid"))
    pk = next(p for p in packets if p["rule_id"] == "AUD03_NO_CROP_CULTIVATED")
    assert set(pk["curve_monthly_median"]["2026"]) == {"ndvi", "ndmi", "bsi"} and pk["photo"]["date"] == "2025-10-01"
    assert pk["type"]["source"] in ("AI photo review", "owner label") and len(pk["evidence_sha"]) == 64


def test_questions_never_say_abandoned(run):
    _, _, hits, _ = run
    words = " ".join(ad.QUESTIONS.values()) + " ".join(h["proposal"]["question"] for h in hits if "proposal" in h)
    assert "abandon" not in words.lower() and "pressure" not in words.lower()


# ---------------------------------------------------------------- ledger
def proposed_ledger(tmp_path, run):
    cfg, ctx, hits, _ = run
    packets = {(h["rule_id"], h["handle"]): ad.evidence_packet(cfg, ctx, h) for h in hits if h.get("_pid")}
    led = ad.Ledger(tmp_path / "proposals.jsonl")
    counts = ad.propose(cfg, led, [ad.public_hit(h) for h in hits], packets, "rtest", "base-sha")
    return cfg, led, counts, packets


def test_ledger_chain_and_tamper(tmp_path, run):
    cfg, led, counts, _ = proposed_ledger(tmp_path, run)
    events = led.read()
    assert counts["new"] == len(events) > 0 and events[0]["prev"] == ad.GENESIS
    lines = led.path.read_text(encoding="utf-8").splitlines()
    lines[0] = lines[0].replace('"ha":', '"ha":9', 1)
    led.path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    with pytest.raises(ad.LedgerError):
        led.read()


def test_rejection_suppresses_reproposal(tmp_path, run):
    cfg, led, counts, packets = proposed_ledger(tmp_path, run)
    hits = [ad.public_hit(h) for h in run[2]]
    state = ad.ledger_state(led.read())
    first = next(pid for pid, s in state.items() if s["proposal"]["rule_id"] == "AUD03_NO_CROP_CULTIVATED")
    ad.decide(cfg, led, first, "reject", comment="looks like a track")
    again = ad.propose(cfg, led, hits, packets, "rtest2", "base-sha")
    assert again["new"] == 0 and again["rejected before"] == 1 and again["already proposed"] == counts["new"] - 1
    led2 = ad.Ledger(tmp_path / "x.jsonl")                      # a fresh ledger would propose it again
    assert ad.propose(cfg, led2, hits, packets, "r", "base-sha")["new"] == counts["new"]


def test_decide_validation(tmp_path, run):
    cfg, led, _, _ = proposed_ledger(tmp_path, run)
    pid, s = next(iter(ad.ledger_state(led.read()).items()))
    with pytest.raises(ValueError):
        ad.decide(cfg, led, pid, "accept", value=s["proposal"]["from"])
    with pytest.raises(ValueError):
        ad.decide(cfg, led, pid, "accept", value="MADE_UP")
    with pytest.raises(ValueError):
        ad.decide(cfg, led, pid, "accept_all")
    with pytest.raises(ValueError):
        ad.decide(cfg, led, "AP99999", "reject")
    ev = ad.decide(cfg, led, pid, "accept", value="ORCHARD")
    assert ad.ledger_state(led.read())[pid]["decision"]["value"] == "ORCHARD" and ev["event"] == "decided"


# ---------------------------------------------------------------- apply
def base_release(tmp_path, cfg):
    base = tmp_path / "releases" / "landtypes_v1_4"
    base.mkdir(parents=True)
    rows = [{"internal_parcel_id": k, "item_id": f"f{n:05d}", "type6": t, "main_class": cfg["main_class"].get(t, t),
             "owner_label": o, "source_v11": "v1", "area_ha": a, "mean_width_m": w, "lon": 44.3, "lat": 40.1}
            for n, (k, (t, w, a, o)) in enumerate(PARCELS.items(), 1)]
    pd.DataFrame(rows).to_parquet(base / "parcels.parquet", index=False)
    return base, ad.sha256_file(base / "parcels.parquet")


def accepted_ledger(tmp_path, cfg, base_sha, picks):
    led = ad.Ledger(tmp_path / "proposals.jsonl")
    for n, (pid, frm, to) in enumerate(picks, 1):
        h = ad.handle_of(pid, cfg["handle_salt"])
        led.append({"event": "proposed", "proposal_id": f"AP{n:05d}", "fingerprint": ad.fingerprint("R", h, "type6", frm),
                    "rule_id": "AUD03_NO_CROP_CULTIVATED", "handle": h, "field": "type6", "from": frm, "suggested": to,
                    "question": "q", "ha": 1.0, "width_class": "wide", "evidence_sha": "e", "run_id": "r",
                    "inputs_sha": base_sha})
        if to:
            ad.decide(cfg, led, f"AP{n:05d}", "accept", value=to)
    return led


def test_apply_writes_v15_reconciles_and_refuses_overwrite(tmp_path):
    cfg = cfg_for(len(PARCELS))
    base, sha = base_release(tmp_path, cfg)
    led = accepted_ledger(tmp_path, cfg, sha, [("parcel_nocrop", "NO_CROP", "FIELD_CROP"),
                                               ("parcel_nocrop_owner", "NO_CROP", "FIELD_CROP"),
                                               ("parcel_field_idle", "FIELD_CROP", None)])
    target = tmp_path / "releases" / "landtypes_v1_5"
    s = ad.build_release(cfg, base, target, led.path)
    assert ad.sha256_file(base / "parcels.parquet") == sha                      # v1.4 untouched
    assert s["applied"] == 1 and s["skipped"] == [{"proposal_id": "AP00002", "why": "owner label wins"}]
    assert s["reconcile"]["parcels"] == len(PARCELS) and s["reconcile"]["changed_rows"] == 1
    out = pd.read_parquet(target / "parcels.parquet").set_index("internal_parcel_id")
    assert out.at["parcel_nocrop", "type6"] == "FIELD_CROP" and out.at["parcel_nocrop", "type6_v14"] == "NO_CROP"
    assert out.at["parcel_nocrop_owner", "type6"] == "NO_CROP"
    m = json.loads((target / "manifest.json").read_text(encoding="utf-8"))
    assert m["files_sha256"]["parcels.parquet"] == ad.sha256_file(target / "parcels.parquet") == m["parcels_sha256"]
    assert (target / "frozen" / "proposals.jsonl").exists()
    with pytest.raises(ad.ApplyRefused, match="never overwritten"):
        ad.build_release(cfg, base, target, led.path)


def test_apply_refuses_without_acceptance_or_after_base_change(tmp_path):
    cfg = cfg_for(len(PARCELS))
    base, sha = base_release(tmp_path, cfg)
    target = tmp_path / "releases" / "landtypes_v1_5"
    led = accepted_ledger(tmp_path, cfg, sha, [("parcel_field_idle", "FIELD_CROP", None)])
    with pytest.raises(ad.ApplyRefused, match="nothing to apply"):
        ad.build_release(cfg, base, target, led.path)
    led.path.unlink()
    led = accepted_ledger(tmp_path, cfg, "another-sha", [("parcel_nocrop", "NO_CROP", "FIELD_CROP")])
    with pytest.raises(ad.ApplyRefused, match="lapsed"):
        ad.build_release(cfg, base, target, led.path)
    assert not target.exists() and not list(target.parent.glob(".landtypes_v1_5_*"))


def test_reconcile_catches_area_edits(tmp_path):
    cfg = cfg_for(len(PARCELS))
    base, _ = base_release(tmp_path, cfg)
    before = pd.read_parquet(base / "parcels.parquet")
    after = before.copy()
    after.loc[0, "area_ha"] += 0.1
    with pytest.raises(ad.ApplyRefused, match="area_ha changed"):
        ad.reconcile(cfg, before, after, [])


def test_apply_cli_refuses_without_owner(monkeypatch):
    monkeypatch.setattr("sys.argv", ["audit_review_v1.py", "apply"])
    with pytest.raises(SystemExit, match="owner only"):
        arv.apply_cmd()
    monkeypatch.setattr("sys.argv", ["audit_review_v1.py", "apply", "--i-am-the-owner"])
    monkeypatch.setattr("sys.stdin.isatty", lambda: False, raising=False)
    with pytest.raises(SystemExit, match="terminal"):
        arv.apply_cmd()


# ---------------------------------------------------------------- review page
def page_setup(tmp_path, run):
    cfg, ctx, hits, _ = run
    runs = tmp_path / "runs"
    (runs / "rtest").mkdir(parents=True)
    packets = {(h["rule_id"], h["handle"]): ad.evidence_packet(cfg, ctx, h) for h in hits if h.get("_pid")}
    with open(runs / "rtest" / "evidence.jsonl", "w", encoding="utf-8") as f:
        for p in packets.values():
            f.write(ad.canonical(p).decode() + "\n")
    led = ad.Ledger(tmp_path / "proposals.jsonl")
    ad.propose(cfg, led, [ad.public_hit(h) for h in hits], packets, "rtest", "base-sha")
    locate = lambda h: {"lat": 40.1, "lon": 44.3, "rings": [[[44.3, 40.1], [44.31, 40.1], [44.31, 40.11]]],  # noqa: E731
                        "photo_meta": {"resolution_m": 0.3, "max_zoom": 19, "provider": None}}
    return cfg, led, runs, locate


def test_page_payload_has_no_codes(tmp_path, run):
    cfg, led, runs, locate = page_setup(tmp_path, run)
    body = arv.page_payload(cfg, led.read(), runs, locate, b"k" * 32, "tok").decode()
    d = json.loads(body)
    assert len(d["items"]) == len(ad.ledger_state(led.read()))
    assert "parcel_" not in body and not CODE.search(body)
    leaky = lambda h: locate(h) | {"note": "04-002-9199-0012"}                  # noqa: E731
    with pytest.raises(ValueError, match="cadastral code"):
        arv.page_payload(cfg, led.read(), runs, leaky, b"k" * 32, "tok")


def test_server_host_token_signature_and_one_at_a_time(tmp_path, run):
    cfg, led, runs, locate = page_setup(tmp_path, run)
    secret, token = b"s" * 32, "tok123"
    srv = ThreadingHTTPServer(("127.0.0.1", 0), None)
    port = srv.server_address[1]
    srv.RequestHandlerClass = arv.make_handler(cfg, led, lambda ev: arv.page_payload(cfg, ev, runs, locate, secret, token),
                                               secret, token, port=port)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    host = f"127.0.0.1:{port}"

    def req(method, path, body=None, headers=None):
        c = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        c.request(method, path, body=None if body is None else json.dumps(body), headers=headers or {})
        r = c.getresponse()
        return r.status, r.read()
    try:
        assert req("GET", "/api/items", headers={"Host": "evil.example:80"})[0] == 403
        st, body = req("GET", "/api/items", headers={"Host": host})
        items = json.loads(body)["items"]
        assert st == 200 and json.loads(body)["token"] == token
        it = items[0]
        ok = {"Host": host, "Content-Type": "application/json", "X-Audit-Token": token}
        decision = {"proposal_id": it["proposal_id"], "sig": it["sig"], "decision": "reject", "comment": "no"}
        assert req("POST", "/api/decide", decision, ok | {"X-Audit-Token": "wrong"})[0] == 403
        assert req("POST", "/api/decide", decision, ok | {"Origin": "http://evil.example"})[0] == 403
        assert req("POST", "/api/decide", [decision, decision], ok)[0] == 400              # no bulk
        assert req("POST", "/api/decide", decision | {"sig": "0" * 32}, ok)[0] == 403
        assert req("POST", "/api/decide", decision, ok)[0] == 200
        assert ad.ledger_state(led.read())[it["proposal_id"]]["decision"]["decision"] == "reject"
    finally:
        srv.shutdown()


# ---------------------------------------------------------------- AI expert panel
def test_expert_panel_records_only_agreement():
    cfg = cfg_for(len(PARCELS))
    a = {"irrigation": {"AP1": {"decision": "accept", "value": "FIELD_CROP", "reason": "x"},
                        "AP2": {"decision": "reject", "reason": "y"},
                        "AP3": {"decision": "accept", "value": "ORCHARD", "reason": "z"},
                        "AP4": {"decision": "unsure", "reason": "no photo"}},
         "remote_sensing": {"AP1": {"decision": "accept", "value": "FIELD_CROP", "reason": "x"},
                            "AP2": {"decision": "reject", "reason": "y"},
                            "AP3": {"decision": "accept", "value": "VINEYARD", "reason": "z"},
                            "AP4": {"decision": "reject", "reason": "w"}}}
    out = arv.combine_expert_answers(cfg, a)
    assert out["AP1"]["decision"] == "accept" and out["AP1"]["value"] == "FIELD_CROP"
    assert out["AP2"]["decision"] == "reject"
    assert out["AP3"]["decision"] == "comment" and out["AP4"]["decision"] == "comment"
    assert "irrigation" in out["AP3"]["comment"] and "remote_sensing" in out["AP3"]["comment"]


def test_ai_panel_and_bulk_decisions_never_become_owner_labels(tmp_path):
    cfg = cfg_for(len(PARCELS))
    base, sha = base_release(tmp_path, cfg)
    led = ad.Ledger(tmp_path / "proposals.jsonl")
    picks = [("parcel_nocrop", "NO_CROP", "FIELD_CROP", arv.EXPERT_VIA),
             ("parcel_field_idle", "FIELD_CROP", "NO_CROP", "bulk accept (owner instruction)"),
             ("parcel_orchard_brown", "ORCHARD", "NO_CROP", "review page 127.0.0.1:8555")]
    for n, (pid, frm, to, via) in enumerate(picks, 1):
        h = ad.handle_of(pid, cfg["handle_salt"])
        led.append({"event": "proposed", "proposal_id": f"AP{n:05d}", "fingerprint": ad.fingerprint("R", h, "type6", frm),
                    "rule_id": "AUD03_NO_CROP_CULTIVATED", "handle": h, "field": "type6", "from": frm, "suggested": to,
                    "question": "q", "ha": 1.0, "width_class": "wide", "evidence_sha": "e", "run_id": "r",
                    "inputs_sha": sha})
        ad.decide(cfg, led, f"AP{n:05d}", "accept", value=to, via=via)
    target = tmp_path / "releases" / "landtypes_v1_5"
    s = ad.build_release(cfg, base, target, led.path)
    assert s["decided_by"] == {"AI expert panel": 1, "owner bulk accept": 1, "owner": 1}
    out = pd.read_parquet(target / "parcels.parquet").set_index("internal_parcel_id")
    assert pd.isna(out.at["parcel_nocrop", "owner_label"]) and out.at["parcel_nocrop", "audit_bulk"]
    assert pd.isna(out.at["parcel_field_idle", "owner_label"])
    assert out.at["parcel_orchard_brown", "owner_label"] == "NO_CROP" and not out.at["parcel_orchard_brown", "audit_bulk"]
    assert "AI expert panel" in (target / "README.md").read_text(encoding="utf-8")
