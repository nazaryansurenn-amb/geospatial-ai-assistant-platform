import pandas as pd
from wp_core import sentinel1_multiyear as m


def test_optical_join_respects_gap_quality_and_preserves_missing():
    obs = pd.DataFrame({"internal_parcel_id": ["p", "p", "p"], "datetime": ["2021-04-05T12:00Z", "2021-04-15T12:00Z", "2021-05-01T00:00Z"]})
    eo = pd.DataFrame({"internal_parcel_id": ["p", "p", "p"], "observation_date": ["2021-04-04", "2021-04-15", "2021-04-17"], "support": [.8, .3, .9], "finite": [True, True, True], "ndvi_median": [.4,.5,.6], "bsi_median": [.1,.1,.1], "ndmi_median": [.2,.2,.2]})
    result = m.join_optical(obs, eo)
    assert result.eo_date.iloc[:2].tolist() == ["2021-04-04", "2021-04-17"]
    assert pd.isna(result.eo_date.iloc[2]) and pd.isna(result.eo_ndvi.iloc[2])
    assert list(obs.columns) == ["internal_parcel_id", "datetime"]


def test_year_cache_changes_with_weather_and_does_not_mix_years():
    a = [{"month": "2021_04", "sha256": "one"}]
    b = a + [{"month": "2021_05", "sha256": "two"}]
    assert m.year_key(2021, a) == m.year_key(2021, a)
    assert m.year_key(2021, a) != m.year_key(2021, b)
    assert m.year_key(2021, a) != m.year_key(2022, a)
