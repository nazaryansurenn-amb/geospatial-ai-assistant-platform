import numpy as np
import pandas as pd
from wp_core import sentinel1_area_compare as fast
from wp_core import sentinel1_peer_comparison as original
from wp_core import sentinel1_multiyear as multi
from test_sentinel1_peer_comparison import fixture_data


def test_fast_comparison_matches_frozen_rules_with_signal_and_missing_context():
    cfg=original.read(original.CONFIG)
    obs,fields,potential,rain=fixture_data()
    obs.loc[(obs.internal_parcel_id=='p4')&(obs.source_item=='s2'),'eo_ndvi']=np.nan
    old_diag,old_links,old_metrics,_=original.compare(obs,fields,potential,rain,cfg)
    diag,links,metrics,signals=fast.compare(obs,fields,potential,rain,cfg)
    keys=['internal_parcel_id','relative_orbit','platform','rain_limit_mm','excursion_db']
    pd.testing.assert_frame_equal(old_metrics.sort_values(keys).reset_index(drop=True),metrics.sort_values(keys).reset_index(drop=True),check_dtype=False)
    dk=['internal_parcel_id','middle_item']
    cols=['supported','peer_count','weather_complete','rise_db','fall_db','peer_median_rise_db','peer_median_fall_db','rain_before_mm','rain_after_mm','rain_recent_48h_mm']
    a=old_diag.drop_duplicates(dk).sort_values(dk).reset_index(drop=True)
    b=diag.sort_values(dk).reset_index(drop=True)
    pd.testing.assert_frame_equal(a[dk+cols],b[dk+cols],check_dtype=False)
    sk=['internal_parcel_id','middle_item','rain_limit_mm','excursion_db']
    expected=old_diag.loc[old_diag.observed_excursion.fillna(False)].sort_values(sk).reset_index(drop=True)
    actual=signals.sort_values(sk).reset_index(drop=True)
    pd.testing.assert_frame_equal(expected[sk+['relative_excursion']],actual[sk+['relative_excursion']],check_dtype=False)
    lk=['internal_parcel_id','peer_id','first_item','middle_item','last_item']
    pd.testing.assert_frame_equal(old_links.sort_values(lk).reset_index(drop=True),links.sort_values(lk).reset_index(drop=True),check_dtype=False)


def test_fast_comparison_with_rain_and_alternating_platforms():
    cfg=original.read(original.CONFIG)
    obs,fields,potential,rain=fixture_data()
    obs.loc[obs.source_item.isin(['s1','s3','s5','s7']),'platform']='sentinel-1c'
    rain['cell'].loc['2025-04-10T00:00Z']=.7
    old=original.compare(obs,fields,potential,rain,cfg)[2]
    new=fast.compare(obs,fields,potential,rain,cfg)[2]
    keys=['internal_parcel_id','relative_orbit','platform','rain_limit_mm','excursion_db']
    pd.testing.assert_frame_equal(old.sort_values(keys).reset_index(drop=True),new.sort_values(keys).reset_index(drop=True),check_dtype=False)


def test_vector_optical_join_matches_original_nearest_and_ties():
    obs=pd.DataFrame({'internal_parcel_id':['p1']*4,'datetime':['2021-04-01T12:00:00Z','2021-04-04T12:00:00Z','2021-04-20T01:00:00Z','2021-04-02T00:00:00Z'],'relative_orbit':72})
    eo=pd.DataFrame({'internal_parcel_id':['p1']*3,'observation_date':['2021-04-01','2021-04-03','2021-04-05'],'support':[1.,1.,.2],'finite':True,'ndvi_median':[.2,.3,.9],'bsi_median':[.1,.2,.8],'ndmi_median':[.4,.5,.8]})
    old=multi.join_optical(obs,eo).sort_values('datetime').reset_index(drop=True)
    new=fast.join_optical(obs,eo).sort_values('datetime').reset_index(drop=True)
    pd.testing.assert_frame_equal(old,new,check_dtype=False)
