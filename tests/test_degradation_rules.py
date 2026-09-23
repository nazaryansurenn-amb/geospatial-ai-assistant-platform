import json
from pathlib import Path
import unittest
import numpy as np
from wp_core.degradation_rules import flags,annual_features
CFG=json.loads((Path(__file__).resolve().parents[1]/'config/degradation_screening_20260906_v1.json').read_text())

class DegradationRules(unittest.TestCase):
    def test_decline_low_and_stable_remain_separate(self):
        n=np.array([[.65,.62,.53,.40,.35],[.20]*5,[.60]*5]);e=np.array([[.43,.40,.34,.26,.20],[.10]*5,[.40]*5])
        r=flags(n,e,np.full_like(n,.60),np.full_like(e,.40),np.full_like(n,.70),np.full_like(e,.50),CFG)
        np.testing.assert_array_equal(r['deterioration'],[True,False,False])
        np.testing.assert_array_equal(r['low_performance'],[False,True,False])

    def test_regional_decline_alone_does_not_flag(self):
        n=np.array([[.65,.62,.53,.40,.35]]);e=np.array([[.43,.40,.34,.26,.20]])
        r=flags(n,e,n,e,n*1.2,e*1.2,CFG)
        self.assertFalse(r['deterioration'][0]);self.assertFalse(r['low_performance'][0])

    def test_missing_observations_and_peers_do_not_become_low(self):
        n=np.full((2,5),.2);e=n*.5;n[0,4]=np.nan
        pn=np.full_like(n,np.nan);pe=pn.copy()
        r=flags(n,e,np.full_like(n,.6),np.full_like(e,.4),pn,pe,CFG)
        self.assertFalse(r['deterioration_assessed'][0]);self.assertFalse(r['low_performance_assessed'].any())
        self.assertFalse(r['low_performance'].any())

    def test_single_bad_year_or_single_index_does_not_flag(self):
        n=np.array([[.6,.6,.6,.6,.1],[.6,.58,.5,.4,.3]]);e=np.full_like(n,.4)
        r=flags(n,e,np.full_like(n,.6),np.full_like(n,.4),np.full_like(n,.7),np.full_like(n,.5),CFG)
        self.assertFalse(r['deterioration'].any());self.assertFalse(r['low_performance'].any())

    def test_full_season_required_and_sampling_not_a_vote(self):
        dates=np.arange(np.datetime64('2021-03-01'),np.datetime64('2021-12-01'),np.timedelta64(5,'D'))
        n=np.full((2,len(dates)),.6);e=np.full_like(n,.4);v=np.ones_like(n,bool)
        v[1,(dates>=np.datetime64('2021-07-01'))&(dates<np.datetime64('2021-08-01'))]=False
        r=annual_features(dates,n,e,v,2021,CFG['quality'])
        self.assertTrue(r['covered'][0]);self.assertFalse(r['covered'][1]);self.assertTrue(np.isnan(r['ndvi'][1]))
        self.assertAlmostEqual(r['ndvi'][0],.6)
        thinned=annual_features(dates[::2],n[:,::2],e[:,::2],v[:,::2],2021,CFG['quality'])
        self.assertAlmostEqual(thinned['ndvi'][0],.6)
