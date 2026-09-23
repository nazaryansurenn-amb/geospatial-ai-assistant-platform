"""Independent saved-table verification of the selected full-area classification."""
from pathlib import Path
import json
import numpy as np
import pandas as pd
from release_tools import sha256, local_path

ROOT = Path(__file__).resolve().parent
OUT = ROOT / "data/analysis/observation_screening/transitions_area_20260906_v1"


def verify():
    complete = json.loads((OUT / "complete.json").read_text())
    for name, digest in complete["outputs"].items():
        assert sha256(local_path(OUT, name)) == digest, name
    manifest = json.loads((OUT / "manifest.json").read_text())
    for group in ("protected_sha256", "code_sha256"):
        for name, digest in manifest[group].items():
            assert sha256(local_path(ROOT, name)) == digest, name
    selected = pd.read_parquet(OUT / "parcels.parquet").set_index("internal_parcel_id").sort_index()
    raw = pd.read_parquet(OUT / "parcels_v3.parquet").set_index("internal_parcel_id").sort_index()
    source = pd.read_parquet(OUT / "selection.parquet").set_index("internal_parcel_id").sort_index()
    assert len(selected) == 22802 and selected.index.is_unique and selected.index.equals(source.index)
    pd.testing.assert_frame_equal(selected[["cadastre_code", "area_official_m2"]], source[["cadastre_code", "area_official_m2"]])
    assert not source[["household", "road_excluded"]].any().any()
    seasons = pd.concat([pd.read_parquet(OUT / "seasons" / f"{y}.parquet") for y in range(2021, 2026)], ignore_index=True)
    assert len(seasons) == 114010 and not seasons.duplicated(["internal_parcel_id", "year"]).any()
    assert seasons.groupby("internal_parcel_id").year.apply(lambda v: sorted(v) == list(range(2021, 2026))).all()
    assert not seasons.loc[~seasons.covered, "crop_type_candidate"].ne("undetermined").any()
    usable = seasons.loc[seasons.covered]
    n = usable.groupby("internal_parcel_id").size().reindex(selected.index, fill_value=0)
    counts = pd.crosstab(usable.internal_parcel_id, usable.crop_type_candidate).reindex(selected.index, fill_value=0)
    kind = pd.Series("undetermined", index=selected.index)
    for label in ("annual", "perennial"):
        kind.loc[(n >= 3) & (counts.get(label, pd.Series(0, index=selected.index)) * 2 > n)] = label
    assert kind.eq(raw.crop_type_candidate).all()
    cycles = pd.crosstab(usable.loc[usable.crop_type_candidate.eq("annual"), "internal_parcel_id"],
                         usable.loc[usable.crop_type_candidate.eq("annual"), "annual_cycle_candidate"]).reindex(selected.index, fill_value=0)
    cycle = pd.Series("undetermined", index=selected.index)
    cycle.loc[kind.eq("perennial")] = ""
    for label in ("single_cycle", "two_cycles"):
        cycle.loc[kind.eq("annual") & (cycles.get(label, pd.Series(0, index=selected.index)) * 2 > n)] = label
    assert cycle.eq(raw.annual_cycle_candidate).all()
    assert raw.assessable_years.eq(n).all()
    assign = kind.eq("annual") & cycle.eq("undetermined")
    expected = cycle.mask(assign, "single_cycle")
    assert selected.crop_type_candidate.eq(kind).all() and selected.annual_cycle_candidate.eq(expected).all()
    assert selected.annual_cycle_candidate_v3.eq(cycle).all() and selected.owner_cycle_assignment_applied.eq(assign).all()
    assert not selected[["accepted", "training_eligible"]].any().any()
    control = pd.read_parquet(ROOT / "data/analysis/observation_screening/transitions_20260906_v3_owner_v1/parcels.parquet").set_index("internal_parcel_id").sort_index()
    common = [c for c in control.columns if c in selected.columns]
    pd.testing.assert_frame_equal(control[common], selected.loc[control.index, common], check_dtype=False)
    report = json.loads((OUT / "report.json").read_text())
    assert report["types"] == selected.crop_type_candidate.value_counts().to_dict()
    assert report["cycles"] == selected.loc[kind.eq("annual"), "annual_cycle_candidate"].value_counts().to_dict()
    assert report["owner_cycle_assignments"] == int(assign.sum())
    result = {"status": "saved_area_checks_passed", "eligible_parcels": len(selected), "parcel_seasons": len(seasons),
              "independent_majority_check": True, "owner_assignment_check": True, "control_parcels_replayed": len(control),
              "control_parcel_seasons_replayed_by_runner": 600, "identity_official_area_and_exclusions_preserved": True,
              "previous_versions_preserved": True, "checker_sha256": sha256(Path(__file__).resolve()),
              "types": report["types"], "cycles": report["cycles"], "owner_assignments": int(assign.sum())}
    destination = ROOT / "server_data/review/transitions_area_20260906_v1/area_verification.json"
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        assert json.loads(destination.read_text()) == result
    else:
        destination.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    return result


if __name__ == "__main__":
    print(json.dumps(verify(), indent=2))
