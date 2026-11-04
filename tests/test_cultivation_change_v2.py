"""Rule functions of scripts/cultivation_change_v2.py on synthetic series (no data files needed)."""
import copy
import unittest

import numpy as np
import pandas as pd

import cultivation_change_v2 as c

CFG = c.load_config()
YEAR = 2025
LIMITS = {"green_dates_min": 6, "green_span_days_min": 30, "idle_max_ndvi_jun_oct": 0.3, "harvest_fall_min": 0.3,
          "idle_max_vegetation_share": 1.01}


def series(points, step=5, end=304):
    """Piecewise-linear NDVI at every `step` days from 1 Jan to `end` (day of year)."""
    days = np.arange(0, end + 1, step)
    x, y = zip(*points)
    v = np.interp(days, x, y)
    vf = np.where(v >= .5, 1.0, np.where(v >= .3, .4, 0.0))
    bsi = np.where(v <= .25, .2, -.1)
    return days, v, vf, bsi, v.copy()


TWO = [(0, .15), (75, .15), (80, .3), (110, .6), (152, .85), (175, .6), (186, .15), (205, .15), (210, .3), (235, .7),
       (248, .85), (270, .6), (288, .15), (304, .15)]
ONE_SUMMER = [(0, .15), (130, .15), (150, .4), (170, .7), (210, .85), (250, .6), (270, .15), (304, .15)]
BARE = [(0, .15), (304, .18)]


def state(points, limits=LIMITS, step=5):
    days, v, vf, _, _ = series(points, step)
    f = pd.DataFrame([c.year_features(days, v, vf, YEAR, CFG)])
    return c.classify_states(f, limits, CFG, YEAR)[0]


class Primitives(unittest.TestCase):
    def test_smooth3_matches_pandas(self):
        v = np.array([.1, .9, .2, .3, .8, .7, .1])
        ref = pd.Series(v).rolling(3, center=True, min_periods=1).median().to_numpy()
        np.testing.assert_allclose(c.smooth3(v), ref)

    def test_find_cycles_closed_and_open(self):
        v = np.array([.1, .6, .7, .1, .2, .6, .4])
        self.assertEqual(c.find_cycles(v, .25, .5), [(0, 3), (4, 7)])          # second cycle open at the end
        self.assertEqual(c.find_cycles(np.array([.6, .7, .1]), .25, .5), [(-1, 2)])  # open at the start
        self.assertEqual(c.find_cycles(np.array([.1, .45, .1]), .25, .5), [])  # weed regrowth below green: no cycle

    def test_max_gap_uses_period_edges(self):
        self.assertEqual(c.max_gap([100, 130, 150, 250], 120, 243), 100)
        self.assertEqual(c.max_gap([], 120, 243), 123)

    def test_clean_daily_drops_out_of_range_rows_in_every_year(self):
        d = pd.DataFrame({"internal_parcel_id": ["a"] * 5, "observation_date": ["2021-01-01", "2021-01-02", "2021-01-03", "2021-01-04", "2021-01-05"],
                          "ndvi_median": [.2, 1.3, -1.5, np.nan, .3], "ndvi_valid_fraction": [1, 1, .1, 1, .5]})
        out, n = c.clean_daily(d, CFG)
        self.assertEqual(list(out.ndvi_median), [.2])
        self.assertEqual(n["rows_ndvi_out_of_range"], 2)
        self.assertEqual(n["of_which_pass_cloud_gate"], 1)
        self.assertEqual(n["rows_ndvi_missing"], 1)


class YearlyState(unittest.TestCase):
    def test_summer_crop_is_cultivated(self):
        self.assertEqual(state(ONE_SUMMER), "cultivated")

    def test_bare_year_is_idle(self):
        self.assertEqual(state(BARE), "idle")

    def test_vegetation_share_limit_makes_idle_stricter(self):
        days, v, vf, _, _ = series([(0, .15), (120, .35), (160, .2), (304, .18)])
        f = pd.DataFrame([c.year_features(days, v, vf, YEAR, CFG)])
        self.assertEqual(c.classify_states(f, LIMITS, CFG, YEAR)[0], "idle")
        self.assertEqual(c.classify_states(f, {**LIMITS, "idle_max_vegetation_share": .2}, CFG, YEAR)[0], "unclear")

    def test_spring_flush_without_harvest_drop_is_unclear(self):
        # green April-May, slow decline that stays above bare until autumn: wild vegetation, not a crop
        self.assertEqual(state([(0, .15), (90, .2), (110, .65), (140, .58), (165, .32), (304, .28)]), "unclear")

    def test_spring_crop_with_harvest_before_july_is_cultivated(self):
        self.assertEqual(state([(0, .15), (70, .15), (100, .65), (150, .8), (165, .15), (304, .15)]), "cultivated")

    def test_green_without_bare_start_is_unclear(self):
        self.assertEqual(state([(0, .6), (90, .8), (200, .7), (250, .15), (304, .15)]), "unclear")

    def test_few_valid_dates_or_summer_gap_is_unusable(self):
        self.assertEqual(state(ONE_SUMMER, step=40), "unusable")

    def test_green_dates_limit(self):
        strict = {**LIMITS, "green_dates_min": 30}
        self.assertEqual(state(ONE_SUMMER, strict), "unclear")


class StartStop(unittest.TestCase):
    def test_started_needs_three_idle_years(self):
        s = {2022: "idle", 2023: "idle", 2024: "idle", 2025: "cultivated", 2026: "cultivated"}
        self.assertEqual(c.start_stop(s, 2025), ("started", "confirmed_by_next_year"))
        self.assertEqual(c.start_stop({**s, 2022: "unclear"}, 2025), (None, None))
        self.assertEqual(c.start_stop({**s, 2026: "unclear"}, 2025), ("started", "next_year_unclear"))
        self.assertEqual(c.start_stop({k: v for k, v in s.items() if k < 2026}, 2025), ("started", "first_season_only"))

    def test_stopped_confirmed_only_with_next_year_idle(self):
        s = {2022: "cultivated", 2023: "cultivated", 2024: "cultivated", 2025: "idle"}
        self.assertEqual(c.start_stop({**s, 2026: "idle"}, 2025), ("stopped", "confirmed"))
        self.assertEqual(c.start_stop({**s, 2026: "cultivated"}, 2025), ("stopped", "resumed_next_year"))
        self.assertEqual(c.start_stop({**s, 2026: "unclear"}, 2025), ("stopped", "provisional"))
        self.assertEqual(c.start_stop(s, 2025), ("stopped", "provisional"))
        self.assertEqual(c.start_stop({**s, 2023: "idle"}, 2025), (None, None))

    def test_tiers(self):
        t = lambda *a, **k: c.tier_start_stop(*a, cfg=CFG, **k)
        self.assertEqual(t("started", "ANN", 30, False, 10, False, green_span=80), ("started", "strong", None))
        self.assertEqual(t("started", "NCU", 30, False, 10, False, green_span=80)[1], "strong")
        self.assertEqual(t("started", "NCU", 30, False, 10, False, green_span=32), ("started", "review", "green < 60 days"))
        self.assertEqual(t("started", "NAT", 30, False, 10, False, green_span=80)[1], "review")
        self.assertEqual(t("started", "ANN", 12, False, 10, False, green_span=80), ("started", "review", "strip < 20 m"))
        self.assertEqual(t("started", "ANN", 30, False, 4, False, green_span=80)[1], "review")
        self.assertEqual(t("stopped", "ANN", 30, True, 0, False)[0], "alfalfa_not_cut")
        self.assertEqual(t("stopped", "PER", 30, False, 0, False)[0], None)
        self.assertEqual(t("stopped", "ANN", 30, False, 0, True)[0], None)       # household / road


class TwoCrops(unittest.TestCase):
    def run_tc(self, points, prev=None, cfg=CFG, cut=None):
        days, v, vf, bsi, inner = series(points)
        return c.two_crops(days, v, vf, bsi, inner, YEAR, prev, cfg, cut)

    def test_two_sowings_two_harvests_pass_every_signal_check(self):
        r = self.run_tc(TWO)
        self.assertEqual((r["status"], r["fail"]), ("two_cycles", None))
        self.assertEqual(r["n_cycles"], 2)

    def test_previous_autumn_green_is_a_winter_sowing(self):
        self.assertEqual(self.run_tc(TWO, prev=.5)["status"], "winter_carryover")
        winter = [(0, .45), (40, .6), (110, .85), (150, .15)] + TWO[6:]
        self.assertEqual(self.run_tc(winter)["status"], "winter_carryover")

    def test_second_cycle_open_on_1_nov(self):
        late = TWO[:9] + [(235, .7), (304, .7)]
        self.assertEqual(self.run_tc(late)["status"], "second_harvest_not_seen")
        autumn = TWO[:7] + [(270, .15), (280, .3), (304, .6)]
        self.assertEqual(self.run_tc(autumn)["status"], "autumn_sowing")

    def test_three_cycles_go_to_multi_cycle_review(self):
        three = [(0, .15), (70, .15), (90, .7), (110, .15), (140, .15), (160, .7), (180, .15), (210, .15), (230, .7), (250, .15), (304, .15)]
        self.assertEqual(self.run_tc(three)["status"], "multi_cycle")

    def test_single_crop_is_not_two_crops(self):
        self.assertIsNone(self.run_tc(ONE_SUMMER)["status"])

    def test_strong_checks_fail(self):
        weak2 = [(x, min(y, .55)) if x > 200 else (x, y) for x, y in TWO]
        self.assertEqual(self.run_tc(weak2)["fail"], "peak_2")
        self.assertEqual(self.run_tc(TWO, cut=lambda: 3)["fail"], "cut_events")
        days, v, vf, bsi, inner = series(TWO)
        r = c.two_crops(days, v, vf, bsi, np.full(len(v), np.nan), YEAR, None, CFG)
        self.assertEqual(r["fail"], "inner_pixels")

    def test_tiers(self):
        t = lambda *a: c.tier_two_crops(*a, cfg=CFG)
        self.assertEqual(t("two_cycles", None, "ANN", 30, False, False), ("two_crops", "strong", None))
        self.assertEqual(t("two_cycles", None, "ANN", 12, False, False), ("two_crops", "review", "strip < 20 m"))
        self.assertEqual(t("two_cycles", "gap_days", "NCU", 30, False, False)[1], "review")
        self.assertEqual(t("two_cycles", None, "ANN", 30, True, False)[0], None)          # alfalfa excluded
        self.assertEqual(t("two_cycles", None, "PER", 30, False, False)[0], None)
        self.assertEqual(t("winter_carryover", None, "ANN", 30, False, False)[0], None)   # never counted
        self.assertEqual(t("multi_cycle", None, "ANN", 30, False, False)[0], "multi_cycle_review")


class Land(unittest.TestCase):
    def test_land_codes(self):
        self.assertEqual(c.land_code("FIELD_CROP", "ANN", ""), "ANN")
        self.assertEqual(c.land_code("FIELD_CROP", "DEFER", "alfalfa"), "ALF")
        self.assertEqual(c.land_code("NO_CROP", "NAT", ""), "NAT")
        self.assertEqual(c.land_code("NO_CROP", "DEFER", ""), "NCU")
        self.assertEqual(c.land_code(None, "NCI", ""), "NCI")
        self.assertEqual(c.land_code("CROPLAND_UNCERTAIN", "DEFER", ""), "CUN")
        self.assertEqual(c.land_code("VINEYARD", "VIN", ""), "PER")

    def test_alfalfa_flag(self):
        self.assertTrue(c.alfalfa_flag("ANN", "", '{"2024": 2}'))
        self.assertFalse(c.alfalfa_flag("ANN", "", '{"2024": 1}'))


class Agreement(unittest.TestCase):
    def test_strict_and_excl_partial_shares(self):
        st = pd.Series({"a": "cultivated", "b": "idle", "c": "idle", "d": "idle", "e": "cultivated"})
        ref = pd.Series({"a": "active", "b": "idle", "c": "partial_not_passing", "d": "unusable", "e": "idle"})
        m = c.agreement(st, ref)
        self.assertEqual(m["cultivated_in_approved_active"], .5)
        self.assertEqual(m["idle_in_approved_idle"], .5)                # unusable left out, partial counted
        self.assertEqual(m["idle_in_approved_idle_excl_partial"], 1.0)


class ConfigFrozen(unittest.TestCase):
    def test_build_refuses_unfrozen_config(self):
        cfg = copy.deepcopy(CFG)
        cfg["frozen"] = False
        import json, tempfile, os
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "cfg.json")
            with open(p, "w", encoding="utf-8") as f:
                json.dump(cfg, f)
            with self.assertRaises(SystemExit):
                c.build(os.path.join(d, "out"), p)


if __name__ == "__main__":
    unittest.main()
