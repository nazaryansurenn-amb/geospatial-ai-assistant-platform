import pandas as pd
import pytest

from wp_core.parcel_identity import (
    MAX_SAFE_JAVASCRIPT_INTEGER,
    attach_parcel_identity,
    build_identity_frame,
    internal_parcel_id,
    public_parcel_id,
)


def test_parcel_ids_are_deterministic_and_safe() -> None:
    code = "04-325-555-6666"
    assert internal_parcel_id(code) == internal_parcel_id(code)
    assert public_parcel_id(code) == public_parcel_id(code)
    assert 0 < public_parcel_id(code) <= MAX_SAFE_JAVASCRIPT_INTEGER


def test_identity_frame_rejects_duplicate_codes() -> None:
    with pytest.raises(ValueError, match="unique"):
        build_identity_frame(["04-1", "04-1"])


def test_attach_parcel_identity_validates_existing_values() -> None:
    frame = pd.DataFrame({"cadastre_code": ["04-1", "04-2"], "value": [1, 2]})
    result = attach_parcel_identity(frame, include_public_id=True)
    assert result["internal_parcel_id"].nunique() == 2
    assert result["public_parcel_id"].nunique() == 2

    result.loc[0, "internal_parcel_id"] = "wrong"
    with pytest.raises(ValueError, match="do not match"):
        attach_parcel_identity(result)
