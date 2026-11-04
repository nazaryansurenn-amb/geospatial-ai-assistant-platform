import re
import unittest


class V12Rules(unittest.TestCase):
    def setUp(self):
        import alphaearth_landtypes_v12 as v
        self.v = v

    def test_signal_owned_same_curve(self):
        nb = [.2, .2, .25, .5, .7, .75, .7, .6, .5, .4, .3, .2]
        self.assertTrue(self.v.signal_owned([x + .01 for x in nb], nb))

    def test_signal_owned_different_curve(self):
        nb = [.2, .2, .25, .5, .7, .75, .7, .6, .5, .4, .3, .2]
        own = [.2, .2, .2, .25, .3, .35, .5, .7, .8, .6, .3, .2]
        self.assertFalse(self.v.signal_owned(own, nb))

    def test_signal_owned_needs_data(self):
        self.assertFalse(self.v.signal_owned([.5] * 5, [.5] * 5))
        self.assertFalse(self.v.signal_owned(None, [.5] * 12))

    def test_priority_order(self):
        self.assertEqual(self.v.first_group({"P": True, "C": True}), "C")
        self.assertEqual(self.v.first_group({"K": True, "A2": True}), "A2")
        self.assertIsNone(self.v.first_group({}))

    def test_dilemma_patterns(self):
        self.assertTrue(re.search(self.v.ALF_ANN, "Alfalfa cuts 2021-2024 but ALF vs ANN unclear", re.I))
        self.assertTrue(re.search(self.v.TREES, "Dense canopy, orchard versus non-fruit trees unclear", re.I))
        self.assertTrue(re.search(self.v.VIN_ORC, "Vine rows with a cluster of trees", re.I))
        self.assertTrue(re.search(self.v.CROP_FALLOW, "Weak crop or fallow with weeds", re.I))
        self.assertFalse(re.search(self.v.ALF_ANN, "Dense trees with faint rows", re.I))


class BroaderClassOnCheckPage(unittest.TestCase):
    def test_perennial_accepts_vineyard_and_orchard(self):
        from alphaearth_photo_review import owner_first_answer, hide_answers
        item = {"vid": "c001", "shown_type": "PERENNIAL", "accept": ["VINEYARD", "ORCHARD"]}
        choices = ["FIELD_CROP", "VINEYARD", "ORCHARD", "NO_CROP"]
        self.assertEqual(owner_first_answer(item, {"owner_type": "ORCHARD"}, None, choices)["verdict"], "agree")
        self.assertEqual(owner_first_answer(item, {"owner_type": "FIELD_CROP"}, None, choices)["verdict"], "disagree")
        self.assertNotIn("accept", hide_answers({"items": [item]})["items"][0])


class OwnerCorrections(unittest.TestCase):
    def setUp(self):
        from alphaearth_landtypes_v12_check import verdict, decide
        self.verdict, self.decide = verdict, decide

    def test_three_modes(self):
        a = {"verdict": "disagree", "note": "i was wrong", "shown": "VINEYARD", "owner_type": "FIELD_CROP"}
        self.assertEqual(self.verdict(a, "first_pick"), "disagree")
        self.assertEqual(self.verdict(a, "corrected"), "agree")
        unsure = {**a, "note": "maybe orchard"}
        self.assertEqual(self.verdict(unsure, "corrected"), "cannot_tell")
        nf = {"verdict": "disagree", "note": "", "shown": "NO_CROP", "owner_type": "NON_FARM"}
        self.assertEqual(self.verdict(nf, "corrected"), "disagree")
        self.assertEqual(self.verdict(nf, "merged"), "agree")
        broad = {"verdict": "disagree", "note": "", "shown": "CROPLAND_UNCERTAIN", "owner_type": "NON_FARM"}
        self.assertEqual(self.verdict(broad, "merged"), "agree")       # NON_FARM merges with NO_CROP, a sub-type

    def test_bars(self):
        self.assertEqual(self.decide(17, 20, 20), "adopt")
        self.assertEqual(self.decide(16, 20, 20), "more")
        self.assertEqual(self.decide(13, 13, 15), "adopt")
        self.assertEqual(self.decide(12, 15, 15), "more")
        self.assertEqual(self.decide(11, 15, 15), "reject")


if __name__ == "__main__":
    unittest.main()
