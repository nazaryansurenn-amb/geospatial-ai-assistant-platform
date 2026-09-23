import numpy as np
import pandas as pd
import pytest
from wp_core.sentinel1_parcel_delivery import corrected_precipitation


def test_precision_correction_keeps_gaps_and_material_negatives_unknown():
    f=pd.DataFrame({"latitude":40.,"longitude":44.,"valid_time":pd.to_datetime(["2025-04-01T00:00Z","2025-04-01T01:00Z","2025-04-01T02:00Z","2025-04-01T03:00Z","2025-04-01T04:00Z","2025-04-01T06:00Z"]),"tp":[.02,.001,.00099999,.002,.0015,.003]})
    original=f.copy(deep=True)
    out=corrected_precipitation(f,.0001)
    np.testing.assert_allclose(out.rain_mm,[np.nan,1.,0.,1.00001,np.nan,np.nan],equal_nan=True)
    assert out.precision_corrected.sum()==1
    pd.testing.assert_frame_equal(f,original)


def test_midnight_remains_previous_forecast_last_hour():
    f=pd.DataFrame({"latitude":40.,"longitude":44.,"valid_time":pd.date_range("2025-04-01T23:00Z",periods=3,freq="h"),"tp":[.01,.012,.0003]})
    np.testing.assert_allclose(corrected_precipitation(f,.0001).rain_mm,[np.nan,2.,.3],equal_nan=True)


def test_missing_value_is_not_dry_and_cells_do_not_share_predecessor():
    f=pd.DataFrame({"latitude":[40.,40.,41.,41.],"longitude":44.,"valid_time":pd.to_datetime(["2025-04-01T02:00Z","2025-04-01T03:00Z"]*2),"tp":[0.,np.nan,.001,.002]})
    np.testing.assert_allclose(corrected_precipitation(f,.0001).rain_mm,[np.nan,np.nan,np.nan,1.],equal_nan=True)


def test_duplicate_hour_rejected():
    f=pd.DataFrame({"latitude":[40.,40.],"longitude":44.,"valid_time":pd.to_datetime(["2025-04-01T01:00Z"]*2),"tp":[0.,0.]})
    with pytest.raises(ValueError,match="Duplicate"):
        corrected_precipitation(f,.0001)
