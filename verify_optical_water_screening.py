"""Independent checks of optical-only inputs, parcel identity, masks and rules."""
import ast
import json
import numpy as np
import pandas as pd
import geopandas as gpd
from wp_core import optical_water_screening as m


def independently_select(rows, separation):
    kept = []
    for r in rows.sort_values(["middle_day", "first_day", "last_day"]).itertuples():
        if not kept or (r.first_day >= kept[-1].last_day and r.middle_day-kept[-1].middle_day >= separation):
            kept.append(r)
    return [(r.first_day, r.middle_day, r.last_day) for r in kept]


def main():
    cfg = m.read(m.CONFIG)
    prepared = m.read(m.OUT / "prepared.json")
    for path, digest in prepared["sources"].items():
        assert m.sha(m.ROOT / path) == digest, path
        assert "sentinel1" not in path.lower(), "Radar-linked source"
    code = ast.parse((m.ROOT / "wp_core/optical_water_screening.py").read_text(encoding="utf-8"))
    for node in ast.walk(code):
        if isinstance(node, ast.ImportFrom):
            assert not any(s in (node.module or "") for s in ("sentinel1", "phenology", "classification"))
    complete = m.read(m.OUT / "complete.json")
    for name, digest in complete["files"].items():
        assert m.sha(m.OUT / name) == digest, name
    result = gpd.read_parquet(m.OUT / "all_parcels.parquet").sort_values("internal_parcel_id").reset_index(drop=True)
    scope = pd.read_parquet(m.BASE / "scope.parquet")
    eligible = scope.loc[scope.included].sort_values("internal_parcel_id").reset_index(drop=True)
    assert len(result) == len(eligible) == 22802 and result.internal_parcel_id.is_unique
    for col in ("internal_parcel_id", "cadastre_code", "area_official_m2"):
        np.testing.assert_array_equal(result[col], eligible[col])
    assert not eligible.household.any() and not eligible.road_excluded.any()
    original = gpd.read_file(m.GEOM, layer="parcels", columns=["cadastre_code", "area_official_m2"]).set_index("cadastre_code").loc[result.cadastre_code].reset_index()
    assert original.crs == result.crs
    assert np.array_equal(original.geometry.to_wkb().to_numpy(), result.geometry.to_wkb().to_numpy())
    np.testing.assert_array_equal(original.area_official_m2, result.area_official_m2)
    gpkg = gpd.read_file(m.OUT / "parcel_results.gpkg", layer="all_22802_parcels").sort_values("internal_parcel_id").reset_index(drop=True)
    assert np.array_equal(gpkg.geometry.to_wkb().to_numpy(), result.geometry.to_wkb().to_numpy())
    assert gpkg.crs == result.crs and len(gpkg) == 22802
    for col in ("cadastre_code", "area_official_m2", "evidence_status", "active_years", "positive_years"):
        np.testing.assert_array_equal(gpkg[col], result[col])
    assert result.water_demand_status.eq("Unassessed").all()
    fields = pd.read_parquet(m.OUT / "fields.parquet")
    with np.load(m.OUT / "neighbors.npz", allow_pickle=False) as z:
        neighbors = z["indices"].copy(); distances = z["distances"].copy()
    weights = {}
    for res in (10, 20):
        with np.load(m.EO / f"weights_{res}m.npz", allow_pickle=False) as z:
            weights[res] = (z["parcel"].copy(), z["area"].copy())
    counters = {"parcel_seasons": 0, "sampled_actual_eo_triplets": 0, "checked_three_date_masks": 0,
                "independent_peer_selections": 0, "season_metric_rows": 0, "independent_event_rows": 0, "weather_event_intervals": 0}
    all_profiles, all_metrics, all_events = [], [], []
    for year in cfg["years"]:
        folder = m.OUT / "years" / str(year)
        assert m.sha(folder / "complete.json") == complete["year_manifests"][str(year)]
        for name, digest in m.read(folder / "complete.json")["files"].items():
            assert m.sha(folder/name) == digest
        profile = pd.read_parquet(folder / "activity.parquet")
        diag = pd.read_parquet(folder / "triplets.parquet")
        metrics = pd.read_parquet(folder / "metrics.parquet")
        events = pd.read_parquet(folder / "events.parquet")
        links = pd.read_parquet(folder / "event_peers.parquet")
        rain = pd.read_parquet(folder / "weather_daily.parquet")
        assert len(profile) == 22802 and set(profile.internal_parcel_id) == set(result.internal_parcel_id)
        assert len(metrics) == 22802*len(cfg["moisture_change_sensitivity"])
        assert diag.common_support.ge(cfg["minimum_common_area_fraction"]).all()
        assert not diag.duplicated(["internal_parcel_id", "middle_day"]).any()
        counters["parcel_seasons"] += len(profile)
        # Independently reconstruct activity from cached, unclassified observations.
        columns = ["internal_parcel_id", "observation_date", "scene_id", "support", "finite", "vegetation_fraction", "ndvi_p10", "ndvi_p90", "valid_mask_10m", "valid_mask_20m"] + [x+"_median" for x in ("ndvi", "evi2", "ndmi", "bsi", "ndre")]
        raw = pd.read_parquet(m.EO / "daily" / f"{year}.parquet", columns=columns,
            filters=[("observation_date", ">=", f"{year}-04-01"), ("observation_date", "<=", f"{year}-09-30")])
        valid = raw.support.ge(cfg["minimum_valid_area_fraction"]) & raw.finite
        valid &= np.isfinite(raw[[x+"_median" for x in ("ndvi", "evi2", "ndmi", "bsi", "ndre")]].to_numpy()).all(axis=1)
        good = raw.loc[valid].copy()
        good["day"] = pd.to_datetime(good.observation_date).astype("int64")//86400_000_000_000
        active = good.loc[good.ndvi_median.ge(cfg["minimum_active_ndvi"]) & good.evi2_median.ge(cfg["minimum_active_evi2"]) & good.vegetation_fraction.ge(cfg["minimum_vegetation_fraction"])]
        counts = active.groupby("internal_parcel_id").day.agg(["size", "min", "max"])
        expected = set(counts.loc[counts["size"].ge(cfg["minimum_active_dates"]) & (counts["max"]-counts["min"]).ge(cfg["minimum_active_span_days"])].index)
        assert expected == set(profile.loc[profile.active_observed, "internal_parcel_id"])
        # Verify saved event indices and sample non-event triplets against real EO.
        sample = pd.concat([diag.sample(n=min(20, len(diag)), random_state=year), events.drop_duplicates(["internal_parcel_id", "middle_day"]).head(20)], ignore_index=True).drop_duplicates(["internal_parcel_id", "middle_day"])
        subset = good.loc[good.internal_parcel_id.isin(sample.internal_parcel_id)].sort_values(["internal_parcel_id", "day"])
        indexed = subset.set_index(["internal_parcel_id", "day"])
        for row in sample.itertuples():
            source = indexed.loc[[(row.internal_parcel_id, d) for d in (row.first_day, row.middle_day, row.last_day)]]
            between = subset.loc[subset.internal_parcel_id.eq(row.internal_parcel_id) & subset.day.between(row.first_day, row.last_day)]
            assert len(between) == 3, "Skipped valid observation"
            assert source.scene_id.tolist() == [row.scene_0, row.scene_1, row.scene_2]
            for name in ("ndvi", "evi2", "ndmi", "bsi"):
                np.testing.assert_array_equal(source[name+"_median"], [getattr(row, f"{name}_{k}") for k in range(3)])
            fractions = []
            for res in (10, 20):
                position, area = weights[res]
                w = area[position == row.position]
                mask = np.ones(len(w), dtype=bool)
                for blob in source[f"valid_mask_{res}m"]:
                    bits = [(byte >> shift) & 1 for byte in blob for shift in range(7, -1, -1)]
                    mask &= np.array(bits[:len(w)], dtype=bool)
                fractions.append(float(w[mask].sum()/w.sum()))
                counters["checked_three_date_masks"] += 1
            np.testing.assert_allclose(min(fractions), row.common_support, atol=1e-12, rtol=0)
            counters["sampled_actual_eo_triplets"] += 1
        # Independently select peers on real relative events, including missing matches.
        targets = pd.concat([events.drop_duplicates(["internal_parcel_id", "middle_day"]).head(8), diag.loc[diag.comparable].head(2)], ignore_index=True).drop_duplicates(["internal_parcel_id", "middle_day"])
        for target in targets.itertuples():
            same_dates = diag.loc[diag.first_day.eq(target.first_day) & diag.middle_day.eq(target.middle_day) & diag.last_day.eq(target.last_day)]
            choices = same_dates.loc[same_dates.position.isin(neighbors[target.position])].copy()
            available = dict(zip(neighbors[target.position], distances[target.position]))
            keep = np.ones(len(choices), dtype=bool); scores = np.zeros(len(choices))
            for name in ("ndvi", "evi2", "bsi"):
                limit = cfg["maximum_peer_"+name+"_difference"]
                for k in range(3):
                    difference = (choices[f"{name}_{k}"]-getattr(target, f"{name}_{k}")).abs().to_numpy()
                    keep &= difference <= limit; scores += difference/limit
            difference = (choices.ndmi_0-target.ndmi_0).abs().to_numpy()
            keep &= difference <= cfg["maximum_peer_initial_ndmi_difference"]
            scores += difference/cfg["maximum_peer_initial_ndmi_difference"]
            choices["score"] = scores; choices["distance"] = choices.position.map(available)
            choices = choices.loc[keep].sort_values(["score", "distance", "internal_parcel_id"]).head(cfg["maximum_peers"])
            assert len(choices) == target.peer_count
            if len(choices):
                for col in ("drop", "recovery", "drying_rate"):
                    np.testing.assert_allclose(choices[col].median(), getattr(target, "peer_median_"+col), rtol=0, atol=1e-12)
            recorded = links.loc[links.internal_parcel_id.eq(target.internal_parcel_id) & links.middle_day.eq(target.middle_day)]
            if len(recorded):
                assert set(recorded.peer_id) == set(choices.internal_parcel_id)
            counters["independent_peer_selections"] += 1
        # Recompute every seasonal count and all three sensitivity rules.
        for threshold in cfg["moisture_change_sensitivity"]:
            tag = f"{threshold:.2f}"
            observed = diag["drop"].ge(threshold) & diag.recovery.ge(threshold)
            expected_signal = (diag.comparable & observed & (diag["drop"]-diag.peer_median_drop).ge(cfg["minimum_relative_change"])
                & (diag.recovery-diag.peer_median_recovery).ge(cfg["minimum_relative_change"])
                & (diag.drying_rate-diag.peer_median_drying_rate).ge(np.maximum(cfg["minimum_relative_drying_rate_per_day"], cfg["minimum_drying_robust_deviations"]*diag.peer_drying_mad)))
            np.testing.assert_array_equal(expected_signal, diag["relative_"+tag])
            current = metrics.loc[metrics.threshold.eq(threshold)].set_index("internal_parcel_id")
            usable = diag.loc[diag.comparable]
            comp = usable.groupby("internal_parcel_id").size().reindex(current.index, fill_value=0)
            np.testing.assert_array_equal(comp, current.comparable_triplets)
            selected = {}
            for pid, group in diag.loc[expected_signal].groupby("internal_parcel_id"):
                selected[pid] = independently_select(group, cfg["minimum_independent_event_separation_days"])
            actual_events = events.loc[events.threshold.eq(threshold)]
            assert len(actual_events) == sum(map(len, selected.values()))
            for pid, group in actual_events.groupby("internal_parcel_id"):
                assert list(group[["first_day", "middle_day", "last_day"]].itertuples(index=False, name=None)) == selected[pid]
            n = pd.Series({p:len(v) for p,v in selected.items()}, dtype=float).reindex(current.index, fill_value=0)
            np.testing.assert_array_equal(n, current.independent_relative_patterns.fillna(0))
            temporal = usable.groupby("internal_parcel_id").agg(first=("first_day", "min"), last=("last_day", "max"))
            span = (temporal["last"]-temporal["first"]).reindex(current.index, fill_value=0)
            assessable = comp.ge(cfg["minimum_comparable_triplets"]) & span.ge(cfg["minimum_comparable_span_days"])
            np.testing.assert_array_equal(assessable, current.assessable)
            own_fraction = usable.groupby("internal_parcel_id")["raw_"+tag].mean().reindex(current.index)
            peer_fraction = usable.groupby("internal_parcel_id")["peer_fraction_"+tag].mean().reindex(current.index)
            passed = assessable & n.ge(cfg["minimum_independent_relative_patterns"]) & (own_fraction-peer_fraction).ge(cfg["minimum_excess_pattern_fraction"])
            np.testing.assert_array_equal(passed, current.season_signal.fillna(False))
            assert current.loc[~assessable, "season_signal"].isna().all()
            counters["season_metric_rows"] += len(current)
            counters["independent_event_rows"] += len(actual_events)
        rain_index = rain.set_index(["cell_id", "day"]).rain_mm
        for row in events.itertuples():
            index = pd.MultiIndex.from_product([[row.cell_id], range(row.middle_day, row.last_day+1)])
            values = rain_index.reindex(index)
            if values.isna().any():
                assert pd.isna(row.recovery_interval_rain_mm) and row.rain_context == "Weather unavailable"
            else:
                np.testing.assert_allclose(values.sum(), row.recovery_interval_rain_mm, rtol=0, atol=1e-7)
                assert row.rain_context == ("Low recorded rain" if values.sum() <= cfg["rain_context_limit_mm"] else "Rainfall present")
            counters["weather_event_intervals"] += 1
        all_profiles.append(profile); all_metrics.append(metrics); all_events.append(events)
        print(json.dumps({"phase":"year_verified", "year":year, **counters}), flush=True)
    profile = pd.concat(all_profiles, ignore_index=True); metrics = pd.concat(all_metrics, ignore_index=True)
    nominal = metrics.loc[metrics.threshold.eq(cfg["nominal_moisture_change"])]
    positives = nominal.loc[nominal.season_signal.fillna(False)].groupby("internal_parcel_id").year.nunique()
    index = result.internal_parcel_id
    np.testing.assert_array_equal(positives.reindex(index, fill_value=0), result.positive_years)
    expected_clear = set(result.loc[result.positive_years.ge(2) & result.clear_spatial_support, "internal_parcel_id"])
    expected_mixed = set(result.loc[result.positive_years.ge(2) & ~result.clear_spatial_support, "internal_parcel_id"])
    assert expected_clear == set(result.loc[result.evidence_status.eq("Recurrent optical candidate"), "internal_parcel_id"])
    assert expected_mixed == set(result.loc[result.evidence_status.eq("Recurrent pattern - mixed spatial support"), "internal_parcel_id"])
    for name, expected in (("all_parcels", set(index)), ("active_parcels", set(result.loc[result.active_years.gt(0), "internal_parcel_id"])), ("recurrent_candidates", expected_clear), ("mixed_support_candidates", expected_mixed)):
        csv = pd.read_csv(m.OUT / (name+".csv"), dtype={"cadastre_code": str})
        assert set(csv.internal_parcel_id) == expected and csv.internal_parcel_id.is_unique
        identity = result.set_index("internal_parcel_id").loc[csv.internal_parcel_id]
        np.testing.assert_array_equal(csv.cadastre_code, identity.cadastre_code)
        np.testing.assert_allclose(csv.area_official_m2.to_numpy(dtype=float), identity.area_official_m2.to_numpy(dtype=float), atol=1e-8, rtol=0)
    report = {"status":"passed", "checked_utc":m.now(), "eligible_parcels":22802, **counters,
              "clear_support_recurrent_candidates":len(expected_clear), "mixed_support_recurrent_candidates":len(expected_mixed),
              "radar_inputs_used":False, "prior_land_use_classes_used":False, "geometry_code_area_unchanged":True,
              "result_manifest_sha256":m.sha(m.OUT / "complete.json"), "checker_sha256":m.sha(m.ROOT / "verify_optical_water_screening.py")}
    m.write(m.ROOT / "server_data/review" / m.VERSION / "verification.json", report)
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
