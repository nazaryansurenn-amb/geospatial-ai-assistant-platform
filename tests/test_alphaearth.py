import json
import re
from pathlib import Path
import unittest
import numpy as np
from wp_core.alphaearth import (adjusted_rand, allocate_sample, area_accuracy, assign, choose_k, dequantise, parcel_groups, quantise,
                                parcel_means, restart_stability, silhouette_cosine, weighted_parcel_mean, spherical_kmeans_numpy, tile_requests,
                                training_pixels)

CFG = json.loads((Path(__file__).resolve().parents[1] / 'config/alphaearth_groups_20261030_v1.json').read_text())


def planted(n=300, k=4, seed=1):
    rng = np.random.default_rng(seed)
    centres = np.linalg.qr(rng.normal(size=(64, 64)))[0][:k]
    truth = np.repeat(np.arange(k), n)
    x = centres[truth] + rng.normal(scale=.05, size=(len(truth), 64))
    return (x / np.linalg.norm(x, axis=1, keepdims=True)).astype(np.float32), truth


class Quantisation(unittest.TestCase):
    def test_round_trip_keeps_unit_length_and_marks_nodata(self):
        x, _ = planted(20)
        q = quantise(x)
        q[3] = -128
        unit, valid = dequantise(q)
        self.assertFalse(valid[3]); self.assertTrue(valid[np.arange(len(q)) != 3].all())
        np.testing.assert_allclose(np.linalg.norm(unit[valid], axis=1), 1, atol=1e-5)
        np.testing.assert_array_equal(unit[3], 0)
        raw = np.linalg.norm(q[valid].astype(np.float32) / 127, axis=1)
        self.assertLess(np.abs(raw - 1).max(), .03)


class EarthEngineRequests(unittest.TestCase):
    def test_tiles_cover_grid_exactly_once(self):
        g = CFG['grid']
        cover = np.zeros((g['height'], g['width']), dtype=np.int8)
        for r in tile_requests(g, CFG['download']['tile_px']):
            cover[r['row']:r['row'] + r['height'], r['col']:r['col'] + r['width']] += 1
            t = r['grid']['affineTransform']
            self.assertEqual(t['translateX'], g['origin_x'] + r['col'] * 10)
            self.assertEqual(t['translateY'], g['origin_y'] - r['row'] * 10)
        self.assertTrue((cover == 1).all())

    def test_requests_carry_grid_numbers_only_never_cadastre(self):
        for r in tile_requests(CFG['grid'], CFG['download']['tile_px']):
            text = json.dumps(r['grid'])
            self.assertIsNone(re.search(r'\d{2}-\d{3}-\d{4}-\d{4}', text))
            self.assertEqual(set(r['grid']), {'dimensions', 'affineTransform', 'crsCode'})
            values = list(r['grid']['dimensions'].values()) + list(r['grid']['affineTransform'].values())
            self.assertTrue(all(isinstance(v, (int, float)) for v in values))
            self.assertEqual(r['grid']['crsCode'], 'EPSG:32638')


class TrainingPixels(unittest.TestCase):
    def test_only_whole_pixels_and_cap_per_parcel(self):
        parcel = np.array([0] * 10 + [1] * 3 + [2] * 2)
        pixel = np.arange(15)
        area = np.array([100.] * 10 + [100., 40., 100.] + [30., 20.])
        px, owner = training_pixels(parcel, pixel, area, 99.9, 4, 7)
        self.assertEqual((owner == 0).sum(), 4); self.assertEqual(sorted(px[owner == 1]), [10, 12])
        self.assertNotIn(2, owner)
        again, _ = training_pixels(parcel, pixel, area, 99.9, 4, 7)
        np.testing.assert_array_equal(px, again)


class Clustering(unittest.TestCase):
    def test_best_restart_recovers_planted_groups(self):
        x, truth = planted()
        result = spherical_kmeans_numpy(x, 4, iterations=30, restarts=5, seed=3)
        self.assertGreater(adjusted_rand(result['labels'], truth), .99)
        self.assertEqual(len(result['restart_labels']), 5)

    def test_restart_stability_compares_every_pair(self):
        a = np.array([0, 0, 1, 1, 2, 2])
        same = restart_stability([a, (a + 1) % 3, a])
        self.assertEqual(same['pairs'], 3); self.assertAlmostEqual(same['min'], 1)
        split = restart_stability([a, a, np.array([0, 1, 2, 2, 2, 2])])
        self.assertAlmostEqual(split['median'], adjusted_rand(a, np.array([0, 1, 2, 2, 2, 2])))
        self.assertLess(split['min'], 1)

    def test_adjusted_rand_ignores_label_names(self):
        a = np.array([0, 0, 1, 1, 2, 2])
        self.assertAlmostEqual(adjusted_rand(a, a), 1); self.assertAlmostEqual(adjusted_rand(a, (a + 1) % 3), 1)
        rng = np.random.default_rng(0)
        self.assertLess(abs(adjusted_rand(rng.integers(0, 5, 5000), rng.integers(0, 5, 5000))), .02)

    def test_silhouette_matches_brute_force(self):
        x, truth = planted(15, 3)
        d = 1 - x @ x.T
        expected = []
        for i in range(len(x)):
            own = truth == truth[i]
            a = d[i, own].sum() / (own.sum() - 1)
            b = min(d[i, truth == c].mean() for c in set(truth) if c != truth[i])
            expected.append((b - a) / max(a, b))
        self.assertAlmostEqual(silhouette_cosine(x, truth, 3, chunk=7), float(np.mean(expected)), places=5)
        self.assertGreater(silhouette_cosine(x, truth, 3), .5)

    def test_choose_k_requires_stability(self):
        results = {6: {'silhouette': .30, 'restart_ari_median': .6}, 8: {'silhouette': .25, 'restart_ari_median': .9},
                   10: {'silhouette': .20, 'restart_ari_median': .95}}
        self.assertEqual(choose_k(results, .8), 8)
        self.assertIsNone(choose_k({6: {'silhouette': .3, 'restart_ari_median': .5}}, .8))

    def test_chunked_assignment_equals_full(self):
        x, _ = planted(50)
        c = spherical_kmeans_numpy(x, 4, 10, 2, 1)['centroids']
        labels, fit = assign(x, c, chunk=33)
        np.testing.assert_array_equal(labels, (x @ c.T).argmax(1)); np.testing.assert_allclose(fit, (x @ c.T).max(1), rtol=1e-6)


class AccuracyEstimates(unittest.TestCase):
    def olofsson_example(self):
        counts = np.array([[66, 0, 5, 4], [0, 55, 8, 12], [1, 0, 153, 11], [2, 1, 9, 313]])
        mapped = np.array([200000, 150000, 3200000, 6450000])
        map_class = np.repeat(np.arange(4), counts.sum(1))
        reference = np.concatenate([np.repeat(np.arange(4), row) for row in counts])
        return area_accuracy(map_class, reference, np.ones(len(map_class)), mapped, mapped, [0, 1, 2, 3]), mapped

    def test_equal_units_reproduce_olofsson_2014_example(self):
        r, mapped = self.olofsson_example()
        self.assertAlmostEqual(r['overall'], .947, places=3)
        np.testing.assert_allclose(r['users'], [.88, .73, .93, .96], atol=.005)
        np.testing.assert_allclose(r['producers'], [.75, .85, .93, .96], atol=.01)
        hectares = r['proportion'] * mapped.sum() * .09
        self.assertAlmostEqual(hectares[0] / 21158, 1, places=2); self.assertAlmostEqual(hectares[1] / 11686, 1, places=2)
        counts = np.array([[66, 0, 5, 4], [0, 55, 8, 12], [1, 0, 153, 11], [2, 1, 9, 313]])
        w, n = mapped / mapped.sum(), counts.sum(1)
        p = counts / n[:, None]
        closed_form = np.sqrt((w[:, None] ** 2 * p * (1 - p) / (n[:, None] - 1)).sum(0))  # Olofsson eq. 10, no fpc
        np.testing.assert_allclose(r['proportion_se'], closed_form, rtol=1e-3)

    def test_perfect_map_and_unequal_areas(self):
        m = np.array([0, 0, 0, 1, 1, 1]); a = np.array([1., 2., 3., 4., 5., 6.])
        r = area_accuracy(m, m, a, [10, 30], [50, 50], [0, 1])
        self.assertEqual(r['overall'], 1); np.testing.assert_allclose(r['proportion'], [.25, .75])
        np.testing.assert_allclose(r['proportion_se'], 0)
        wrong = area_accuracy(m, np.array([0, 0, 1, 1, 1, 1]), a, [10, 30], [50, 50], [0, 1])
        self.assertAlmostEqual(wrong['users'][0], .5)

    def test_allocation_keeps_floor_and_total(self):
        counts = allocate_sample([3038, 1691, 1566, 1447, 1216, 857, 524], 300, 30)
        self.assertEqual(counts.sum(), 300); self.assertTrue((counts >= 30).all())
        self.assertEqual(int(np.argmax(counts)), 0)


class PanelAnswers(unittest.TestCase):
    def test_pure_mixed_and_unsure_answers(self):
        from alphaearth_label_panel import answer_kind, valid_answer
        self.assertTrue(valid_answer('vineyard', [])); self.assertEqual(answer_kind('vineyard', []), 'pure')
        self.assertTrue(valid_answer('orchard', ['annual', 'bare'])); self.assertEqual(answer_kind('orchard', ['annual']), 'mixed')
        self.assertTrue(valid_answer('cannot_tell', [])); self.assertEqual(answer_kind('cannot_tell', []), 'cannot_tell')
        for main, also in [('vineyard', ['vineyard']), ('orchard', ['annual', 'annual']), ('banana', []),
                           ('vineyard', ['banana']), ('cannot_tell', ['annual']), ('vineyard', 'annual'), (None, [])]:
            self.assertFalse(valid_answer(main, also), (main, also))


class ParcelMeans(unittest.TestCase):
    def test_area_weighted_unit_mean_and_invalid_rows(self):
        rows = np.array([[1, 0], [0, 1], [0, 1], [1, 0]], dtype=np.float32)
        valid = np.array([True, True, True, False])
        parcel = np.array([0, 0, 1, 2]); area = np.array([30., 10., 5., 5.])
        m = parcel_means(rows, valid, parcel, area, 3)
        np.testing.assert_allclose(m[0], np.array([3, 1]) / np.sqrt(10), rtol=1e-6)
        np.testing.assert_allclose(m[1], [0, 1]); np.testing.assert_array_equal(m[2], [0, 0])
        v = weighted_parcel_mean(np.array([1., 0., .5, .9]), valid, parcel, area, 3)
        self.assertAlmostEqual(v[0], .75); self.assertAlmostEqual(v[1], .5); self.assertTrue(np.isnan(v[2]))


class ParcelGroups(unittest.TestCase):
    def test_area_shares_dominant_and_invalid_pixels(self):
        group = np.array([0, 1, 1, 2, 0])
        fit = np.array([.9, .8, .7, .6, .5], dtype=np.float32)
        valid = np.array([True, True, True, True, False])
        parcel = np.array([0, 0, 0, 1, 2])
        pixel = np.array([0, 1, 2, 3, 4])
        area = np.array([20., 50., 30., 100., 100.])
        r = parcel_groups(group, fit, valid, parcel, pixel, area, 3, 3)
        np.testing.assert_allclose(r['shares'][0], [.2, .8, 0]); np.testing.assert_allclose(r['shares'][:2].sum(1), 1)
        np.testing.assert_array_equal(r['dominant'], [1, 2, -1])
        self.assertAlmostEqual(r['mean_fit'][0], (.9 * 20 + .8 * 50 + .7 * 30) / 100, places=6)
        self.assertEqual(r['valid_fraction'][2], 0); self.assertTrue(np.isnan(r['mean_fit'][2]))
