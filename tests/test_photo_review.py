import json
import re
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]
QUEUES = ROOT / 'server_data/review/alphaearth_photo_review_20261030_v1/queues'
ITEM = {'id': 't001', 'mode': 'shares', 'area_ha': .8, 'hints': {'cycles': {'2021': 1, '2025': 1}, 'ndvi': {'2025': [.2, .7]}}}
SMALL = {**ITEM, 'mode': 'small'}


class AnswerValidation(unittest.TestCase):
    def setUp(self):
        from alphaearth_photo_review import validate_answer
        self.validate = validate_answer

    def test_final_answer_gets_rule_decision(self):
        r = self.validate(ITEM, {'stage': 'final', 'shares': {'BLT': .6, 'ANN': .4}, 'confidence': 'high'})
        self.assertEqual(r['decision']['type'], 'BLT')

    def test_low_confidence_must_defer_and_defer_needs_reason(self):
        with self.assertRaises(ValueError):
            self.validate(ITEM, {'stage': 'final', 'shares': {'ANN': 1}, 'confidence': 'low'})
        with self.assertRaises(ValueError):
            self.validate(ITEM, {'stage': 'final', 'defer': True, 'reason': ' '})
        r = self.validate(ITEM, {'stage': 'photo', 'defer': True, 'reason': 'photo hazy'})
        self.assertEqual(r['decision']['type'], 'DEFER')

    def test_contradictions_are_reported(self):
        r = self.validate(SMALL, {'stage': 'final', 'shares': {'NCU': 1}, 'confidence': 'medium', 'flags': {'active_2025': True}})
        self.assertTrue(r['contradictions'])
        with self.assertRaises(ValueError):
            self.validate(SMALL, {'stage': 'final', 'shares': {'NCU': 1}, 'confidence': 'medium', 'flags': {'irrigated': True}})

    def test_v2_photo_stage_records_structure_only(self):
        r = self.validate(ITEM, {'stage': 'photo', 'shares': {'FLD': .7, 'TRE': .3}, 'confidence': 'medium'})
        self.assertEqual(r['structure'], {'FLD': .7, 'TRE': .3}); self.assertEqual(r['decision']['type'], 'PHOTO')
        for shares in [{'ANN': 1}, {'NCU': .5, 'FLD': .5}, {'FLD': .5}]:
            with self.assertRaises(ValueError):
                self.validate(ITEM, {'stage': 'photo', 'shares': shares, 'confidence': 'medium'})
        with self.assertRaises(ValueError):
            self.validate(ITEM, {'stage': 'final', 'shares': {'FLD': 1}, 'confidence': 'medium'})

    def test_v2_weak_spring_peak_becomes_uncertain_annual(self):
        weak = {**ITEM, 'hints': {'cycles': {'2025': 0}, 'ndvi': {'2025': [.15, .2, .3, .45, .35, .2, .15, .14, .15, .16, .15, .14]}}}
        r = self.validate(weak, {'stage': 'final', 'shares': {'NCU': 1}, 'confidence': 'medium'})
        self.assertEqual((r['decision']['type'], r['decision']['attribute']), ('ANN', 'uncertain_2025_activity'))
        r = self.validate(weak, {'stage': 'final', 'shares': {'VIN': 1}, 'confidence': 'medium'})
        self.assertEqual(r['decision']['type'], 'VIN')  # the rule only touches ANN/NCU answers

    def test_bad_stage_and_shares_rejected(self):
        for body in [{'stage': 'x', 'shares': {'ANN': 1}, 'confidence': 'high'},
                     {'stage': 'final', 'shares': {'ANN': .5}, 'confidence': 'high'},
                     {'stage': 'final', 'confidence': 'high'}]:
            with self.assertRaises(ValueError):
                self.validate(ITEM, body)


class VerifySet(unittest.TestCase):
    def test_sample_and_disclosed_check_answers(self):
        import tempfile
        from unittest.mock import patch
        import alphaearth_photo_review as tool
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            (out / 'queues').mkdir(); (out / 'answers').mkdir()
            items = [{'id': f'a{n:03d}', 'lat': 40.1, 'lon': 44.3, 'outline': [[44.3, 40.1]], 'area_ha': .3, 'community': 'X',
                      'mode': 'small', 'photo': {'date': '2025-09-07'}} for n in range(1, 61)]
            (out / 'queues/q.json').write_text(json.dumps({'items': items}))
            answers = {i['id']: {'photo': {}, 'final': {'defer': False, 'decision': {'type': 'ANN', 'components': ['ANN']}}} for i in items[:50]}
            answers['a050'] = {'final': {'defer': True, 'decision': {'type': 'DEFER'}}}
            (out / 'answers/q__claude-r1.json').write_text(json.dumps(answers))
            with patch.object(tool, 'OUT', out):
                tool.make_verify('q', size=20, trap_share=.1, seed=1)
                shown = json.loads((out / 'verify/q.json').read_text())['items']
                key = json.loads((out / 'verify/q.key.json').read_text())
                with self.assertRaises(SystemExit):
                    tool.make_verify('q', size=20)
        traps = [v for v, k in key.items() if k['trap']]
        self.assertEqual((len(shown), len(traps)), (22, 2))
        self.assertNotIn('a050', {k['item'] for k in key.values()})
        for item in shown:
            self.assertNotIn('reviewer', item)
            self.assertEqual(item['shown_type'] != 'ANN', item['vid'] in traps)


@unittest.skipUnless(QUEUES.exists(), 'review queues not prepared')
class QueuePrivacy(unittest.TestCase):
    def test_queues_hide_codes_groups_and_classes(self):
        pilot_hints = {'activity_2026', 'annual_states', 'bsi_2025', 'cycles', 'history_2021_2025', 'ndmi_2025', 'ndvi', 'stability'}
        v2_hints = {'bsi_2025', 'cycles', 'ndmi_2025', 'ndvi'}  # independent raw Sentinel-2 only (owner decision)
        v23_hints = v2_hints | {'cuts', 'green_days'}  # v2_3: same frequent series, cut events and green days
        for path in QUEUES.glob('*.json'):
            if path.name.endswith('.key.json'):
                continue
            data = json.loads(path.read_text(encoding='utf-8'))
            text = json.dumps(data, ensure_ascii=False)
            self.assertIsNone(re.search(r'\d{2}-\d{3}-\d{4}-\d{4}', text), path.name)
            v2 = re.search(r'_v2(_\d+)?$', data.get('rules', '')) is not None
            base = {'area_ha', 'community', 'hints', 'id', 'lat', 'lon', 'mode', 'outline', 'photo'}
            if v2:
                base |= {'mean_width_m', 'parts', 'metric'}
            for item in data['items']:
                # Owner queues add the reviewers' deferral notes; reviewer queues never carry notes.
                self.assertEqual(set(item), base | ({'notes'} if path.name.startswith('owner_') else set()))
                minor = re.search(r'_v2_(\d+)$', data.get('rules', ''))
                allowed = (v23_hints if minor and int(minor.group(1)) >= 3 else v2_hints) if v2 else pilot_hints
                self.assertLessEqual(set(item['hints']), allowed)
            for fixed in data.get('fixed_status', []):
                # the rule uses the exact width; the stored width is rounded to 0.1 m (4.96 shows as 5.0)
                self.assertTrue(fixed['area_ha'] < .01 or fixed['mean_width_m'] < 5.05)

    def test_gold_queues_cover_both_owner_samples_without_overlap(self):
        panel = json.loads((QUEUES / 'gold_panel.key.json').read_text())
        accuracy = json.loads((QUEUES / 'gold_accuracy.key.json').read_text())
        self.assertEqual((len(panel), len(accuracy)), (200, 300))
        self.assertFalse({p['internal_parcel_id'] for p in panel} & {p['internal_parcel_id'] for p in accuracy})


class CrashSafety(unittest.TestCase):
    def test_no_second_reviewer_and_live_claim_required(self):
        from alphaearth_photo_review import batch_conflicts
        claims = {'B1': {'reviewer': 'claude-r1', 'until': 100}, 'B2': {'reviewer': 'claude-r2', 'until': 50}}
        unit_of = {'t1': 'B1', 't2': 'B1', 't3': 'B2'}
        done = {'t2': 'claude-r9'}
        self.assertEqual(batch_conflicts(['t1'], 'claude-r1', done, claims, unit_of, 60), {})
        out = batch_conflicts(['t1', 't2', 't3'], 'claude-r1', done, claims, unit_of, 60)
        self.assertEqual(set(out), {'t2', 't3'})
        self.assertIn('already answered', out['t2'])
        self.assertIn('claim', out['t3'])
        # an expired claim of the reviewer itself also blocks the save (the unit may be with someone else now)
        self.assertIn('t1', batch_conflicts(['t1'], 'claude-r1', {}, claims, unit_of, 200))
        # correcting one's own answer stays possible
        self.assertEqual(batch_conflicts(['t2'], 'claude-r9', done, {'B1': {'reviewer': 'claude-r9', 'until': 100}}, unit_of, 60), {})

    def test_compact_answers(self):
        from alphaearth_review_cli import parse_compact
        out = parse_compact('t009 ? strip 5m wide; vines or trees\nt010 FLD.6 BLT.4 m track; t011 VIN h -a rows', 'final')
        self.assertEqual(out['t009'], {'defer': True, 'reason': 'strip 5m wide; vines or trees'})
        self.assertEqual(out['t010']['shares'], {'FLD': .6, 'BLT': .4})
        self.assertTrue(out['t010']['flags']['built'])
        self.assertFalse(out['t011']['flags']['active_2025'])
        with self.assertRaises(SystemExit):
            parse_compact('t012 VIN rows', 'photo')

    def test_reconcile_states_add_up(self):
        from alphaearth_review_reconcile import reconcile
        r = reconcile('blocktest_lean')
        self.assertTrue(r['adds_up'])
        self.assertEqual(r['parcels'], 198)


class VerifyCompare(unittest.TestCase):
    def test_strata_truth_and_weighted_scores(self):
        import tempfile
        from unittest.mock import patch
        import alphaearth_photo_review as tool
        import alphaearth_review_verify_score as scorer
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            for d in ('queues', 'answers', 'verify'):
                (out / d).mkdir()
            items = [{'id': f'k{n:03d}', 'lat': 40, 'lon': 44, 'outline': [], 'photo': {}, 'area_ha': .2, 'community': 'x'}
                     for n in range(40)]
            (out / 'queues/base.json').write_text(json.dumps({'items': items}))
            (out / 'queues/sub.json').write_text(json.dumps({'items': items[:10]}))
            fin = lambda t: {'final': {'decision': {'type': t, 'components': [t]}}}
            v1 = {i['id']: fin('ANN') for i in items}
            v2 = {i['id']: fin('ANN' if n % 4 else 'NCU') for n, i in enumerate(items)}
            (out / 'answers/v1__claude-r1.json').write_text(json.dumps(v1))
            (out / 'answers/v2__claude-r1.json').write_text(json.dumps(v2))
            with patch.object(tool, 'OUT', out):
                tool.make_verify_compare('cmp', 'base', ['v1', 'v2'], 'sub', size=12, trap_share=.1)
            key = json.loads((out / 'verify/cmp.key.json').read_text())
            self.assertEqual(sum(key['strata_sizes'].values()), 40)
            self.assertEqual(key['strata_sizes']['disagree'] + key['strata_sizes']['disagree_std'], 10)
            # owner agrees with every shown answer except the checks, which are caught
            owner = {vid: {'verdict': 'disagree' if k['trap'] else 'agree', 'correct': 'ANN'} for vid, k in key['items'].items()}
            (out / 'answers/verify_cmp__owner.json').write_text(json.dumps(owner))
            with patch.object(scorer, 'VERIFY', out / 'verify'), patch.object(scorer, 'ANSWERS', out / 'answers'):
                r = scorer.score('cmp')
            self.assertEqual(r['check_answers']['missed'], 0)
            self.assertEqual(r['versions']['v2']['accuracy_weighted'], 1.0)  # v2 is the shown version
            self.assertLess(r['versions']['v1']['accuracy_weighted'], 1.0)


class VerifyVersion(unittest.TestCase):
    def test_every_typed_answer_plus_checks_scored(self):
        import tempfile
        from unittest.mock import patch
        import alphaearth_photo_review as tool
        import alphaearth_review_verify_score as scorer
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            for d in ('queues', 'answers', 'verify'):
                (out / d).mkdir()
            items = [{'id': f'k{n:03d}', 'lat': 40, 'lon': 44, 'outline': [], 'photo': {}, 'area_ha': .2, 'community': 'x'}
                     for n in range(60)]
            (out / 'queues/base.json').write_text(json.dumps({'items': items}))
            (out / 'queues/son.json').write_text(json.dumps({'items': items[:30]}))
            fin = lambda t: {'final': {'decision': {'type': t, 'components': [t]}}} if t != 'DEFER' else {'final': {'defer': True}}
            son = {f'k{n:03d}': fin('DEFER' if n < 5 else 'NAT' if n < 10 else 'ANN') for n in range(30)}
            opus = {f'k{n:03d}': fin('ANN') for n in range(30)}
            (out / 'answers/son__claude-r1.json').write_text(json.dumps(son))
            (out / 'answers/opus__claude-r1.json').write_text(json.dumps(opus))
            with patch.object(tool, 'OUT', out):
                tool.make_verify_version('sv', 'son', ['opus'], 'base', traps=5)
            key = json.loads((out / 'verify/sv.key.json').read_text())
            self.assertEqual(sum(not k['trap'] for k in key['items'].values()), 25)  # every typed Sonnet answer
            self.assertTrue(all(k['item'] >= 'k030' for k in key['items'].values() if k['trap']))  # checks from other parcels
            # owner: every parcel is really ANN; checks caught
            owner = {vid: {'verdict': 'disagree', 'correct': 'ANN'} if k['trap'] or k['answers']['son'] == 'NAT'
                     else {'verdict': 'agree'} for vid, k in key['items'].items()}
            (out / 'answers/verify_sv__owner.json').write_text(json.dumps(owner))
            with patch.object(scorer, 'VERIFY', out / 'verify'), patch.object(scorer, 'ANSWERS', out / 'answers'):
                r = scorer.score('sv')
            self.assertEqual(r['check_answers'], {'caught': 5, 'missed': 0})
            self.assertEqual(r['versions']['son']['accuracy_weighted'], .8)  # 20 of 25
            self.assertEqual(r['versions']['opus']['accuracy_weighted'], 1.0)


class NaturalGroundRuleVersion(unittest.TestCase):
    def test_server_flags_nat_with_detected_cycle(self):
        from alphaearth_photo_review import TYPES_CONFIG, validate_answer
        self.assertIn('natural_ground_crop_season', TYPES_CONFIG['rules'])
        item = {**SMALL, 'hints': {'cycles': {'2021': 1, '2025': 0}, 'ndvi': {'2021': [.2, .6], '2025': [.15, .2]}}}
        body = {'stage': 'final', 'shares': {'NAT': 1}, 'confidence': 'medium', 'reason': 'bare',
                'flags': {'perennial_structure': False, 'built': False, 'active_2025': False}}
        # v2_4 converts reviewer answers (see NaturalGroundConversionV24); the check remains on unconverted owner answers
        record = validate_answer(item, body, owner=True)
        self.assertTrue(any(c.startswith('natural ground but') for c in record['contradictions']))
        quiet = {**item, 'hints': {'cycles': {'2021': 0, '2025': 0}, 'ndvi': {'2021': [.2, .3], '2025': [.15, .2]}}}
        self.assertFalse(validate_answer(quiet, body)['contradictions'])


class OwnerAnswersKeepTheirType(unittest.TestCase):
    def test_weak_spring_rule_does_not_rewrite_owner(self):
        from alphaearth_photo_review import validate_answer
        spring = [.15, .2, .3, .45, .35, .2, .15, .14, .15, .16, .15, .14]
        item = {**SMALL, 'hints': {'cycles': {'2025': 0}, 'ndvi': {'2025': spring}}}
        body = {'stage': 'final', 'shares': {'NCU': 1}, 'confidence': 'high', 'reason': 'x',
                'flags': {'perennial_structure': False, 'built': False, 'active_2025': False}}
        self.assertEqual(validate_answer(item, body)['decision']['type'], 'ANN')            # reviewer: rule applies
        self.assertEqual(validate_answer(item, body, owner=True)['decision']['type'], 'NCU')  # owner: kept


class BlindTest(unittest.TestCase):
    def test_blind_queue_hides_ai_answers_and_scores_by_stratum(self):
        import tempfile
        from unittest.mock import patch
        import alphaearth_photo_review as tool
        import alphaearth_review_blind_score as scorer
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            for d in ('queues', 'answers'):
                (out / d).mkdir()
            items = [{'id': f'n{n:03d}', 'area_ha': .2, 'hints': {}} for n in range(60)]
            (out / 'queues/q.json').write_text(json.dumps({'items': items, 'rules': 'x_v2_2'}))
            (out / 'queues/q.key.json').write_text(json.dumps({'items': [{'id': i['id'], 'internal_parcel_id': 'p' + i['id']} for i in items]}))
            ai = {f'n{n:03d}': ('VIN' if n < 12 else 'ALF' if n < 15 else 'DEFER' if n < 20 else 'ANN') for n in range(60)}
            fin = lambda t: {'final': {'defer': True}} if t == 'DEFER' else {'final': {'decision': {'type': t}}}
            (out / 'answers/q__claude-r1.json').write_text(json.dumps({k: fin(t) for k, t in ai.items()}))
            with patch.object(tool, 'OUT', out):
                tool.make_blind_test('b', 'q', size=20, per_rare=5)
            served = json.loads((out / 'queues/blind_b.json').read_text())
            text = json.dumps(served)
            self.assertEqual(len(served['items']), 20)
            self.assertNotIn('VIN', text)  # no AI answer reaches the owner page
            design = json.loads((out / 'queues/blind_b.key.json').read_text())['design']
            self.assertEqual(design['sampled'], {'VIN': 5, 'ALF': 3, 'common': 12})
            self.assertTrue(all(design['ai'][i] != 'DEFER' for i in design['ai']))
            # owner: VIN answers right, ALF wrong (really ANN), common right
            owner = {i: {'final': {'decision': {'type': ('ANN' if t == 'ALF' else t)}}} for i, t in design['ai'].items()}
            (out / 'answers/blind_b__owner.json').write_text(json.dumps(owner))
            with patch.object(scorer, 'OUT', out):
                r = scorer.score('b')
            self.assertEqual(r['by_ai_type']['ALF']['rate'], 0.0)
            self.assertEqual(r['by_ai_type']['VIN']['rate'], 1.0)
            # weighted: (12*1 + 3*0 + 40*1) / 55
            self.assertAlmostEqual(r['accuracy_weighted'], round(52 / 55, 3))


class AlfalfaRuleVersion(unittest.TestCase):
    def test_server_uses_frequent_cuts(self):
        from alphaearth_photo_review import TYPES_CONFIG, validate_answer
        self.assertIn('alfalfa_cut_signature', TYPES_CONFIG['rules'])
        hints = {'cycles': {'2025': 1}, 'ndvi': {'2025': [.3, .8]},
                 'cuts': {'2023': 4, '2024': 3, '2025': 1}, 'green_days': {'2023': 180, '2024': 170, '2025': 90}}
        body = {'stage': 'final', 'shares': {'ANN': 1}, 'confidence': 'medium', 'reason': 'x',
                'flags': {'perennial_structure': False, 'built': False, 'active_2025': True}}
        out = validate_answer({**SMALL, 'hints': hints}, body)['contradictions']
        self.assertTrue(any(c.startswith('repeated cuts like alfalfa in 2023, 2024') for c in out))
        old_queue = {**SMALL, 'hints': {k: v for k, v in hints.items() if k not in ('cuts', 'green_days')}}
        self.assertFalse(validate_answer(old_queue, body)['contradictions'])  # pre-v2_3 queues are not judged by it


class NaturalGroundConversionV24(unittest.TestCase):
    def test_nat_on_cropped_land_is_converted_except_for_owner(self):
        from alphaearth_photo_review import validate_answer
        body = {'stage': 'final', 'shares': {'NAT': 1}, 'confidence': 'medium', 'reason': 'bare',
                'flags': {'perennial_structure': False, 'built': False, 'active_2025': False}}
        past = {**SMALL, 'hints': {'cycles': {'2022': 1, '2025': 0}, 'ndvi': {'2025': [.15, .2]}}}
        d = validate_answer(past, body)['decision']
        self.assertEqual((d['type'], d['converted_from']), ('NCU', 'NAT'))
        now = {**SMALL, 'hints': {'cycles': {'2025': 1}, 'ndvi': {'2025': [.2, .7]}}}
        self.assertEqual(validate_answer(now, body)['decision']['type'], 'ANN')
        self.assertEqual(validate_answer(past, body, owner=True)['decision']['type'], 'NAT')
        never = {**SMALL, 'hints': {'cycles': {}, 'ndvi': {'2025': [.15, .2]}}}
        self.assertEqual(validate_answer(never, body)['decision']['type'], 'NAT')


class ConversionOnlyChangesTheNaturalPart(unittest.TestCase):
    def test_orchard_with_natural_part_stays_orchard(self):
        from alphaearth_photo_review import validate_answer
        body = {'stage': 'final', 'shares': {'ORC': .5, 'BLT': .2, 'NAT': .3}, 'confidence': 'medium', 'reason': 'grid',
                'flags': {'perennial_structure': True, 'built': True, 'active_2025': True}}
        item = {**ITEM, 'hints': {'cycles': {'2023': 1, '2025': 1}, 'ndvi': {'2025': [.3, .7]}}}
        d = validate_answer(item, body)['decision']
        self.assertEqual(d['type'], 'ORC')                       # f15386 case
        self.assertEqual(d['converted_shares'], {'ORC': .5, 'BLT': .2, 'ANN': .3})
        body2 = {**body, 'shares': {'NAT': .7, 'BLT': .3}, 'flags': {'perennial_structure': False, 'built': True, 'active_2025': False}}
        self.assertEqual(validate_answer(item, body2)['decision']['type'], 'ANN')


class ForcedChoice(unittest.TestCase):
    def test_forced_choice_allows_low_and_rejects_defer(self):
        from alphaearth_photo_review import validate_answer
        body = {'stage': 'final', 'shares': {'ANN': 1}, 'confidence': 'low', 'reason': 'x',
                'flags': {'perennial_structure': False, 'built': False, 'active_2025': True}}
        item = {**SMALL, 'hints': {'cycles': {'2025': 1}, 'ndvi': {'2025': [.2, .7]}}}
        with self.assertRaises(ValueError):
            validate_answer(item, body)                         # normal queues: low confidence must defer
        self.assertEqual(validate_answer(item, body, forced=True)['decision']['type'], 'ANN')
        with self.assertRaises(ValueError):
            validate_answer(item, {'stage': 'final', 'defer': True, 'reason': 'unsure'}, forced=True)
        from alphaearth_review_cli import parse_compact
        self.assertEqual(parse_compact('f1 ANN l weak', 'final')['f1']['confidence'], 'low')
