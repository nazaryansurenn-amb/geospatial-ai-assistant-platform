"""Materialize the owner's selected v3 parcel categories without recalculating EO."""
import hashlib
import json
from pathlib import Path
import shutil

import pandas as pd

from wp_core.classification_selection import select_parcel_categories

ROOT = Path(__file__).resolve().parent
CONFIG = "config/classification_selection.json"
CODE = ["run_classification_selection.py", "wp_core/classification_selection.py"]


def local(relative):
    path = (ROOT / relative).resolve()
    if Path(relative).is_absolute() or not path.is_relative_to(ROOT):
        raise ValueError("Path must remain inside WORKING_PRODUCT")
    return path


def read(path):
    return json.loads(path.read_text(encoding="utf-8"))


def sha(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def check(hashes):
    for relative, expected in hashes.items():
        if sha(local(relative)) != expected:
            raise ValueError("Preserved file changed: " + relative)


def write(path, value):
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def main():
    config = read(local(CONFIG))
    if (config["baseline"] != "data/analysis/observation_screening/transitions_20260906_v3"
            or config["version"] != "transitions_20260906_v3_owner_v1"
            or config["output"] != "data/analysis/observation_screening/" + config["version"]
            or config["years"] != list(range(2021, 2026))
            or config["application_stage"] != "after_predominant_parcel_summary"
            or config["annual_uncertain_cycle_assignment"] != "single_cycle"
            or config["undetermined_type_assignment"] != "unchanged"
            or config["calculation_scope"] != "existing_control_120"
            or config["owner_approved_rule"] is not True
            or config["full_area_run_authorized"] or config["public_release_change_authorized"]):
        raise ValueError("Unexpected selected rule or scope")
    source, out = local(config["baseline"]), local(config["output"])
    preserved = {}
    for version in ("transitions_20260906_v3", "events_20260906_v1", "events_20260906_v2"):
        prefix = "data/analysis/observation_screening/" + version
        complete = read(local(prefix + "/complete.json"))
        preserved[prefix + "/complete.json"] = sha(local(prefix + "/complete.json"))
        preserved.update({prefix + "/" + p: h for p, h in complete["outputs"].items()})
        manifest = read(local(prefix + "/manifest.json"))
        for group in ("input_sha256", "code_sha256", "protected_sha256"):
            preserved.update(manifest.get(group, {}))
    preserved.update({p: sha(local(p)) for p in ("config/releases.json", "config/data_collector.json",
                                                  "run_product.py", "run_use_type_candidate.py",
                                                  "wp_core/use_type_release.py")})
    check(preserved)
    pins = {p: sha(local(p)) for p in [CONFIG, *CODE]}
    manifest = {"config": config, "source_sha256": preserved, "code_and_config_sha256": pins,
                "observed_cycle_evidence_preserved": True, "privacy": "internal_only"}
    if out.exists():
        if not (out / "complete.json").exists():
            raise ValueError("Incomplete selection already exists; preserve it before recovery")
        if read(out / "manifest.json") != manifest:
            raise ValueError("Completed selection pins changed; choose a new version")
        check({config["output"] + "/" + p: h for p, h in read(out / "complete.json")["outputs"].items()})
        print("Selected classification verified; no files rewritten")
        return
    baseline = pd.read_parquet(source / "parcels.parquet")
    if len(baseline) != 120:
        raise ValueError("Expected the existing 120-parcel control")
    selected = select_parcel_categories(baseline)
    # Restore the raw cycle column and require every original field to match.
    restored = selected[baseline.columns].copy()
    restored["annual_cycle_candidate"] = selected.annual_cycle_candidate_v3
    pd.testing.assert_frame_equal(restored, baseline)
    applied = selected.owner_cycle_assignment_applied
    if int(applied.sum()) != 17 or selected[["accepted", "training_eligible"]].any().any():
        raise ValueError("Unexpected source selection or evidence status")
    counts = selected.groupby(["crop_type_candidate", "annual_cycle_candidate"]).size()
    if counts.to_dict() != {("annual", "single_cycle"): 42, ("annual", "two_cycles"): 14,
                            ("perennial", ""): 21, ("undetermined", "undetermined"): 43}:
        raise ValueError("Selected categories do not reconcile")
    out.mkdir(parents=True, exist_ok=False)
    write(out / "manifest.json", manifest)
    for relative in [CONFIG, *CODE]:
        target = out / "source" / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(local(relative), target)
    selected.to_parquet(out / "parcels.parquet", index=False, compression="zstd")
    pd.testing.assert_frame_equal(pd.read_parquet(out / "parcels.parquet"), selected)
    report = {"status": "owner_selected_first_attempt", "baseline": "Transitions v3", "parcels": 120,
              "annual_single": 42, "annual_two": 14, "perennial": 21, "undetermined_type": 43,
              "annual_uncertain_assigned_single": 17, "owner_approved_rule": True,
              "original_evidence_preserved": True, "seasonal_evidence_recalculated": False,
              "accuracy_measured": False, "full_area_recalculated": False, "public_release_changed": False}
    write(out / "report.json", report)
    check(preserved)
    check(pins)
    hashes = {p.relative_to(out).as_posix(): sha(p) for p in out.rglob("*") if p.is_file()}
    write(out / "complete.json", {"outputs": hashes})
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
