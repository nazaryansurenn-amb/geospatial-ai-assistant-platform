"""Fill the packaged Excel template from an existing authorized result.

Only Python's standard library is required at runtime. No model, query, runtime
outside the independent product, or recalculation is involved in a download.
"""
from pathlib import Path
from io import BytesIO, StringIO
import csv
import json
import math
import xml.etree.ElementTree as ET
from zipfile import ZipFile, ZIP_DEFLATED
from wp_core.agent_queries import label

TEMPLATE=Path(__file__).resolve().parents[1]/'server_data/agent_v5/export_template.xlsx'
NS={'x':'http://schemas.openxmlformats.org/spreadsheetml/2006/main',
    'c':'http://schemas.openxmlformats.org/drawingml/2006/chart',
    'xdr':'http://schemas.openxmlformats.org/drawingml/2006/spreadsheetDrawing',
    'a':'http://schemas.openxmlformats.org/drawingml/2006/main',
    'r':'http://schemas.openxmlformats.org/officeDocument/2006/relationships'}
for prefix,uri in NS.items():ET.register_namespace(prefix,uri)
def tag(prefix,name):return '{'+NS[prefix]+'}'+name
def xml(el):return ET.tostring(el,encoding='utf-8',xml_declaration=True)
TOPICS={'activity':'2026 ակտիվություն','history':'Պատմություն՝ ըստ տարիների','history_summary':'2021–2025 պատմություն','use_type':'Օգտագործման տեսակ','cycles':'Տարեկան ցիկլեր','potential':'Ներուժ','consolidation':'Կոնսոլիդացիայի հնարավորություն','parcel':'Հողամաս'}
SCOPES={'lower_hrazdan':'Ստորին Հրազդան I + II','stage_1':'Ստորին Հրազդան I','stage_2':'Ստորին Հրազդան II','inner':'Հիմնական դիտարկվող տարածք','expansion':'Հարակից 1 կմ գոտի'}

def number(value,integer=False):
    if value in (None,''):return None
    n=int(value) if integer else float(value)
    if not math.isfinite(n):raise ValueError('Invalid export number')
    return n

def cell(ref,value,style):
    el=ET.Element(tag('x','c'),{'r':ref,'s':str(style)})
    if value is None:return el  # Missing observations stay blank, never zero.
    if isinstance(value,(int,float)) and not isinstance(value,bool):
        number(value);el.set('t','n');ET.SubElement(el,tag('x','v')).text=str(value)
    else:
        # Literal strings preserve cadastral codes and cannot become formulas.
        el.set('t','inlineStr');ET.SubElement(ET.SubElement(el,tag('x','is')),tag('x','t')).text=str(value)
    return el

def fill_sheet(data,rows,columns=None,numeric_styles=None):
    root=ET.fromstring(data);sd=root.find('x:sheetData',NS)
    old=list(sd);styles=[c.get('s','0') for c in old[1]]
    for row in old[1:]:sd.remove(row)
    if columns is not None:
        for c in list(old[0])[columns:]:old[0].remove(c)
    for i,values in enumerate(rows,2):
        row=ET.SubElement(sd,tag('x','row'),{'r':str(i),'ht':'23','customHeight':'1'})
        for j,value in enumerate(values):
            style=styles[j]
            if numeric_styles and type(value) in (int,float):style=numeric_styles[type(value)]
            row.append(cell(chr(65+j)+str(i),value,style))
    if columns is not None:
        filt=ET.Element(tag('x','autoFilter'),{'ref':f'A1:{chr(64+columns)}{max(1,len(rows)+1)}'})
        root.insert(list(root).index(sd)+1,filt)
    return xml(root)

def chart_xml(data,rows,column,key):
    root=ET.fromstring(data)
    root.find('.//c:barDir',NS).set('val','bar')
    root.find('.//c:catAx/c:axPos',NS).set('val','l')
    root.find('.//c:valAx/c:axPos',NS).set('val','b')
    scaling=root.find('.//c:valAx/c:scaling',NS);ET.SubElement(scaling,tag('c','min'),{'val':'0'})
    cat_axis=root.find('.//c:catAx',NS)
    grid=cat_axis.find('c:majorGridlines',NS)
    if grid is not None:cat_axis.remove(grid)
    for container,col,values,is_number in [('c:cat/c:strRef','A',[r['label'] for r in rows],False),('c:val/c:numRef',column,[r.get(key) for r in rows],True)]:
        ref=root.find('.//c:ser/'+container,NS)
        ref.find('c:f',NS).text=f"'Աղյուսակ'!${col}$2:${col}${len(rows)+1}"
        cache=ref.find('c:numCache' if is_number else 'c:strCache',NS);cache.clear()
        if is_number:ET.SubElement(cache,tag('c','formatCode')).text='#,##0' if key=='parcel_count' else '#,##0.00'
        ET.SubElement(cache,tag('c','ptCount'),{'val':str(len(rows))})
        for i,value in enumerate(values):
            if value is None:continue
            ET.SubElement(ET.SubElement(cache,tag('c','pt'),{'idx':str(i)}),tag('c','v')).text=str(value)
    return xml(root)

def workbook_bytes(result,parcel_csv):
    q=result['query'];s=result['summary'];rows=result.get('rows',[])
    active=q['topic']=='activity';col_count=4 if active else 3
    summary=[['Բաժին',TOPICS.get(q['topic'],'Հողամաս')],['Տարածք',SCOPES.get(q.get('scope'),'Ընտրված հողամաս')],
      ['Հարակից 1 կմ գոտի','Այո' if q.get('include_expansion') else 'Ոչ'],
      ['Համայնք',', '.join(label(c,'hy') for c in q.get('communities',[])) or 'Բոլոր համայնքները ընտրված տարածքում'],
      ['Տարիներ',', '.join(map(str,result.get('years',[])))],
      ['Հողամաս',s['parcel_count']],['Կադաստրային հա',s['official_area_ha']]]
    if result.get('observation_date'):summary.append(['Դիտարկում',result['observation_date']])
    if active:summary.append(['Դիտվող ակտիվ հա',s.get('observed_active_area_ha')])
    if s.get('group_count') is not None:summary.append(['Խումբ',s['group_count']])
    if s.get('unresolved_parcels'):summary.extend([['Չգնահատված հողամաս',s['unresolved_parcels']],['Չգնահատված կադաստրային հա',s['unresolved_official_area_ha']]])
    if q.get('classes'):summary.append(['Դասեր',', '.join(label(c,'hy') for c in q['classes'])])
    if q.get('code'):summary.append(['Կադաստրային կոդ',q['code']])
    if q.get('households'):summary.append(['Տնամերձ',{'exclude':'Բացառված','include':'Ներառված','only':'Միայն տնամերձ'}[q['households']]])
    for k,title in [('min_area_ha','Նվազագույն հա'),('max_area_ha','Առավելագույն հա')]:
        if q.get(k) is not None:summary.append([title,q[k]])
    summary.extend([['Աղբյուր','Ջրային և հողային ռեսուրսների կառավարման համակարգի ընտրված հաշվարկ'],
      ['Մակերես','Հողամասերի ամբողջ պաշտոնական կադաստրային մակերեսը։'],
      ['Տարածքի սահման','Ընտրված դիտարկվող տարածքը, ոչ ամբողջ համայնքը։']])
    if q['topic']=='history':summary.append(['Տարիների համեմատություն','Տարեկան տողերը չեն գումարվում։ Ամփոփումը եզակի հողամասերն է։'])
    if q['topic'] in ['potential','consolidation']:summary.append(['Մեկնաբանություն','Նախնական թեկնածուներ․ իրագործելիությունը հաստատված չէ։'])
    if active:summary.append(['Դիտարկման սահման','2026-ի սեզոնը կիսատ է։ Չգնահատված ակտիվ մակերեսը մնում է դատարկ։'])
    table=[[r['label'],r['parcel_count'],r['official_area_ha']]+([r.get('observed_active_area_ha')] if active else []) for r in rows]
    parcels=[]
    for r in csv.DictReader(StringIO(parcel_csv)):
        parcels.append([r['cadastre_code'],label(r['community'],'hy'),number(r['official_area_ha']),label(r['activity_stage'],'hy'),
            SCOPES.get(r['scope'],'Հարակից 1 կմ գոտի' if r['scope'].startswith('expansion') else 'Հիմնական դիտարկվող տարածք'),
            label(r['class'],'hy'),number(r.get('year'),True),number(r.get('observed_active_area_ha'))])
    with ZipFile(TEMPLATE) as z:parts={name:z.read(name) for name in z.namelist()}
    prototype=ET.fromstring(parts['xl/worksheets/sheet2.xml']).find('x:sheetData',NS)[1]
    parts['xl/worksheets/sheet1.xml']=fill_sheet(parts['xl/worksheets/sheet1.xml'],summary,numeric_styles={int:prototype[1].get('s'),float:prototype[2].get('s')})
    parts['xl/worksheets/sheet2.xml']=fill_sheet(parts['xl/worksheets/sheet2.xml'],table,col_count)
    parts['xl/worksheets/sheet3.xml']=fill_sheet(parts['xl/worksheets/sheet3.xml'],parcels,8)
    drawing=ET.fromstring(parts['xl/drawings/drawing1.xml'])
    chart_count=(3 if active else 2) if rows else 0
    height=max(24,len(rows)+7)
    for i,anchor in enumerate(list(drawing)):
        if i>=chart_count:drawing.remove(anchor)
        else:
            anchor.find('xdr:from/xdr:row',NS).text=str(i*(height+3))
            anchor.find('xdr:to/xdr:row',NS).text=str(i*(height+3)+height)
    parts['xl/drawings/drawing1.xml']=xml(drawing)
    for i,(col,key) in enumerate([('B','parcel_count'),('C','official_area_ha'),('D','observed_active_area_ha')],1):
        path=f'xl/drawings/charts/chart{i}.xml'
        if i<=chart_count:parts[path]=chart_xml(parts[path],rows,col,key)
        else:
            # Remove unused charts, their relationships and their content types.
            del parts[path]
            for name in ['xl/drawings/_rels/drawing1.xml.rels','[Content_Types].xml']:
                root=ET.fromstring(parts[name])
                for el in list(root):
                    if el.get('Target','').endswith(f'/chart{i}.xml') or el.get('PartName')=='/'+path:root.remove(el)
                # OPC package metadata uses a default namespace (the .NET/Excel
                # package reader rejects a prefixed Types root).
                parts[name]=xml(root).replace(b'ns0:',b'').replace(b'xmlns:ns0=',b'xmlns=')
    out=BytesIO()
    with ZipFile(out,'w',ZIP_DEFLATED) as z:
        for name,data in parts.items():z.writestr(name,data)
    return out.getvalue()

def result_workbook(directory):
    directory=Path(directory)
    return workbook_bytes(json.loads((directory/'result.json').read_text(encoding='utf-8')),(directory/'parcels.csv').read_text(encoding='utf-8-sig'))
