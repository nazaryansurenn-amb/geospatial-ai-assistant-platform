import json
import sqlite3
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from wp_core import sentinel1_peer_comparison as m


@pytest.fixture
def cfg():
    return m.read(m.CONFIG)


def fixture_data():
    times = pd.date_range("2025-04-01T05:30Z", periods=9, freq="6D")
    fields = pd.DataFrame({"internal_parcel_id": [f"p{i}" for i in range(5)],
                           "sample_group": ["clean_interior"]*5, "cell_id": ["cell"]*5})
    obs = []
    for p in range(5):
        for k, time in enumerate(times):
            vv = 3.5 if p == 0 and k % 2 else 0.
            obs.append({"internal_parcel_id": f"p{p}", "relative_orbit": 72, "platform": "sentinel-1a",
                        "datetime": time.isoformat(), "source_item": f"s{k}", "radar_usable": True,
                        "eo_date": time.strftime("%Y-%m-%d"), "eo_ndvi": .45, "eo_bsi": .1, "eo_ndmi": .2,
                        "vv_inner_median_db": vv, "vh_inner_median_db": vv})
    potential = pd.DataFrame([{"internal_parcel_id": a, "peer_id": b, "distance_m": 100., "area_ratio": 1.}
                              for a in fields.internal_parcel_id for b in fields.internal_parcel_id if a != b])
    rain = {"cell": pd.Series(0., index=pd.date_range("2025-03-01", "2025-06-01", freq="h", tz="UTC"))}
    return pd.DataFrame(obs), fields, potential, rain


def test_deaccumulation_midnight_reset_gap_and_negative():
    times = pd.to_datetime(["2025-03-31T23:00Z", "2025-04-01T00:00Z", "2025-04-01T01:00Z", "2025-04-01T02:00Z", "2025-04-01T04:00Z", "2025-04-01T05:00Z"])
    f = pd.DataFrame({"valid_time": times, "latitude": 40., "longitude": 44., "tp": [.01, .012, .001, .003, .008, .007]})
    actual = m.deaccumulate(f).rain_mm.to_numpy()
    np.testing.assert_allclose(actual, [np.nan, 2, 1, 2, np.nan, np.nan], equal_nan=True)


def test_rain_missing_hour_and_partial_boundary_are_not_invented():
    s = pd.Series([1., 2., 3.], index=pd.date_range("2025-04-01T01:00Z", periods=3, freq="h"))
    assert m.rain_between(s, "2025-04-01T00:30Z", "2025-04-01T02:10Z") == 6
    assert np.isnan(m.rain_between(s.drop(s.index[1]), "2025-04-01T00:30Z", "2025-04-01T02:10Z"))


def test_triplets_reject_platform_gap_missing_radar_and_canopy_change(cfg):
    obs, *_ = fixture_data()
    a = obs.loc[obs.internal_parcel_id.eq("p0")].iloc[:3].copy()
    assert m.triplets(a, cfg).supported.item()
    for column, value in [("radar_usable", False), ("eo_ndvi", np.nan), ("eo_ndvi", .9)]:
        b = a.copy()
        b.loc[b.index[1], column] = value
        assert not m.triplets(b, cfg).supported.item()
    b = a.copy()
    b.loc[b.index[2], "datetime"] = "2025-05-20T05:30Z"
    assert not m.triplets(b, cfg).supported.item()
    b = a.copy()
    b.loc[b.index[1], "relative_orbit"] = 152
    assert m.triplets(b, cfg).empty
    b = a.copy()
    b.loc[b.index[1], "platform"] = "sentinel-1c"
    assert m.triplets(b, cfg).empty


def test_alternating_platforms_use_separate_twelve_day_sequences(cfg):
    obs, *_ = fixture_data()
    obs = obs.loc[obs.internal_parcel_id.eq("p0")].copy()
    obs.loc[obs.index[1::2], "platform"] = "sentinel-1c"
    t = m.triplets(obs, cfg)
    assert len(t) == 5 and t.supported.all()
    assert t.before_days.eq(12).all() and t.after_days.eq(12).all()
    assert set(t.platform) == {"sentinel-1a", "sentinel-1c"}


def test_matching_uses_optical_state_not_radar_outcome(cfg):
    obs, fields, potential, rain = fixture_data()
    t = m.triplets(obs, cfg)
    g = t.loc[t.middle_item.eq("s1")]
    a = g.loc[g.internal_parcel_id.eq("p0")].iloc[0]
    peers = m.match_peers(a, g, potential, cfg)
    assert set(peers.internal_parcel_id) == {"p1", "p2", "p3", "p4"}
    g = g.copy()
    g.loc[g.internal_parcel_id.eq("p1"), "ndvi_1"] = .8
    g.loc[g.internal_parcel_id.eq("p2"), "eo_date_1"] = "2025-04-12"
    peers = m.match_peers(a, g, potential, cfg)
    assert set(peers.internal_parcel_id) == {"p3", "p4"}


def test_repetition_normalizes_by_comparable_exposure_and_keeps_claims_null(cfg):
    diag, links, metrics, parcels = m.compare(*fixture_data(), cfg)
    target = metrics.loc[metrics.internal_parcel_id.eq("p0")]
    assert target.comparable_triplets.eq(7).all()
    assert target.observed_excursions.eq(4).all()
    assert target.relative_excursions.eq(4).all()
    np.testing.assert_allclose(target.observed_excursion_fraction, 4/7)
    assert target.repeated_relative_signal.all()
    assert parcels.high_water_demand_candidate.isna().all()
    assert not links.internal_parcel_id.eq(links.peer_id).any()
    assert len(diag) == 5*7*9


def test_rain_and_insufficient_peers_withdraw_conclusions(cfg):
    obs, fields, potential, rain = fixture_data()
    rain["cell"].iloc[:] = 1.
    diag, _, metrics, parcels = m.compare(obs, fields, potential, rain, cfg)
    assert not diag.comparable.any()
    assert metrics.observed_excursions.isna().all()
    assert metrics.repeated_relative_signal.isna().all()
    rain["cell"].iloc[:] = 0.
    potential = potential.loc[potential.peer_id.eq("p1")]
    diag, _, metrics, _ = m.compare(obs, fields, potential, rain, cfg)
    assert not diag.comparable.any()
    assert metrics.observed_excursions.isna().all()


def test_spatially_unsupported_parcel_is_never_a_dry_negative(cfg):
    obs, fields, potential, rain = fixture_data()
    obs.loc[obs.internal_parcel_id.eq("p0"), "radar_usable"] = False
    fields.loc[fields.internal_parcel_id.eq("p0"), "sample_group"] = "boundary_challenge"
    diag, _, metrics, parcels = m.compare(obs, fields, potential, rain, cfg)
    assert metrics.loc[metrics.internal_parcel_id.eq("p0"), "observed_excursions"].isna().all()
    assert parcels.loc[parcels.internal_parcel_id.eq("p0"), "assessment_state"].item() == "insufficient_spatial_support"


def test_gate_requires_checkpoint_and_hash_not_just_file(tmp_path, monkeypatch):
    monkeypatch.setattr(m, "ROOT", tmp_path)
    weather = tmp_path / "weather"
    weather.mkdir()
    path = weather / "weather_2025_04.parquet"
    pd.DataFrame({"tp": [0.]}).to_parquet(path)
    jobs = tmp_path / "jobs.sqlite3"
    with sqlite3.connect(jobs) as db:
        db.execute("create table jobs (id text,kind text,state text,output text,sha256 text,rows integer)")
        db.execute("insert into jobs values (?,?,?,?,?,?)", ("weather_2025_04", "weather", "waiting", "weather/"+path.name, m.sha(path), 1))
    cfg = {"weather_months": ["2025_04"]}
    assert m.weather_status(cfg, jobs, weather)["status"] == "waiting_for_weather"
    with sqlite3.connect(jobs) as db:
        db.execute("update jobs set state='complete'")
    assert m.weather_status(cfg, jobs, weather)["status"] == "ready"
    path.write_bytes(b"changed")
    with pytest.raises(ValueError, match="changed"):
        m.weather_status(cfg, jobs, weather)


def test_waiting_run_does_not_call_analysis_or_write_results(tmp_path, monkeypatch, cfg):
    monkeypatch.setattr(m, "OUT", tmp_path)
    monkeypatch.setattr(m, "verify_prepared", lambda: {})
    monkeypatch.setattr(m, "weather_status", lambda cfg: {"status": "waiting_for_weather", "missing_months": ["2025_04"]})
    monkeypatch.setattr(m, "load_weather", lambda status: pytest.fail("Analysis launched before weather"))
    assert m.run_if_ready()["status"] == "waiting_for_weather"
    assert list(tmp_path.iterdir()) == []


def test_sealed_run_reuses_result_without_reanalysis(tmp_path, monkeypatch):
    monkeypatch.setattr(m, "OUT", tmp_path)
    (tmp_path/"complete.json").write_text("{}")
    monkeypatch.setattr(m, "verify_prepared", lambda: {})
    monkeypatch.setattr(m, "verify_result", lambda: {"status": "comparison_complete_private_diagnostics"})
    monkeypatch.setattr(m, "weather_status", lambda cfg: pytest.fail("Repeated completed analysis"))
    assert m.run_if_ready()["status"] == "comparison_complete_private_diagnostics"


def test_complete_pipeline_serializes_seals_and_reuses(tmp_path, monkeypatch, cfg):
    obs, fields, potential, rain = fixture_data()
    out = tmp_path / "result"
    out.mkdir()
    monkeypatch.setattr(m, "ROOT", tmp_path)
    monkeypatch.setattr(m, "OUT", out)
    config = tmp_path / "config.json"
    cfg["pilot"] = "pilot"
    config.write_text(json.dumps(cfg))
    monkeypatch.setattr(m, "CONFIG", config)
    (tmp_path/"pilot/analysis").mkdir(parents=True)
    obs.to_parquet(tmp_path/"pilot/analysis/radar_observations.parquet", index=False)
    fields.to_parquet(out/"fields.parquet", index=False)
    potential.to_parquet(out/"potential_peers.parquet", index=False)
    monkeypatch.setattr(m, "verify_prepared", lambda: {})
    monkeypatch.setattr(m, "weather_status", lambda cfg: {"status": "ready", "completed": []})
    monkeypatch.setattr(m, "load_weather", lambda status: (None, rain))
    result = m.run_if_ready()
    assert result["status"] == "comparison_complete_private_diagnostics"
    assert result["high_water_demand_parcels"] is None
    first_hash = m.sha(out/"complete.json")
    assert m.run_if_ready() == result and m.sha(out/"complete.json") == first_hash
    assert len(list((out/"attempts").iterdir())) == 1
