import io,json,sqlite3,tempfile,unittest
from pathlib import Path
from zipfile import ZipFile
import xml.etree.ElementTree as ET
import pandas as pd
from wp_core import agent_queries_v6 as old,agent_queries_v7 as new
from wp_core.agent_excel_v7 import result_workbook,NS
ROOT=Path(__file__).resolve().parents[1]

class StressDelivery(unittest.TestCase):
    def test_previous_queries_are_unchanged(self):
        for topic in old.TOPICS:
            for scope in old.SCOPES:
                a=old.build_result({'topic':topic,'scope':scope});b=new.build_result({'topic':topic,'scope':scope})
                for k in ['query','summary','rows','notes','years','observation_date']:self.assertEqual(a[k],b[k],(topic,scope,k))

    def test_scope_counts_years_and_exclusions(self):
        payload=json.loads((ROOT/'server_data/agent_v7/irrigation_stress.json').read_text())
        for scope in new.SCOPES:
            r=new.build_result({'topic':'irrigation_stress','scope':scope,'group_by':'community'})
            self.assertEqual(r['years'],[2026]);self.assertEqual(r['observation_date'],'2026-09-05')
            self.assertEqual(payload['scopes'][scope]['ids'],[int(v) for v in r['_unique'].public_parcel_id])
            self.assertEqual(sum(v['parcel_count'] for v in r['rows']),r['summary']['parcel_count'])
            self.assertFalse(r['_unique'].household.any());self.assertFalse(r['_unique'].road_excluded.any())
        r=new.build_result({'topic':'irrigation_stress'});self.assertEqual(r['summary']['parcel_count'],43);self.assertAlmostEqual(r['summary']['official_area_ha'],15.092812)
        for fields in [{'years':[2025]},{'households':'include'},{'include_expansion':True}]:
            with self.assertRaises(new.QueryError):new.build_result({'topic':'irrigation_stress',**fields})
        r=new.build_result({'topic':'irrigation_stress','classes':new.CLASSES['irrigation_stress']})
        self.assertEqual(r['summary']['parcel_count'],22802);self.assertEqual(r['summary']['assessment_unassessed_parcels'],10421)

    def test_excel_complete_numeric_and_dated(self):
        r=new.build_result({'topic':'irrigation_stress','group_by':'community'},language='hy')
        with tempfile.TemporaryDirectory(dir=ROOT/'server_data/agent_v7') as tmp:
            p=new.persist_result(r,tmp);data=result_workbook(Path(tmp)/p['result_id'])
        with ZipFile(io.BytesIO(data)) as z:
            rows=ET.fromstring(z.read('xl/worksheets/sheet2.xml')).find('x:sheetData',NS)
            details=ET.fromstring(z.read('xl/worksheets/sheet3.xml')).find('x:sheetData',NS)
            self.assertEqual(len(details),44);self.assertEqual(len(rows),len(r['rows'])+1)
            self.assertEqual(sum(int(row[1].find('x:v',NS).text) for row in list(rows)[1:]),43)
            self.assertIn('ECMWF',z.read('xl/worksheets/sheet1.xml').decode())
            self.assertIn('Վերջին դիտարկում',z.read('xl/worksheets/sheet3.xml').decode())
            self.assertTrue(all(row[0].get('t')=='inlineStr' for row in list(details)[1:]))
            self.assertTrue(all('2026-09-0' in ET.tostring(row[-1],encoding='unicode') for row in list(details)[1:]))

if __name__=='__main__':unittest.main()
