import copy,json,unittest
from pathlib import Path
import numpy as np
from wp_core.current_irrigation_stress import seasonal_features,classify

CFG=json.loads((Path(__file__).resolve().parents[1]/'config/irrigation_stress_20260907_v1.json').read_text())

class CurrentStressRules(unittest.TestCase):
    def fixture(self):
        dates=['2026-08-01','2026-08-09','2026-08-17','2026-09-02','2026-09-05']
        values={k:np.array([v],dtype=float) for k,v in {'ndvi':[.6,.61,.59,.57,.56],'evi2':[.4,.41,.39,.37,.36],
                 'ndmi':[.3,.31,.29,.19,.18],'vegetation':[.9,.9,.9,.87,.86],'bare':[.04,.04,.04,.06,.07]}.items()}
        return dates,values,np.ones((1,5),dtype=bool),np.arange(5,dtype=np.int64)[None,:]

    def get(self,data=None):return seasonal_features(*(data or self.fixture()),2026,CFG)

    def result(self,f=None,**overrides):
        args=dict(features=f or self.get(),peer_change=np.array([-.01]),peer_count=np.array([25]),history_change=np.array([-.02]),history_count=np.array([3]),weather_complete=np.array([True]),weather_support=np.array([True]),cfg=CFG)
        args.update(overrides);return classify(**args)

    def test_repeated_decline_with_all_context_can_flag(self):
        r=self.result();self.assertTrue(r['candidate'][0]);self.assertEqual(r['state'][0],'candidate')

    def test_cloud_gap_is_unknown_not_healthy(self):
        d=self.fixture();d[2][0,3]=False
        r=self.result(self.get(d));self.assertFalse(r['assessed'][0]);self.assertEqual(r['state'][0],'unassessed')

    def test_harvested_latest_date_cannot_fall_back_to_older_green(self):
        d=self.fixture();d[1]['ndvi'][0,-1]=.19;d[1]['evi2'][0,-1]=.08;d[1]['vegetation'][0,-1]=.05
        f=self.get(d);r=self.result(f);self.assertEqual(f['last_day'][0],np.datetime64('2026-09-05').astype(int))
        self.assertEqual(r['state'][0],'not_current_vegetation');self.assertFalse(r['candidate'][0])

    def test_large_vegetation_loss_remains_review(self):
        d=self.fixture();d[1]['ndvi'][0,-1]=.42;d[1]['evi2'][0,-1]=.24
        r=self.result(self.get(d));self.assertEqual(r['state'][0],'unassessed')

    def test_one_moisture_excursion_is_not_repetition(self):
        d=self.fixture();d[1]['ndmi'][0,-2]=.28
        self.assertFalse(self.result(self.get(d))['candidate'][0])

    def test_weather_peers_and_history_are_real_gates(self):
        for override in [dict(peer_count=np.array([19])),dict(history_count=np.array([1])),dict(weather_complete=np.array([False])),dict(peer_change=np.array([np.nan]))]:
            with self.subTest(override=override):self.assertEqual(self.result(**override)['state'][0],'unassessed')
        for override in [dict(weather_support=np.array([False])),dict(peer_change=np.array([-.12])),dict(history_change=np.array([-.12]))]:
            with self.subTest(override=override):self.assertEqual(self.result(**override)['state'][0],'not_flagged')

    def test_future_and_stale_dates_are_not_current_evidence(self):
        d=list(self.fixture());d[0]=['2026-08-01','2026-08-09','2026-08-17','2026-08-28','2026-09-06']
        self.assertEqual(self.result(self.get(d))['state'][0],'unassessed')

    def test_two_close_dates_do_not_satisfy_repetition(self):
        d=list(self.fixture());d[0][-2]='2026-09-04'
        self.assertFalse(self.get(d)['recent_covered'][0])

if __name__=='__main__':unittest.main()
