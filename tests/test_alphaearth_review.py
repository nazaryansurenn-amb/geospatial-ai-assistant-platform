import json
import re
from pathlib import Path
import unittest
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
REVIEW = ROOT / 'server_data/review/alphaearth_review_20261030_v1'
PARCELS = ROOT / 'data/analysis/alphaearth/alphaearth_groups_20261030_v1/parcels_2025/parcels.parquet'
CROP_WORDS = ['wheat', 'cereal', 'maize', 'corn', 'sorghum', 'vegetable', 'orchard', 'vineyard', 'alfalfa', 'grape',
              'ցորեն', 'հացահատիկ', 'եգիպտացորեն', 'սորգո', 'բանջար', 'այգի', 'խաղող', 'առվույտ']


@unittest.skipUnless((REVIEW / 'alphaearth.json').exists(), 'AlphaEarth review data not prepared')
class AlphaEarthReviewData(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.payload = json.loads((REVIEW / 'alphaearth.json').read_text(encoding='utf-8'))
        cls.lookup = json.loads((REVIEW / 'alphaearth_lookup.json').read_text())
        cls.parcels = pd.read_parquet(PARCELS)

    def test_one_class_per_parcel_by_majority_rule(self):
        self.assertEqual(len(self.lookup), 22802)
        expected = self.parcels.apply(lambda r: f'g{r.group}' if r.group_share >= .5 else 'mixed', axis=1)
        self.assertEqual(dict(zip(self.parcels.cadastre_code, expected)), self.lookup)

    def test_scope_counts_add_up_and_ids_are_unique(self):
        for scope, summary in self.payload['scopes'].items():
            classes = summary['classes'].values()
            self.assertEqual(sum(c['count'] for c in classes), summary['included']['count'], scope)
            ids = [i for c in classes for i in c['ids']]
            self.assertEqual(len(ids), len(set(ids)), scope)
            self.assertTrue(all(c['count'] == len(c['ids']) for c in classes))
            self.assertAlmostEqual(sum(c['area_ha'] for c in classes), summary['included']['area_ha'], places=3)
        whole = self.payload['scopes']['lower_hrazdan']['classes']
        for c in whole:
            parts = sum(self.payload['scopes'][s]['classes'][c]['count'] for s in ('stage_1', 'stage_2'))
            self.assertEqual(parts, whole[c]['count'])

    def test_public_names_are_neutral_patterns(self):
        for c in self.payload['classes']:
            text = (c['label'] + ' ' + c['label_en']).lower()
            self.assertFalse([w for w in CROP_WORDS if w in text], c['id'])
        self.assertIn('Google DeepMind', self.payload['credit'])

    def test_public_payload_has_no_private_fields(self):
        text = json.dumps(self.payload, ensure_ascii=False)
        for marker in ['cadastre_code', 'internal_parcel_id', 'group_share', 'mean_fit', 'server_data', 'data/analysis']:
            self.assertNotIn(marker, text)
        self.assertIsNone(re.search(r'\d{2}-\d{3}-\d{4}-\d{4}', text))
        self.assertEqual(set(self.lookup.values()) - {c['id'] for c in self.payload['classes']}, set())
