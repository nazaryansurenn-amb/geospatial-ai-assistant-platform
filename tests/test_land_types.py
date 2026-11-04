import json
from pathlib import Path
import unittest
import numpy as np
from wp_core.alphaearth import area_accuracy
from wp_core.land_types import contradictions, decide, field_blocks, stehman_accuracy, stratified_ratio

CFG = json.loads((Path(__file__).resolve().parents[1] / 'config/alphaearth_types_20261030_v1.json').read_text(encoding='utf-8'))


class DecisionRules(unittest.TestCase):
    def check(self, shares, expected, main=None):
        r = decide(shares, CFG)
        self.assertEqual(r['type'], expected, shares)
        if main:
            self.assertEqual(r['main'], main)

    def test_non_agricultural_is_checked_first(self):
        self.check({'BLT': .6, 'ANN': .4}, 'BLT')
        self.check({'BLT': 1}, 'BLT')
        self.check({'BLT': .3, 'WAT': .3, 'ANN': .4}, 'BLT')

    def test_single_perennial_majority(self):
        self.check({'VIN': .5, 'ANN': .5}, 'VIN')
        self.check({'ORC': .7, 'NCU': .3}, 'ORC')

    def test_mixed_cultivation_and_partly_cultivated(self):
        self.check({'VIN': .3, 'ORC': .3, 'NCU': .4}, 'MIX', 'VIN')  # tie -> type listed first in config
        self.check({'ANN': .6, 'NCU': .4}, 'ANN')
        self.check({'ANN': .4, 'NCU': .6}, 'PART', 'ANN')
        self.check({'GRH': .2, 'NCU': .8}, 'PART', 'GRH')

    def test_not_cultivated_and_small_slivers(self):
        self.check({'NCU': 1}, 'NCU')
        self.check({'ANN': .05, 'NCU': .95}, 'NCU')
        self.check({'GRS': .7, 'NCU': .3}, 'GRS')

    def test_tree_rows_are_never_a_parcel_type(self):
        r = decide({'TRE': .6, 'ANN': .4}, CFG)
        self.assertEqual(r['type'], 'DEFER'); self.assertIn('reason', r)
        self.check({'TRE': .3, 'BLT': .3, 'ANN': .4}, 'BLT')

    def test_invalid_shares_are_rejected(self):
        for shares in [{'ANN': .5}, {'XYZ': 1}, {'ANN': -.1, 'NCU': 1.1}, {}]:
            with self.assertRaises(ValueError):
                decide(shares, CFG)


class Contradictions(unittest.TestCase):
    def test_signals_against_types(self):
        self.assertTrue(contradictions({'type': 'VIN'}, {'green_years': 0}))
        self.assertTrue(contradictions({'type': 'NCU'}, {'cycles_2025': 1}))
        self.assertTrue(contradictions({'type': 'BLT'}, {'peak_ndvi_2025': .7}))
        self.assertTrue(contradictions({'type': 'NCU', 'flags': {'active_2025': True}}, {}))
        self.assertFalse(contradictions({'type': 'VIN'}, {'green_years': 5, 'cycles_2025': 1}))
        self.assertFalse(contradictions({'type': 'ANN'}, {}))

    def test_perennials_with_zero_annual_cycles_are_not_flagged(self):
        # Pilot round 1: vineyards never show bare soil, so the annual-cycle counter is 0 for them.
        self.assertFalse(contradictions({'type': 'VIN'}, {'green_years': 5, 'cycles_2025': 0}))
        self.assertTrue(contradictions({'type': 'NCU'}, {'cycles_2025': 0, 'peak_ndvi_2025': .62}))
        self.assertFalse(contradictions({'type': 'BLT'}, {'peak_ndvi_2025': .7, 'area_ha': .1}))


class V2Helpers(unittest.TestCase):
    def test_weak_spring_peak_and_mean_width(self):
        from wp_core.land_types import mean_width, weak_spring_peak
        spring = [.15, .2, .3, .45, .35, .2, .15, .14, .15, .16, .15, .14]
        self.assertTrue(weak_spring_peak({'ndvi': {'2025': spring}, 'cycles': {'2025': 0}}))
        self.assertFalse(weak_spring_peak({'ndvi': {'2025': spring}, 'cycles': {'2025': 1}}))
        crop = [.15, .2, .3, .45, .6, .7, .65, .4, .2, .16, .15, .14]
        self.assertFalse(weak_spring_peak({'ndvi': {'2025': crop}, 'cycles': {'2025': 0}}))
        rule = {'min_peak': .35, 'min_rise_over_winter': .15}
        self.assertTrue(weak_spring_peak({'ndvi': {'2025': spring}, 'cycles': {'2025': 0}}, rule))
        bare = [.18, .18, .2, .25, .21, .12, .1, .1, .1, .12, .1, .1]  # bare all year: no real spring peak
        self.assertTrue(weak_spring_peak({'ndvi': {'2025': bare}, 'cycles': {'2025': 0}}))
        self.assertFalse(weak_spring_peak({'ndvi': {'2025': bare}, 'cycles': {'2025': 0}}, rule))
        flat_high = [.3, .3, .32, .38, .36, .25, .2, .2, .2, .22, .2, .2]  # peak 0.38 but only 0.08 above winter
        self.assertFalse(weak_spring_peak({'ndvi': {'2025': flat_high}, 'cycles': {'2025': 0}}, rule))
        self.assertAlmostEqual(mean_width(1000 * 5, 2 * (1000 + 5)), 4.975, places=3)


class FieldBlocks(unittest.TestCase):
    def test_every_parcel_once_and_blocks_bounded(self):
        rng = np.random.default_rng(3)
        xy = rng.uniform(0, 1000, (200, 2)); half = rng.uniform(5, 30, (200, 1))
        bounds = np.hstack([xy - half, xy + half])
        blocks = field_blocks(xy, bounds, 12, 250)
        flat = sorted(i for b in blocks for i in b)
        self.assertEqual(flat, list(range(200)))
        for b in blocks:
            self.assertLessEqual(len(b), 12)
            box = np.r_[bounds[b, :2].min(0), bounds[b, 2:].max(0)]
            self.assertLessEqual(max(box[2] - box[0], box[3] - box[1]), 250 + 1e-9)


class StehmanEstimates(unittest.TestCase):
    def test_census_gives_exact_values_without_error(self):
        area = np.array([1., 2., 3., 4.]); stratum = np.array([0, 0, 1, 1])
        y = area * np.array([1, 0, 1, 1])
        r, se = stratified_ratio(y, area, stratum, {0: 2, 1: 2})
        self.assertAlmostEqual(r, 8 / 10); self.assertAlmostEqual(se, 0)

    def test_equal_units_and_map_strata_match_olofsson_estimates(self):
        counts = np.array([[66, 0, 5, 4], [0, 55, 8, 12], [1, 0, 153, 11], [2, 1, 9, 313]])
        mapped = np.array([200000, 150000, 3200000, 6450000])
        m = np.repeat(np.arange(4), counts.sum(1))
        ref = np.concatenate([np.repeat(np.arange(4), row) for row in counts])
        ones = np.ones(len(m))
        old = area_accuracy(m, ref, ones, mapped, mapped, [0, 1, 2, 3])
        new = stehman_accuracy(m, ref, ones, m, dict(enumerate(mapped)), [0, 1, 2, 3])
        self.assertAlmostEqual(new['overall'][0], old['overall'], places=9)
        self.assertAlmostEqual(new['overall'][1], old['overall_se'], places=9)
        for c in range(4):
            self.assertAlmostEqual(new['classes'][c]['area_proportion'][0], old['proportion'][c], places=9)
            self.assertAlmostEqual(new['classes'][c]['users'][0], old['users'][c], places=9)
            self.assertAlmostEqual(new['classes'][c]['producers'][0], old['producers'][c], places=9)

    def test_strata_different_from_map_classes(self):
        stratum = np.array([0, 0, 0, 1, 1, 1])
        m = np.array(['A', 'B', 'A', 'B', 'B', 'A']); ref = np.array(['A', 'B', 'B', 'B', 'A', 'A'])
        r = stehman_accuracy(m, ref, np.ones(6), stratum, {0: 30, 1: 30}, ['A', 'B'])
        self.assertAlmostEqual(r['overall'][0], 4 / 6)
        self.assertAlmostEqual(r['classes']['A']['area_proportion'][0], .5)


class NaturalGroundCropSeason(unittest.TestCase):
    def test_natural_ground_with_crop_cycle_is_flagged(self):
        from wp_core.land_types import contradictions
        out = contradictions({'type': 'NAT'}, {'crop_cycle_years': ['2021', '2024']})
        self.assertEqual(len(out), 1)
        self.assertIn('2021, 2024', out[0])
        self.assertEqual(contradictions({'type': 'NAT'}, {'crop_cycle_years': []}), [])
        self.assertEqual(contradictions({'type': 'NAT'}, {}), [])  # older rules versions pass no such evidence
        self.assertEqual(contradictions({'type': 'NCU'}, {'crop_cycle_years': ['2021']}), [])


class AlfalfaCuts(unittest.TestCase):
    def test_cut_events_and_green_days(self):
        from datetime import datetime, timedelta
        from wp_core.land_types import contradictions, cut_events, green_days
        start = datetime(2025, 4, 1)
        def series(values, step=5):
            return [(start + timedelta(days=step * k), v) for k, v in enumerate(values)]
        alfalfa = series([.4, .7, .8, .8, .4, .5, .7, .8, .8, .35, .5, .7, .8, .8, .4, .5, .7, .75])
        annual = series([.2, .3, .5, .7, .8, .8, .7, .5, .3, .2, .2, .2])
        self.assertEqual(cut_events(alfalfa), 3)
        self.assertEqual(cut_events(annual), 0)
        self.assertEqual(green_days(series([.6, .6, .6, .3])), 10)
        flag = contradictions({'type': 'ANN'}, {'alfalfa_cut_years': ['2024', '2025']})
        self.assertTrue(flag and flag[0].startswith('repeated cuts like alfalfa'))
        self.assertEqual(contradictions({'type': 'ALF'}, {'alfalfa_cut_years': ['2024', '2025']}), [])
        self.assertTrue(contradictions({'type': 'ALF'}, {'alfalfa_without_cuts': True}))
        self.assertEqual(contradictions({'type': 'ORC'}, {'alfalfa_cut_years': ['2024', '2025']}), [])  # orchards with mown grass


class OwnerRulesV24(unittest.TestCase):
    def test_tree_stand_becomes_a_type(self):
        import json
        from pathlib import Path
        from wp_core.land_types import decide
        cfg = json.loads((Path(__file__).resolve().parents[1] / 'config/alphaearth_types_20261030_v2_4.json').read_text(encoding='utf-8'))
        self.assertEqual(decide({'TRE': .7, 'BLT': .3}, cfg)['type'], 'TRE')
        self.assertEqual(decide({'TRE': .5, 'GRS': .5}, cfg)['type'], 'TRE')
        self.assertEqual(decide({'TRE': .4, 'NAT': .3, 'ANN': .3}, cfg)['type'], 'NAT')
        old = json.loads((Path(__file__).resolve().parents[1] / 'config/alphaearth_types_20261030_v2_3.json').read_text(encoding='utf-8'))
        self.assertEqual(decide({'TRE': .7, 'BLT': .3}, old)['type'], 'BLT')  # earlier versions unchanged
