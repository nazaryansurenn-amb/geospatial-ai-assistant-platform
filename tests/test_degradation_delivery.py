from pathlib import Path
import io,json,sqlite3,tempfile,unittest
import xml.etree.ElementTree as ET
from zipfile import ZipFile
import pandas as pd
from wp_core import agent_queries as old
from wp_core import agent_queries_v6 as new
from wp_core.agent_excel_v6 import result_workbook,NS
ROOT=Path(__file__).resolve().parents[1]

class DegradationDelivery(unittest.TestCase):
    def test_previous_queries_unchanged(self):
        for topic in old.TOPICS:
            for scope in old.SCOPES:
                q={'topic':topic,'scope':scope}
                a=old.build_result(q);b=new.build_result(q)
                for key in ['query','summary','rows','notes']:
                    self.assertEqual(a[key],b[key],(topic,scope,key))

    def test_population_and_union_reconcile(self):
        df=pd.read_parquet(ROOT/'data/analysis/degradation/degradation_screening_20260906_v1/parcels.parquet')
        r=new.build_result({'topic':'degradation','group_by':'community'},language='hy')
        self.assertEqual(r['summary']['parcel_count'],299)
        self.assertAlmostEqual(r['summary']['official_area_ha'],286.820136,6)
        self.assertEqual(r['summary']['denominator_parcels'],19421)
        self.assertEqual(r['summary']['assessment_unassessed_parcels'],15378)
        self.assertEqual(set(r['_unique'].cadastre_code),set(df.loc[df.candidate,'cadastre_code']))
        self.assertFalse(r['_unique'].household.any());self.assertFalse(r['_unique'].road_excluded.any())
        self.assertEqual(sum(row['parcel_count'] for row in r['rows']),299)
        self.assertAlmostEqual(sum(row['official_area_ha'] for row in r['rows']),286.820136,6)
        all_states=new.build_result({'topic':'degradation','classes':['possible_signs','not_flagged','unassessed']})
        self.assertEqual(all_states['summary']['parcel_count'],19421)
        self.assertEqual(set(row['label'] for row in all_states['rows']),{'Possible signs of degradation','No sign flagged in assessed indicators','Unassessed'})

    def test_scope_and_unsupported_requests(self):
        payload=json.loads((ROOT/'server_data/agent_v6/degradation.json').read_text(encoding='utf-8'))
        for scope in old.SCOPES:
            r=new.build_result({'topic':'degradation','scope':scope})
            self.assertEqual(payload['scopes'][scope]['ids'],[int(v) for v in r['_unique'].public_parcel_id])
        for kwargs in [{'households':'include'},{'households':'only'},{'include_expansion':True},{'years':[2025]}]:
            with self.assertRaises(new.QueryError):new.build_result({'topic':'degradation',**kwargs})
        r=new.build_result({'topic':'degradation'},{'include_expansion':True})
        self.assertFalse(r['query']['include_expansion'])

    def test_excel_has_complete_numeric_table_and_candidates(self):
        r=new.build_result({'topic':'degradation','group_by':'community'},language='hy')
        with tempfile.TemporaryDirectory(dir=ROOT/'server_data/agent_v6') as tmp:
            p=new.persist_result(r,tmp);data=result_workbook(Path(tmp)/p['result_id'])
        with ZipFile(io.BytesIO(data)) as z:
            table=ET.fromstring(z.read('xl/worksheets/sheet2.xml')).find('x:sheetData',NS)
            details=ET.fromstring(z.read('xl/worksheets/sheet3.xml')).find('x:sheetData',NS)
            self.assertEqual(len(table),len(r['rows'])+1);self.assertEqual(len(details),300)
            self.assertEqual(sum(int(row[1].find('x:v',NS).text) for row in list(table)[1:]),299)
            self.assertEqual(len([n for n in z.namelist() if '/charts/chart' in n and n.endswith('.xml')]),2)
            self.assertIn('հողի դեգրադացիան հաստատված չէ',z.read('xl/worksheets/sheet1.xml').decode().lower())
            self.assertTrue(all(row[0].get('t')=='inlineStr' for row in list(details)[1:]))
