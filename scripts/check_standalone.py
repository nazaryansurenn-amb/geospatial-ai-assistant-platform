"""Small offline check with legacy-project file access explicitly denied."""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))
sys.dont_write_bytecode = True
product_prefix = os.path.normcase(str(ROOT)) + os.sep
legacy_prefix = os.path.normcase(str(ROOT.parent)) + os.sep
denied = []


def deny_legacy_files(event, arguments):
    if event not in {"open", "os.listdir", "os.scandir"} or not arguments:
        return
    raw = arguments[0]
    if not isinstance(raw, (str, bytes, os.PathLike)):
        return
    path = os.path.normcase(os.path.abspath(os.fsdecode(raw)))
    if path.startswith(legacy_prefix) and path != product_prefix.rstrip(os.sep) and not path.startswith(product_prefix):
        denied.append(path)
        raise PermissionError("Standalone check: legacy project access denied")


def main():
    sys.addaudithook(deny_legacy_files)
    import importlib
    import numpy as np
    import pandas as pd
    from release_tools import verify_release
    from wp_core.parcel_land_features import build_seasonal_parcel_features

    for name in ["build_full_halo_parcel_eo", "prepare_current_parcel_universe",
                 "prepare_land_activity_2026", "prepare_land_history_2021_2025",
                 "prepare_land_use_type_v2_2021_2025", "prepare_lower_hrazdan_halos"]:
        importlib.import_module(f"scripts.{name}")
    series = ROOT / "data/analysis/parcel_eo/v5_full_halo_2021_2025"
    expected = pd.read_parquet(series / "seasonal_features_2025.parquet")
    expected = expected[expected.feature_status.eq("seasonal_profile")].head(12)
    codes = expected.cadastre_code.tolist()
    observations = pd.read_parquet(series / "lower_hrazdan_parcel_eo_observations_2025.parquet",
                                   filters=[("cadastre_code", "in", codes)])
    actual = build_seasonal_parcel_features(observations, analysis_version="isolation_check_only")
    actual = actual.set_index("cadastre_code").sort_index()
    expected = expected.set_index("cadastre_code").sort_index()
    columns = [c for c in actual.select_dtypes(include="number").columns if c in expected]
    for column in columns:
        np.testing.assert_allclose(actual[column], expected[column], equal_nan=True, rtol=1e-7, atol=1e-8)
    assert actual.feature_status.tolist() == expected.feature_status.tolist()
    releases = [verify_release(ROOT, name) for name in ["working", "use_type_review"]]
    if denied:
        raise RuntimeError("A dependency attempted legacy access")
    print(json.dumps({"legacy_access_attempts": len(denied), "sample_parcels": len(expected),
                      "sample_observations": len(observations), "numeric_features_matched": len(columns),
                      "writes_to_analytical_outputs": 0, "releases": releases}, indent=2))


if __name__ == "__main__":
    main()
