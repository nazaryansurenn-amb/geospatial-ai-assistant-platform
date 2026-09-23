from __future__ import annotations

import hashlib
from collections.abc import Iterable

import pandas as pd


PARCEL_IDENTITY_SCHEMA_VERSION = 1
PARCEL_ID_NAMESPACE = "echmiadzin-cadastral-parcel-v1"
MAX_SAFE_JAVASCRIPT_INTEGER = (1 << 53) - 1


def normalize_cadastre_code(value: object) -> str:
    code = str(value).strip()
    if not code or code.lower() in {"nan", "none"}:
        raise ValueError("Cadastral code cannot be empty")
    return code


def internal_parcel_id(cadastre_code: object) -> str:
    code = normalize_cadastre_code(cadastre_code)
    digest = hashlib.sha256(f"{PARCEL_ID_NAMESPACE}:{code}".encode("utf-8")).hexdigest()
    return f"parcel_{digest[:24]}"


def public_parcel_id(cadastre_code: object) -> int:
    code = normalize_cadastre_code(cadastre_code)
    digest = hashlib.sha256(
        f"{PARCEL_ID_NAMESPACE}:public:{code}".encode("utf-8")
    ).digest()
    value = int.from_bytes(digest[:8], "big") & MAX_SAFE_JAVASCRIPT_INTEGER
    return value or 1


def build_identity_frame(cadastre_codes: Iterable[object]) -> pd.DataFrame:
    codes = [normalize_cadastre_code(value) for value in cadastre_codes]
    if pd.Series(codes, dtype="string").duplicated().any():
        raise ValueError("Canonical cadastral codes must be unique")
    result = pd.DataFrame(
        {
            "schema_version": PARCEL_IDENTITY_SCHEMA_VERSION,
            "internal_parcel_id": [internal_parcel_id(code) for code in codes],
            "public_parcel_id": [public_parcel_id(code) for code in codes],
            "cadastre_code": codes,
        }
    )
    if result["internal_parcel_id"].duplicated().any():
        raise ValueError("Internal parcel ID collision")
    if result["public_parcel_id"].duplicated().any():
        raise ValueError("Public parcel ID collision")
    return result


def attach_parcel_identity(
    frame: pd.DataFrame,
    *,
    code_column: str = "cadastre_code",
    include_public_id: bool = False,
) -> pd.DataFrame:
    if code_column not in frame.columns:
        raise ValueError(f"Missing cadastral code column: {code_column}")
    result = frame.copy()
    result[code_column] = result[code_column].map(normalize_cadastre_code)
    expected_internal = result[code_column].map(internal_parcel_id)
    if "internal_parcel_id" in result.columns:
        actual = result["internal_parcel_id"].astype(str)
        if not actual.eq(expected_internal).all():
            raise ValueError("Stored internal parcel IDs do not match cadastral codes")
    else:
        position = result.columns.get_loc(code_column)
        result.insert(position, "internal_parcel_id", expected_internal)
    if include_public_id:
        expected_public = result[code_column].map(public_parcel_id).astype("int64")
        if "public_parcel_id" in result.columns:
            actual_public = pd.to_numeric(
                result["public_parcel_id"], errors="raise"
            ).astype("int64")
            if not actual_public.eq(expected_public).all():
                raise ValueError("Stored public parcel IDs do not match cadastral codes")
        else:
            position = result.columns.get_loc(code_column)
            result.insert(position, "public_parcel_id", expected_public)
    return result
