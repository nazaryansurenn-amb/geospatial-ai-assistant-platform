import unittest

import numpy as np
import pandas as pd


class StartStop(unittest.TestCase):
    def setUp(self):
        import cultivation_change_v1 as c
        self.c = c

    def test_effective_states_gate(self):
        s = self.c.effective_states([1, 2, 2, 3, 0], {2022: True, 2023: False})
        self.assertEqual(s, {2021: "active", 2022: "active", 2023: "unclear", 2024: "idle", 2025: "unusable"})

    def test_started_and_strong(self):
        st = lambda codes: self.c.start_stop(self.c.effective_states(codes, {}), 2025)
        self.assertEqual(st([1, 1, 3, 3, 1]), "started")
        self.assertEqual(st([1, 3, 3, 3, 1]), "started_strong")
        self.assertIsNone(st([1, 1, 1, 3, 1]))          # one idle year is not a start
        self.assertIsNone(st([1, 3, 0, 3, 1]))          # an unusable year breaks the chain

    def test_stopped_needs_three_active(self):
        st = lambda codes: self.c.start_stop(self.c.effective_states(codes, {}), 2025)
        self.assertEqual(st([3, 1, 1, 1, 3]), "stopped_provisional")
        self.assertIsNone(st([3, 3, 1, 1, 3]))
        self.assertIsNone(st([1, 1, 2, 1, 3]))          # a partial year without the area gate is unclear


class TwoCrops(unittest.TestCase):
    def setUp(self):
        import cultivation_change_v1 as c
        self.c = c
        self.dates = pd.date_range("2025-01-01", "2025-12-31", freq="10D").to_numpy()

    def series(self, peaks):
        v = np.full(len(self.dates), .15)
        for centre, h in peaks:
            v = np.maximum(v, h * np.exp(-((np.arange(len(v)) - centre) / 3.0) ** 2))
        return v

    def events(self, rows):
        return pd.DataFrame([{"start_date": s, "peak_date": p, "end_date": e, "left_censored": lc} for s, p, e, lc in rows])

    def test_two_cycles_with_bare_gap(self):
        v = self.series([(13, .75), (24, .7)])          # peaks early May and mid August
        ev = self.events([("2025-03-20", "2025-05-11", "2025-06-30", False), ("2025-07-10", "2025-08-29", "2025-10-20", False)])
        cyc = self.c.crop_cycles(ev, (self.dates, v))
        self.assertEqual(len(cyc), 2)
        self.assertEqual(self.c.two_crops(cyc, (self.dates, v)), "two_crops")

    def test_winter_crop_from_last_autumn_not_counted(self):
        v = self.series([(11, .8), (24, .7)])
        ev = self.events([("2025-01-01", "2025-04-21", "2025-06-30", True), ("2025-07-10", "2025-08-29", "2025-10-20", False)])
        self.assertEqual(len(self.c.crop_cycles(ev, (self.dates, v))), 1)

    def test_low_regrowth_not_a_crop(self):
        v = self.series([(13, .75), (24, .4)])
        ev = self.events([("2025-03-20", "2025-05-11", "2025-06-30", False), ("2025-07-10", "2025-08-29", "2025-10-20", False)])
        self.assertIsNone(self.c.two_crops(self.c.crop_cycles(ev, (self.dates, v)), (self.dates, v)))


if __name__ == "__main__":
    unittest.main()
