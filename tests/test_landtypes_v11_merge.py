import unittest


class PairedAdoption(unittest.TestCase):
    def setUp(self):
        from alphaearth_landtypes_v11_merge import paired
        self.paired = paired

    def test_only_differing_pairs_count(self):
        same = [('FIELD_CROP', 'FIELD_CROP', 'FIELD_CROP')] * 20
        self.assertEqual(self.paired(same), (0, 0, False))

    def test_five_wins_one_loss_adopts(self):
        pairs = [('ORCHARD', 'FIELD_CROP', 'ORCHARD')] * 5 + [('ORCHARD', 'FIELD_CROP', 'FIELD_CROP')]
        self.assertEqual(self.paired(pairs), (5, 1, True))

    def test_two_losses_block(self):
        pairs = [('ORCHARD', 'UNCLASSIFIED', 'ORCHARD')] * 8 + [('NO_CROP', 'UNCLASSIFIED', 'FIELD_CROP')] * 2
        self.assertEqual(self.paired(pairs), (8, 2, False))

    def test_four_wins_not_enough(self):
        self.assertFalse(self.paired([('VINEYARD', 'ORCHARD', 'VINEYARD')] * 4)[2])


class ThinStrip(unittest.TestCase):
    def setUp(self):
        from alphaearth_landtypes_v11_merge import thin_strip
        self.thin = thin_strip

    def test_weak_spring_on_thin_strip_unclassified(self):
        self.assertEqual(self.thin(9, 'rule weak-spring', None, 'NO_CROP'),
                         ('UNCLASSIFIED', 'thin strip: weak-spring decision dropped'))

    def test_crop_history_conversion_dropped(self):
        self.assertEqual(self.thin(12, 'v1', 'NAT', 'FIELD_CROP'),
                         ('NO_CROP', 'thin strip: crop-history conversion dropped'))

    def test_wide_parcels_untouched(self):
        self.assertEqual(self.thin(15, 'rule weak-spring', 'NAT', 'FIELD_CROP'), (None, None))

    def test_photo_answers_on_thin_strips_untouched(self):
        self.assertEqual(self.thin(6, 'v1', None, 'VINEYARD'), (None, None))
        self.assertEqual(self.thin(6, 'rule weak-spring', None, 'UNCLASSIFIED'), (None, None))

    def test_missing_width_untouched(self):
        self.assertEqual(self.thin(None, 'rule weak-spring', None, 'NO_CROP'), (None, None))


class OwnerCheckPage(unittest.TestCase):
    def test_allocation_floor_and_total(self):
        from alphaearth_landtypes_v11_verify import allocate
        sizes = {'FIELD_CROP': 8600, 'NO_CROP': 4400, 'VINEYARD': 530, 'GREENHOUSE': 9, 'road shape': 210}
        per = allocate(sizes, 150)
        self.assertEqual(per['GREENHOUSE'], 9)
        self.assertTrue(all(per[s] >= 15 for s in ('VINEYARD', 'road shape')))
        self.assertLessEqual(sum(per.values()), 150)
        self.assertGreater(per['FIELD_CROP'], per['NO_CROP'])

    def test_strata(self):
        from alphaearth_landtypes_v11_verify import stratum
        self.assertEqual(stratum('x; structure seen 2021-22, current use not confirmed', 'v1', 'VINEYARD'), 'old-photo perennial')
        self.assertEqual(stratum(None, 'rule road (curved long)', 'NON_FARM'), 'road shape')
        self.assertEqual(stratum(None, 'v1', 'ORCHARD'), 'ORCHARD')

    def test_score_is_weighted_and_keeps_comments(self):
        from alphaearth_landtypes_v11_verify import summarise
        key = {'strata_sizes': {'A': 900, 'B': 100}, 'items': {
            'v1': {'stratum': 'A', 'shown': 'FIELD_CROP'}, 'v2': {'stratum': 'A', 'shown': 'FIELD_CROP'},
            'v3': {'stratum': 'B', 'shown': 'ORCHARD'}, 'v4': {'stratum': 'B', 'shown': 'ORCHARD'}}}
        ans = {'v1': {'verdict': 'agree', 'correct': None, 'note': ''},
               'v2': {'verdict': 'cannot_tell', 'correct': None, 'note': 'hard case'},
               'v3': {'verdict': 'agree', 'correct': None, 'note': ''},
               'v4': {'verdict': 'disagree', 'correct': 'VINEYARD', 'note': ''}}
        out = summarise(key, ans)
        self.assertEqual(out['by_stratum']['A']['accuracy'], 1.0)
        self.assertEqual(out['by_stratum']['A']['cannot_tell'], 1)
        self.assertEqual(out['by_stratum']['B']['owner_says'], {'VINEYARD': 1})
        self.assertEqual(out['weighted_accuracy'], 0.95)
        self.assertEqual([c['note'] for c in out['comments']], ['hard case'])
        lo, hi = out['weighted_accuracy_95']
        self.assertTrue(lo < .95 <= hi)


class OwnerFirst(unittest.TestCase):
    def setUp(self):
        from alphaearth_photo_review import owner_first_answer, hide_answers
        self.answer, self.hide = owner_first_answer, hide_answers
        self.item = {'vid': 'v001', 'shown_type': 'ORCHARD', 'shown_note': 'note'}
        self.choices = ['FIELD_CROP', 'VINEYARD', 'ORCHARD']

    def test_verdict_from_owner_pick(self):
        self.assertEqual(self.answer(self.item, {'owner_type': 'ORCHARD'}, None, self.choices)['verdict'], 'agree')
        r = self.answer(self.item, {'owner_type': 'VINEYARD'}, None, self.choices)
        self.assertEqual((r['verdict'], r['correct'], r['shown']), ('disagree', 'VINEYARD', 'ORCHARD'))
        self.assertEqual(self.answer(self.item, {'owner_type': 'cannot_tell'}, None, self.choices)['verdict'], 'cannot_tell')

    def test_first_pick_is_locked_comment_updates(self):
        first = self.answer(self.item, {'owner_type': 'VINEYARD'}, None, self.choices)
        later = self.answer(self.item, {'owner_type': 'ORCHARD', 'note': 'seen on Google'}, first, self.choices)
        self.assertEqual((later['owner_type'], later['verdict'], later['note']), ('VINEYARD', 'disagree', 'seen on Google'))

    def test_bad_pick_rejected(self):
        with self.assertRaises(ValueError):
            self.answer(self.item, {'owner_type': 'ALF'}, None, self.choices)

    def test_answers_hidden_before_pick(self):
        out = self.hide({'owner_first': True, 'items': [{'vid': 'v1', 'shown_type': 'ORCHARD', 'shown_note': 'x',
                                                         'shown_components': [], 'lat': 1}]})
        self.assertEqual(out['items'], [{'vid': 'v1', 'lat': 1}])


class Wilson(unittest.TestCase):
    def test_range(self):
        from alphaearth_landtypes_v11_merge import wilson
        self.assertEqual(wilson(70, 82), [0.761, 0.914])
        self.assertIsNone(wilson(0, 0))


if __name__ == '__main__':
    unittest.main()
