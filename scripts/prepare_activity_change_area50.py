"""Read-only source replay; append a narrower cultivation-change version."""
import json
from pathlib import Path
import sys
import geopandas as gpd
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))
from release_tools import sha256
from run_observation_analysis import write_json, now
from wp_core.activity_change_history import YEARS, CLASSES, summary
from wp_core.activity_change_area50 import annual_area_evidence, gated_transition

SLUG = "activity_change_area50_20260907_v1"
OUT = ROOT / "data/analysis/activity_change" / SLUG
REVIEW = ROOT / "server_data/review" / SLUG
PREVIOUS = ROOT / "data/analysis/activity_change/activity_change_history_review_20260906_v1"
SEASONS = ROOT / "data/analysis/parcel_eo/v5_full_halo_2021_2025"


def prepare():
    if OUT.exists() or REVIEW.exists():
        raise ValueError("Preserve existing versions")
    old_manifest = json.loads((PREVIOUS / "manifest.json").read_text())
    inputs = {**old_manifest["inputs"], (PREVIOUS / "register.parquet").relative_to(ROOT).as_posix(): sha256(PREVIOUS / "register.parquet")}
    for name, digest in inputs.items():
        assert sha256(ROOT / name) == digest, name
    register = gpd.read_parquet(PREVIOUS / "register.parquet")
    assert len(register) == 43984 and register.cadastre_code.is_unique and register.internal_parcel_id.is_unique
    assert register.crs.to_epsg() == 4326 and register.geometry.is_valid.all() and not register.geometry.is_empty.any()
    eligible = register[~register.household.astype(bool) & ~register.road_excluded.astype(bool)]
    assert len(eligible) == 22802
    OUT.mkdir(parents=True)
    REVIEW.mkdir(parents=True)
    yearly, quality = [], []
    for year in YEARS:
        path = SEASONS / f"lower_hrazdan_parcel_eo_observations_{year}.parquet"
        inputs[path.relative_to(ROOT).as_posix()] = sha256(path)
        observations = pd.read_parquet(path)
        assert not observations.duplicated(["cadastre_code", "observation_date", "scene_id"]).any()
        identities = observations[["cadastre_code", "internal_parcel_id"]].drop_duplicates()
        assert identities.cadastre_code.is_unique
        assert set(identities.cadastre_code) == set(register.cadastre_code)
        joined = identities.merge(register[["cadastre_code", "internal_parcel_id"]], on="cadastre_code", validate="one_to_one")
        assert joined.internal_parcel_id_x.eq(joined.internal_parcel_id_y).all()
        assert set(observations.source_program) == {"Copernicus Sentinel-2 L2A"}, set(observations.source_program)
        annual, daily = annual_area_evidence(observations[observations.cadastre_code.isin(eligible.cadastre_code)], year)
        assert set(annual.cadastre_code) == set(eligible.cadastre_code)
        yearly.append(annual)
        daily[["cadastre_code", "observation_date", "scene_id", "usable_area_date", "area_share_lower", "area_share_upper", "count_recovery_exact"]].to_parquet(OUT / f"dated_area_evidence_{year}.parquet", index=False)
        quality.append({"year": year, "parcels": len(annual), "dates": observations.observation_date.nunique(),
                        "source_rows": len(observations), "area50_supported_parcels": int(annual.passes_area50.sum()),
                        "unavailable_area_parcels": int(annual.usable_area_dates.lt(2).sum())})
        print(json.dumps(quality[-1]), flush=True)
    annual = pd.concat(yearly, ignore_index=True)
    annual.to_parquet(OUT / "annual_area_evidence.parquet", index=False)
    wide = annual.pivot(index="cadastre_code", columns="analysis_year", values="repeated_area_share_lower").reindex(columns=YEARS)
    shares = {code: [None if pd.isna(v) else float(v) for v in values] for code, values in zip(wide.index, wide.to_numpy())}
    result = register.copy()
    result["previous_change_class"] = register.change_class
    decisions = [gated_transition(r.annual_state_codes, r.profile_year_count, shares.get(r.cadastre_code, [None] * 5), bool(r.household), bool(r.road_excluded)) for r in result.itertuples()]
    result["change_class"] = [r[0] for r in decisions]
    result["change_year"] = pd.array([r[1] for r in decisions], dtype="Int64")
    result["area_gate_reason"] = [r[2] for r in decisions]
    winners = result[result.change_class.isin(CLASSES)]
    assert set(winners.cadastre_code) <= set(register[register.change_class.isin(CLASSES)].cadastre_code)
    assert winners.change_class.eq(winners.previous_change_class).all()
    assert result.geometry.to_wkb().equals(register.geometry.to_wkb()) and result.area_official_m2.equals(register.area_official_m2)
    result.to_parquet(OUT / "register.parquet", index=False)
    winners.to_parquet(OUT / "candidates.parquet", index=False)
    lookup = {r.cadastre_code: {"changeClass": r.change_class, "changeYear": None if pd.isna(r.change_year) else int(r.change_year)} for r in result.itertuples()}
    payload = {"analysis_version": SLUG, "observation_years": list(YEARS), "summaries": {}, "bounds": {}, "created_at": now(), "area_basis": "whole_cadastral_parcels"}
    for scope in ("lower_hrazdan", "stage_1", "stage_2"):
        part = result if scope == "lower_hrazdan" else result[result.stage.eq(scope)]
        payload["summaries"][scope] = summary(part)
        payload["bounds"][scope] = {key: list(part[part.change_class.eq(key)].total_bounds) if part.change_class.eq(key).any() else None for key in CLASSES}
    write_json(REVIEW / "summary.json", payload)
    write_json(REVIEW / "parcel_lookup.json", lookup)
    changes = result[result.change_class.ne(result.previous_change_class)]
    write_json(OUT / "manifest.json", {"analysis_version": SLUG, "created_at": now(), "inputs": inputs,
        "rule": "Preserved single transition; each partial season needs >=50% observed vegetation/whole 10m sample support on >=2 distinct usable dates in that year",
        "metric": "Second-largest lower bound of reconstructed vegetation pixel count / parcel pixel count; not a cadastral survey or cropped-area truth",
        "unknown_or_below_cutoff_never_becomes_inactive": True, "no_250m_inputs": True, "no_2026_inputs": True,
        "geometry_code_official_area_unchanged": True, "existing_active_and_no_activity_states_unchanged": True})
    write_json(OUT / "verification.json", {"passed": True, "register_parcels": len(result), "eligible_parcels": len(eligible),
        "annual_evidence_rows": len(annual), "yearly": quality, "previous_candidates": int(register.change_class.isin(CLASSES).sum()),
        "new_candidates": len(winners), "removed": len(changes), "removal_reasons": changes.area_gate_reason.value_counts().to_dict(),
        "no_class_flips": True, "summary": payload["summaries"]["lower_hrazdan"]})
    print(json.dumps(payload["summaries"]["lower_hrazdan"], indent=2), flush=True)


if __name__ == "__main__":
    prepare()
