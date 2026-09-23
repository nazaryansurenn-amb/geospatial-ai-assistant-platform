"""Independent structural verification of the private event experiment."""
from __future__ import annotations

import ast
import json
from pathlib import Path

import numpy as np
import pandas as pd

from run_phenology_events_v2 import (
    ROOT, SOURCE, OUTPUT, CONFIG, CODE, IDENTITY, PREVIOUS_EVENT, local, read_json, digest, check_hashes,
    write_json, stable_json, analyze_parcel, load_inputs, tasks_from_inputs,
)


def require(condition, message):
    if not condition:
        raise ValueError(message)


def independent_imports():
    """The calculation module can import only numerical/standard libraries."""
    tree = ast.parse(local("wp_core/phenology_events_v2.py").read_text(encoding="utf-8"))
    allowed = {"__future__", "dataclasses", "datetime", "math", "typing", "collections", "numpy", "pandas"}
    imports = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            require(node.level == 0, "Relative analytical dependency in independent engine")
            imports.append(node.module or "")
    require(all(name.split(".")[0] in allowed for name in imports), "Unexpected analytical dependency")
    return imports


def verify():
    out = local(OUTPUT)
    complete, manifest = read_json(out / "complete.json"), read_json(out / "manifest.json")
    check_hashes({OUTPUT + "/" + p: h for p, h in complete["outputs"].items()})
    for key in ("input_sha256", "code_sha256", "protected_sha256"):
        check_hashes(manifest[key])
    require(digest(local(CONFIG)) == manifest["config_sha256"], "New policy changed")
    for relative in [*CODE, CONFIG]:
        require(digest(out / "source" / relative) == digest(local(relative)), "Code snapshot changed")
    imports = independent_imports()
    forbidden = {"vegetation_fraction", "bare_fraction", "vegetated_mask_10m", "crop_type_candidate",
                 "annual_cycle_candidate", "reason", "support", "finite", "cadastre_code", "internal_parcel_id"}
    require(not forbidden.intersection(manifest["daily_predictors"]), "Old/identity fields entered engine")
    require(not forbidden.intersection(manifest["spatial_predictors"]), "Old/identity fields entered spatial inference")
    sample = pd.read_parquet(local(SOURCE + "/selection.parquet"), columns=IDENTITY)
    seasons = pd.read_parquet(out / "seasons.parquet")
    parcels = pd.read_parquet(out / "parcels.parquet")
    probes = pd.read_parquet(out / "probes.parquet")
    events = pd.read_parquet(out / "events.parquet")
    focus = pd.read_parquet(out / "focus18.parquet")
    require(len(parcels) == 120 and parcels.internal_parcel_id.is_unique, "Parcel population mismatch")
    require(set(parcels.internal_parcel_id) == set(sample.internal_parcel_id), "Lost control parcel")
    require(len(seasons) == 600 and not seasons.duplicated(["internal_parcel_id", "year"]).any(), "Season population mismatch")
    require(seasons.groupby("internal_parcel_id").year.apply(lambda s: sorted(s) == list(range(2021, 2026))).all(), "Wrong years")
    require(len(probes) == 1200 and not probes.duplicated(["internal_parcel_id", "year", "phase"]).any(), "Probe population mismatch")
    expected_probes = {(i, int(y), phase) for i, y in seasons[["internal_parcel_id", "year"]].itertuples(index=False, name=None) for phase in (0, 1)}
    require(set(probes[["internal_parcel_id", "year", "phase"]].itertuples(index=False, name=None)) == expected_probes, "Incorrect season/phase probe keys")
    probe_join = probes.merge(seasons[["internal_parcel_id", "year", "covered", "crop_type_candidate", "annual_cycle_candidate"]],
                              on=["internal_parcel_id", "year"], validate="many_to_one", suffixes=("", "_season"))
    require(probe_join.full_type.eq(probe_join.crop_type_candidate).all() and probe_join.full_cycle.eq(probe_join.annual_cycle_candidate).all(), "Probe baseline differs from saved season")
    require(probe_join.comparable.eq(probe_join.covered & probe_join.covered_season).all(), "Probe comparable flag mismatch")
    changed = probe_join.full_type.ne(probe_join.crop_type) | probe_join.full_cycle.ne(probe_join.cycle)
    require(probe_join.changed.eq(changed).all(), "Probe changed flag mismatch")
    if len(events):
        season_keys = set(seasons[["internal_parcel_id", "year"]].itertuples(index=False, name=None))
        require(set(events[["internal_parcel_id", "year"]].itertuples(index=False, name=None)) <= season_keys, "Event outside authorized seasons")
        require(not events.duplicated(["internal_parcel_id", "year", "episode_number"]).any(), "Duplicate episode")
        event_counts = events.groupby(["internal_parcel_id", "year"]).agg(event_rows=("episode_number", "size"), complete_rows=("complete_cycle", "sum"))
        reconciliation = seasons.join(event_counts, on=["internal_parcel_id", "year"])
        require(reconciliation.episode_count.eq(reconciliation.event_rows.fillna(0)).all(), "Season/event counts disagree")
        require(reconciliation.complete_cycles.eq(reconciliation.complete_rows.fillna(0)).all(), "Completed cycle counts disagree")
        require(events.complete_cycle.eq(events.status.eq("complete")).all(), "Episode status/completion flag mismatch")
        complete_events = events.loc[events.complete_cycle]
        require(not complete_events[["left_censored", "right_censored", "gap_interrupted"]].any().any(), "Censored or interrupted episode called complete")
        require(complete_events.observed_rise.all() and complete_events.observed_soil_end.all(), "Complete episode lacks observed boundaries")
        policy = manifest["config"]["policy"]
        require(complete_events.duration_days.ge(policy["minimum_growth_duration_days"]).all(), "Short growth fragment called complete")
        require(complete_events.green_dates.ge(policy["minimum_growth_dates"]).all(), "Too few growth observations")
        require(complete_events[["support_10m", "support_20m"]].ge(policy["minimum_common_support"]).all().all(), "Complete episode on incomparable ground")
        require(not events.accepted.any(), "Episode promoted without review")
        starts, peaks, ends = [pd.to_datetime(events[name]) for name in ("start_date", "peak_date", "end_date")]
        require((starts <= peaks).all() and (peaks <= ends).all(), "Episode chronology invalid")
        for name in ("start_date", "peak_date", "end_date", "end_confirmed_date", "soil_start_date", "soil_end_date", "preceding_soil_start_date", "preceding_soil_end_date"):
            if name in events:
                dates = pd.to_datetime(events[name])
                require(dates.loc[dates.notna()].dt.year.eq(events.loc[dates.notna(), "year"]).all(), "Event date outside its completed season")
    require(len(focus) == 18, "Focus comparison mismatch")
    joined = parcels.merge(sample, on="internal_parcel_id", validate="one_to_one", suffixes=("", "_source"))
    require(joined.cadastre_code.eq(joined.cadastre_code_source).all(), "Cadastral code changed")
    require(np.array_equal(joined.area_official_m2, joined.area_official_m2_source), "Official area changed")
    require(not joined[["household", "road_excluded"]].any().any(), "Road or household exclusion changed")
    for frame in (parcels, seasons):
        require(not frame.accepted.any(), "Unapproved classification promoted")
        require(not frame[["crop_type_candidate", "annual_cycle_candidate"]].isna().any().any(), "Null classification state")
        require(set(frame.crop_type_candidate) <= {"annual", "perennial", "undetermined"}, "Unexpected land-use class")
        require(set(frame.annual_cycle_candidate) <= {"single_cycle", "two_cycles", "undetermined", ""}, "Unexpected cycle class")
        require(frame.loc[frame.crop_type_candidate.eq("perennial"), "annual_cycle_candidate"].eq("").all(), "Perennial annual-cycle assertion")
        require(frame.loc[frame.crop_type_candidate.eq("undetermined"), "annual_cycle_candidate"].eq("undetermined").all(), "Cycle without resolved type")
    require(not parcels.training_eligible.any(), "Candidates turned into training labels")
    require(seasons.loc[~seasons.covered, "crop_type_candidate"].eq("undetermined").all(), "Uncovered season assigned a type")
    require(seasons.loc[seasons.spatial_reason.ne(""), "crop_type_candidate"].eq("undetermined").all(), "Spatial limitation ignored")

    # Check multiyear conclusions independently, without calling the aggregator.
    for row in parcels.itertuples(index=False):
        usable = seasons.loc[seasons.internal_parcel_id.eq(row.internal_parcel_id) & seasons.covered]
        n = len(usable)
        counts = usable.crop_type_candidate.value_counts()
        expected_type = "undetermined"
        if n >= 3:
            for kind in ("annual", "perennial"):
                if int(counts.get(kind, 0)) * 2 > n:
                    expected_type = kind
        expected_cycle = "" if expected_type == "perennial" else "undetermined"
        annual = usable.loc[usable.crop_type_candidate.eq("annual")]
        cycle_counts = annual.annual_cycle_candidate.value_counts()
        if expected_type == "annual":
            for cycle in ("single_cycle", "two_cycles"):
                if int(cycle_counts.get(cycle, 0)) * 2 > n:
                    expected_cycle = cycle
        require((row.crop_type_candidate, row.annual_cycle_candidate) == (expected_type, expected_cycle), "Predominant majority disagreement")
        require(row.assessable_years == n, "Ambiguous covered seasons lost from denominator")
        require(row.single_cycle_years == int(cycle_counts.get("single_cycle", 0)), "Single-cycle history count mismatch")
        require(row.two_cycle_years == int(cycle_counts.get("two_cycles", 0)), "Two-cycle history count mismatch")

    report = read_json(out / "report.json")
    require(report["types"] == parcels.crop_type_candidate.value_counts().to_dict(), "Report class counts mismatch")
    require(report["annual_cycles"] == parcels.loc[parcels.crop_type_candidate.eq("annual"), "annual_cycle_candidate"].value_counts().to_dict(), "Report cycle counts mismatch")
    require(report["covered_seasons"] == int(seasons.covered.sum()), "Report coverage mismatch")
    require(report["accuracy_measured"] is False and report["accepted"] is False, "Unsupported accuracy/approval")
    require(report["previous_labels_as_predictors"] is False and report["previous_binary_features_as_predictors"] is False, "Input independence claim changed")
    comparable = probes.loc[probes.comparable]
    specific = lambda kind, cycle: kind.eq("perennial") | (kind.eq("annual") & cycle.isin(["single_cycle", "two_cycles"]))
    full_specific = specific(comparable.full_type, comparable.full_cycle)
    probe_specific = specific(comparable.crop_type, comparable.cycle)
    full_label = comparable.full_type.where(comparable.full_type.ne("annual"), comparable.full_cycle)
    probe_label = comparable.crop_type.where(comparable.crop_type.ne("annual"), comparable.cycle)
    temporal = {"probes": len(probes), "comparable": len(comparable), "all_state_changes": int(comparable.changed.sum()),
                "jointly_resolved": int((full_specific & probe_specific).sum()),
                "resolved_disagreements": int((full_specific & probe_specific & full_label.ne(probe_label)).sum()),
                "resolved_to_unresolved": int((full_specific & ~probe_specific).sum()),
                "unresolved_to_resolved": int((~full_specific & probe_specific).sum())}
    require(report["temporal_checks"] == temporal, "Temporal report differs from independent calculation")
    type_switches = (comparable.full_type.isin(["annual", "perennial"])
                     & comparable.crop_type.isin(["annual", "perennial"])
                     & comparable.full_type.ne(comparable.crop_type))
    require(report["annual_perennial_type_switches"] == int(type_switches.sum()), "Type switches undercounted")
    for name, current, keys in (("seasons", seasons, ["internal_parcel_id", "year"]),
                                ("parcels", parcels, ["internal_parcel_id"])):
        previous = pd.read_parquet(local(PREVIOUS_EVENT + "/" + name + ".parquet"),
                                   columns=[*keys, "crop_type_candidate", "annual_cycle_candidate"])
        compared = current.merge(previous, on=keys, suffixes=("", "_events_v1"), validate="one_to_one")
        compared["changed_from_events_v1"] = (compared.crop_type_candidate.ne(compared.crop_type_candidate_events_v1)
                                               | compared.annual_cycle_candidate.ne(compared.annual_cycle_candidate_events_v1))
        saved = pd.read_parquet(out / ("comparison_events_v1_" + name + ".parquet"))
        pd.testing.assert_frame_equal(saved, compared)
        require(report["changes_from_events_v1"][name] == int(compared.changed_from_events_v1.sum()), "Previous-event comparison mismatch")

    # Re-run representative actual parcels after removing all old labels at input.
    sample2, static, daily, weights = load_inputs()
    policy = manifest["config"]["policy"]
    measured = daily.assign(date=pd.to_datetime(daily.observation_date).dt.strftime("%Y-%m-%d")).set_index(["internal_parcel_id", "date"])
    require(seasons.soil_interval_method.eq("continuous_low_envelope_with_repeated_soil_witnesses").all(), "Wrong soil grouping method")
    if len(events):
        require(events.soil_witness_count.ge(0).all() and events.soil_intermediate_low_dates.ge(0).all(), "Invalid soil counts")
        require(events.loc[events.observed_soil_end, "soil_witness_count"].ge(policy["minimum_soil_dates"]).all(), "Low-only dates certified an end")
        for row in events.itertuples(index=False):
            for field in ("soil_start_date", "soil_end_date", "end_confirmed_date"):
                date = getattr(row, field)
                if pd.isna(date):
                    continue
                observation = measured.loc[(row.internal_parcel_id, str(date))]
                require(observation.ndvi_median <= policy["soil_ndvi_maximum"]
                        and observation.evi2_median <= policy["soil_evi2_maximum"]
                        and observation.bsi_median > policy["soil_bsi_minimum"], "A low-only observation was relabeled as a soil witness")
            if row.observed_soil_end:
                require((pd.Timestamp(row.end_confirmed_date) - pd.Timestamp(row.end_date)).days >= policy["minimum_soil_span_days"], "Soil end confirmed too soon")
    observed_dates = daily.assign(date=pd.to_datetime(daily.observation_date).dt.strftime("%Y-%m-%d")).groupby("internal_parcel_id").date.apply(set).to_dict()
    if len(events):
        for row in events.itertuples(index=False):
            for field in ("start_date", "peak_date", "end_date", "end_confirmed_date", "soil_start_date", "soil_end_date", "preceding_soil_start_date", "preceding_soil_end_date"):
                value = getattr(row, field, None)
                if pd.notna(value):
                    require(str(value) in observed_dates[row.internal_parcel_id], "An event date was invented")
    tasks = tasks_from_inputs(sample2, static, daily, weights, manifest["config"]["policy"])
    replay_ids = np.linspace(0, len(tasks) - 1, 4, dtype=int)
    for i in replay_ids:
        result = analyze_parcel(tasks[int(i)])
        expected = seasons.loc[seasons.internal_parcel_id.eq(tasks[int(i)][0])].sort_values("year").reset_index(drop=True)
        actual = pd.DataFrame(result["seasons"]).sort_values("year").reset_index(drop=True)
        pd.testing.assert_frame_equal(actual, expected, check_dtype=False)

    return {"status": "structural_checks_passed", "parcels": 120, "seasons": 600, "probes": 1200,
            "episodes": len(events), "focus_seasons": len(focus), "imports": imports,
            "prior_outputs_code_geometry_and_exclusions_preserved": True,
            "no_previous_classifiers_or_label_predictors": True, "independent_majority_check_passed": True,
            "representative_parcels_replayed": len(replay_ids), "serial_parallel_identical": report["performance"]["benchmark"].get("serial_parallel_identical"),
            "accuracy_verified": False, "accepted": False, "class_counts": report["types"], "annual_cycle_counts": report["annual_cycles"]}


if __name__ == "__main__":
    result = verify()
    target = ROOT / "server_data/review/events_20260906_v2/verification.json"
    if target.exists():
        require(read_json(target) == result, "Existing verification differs; no overwrite")
    else:
        write_json(target, result)
    print(json.dumps(result, indent=2))
