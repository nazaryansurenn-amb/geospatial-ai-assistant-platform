import csv
import io
import json
from pathlib import Path
import tempfile
import unittest
import xml.etree.ElementTree as ET
from zipfile import ZipFile
from wp_core.agent_excel import workbook_bytes,result_workbook,NS
from wp_core.agent_queries import build_result,persist_result
from wp_core.agent_service_v5 import AgentService

CTX={'scope':'lower_hrazdan','include_expansion':True}
def cells(z,name):
    out={}
    for c in ET.fromstring(z.read(name)).findall('.//x:c',NS):
        value=c.find('x:v',NS) if c.get('t')=='n' else c.find('x:is/x:t',NS)
        out[c.get('r')]=(c.get('t'),value.text if value is not None else None)
    return out

class ExcelExport(unittest.TestCase):
    def test_community_export_reconciles_full_table_and_parcels(self):
        result=build_result({'topic':'potential','group_by':'community'},CTX,'hy')
        with tempfile.TemporaryDirectory() as tmp:
            public=persist_result(result,tmp);data=result_workbook(Path(tmp)/public['result_id'])
        with ZipFile(io.BytesIO(data)) as z:
            table=cells(z,'xl/worksheets/sheet2.xml');detail=cells(z,'xl/worksheets/sheet3.xml')
            self.assertEqual(table['A2'],('inlineStr','Ակնալիճ'))
            self.assertEqual(table['A24'],('inlineStr','Ֆերիկ'))
            self.assertEqual(sum(int(table[f'B{n}'][1]) for n in range(2,25)),3169)
            self.assertAlmostEqual(sum(float(table[f'C{n}'][1]) for n in range(2,25)),3043.159368)
            self.assertEqual(len([r for r in detail if r.startswith('A')]),3170)
            self.assertEqual(detail['A2'],('inlineStr',str(result['_frame'].iloc[0].cadastre_code)))
            charts=[n for n in z.namelist() if '/charts/chart' in n and n.endswith('.xml')]
            self.assertEqual(len(charts),2)
            for path in charts:
                tree=ET.fromstring(z.read(path))
                self.assertTrue(tree.find('.//c:cat/c:strRef/c:f',NS).text.endswith('$A$24'))
                self.assertEqual(tree.find('.//c:cat/c:strRef/c:strCache/c:ptCount',NS).get('val'),'23')

    def test_missing_active_measurements_and_codes_are_preserved(self):
        raw={'query':{'topic':'activity'},'summary':{'parcel_count':1,'official_area_ha':2.5,'observed_active_area_ha':None},'rows':[{'label':'=1+1','parcel_count':1,'official_area_ha':2.5,'observed_active_area_ha':None}]}
        csv_text='cadastre_code,community,official_area_ha,activity_stage,scope,class,observed_active_area_ha\n04-001-0001-0001,Ակնալիճ,2.5,stage_1,inner,unresolved,\n'
        with ZipFile(io.BytesIO(workbook_bytes(raw,csv_text))) as z:
            table=cells(z,'xl/worksheets/sheet2.xml');detail=cells(z,'xl/worksheets/sheet3.xml')
            self.assertEqual(table['A2'],('inlineStr','=1+1'))
            self.assertEqual(table['D2'][1],None);self.assertEqual(detail['H2'][1],None)
            self.assertEqual(detail['A2'],('inlineStr','04-001-0001-0001'))
            self.assertEqual(len(ET.fromstring(z.read('xl/drawings/charts/chart3.xml')).findall('.//c:numCache/c:pt',NS)),0)

    def test_history_summary_is_unique_and_empty_export_has_no_charts(self):
        raw={'query':{'topic':'history'},'summary':{'parcel_count':1,'official_area_ha':2.5},'rows':[],'years':[2021,2022]}
        with ZipFile(io.BytesIO(workbook_bytes(raw,'cadastre_code,community,official_area_ha,activity_stage,scope,class,year\n'))) as z:
            self.assertFalse(any('/charts/chart' in n for n in z.namelist()))
            values=[v[1] for v in cells(z,'xl/worksheets/sheet1.xml').values()]
            self.assertIn('Տարեկան տողերը չեն գումարվում։ Ամփոփումը եզակի հողամասերն է։',values)

    def test_result_authorization_precedes_export_read(self):
        service=AgentService();owner=service.session()['token'];other=service.session()['token']
        try:
            rid='a'*32;service.sessions[owner]['results'].add(rid)
            self.assertTrue(str(service.result_path(owner,rid,'summary')).endswith('result.json'))
            with self.assertRaises(PermissionError):service.result_path(other,rid,'summary')
        finally:service.pool.shutdown()
