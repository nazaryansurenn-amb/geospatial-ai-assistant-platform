import numpy as np
import pandas as pd
from wp_core.sentinel1_pilot import power_summary, excursion_rows


def test_invalid_power_is_not_zero_db_or_a_dry_observation():
    a = np.array([-32768., np.nan, 0., .1, 1.])
    r = power_summary(a, np.ones(5, dtype=bool))
    assert r["valid_pixel_count"] == 2 and r["valid_fraction"] == .4
    assert r["median_db"] == -5.


def test_empty_interior_stays_missing():
    r = power_summary(np.ones(4), np.zeros(4, dtype=bool))
    assert r["median_db"] is None and r["valid_fraction"] == 0


def sequence(days=(0,6,12), platforms=("a","a","a"), usable=(True,True,True)):
    return pd.DataFrame({"internal_parcel_id":[1]*3,"relative_orbit":[72]*3,
        "datetime":[pd.Timestamp("2025-06-01",tz="UTC")+pd.Timedelta(days=d) for d in days],
        "platform":platforms,"radar_usable":usable,"vv_inner_median_db":[-15.,-10.,-15.]})


def test_six_day_excursion_cannot_resolve_three_day_drying():
    r = excursion_rows(sequence(),[2.],3)
    assert len(r)==1 and not r[0]["rapid_drying_resolved"]
    assert not r[0]["confirmed_irrigation"] and not r[0]["rainfall_resolved"]


def test_platform_change_is_not_calibrated_moisture_change():
    r = excursion_rows(sequence((0,1,2),("a","c","a")),[2.],3)
    assert len(r)==1 and not r[0]["same_platform"] and not r[0]["rapid_drying_resolved"]


def test_missing_observation_does_not_create_an_event():
    assert excursion_rows(sequence(usable=(True,False,True)),[2.],3)==[]


def test_different_tracks_are_never_combined_into_drying_curve():
    f=sequence((0,1,2));f.loc[1,"relative_orbit"]=152
    assert excursion_rows(f,[2.],3)==[]
