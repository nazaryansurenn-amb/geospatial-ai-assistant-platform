import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from wp_core.agent_queries import INDEX, QueryError, build_result, persist_result, validate, community, parcel_result
from wp_core.agent_service import AgentService, clean_context

class Queries(unittest.TestCase):
    def test_preserved_live_population(self):
        r=build_result({'topic':'activity'})
        self.assertEqual(r['summary']['parcel_count'],22802)
        self.assertAlmostEqual(r['summary']['official_area_ha'],10339.309066,6)
        self.assertAlmostEqual(r['summary']['observed_active_area_ha'],6696.629,6)
        self.assertEqual(r['summary']['unresolved_parcels'],1967)
        self.assertGreater(r['summary']['official_area_ha'],r['summary']['observed_active_area_ha'])

    def test_communities_partition_not_nearest_label(self):
        r=build_result({'topic':'activity','group_by':'community'})
        self.assertEqual(sum(x['parcel_count'] for x in r['rows']),22802)
        self.assertAlmostEqual(sum(x['official_area_ha'] for x in r['rows']),10339.309066,6)
        self.assertEqual(community('aknalitch'),'Ակնալիճ')
        r=build_result({'topic':'activity','communities':['Aknalich']})
        with sqlite3.connect(INDEX) as c:
            count,area=c.execute("select count(*),sum(official_area_ha) from parcels where community=? and scope='inside_I_II' and household=0 and road_excluded=0",['Ակնալիճ']).fetchone()
        self.assertEqual(r['summary']['parcel_count'],count)
        self.assertAlmostEqual(r['summary']['official_area_ha'],area,7)

    def test_newer_rules_and_map_history(self):
        r=build_result({'topic':'use_type'})
        self.assertEqual({x['key']:x['parcel_count'] for x in r['rows']},{'annual':4804,'perennial':2738,'undetermined':15260})
        r=build_result({'topic':'history_summary'})
        self.assertEqual({x['key']:x['parcel_count'] for x in r['rows']},{'stable_active':16584,'periodic':1546,'stable_no_activity':2664,'insufficient':2008})
        p=build_result({'topic':'potential','include_expansion':True})
        self.assertEqual(p['summary']['parcel_count'],3169)
        self.assertAlmostEqual(p['summary']['official_area_ha'],3043.159368,6)
        c=build_result({'topic':'consolidation'})
        self.assertEqual(c['summary']['group_count'],31)
        self.assertEqual(c['summary']['parcel_count'],262)

    def test_history_unique_and_missing(self):
        r=build_result({'topic':'history','years':[2021,2025]})
        self.assertEqual(r['summary']['parcel_count'],22802)
        self.assertEqual(sum(x['parcel_count'] for x in r['rows']),45604)
        self.assertTrue(all(x['observed_active_area_ha'] is None for x in r['rows']))
        unknown=r['_frame'].loc[r['_frame']['class'].eq('unresolved'),'cadastre_code'].nunique()
        self.assertEqual(r['summary']['unresolved_parcels'],unknown)
        r=build_result({'topic':'activity','classes':['unresolved']})
        self.assertIsNone(r['summary']['observed_active_area_ha'])

    def test_exclusions_scope_and_exports(self):
        a=build_result({'topic':'activity','scope':'stage_1'})
        b=build_result({'topic':'activity','scope':'stage_2'})
        self.assertEqual(a['summary']['parcel_count']+b['summary']['parcel_count'],22802)
        h=build_result({'topic':'use_type','households':'only'})
        self.assertEqual(h['summary']['parcel_count'],20096)
        self.assertEqual(h['rows'][0]['key'],'household')
        with tempfile.TemporaryDirectory(dir=INDEX.parent) as tmp:
            result=persist_result(a,tmp)
            out=Path(tmp)/result['result_id']
            selection=json.loads((out/'map.json').read_text())
            self.assertEqual(len(selection['ids']),a['summary']['parcel_count'])
            self.assertEqual(len(set(selection['codes'])),len(selection['codes']))
            self.assertNotIn('internal_', (out/'parcels.csv').read_text(encoding='utf-8-sig'))

    def test_invalid_inputs_never_expand_scope(self):
        for q in [{'scope':'wua'}, {'topic':'activity','years':[2025]}, {'topic':'activity','include_expansion':True},
                  {'communities':["x'); DROP TABLE parcels;--"]}, {'topic':'consolidation','households':'include'},
                  {'max_area_ha':float('nan')}, {'topic':'history_summary','years':[2024]}]:
            with self.subTest(q=q),self.assertRaises(QueryError):validate(q)
        self.assertFalse(validate({'topic':'activity'},{'include_expansion':True})['include_expansion'])
        with self.assertRaises(QueryError):clean_context({'scope':'wua'})

    def test_result_ownership(self):
        service=AgentService()
        a=service.session()['token'];b=service.session()['token']
        service.sessions[a]['results'].add('a'*32)
        self.assertTrue(str(service.result_path(a,'a'*32,'map')).endswith('map.json'))
        with self.assertRaises(PermissionError):service.result_path(b,'a'*32,'map')
        with self.assertRaises(PermissionError):service.result_path(a,'../.env','csv')
        service.pool.shutdown()

    def test_profiles_keep_unavailable_fields_null(self):
        with sqlite3.connect(INDEX) as c:
            code=c.execute("select cadastre_code from parcels where scope='nearby_1km' limit 1").fetchone()[0]
        r=parcel_result(code)
        self.assertIsNone(r['profile']['observed_active_area_ha'])
        self.assertIsNone(r['profile']['crop_type'])
        with tempfile.TemporaryDirectory(dir=INDEX.parent) as tmp:
            result=persist_result(r,tmp)
            self.assertEqual(result['summary']['parcel_count'],1)

if __name__=='__main__':unittest.main()
