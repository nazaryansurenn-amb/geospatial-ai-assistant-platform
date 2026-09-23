import numpy as np
import pandas as pd
from wp_core import optical_water_screening as m


def cfg():
    return m.read(m.CONFIG)


def fields(n=8):
    return pd.DataFrame({"internal_parcel_id": [f"p{i}" for i in range(n)], "x_m": np.arange(n)*100., "y_m": 0.,
        "area_official_m2": 5000., "minimum_width_m": 50., "cell_id": "cell", "clear_spatial_support": True})


def sample_triplets(n=8):
    f = fields(n); rows = []
    for start in range(0, 100, 10):
        for i in range(n):
            row = {"internal_parcel_id": f"p{i}", "position": i, "first_day": start, "middle_day": start+3, "last_day": start+6,
                   "common_support": 1., "ndmi_0": .25, "ndmi_1": .25, "ndmi_2": .25}
            if i == 0 and start in (0, 30, 60):
                row.update(ndmi_1=.15, ndmi_2=.25)
            for name, value in (("ndvi", .55), ("evi2", .4), ("bsi", -.1)):
                for k in range(3): row[f"{name}_{k}"] = value
            row["drop"] = row["ndmi_0"]-row["ndmi_1"]; row["recovery"] = row["ndmi_2"]-row["ndmi_1"]
            row["drying_rate"] = row["drop"]/3
            rows.append(row)
    return f, pd.DataFrame(rows)


def test_three_way_common_area_rejects_changing_visible_parts():
    masks = [np.packbits(np.array(bits)).tobytes() for bits in ([1,1,1,1,0], [1,1,1,0,1], [1,1,0,1,1])]
    assert m.common_fraction(masks, np.ones(5)) == .4
    assert m.common_fraction([masks[0]]*3, np.array([2.,2.,2.,2.,1.])) == 8/9


def test_repeated_local_dips_pass_and_common_scene_change_does_not():
    f, t = sample_triplets()
    ids, dist = m.static_neighbors(f, cfg())
    d, links = m.match_triplets(t, f, ids, dist, cfg())
    profile = f[["internal_parcel_id"]]
    metrics, events = m.season_metrics(d, profile, cfg())
    own = metrics.loc[metrics.internal_parcel_id.eq("p0") & metrics.threshold.eq(.04)].iloc[0]
    assert own.season_signal and own.independent_relative_patterns == 3 and own.comparable_triplets == 10
    assert not metrics.loc[metrics.internal_parcel_id.ne("p0"), "season_signal"].fillna(False).any()
    assert len(links) > 0 and events.internal_parcel_id.eq("p0").all()
    t["ndmi_1"] = .15; t["drop"] = .1; t["recovery"] = .1; t["drying_rate"] = .1/3
    common, _ = m.match_triplets(t, f, ids, dist, cfg())
    assert not common["relative_0.04"].any()


def test_missing_peers_are_unassessed_not_negative():
    f, t = sample_triplets(n=4)
    ids, dist = m.static_neighbors(f, cfg())
    d, _ = m.match_triplets(t, f, ids, dist, cfg())
    metrics, events = m.season_metrics(d, f, cfg())
    assert not d.comparable.any() and metrics.season_signal.isna().all() and events.empty
    assert metrics.independent_relative_patterns.isna().all()


def test_independent_patterns_never_double_count_overlap_or_short_regrowth():
    f = pd.DataFrame({"first_day": [0,5,15,20,35], "middle_day": [5,10,20,25,40], "last_day": [10,15,25,30,45]})
    assert m.independent_patterns(f, cfg()) == [0,2,4]


def test_static_peers_keep_weather_and_spatial_support_separate():
    f = fields(); f.loc[1, "cell_id"] = "other"; f.loc[2, "clear_spatial_support"] = False
    f.loc[3, "area_official_m2"] = 20000
    ids, _ = m.static_neighbors(f, cfg())
    assert not set([0,1,2,3]) & set(ids[0])
    assert set(ids[0]) >= {4,5,6,7}


def raw_frame():
    rows = []
    for day in (1,6,11,16,21,26,31,36):
        rows.append({"internal_parcel_id": "p0", "observation_date": str(pd.Timestamp("2021-04-01")+pd.Timedelta(days=day-1))[:10],
                     "scene_id": f"s{day}", "support": 1., "finite": True, "vegetation_fraction": .8, "ndvi_p10": .5, "ndvi_p90": .6,
                     "ndvi_median": .55, "evi2_median": .4, "bsi_median": -.1, "ndmi_median": .25, "ndre_median": .2,
                     "valid_mask_10m": b"\xf0", "valid_mask_20m": b"\xf0"})
    return pd.DataFrame(rows)


def test_activity_and_harvest_gap_guards_use_observations_not_old_labels():
    f = fields(1); raw = raw_frame(); weights = {10: [np.ones(4)], 20: [np.ones(4)]}
    t, profile = m.make_triplets(raw, f, weights, cfg())
    assert profile.active_observed.all() and len(t) == 6
    raw.loc[3, "ndvi_median"] = .1
    t, _ = m.make_triplets(raw, f, weights, cfg())
    assert len(t) == 3
    # A quality observation of harvest remains in sequence, never skipped over.
    harvest_day = pd.Timestamp("2021-04-16").value//86400_000_000_000
    assert not ((t.first_day < harvest_day) & (t.last_day > harvest_day)).any()


def test_empty_evidence_keeps_register_and_unknown_state():
    raw = raw_frame(); raw["support"] = 0.
    f = fields(1)
    t, profile = m.make_triplets(raw, f, {10:[np.ones(4)],20:[np.ones(4)]}, cfg())
    assert t.empty and not profile.active_observed.any()
    ids, distances = m.static_neighbors(f, cfg())
    d, _ = m.match_triplets(t, f, ids, distances, cfg())
    metrics, events = m.season_metrics(d, profile, cfg())
    assert len(metrics) == 3 and metrics.season_signal.isna().all() and events.empty


def test_recurrence_requires_distinct_years_and_keeps_mixed_support_separate():
    f = fields(2); f.loc[1, "clear_spatial_support"] = False
    activity = pd.DataFrame({"internal_parcel_id": ["p0","p1"]*2, "year": [2021,2021,2023,2023], "active_observed": True})
    metrics = pd.DataFrame([{"internal_parcel_id":p, "year":y, "threshold":t, "assessable":True, "season_signal":True, "comparable_triplets":10} for p in ("p0","p1") for y in (2021,2023) for t in (.03,.04,.05)])
    events = pd.DataFrame([{"internal_parcel_id":p, "year":y, "threshold":.04, "rain_context":"Weather unavailable"} for p in ("p0","p1") for y in (2021,2023)])
    r = m.aggregate(activity, metrics, events, f, cfg())
    assert r.evidence_status.tolist() == ["Recurrent optical candidate", "Recurrent pattern - mixed spatial support"]
    metrics["year"] = 2021
    r = m.aggregate(activity, metrics, events, f, cfg())
    assert r.evidence_status.eq("One-season relative pattern").all()
