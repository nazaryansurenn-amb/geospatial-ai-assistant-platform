"""Add a current-stress section on top of the accepted area50 seven-section UI."""
from pathlib import Path
import json,shutil,sqlite3
import pandas as pd
ROOT=Path(__file__).resolve().parents[1]
SLUG='irrigation_stress_review_20260907_v1'
SOURCE=ROOT/'server_data/review'/SLUG/'frontend_source'
TITLE='Ոռոգման սթրեսի հնարավոր նշաններ'
OUT=ROOT/'data/analysis/irrigation_stress/irrigation_stress_20260907_v2'

def replace(s,a,b):
    assert a in s,a
    return s.replace(a,b)

def main():
    assert not (ROOT/'config'/f'{SLUG}.review.lock.json').exists(),'Sealed review'
    assert not SOURCE.exists(),'Do not overwrite prepared source'
    shutil.copytree(ROOT/'server_data/review/agent_review_20260906_v6/frontend_source',SOURCE)
    dist=ROOT/'output'/f'frontend_{SLUG}';dist.mkdir(parents=True,exist_ok=False)
    shutil.copytree(ROOT/'output/frontend_activity_change_area50_20260907_v1/data',dist/'data')
    v=SOURCE/'vite.config.mjs';v.write_text(v.read_text().replace('frontend_agent_review_20260906_v6','frontend_'+SLUG))
    for name in ['agent_service','agent_queries','agent_knowledge','agent_presentation','agent_excel','activity_change_history_agent']:
        s=(ROOT/'wp_core'/f'{name}_v6.py').read_text(encoding='utf-8')
        for module in ['agent_service','agent_queries','agent_knowledge','agent_presentation','agent_excel','activity_change_history_agent']:s=s.replace(module+'_v6',module+'_v7')
        if name in ['agent_service','agent_queries']:s=s.replace('server_data/agent_v6/','server_data/agent_v7/')
        if name=='agent_queries':
            s=replace(s,"'consolidation', 'degradation']","'consolidation', 'degradation', 'irrigation_stress']")
            s=replace(s,"    'degradation': ['possible_signs', 'not_flagged', 'unassessed'],","    'degradation': ['possible_signs', 'not_flagged', 'unassessed'],\n    'irrigation_stress': ['possible_irrigation_stress','not_flagged','unassessed','not_current_vegetation'],")
            s=replace(s,"LABELS = {","LABELS = {\n    'possible_irrigation_stress':['Ոռոգման սթրեսի հնարավոր նշաններ','Possible signs of irrigation stress','Возможные признаки оросительного стресса'],\n    'not_current_vegetation':['Ընթացիկ աճող բուսականությունը չի հաստատվել','Current growing vegetation not established','Текущая растущая растительность не установлена'],")
            s=replace(s,"(['possible_signs'] if q['topic']=='degradation' else ['candidate'])","(['possible_signs'] if q['topic']=='degradation' else (['possible_irrigation_stress'] if q['topic']=='irrigation_stress' else ['candidate']))")
            s=s.replace("q['topic'] == 'activity'","q['topic'] in ['activity','irrigation_stress']").replace("q['topic'] != 'activity'","q['topic'] not in ['activity','irrigation_stress']")
            s=replace(s,"    elif topic == 'degradation':", "    elif topic == 'irrigation_stress':\n        frame['class'] = frame.stress_state.replace({'candidate':'possible_irrigation_stress'})\n    elif topic == 'degradation':")
            s=replace(s,"    if topic=='potential':\n        notes", "    if topic=='irrigation_stress':\n        summary['assessment_unassessed_parcels']=int(denominator.stress_state.eq('unassessed').sum())\n        summary['assessment_unassessed_official_area_ha']=area_sum(denominator[denominator.stress_state.eq('unassessed')])\n        summary['assessed_parcels']=int(denominator.stress_state.isin(['candidate','not_flagged']).sum())\n        dates=unique.stress_observation_date.dropna()\n        summary['observation_date_from']=str(dates.min()) if len(dates) else None\n        summary['observation_date_to']=str(dates.max()) if len(dates) else None\n        notes += ['Possible optical signs of irrigation stress as of 5 September 2026, not confirmed irrigation failure, a root-zone measurement, a water amount or an instruction to irrigate. Weather is ECMWF model estimates, not ERA5 or station observations. Only weather preceding each satellite observation is used; no forecast-risk layer is calculated.', 'Recent growing vegetation is compared with its own earlier observations, comparable nearby vegetation and prior covered seasons. Missing evidence stays unassessed, not healthy. Harvesting, normal seasonal change, soil and management can still explain an apparent signal. No independent field accuracy has been measured.', 'Area-weighted 20 m moisture observations may share boundary pixels across adjacent parcels. They are shared screening evidence, not independent parcel confirmation. Official hectares are whole cadastral parcel areas, not measured stressed vegetation area.']\n    if topic=='potential':\n        notes")
            s=replace(s,"'observation_date':DATE if topic=='activity' else None", "'observation_date':DATE if topic=='activity' else ('2026-09-05' if topic=='irrigation_stress' else None)")
            s=replace(s,"    if 'year' in frame:","    if result.get('query',{}).get('topic')=='irrigation_stress':\n        columns.append('stress_observation_date')\n    if 'year' in frame:")
            s=replace(s,"    profile['degradation_state']", "    profile['irrigation_stress_state']=scalar(row.stress_state)\n    profile['irrigation_stress_observation_date']=scalar(row.stress_observation_date)\n    profile['degradation_state']")
        elif name=='agent_service':
            s=replace(s,"'consolidation','degradation']:","'consolidation','degradation','irrigation_stress']:")
            s=s.replace('Activity [2026];','Activity and irrigation stress [2026];')
        elif name=='agent_knowledge':
            s=s.replace('8526-agent-20260906-v6','8526-agent-20260907-v7').replace('seven visible sections','eight visible sections').replace('seven analysis tabs','eight analysis tabs').replace('active seven','active eight')
            s=replace(s,"'Հողերի դեգրադացիայի հնարավոր նշաններ'],",f"'Հողերի դեգրադացիայի հնարավոր նշաններ', '{TITLE}'],")
            s=replace(s,"    'sections': {","    'sections': {\n        'irrigation_stress': 'Ոռոգման սթրեսի հնարավոր նշաններ / Possible signs of irrigation stress. One red colour, current-season assessment as of 2026-09-05, separate from the older 2026 activity layer through 23 August. Use analyze_land topic irrigation_stress and year 2026. The default returns possible signs; query all available states for coverage. Counts/hectares come only from tools. Excludes households, roads and the potential expansion. Repeated moisture decline while vegetation remains growing is checked against earlier observations, comparable nearby land, prior seasons and preceding ECMWF weather-model estimates. No future weather risk, radar, 250 m layer, measured delivered water, confirmed root-zone deficit or automatic irrigation instruction. Boundary pixels may be shared; this is screening evidence rather than independent parcel confirmation. Hectares are whole cadastral areas, not the measured area under stress. Missing evidence stays unassessed; an uncoloured parcel is not certified free of stress. Harvest, senescence, soil and management remain possible explanations; field checking is needed. Use natural Armenian names, not index names or numerical thresholds. Explain the difference from long-term degradation and above-normative water demand.',")
            s += '\n# The current stress endpoint and deterministic queries supply all live totals.\n'
        elif name=='agent_presentation':
            for a,b in [('Հողերի դեգրադացիայի հնարավոր նշաններ',TITLE),('Possible signs of land degradation','Possible signs of irrigation stress'),('Возможные признаки деградации земель','Возможные признаки оросительного стресса')]:
                s=replace(s,"'degradation':'"+a+"'","'degradation':'"+a+"','irrigation_stress':'"+b+"'")
        elif name=='agent_excel':
            s=replace(s,"TOPICS={",f"TOPICS={{'irrigation_stress':'{TITLE}',")
            s=replace(s,"    if active:summary.append(['Դիտարկման սահման'", "    if q['topic']=='irrigation_stress':summary.extend([['Մեկնաբանություն','Ոռոգման սթրեսի հնարավոր նշաններ․ անհրաժեշտ է տեղում ստուգում։ Հեկտարները սթրեսի ենթարկված հատվածի չափում չեն։ Հարևան հողամասերը կարող են կիսել արբանյակային ազդանշանը։'],['Եղանակ','ECMWF եղանակային մոդելի գնահատականներ'],['Չգնահատված հողամաս',s.get('assessment_unassessed_parcels')],['Չգնահատված կադաստրային հա',s.get('assessment_unassessed_official_area_ha')]])\n    if active:summary.append(['Դիտարկման սահման'")
            s=replace(s,"number(r.get('observed_active_area_ha'))])", "(r.get('stress_observation_date') if q['topic']=='irrigation_stress' else number(r.get('observed_active_area_ha')))])")
            s=replace(s,"    drawing=ET.fromstring(parts['xl/drawings/drawing1.xml'])", "    if q['topic']=='irrigation_stress':\n        detail=ET.fromstring(parts['xl/worksheets/sheet3.xml'])\n        header=detail.find('x:sheetData',NS)[0][-1]\n        for el in list(header):header.remove(el)\n        header.set('t','inlineStr');ET.SubElement(ET.SubElement(header,tag('x','is')),tag('x','t')).text='Վերջին դիտարկում'\n        parts['xl/worksheets/sheet3.xml']=xml(detail)\n    drawing=ET.fromstring(parts['xl/drawings/drawing1.xml'])")
        (ROOT/'wp_core'/f'{name}_v7.py').write_text(s,encoding='utf-8')
    a=(ROOT/'wp_core/activity_change_area50_agent.py').read_text().replace('_v6','_v7')
    (ROOT/'wp_core/activity_change_area50_agent_v7.py').write_text(a)
    directory=ROOT/'server_data/agent_v7';directory.mkdir(exist_ok=True)
    target=directory/'parcels.sqlite3';assert not target.exists();shutil.copy2(ROOT/'server_data/agent_v6/parcels.sqlite3',target)
    df=pd.read_parquet(OUT/'parcels.parquet')
    with sqlite3.connect(target) as c:
        c.execute("alter table parcels add column stress_state TEXT NOT NULL DEFAULT 'outside_mask'")
        c.execute('alter table parcels add column stress_observation_date TEXT')
        c.executemany('update parcels set stress_state=?,stress_observation_date=? where cadastre_code=?',[(r.state,r.observation_date,r.cadastre_code) for r in df.itertuples()])
    payload={'as_of':'2026-09-05','weather':'ecmwf_model_estimates','period':'2026-08-01/2026-09-05','scopes':{}}
    for scope in ['lower_hrazdan','stage_1','stage_2']:
        part=df if scope=='lower_hrazdan' else df[df.activity_stage.eq(scope)];candidate=part[part.candidate]
        summary={}
        for name,condition in [('included',pd.Series(True,index=part.index)),('current_vegetation',part.current_vegetation),('assessed',part.assessed),('candidate',part.candidate),('unassessed',part.state.eq('unassessed')),('not_flagged',part.state.eq('not_flagged')),('not_current_vegetation',part.state.eq('not_current_vegetation'))]:
            selected=part[condition];summary[name]={'count':len(selected),'area_ha':float(selected.official_area_ha.sum())}
        summary['ids']=[int(v) for v in candidate.public_parcel_id]
        summary['bbox']=[float(candidate.minx.min()),float(candidate.miny.min()),float(candidate.maxx.max()),float(candidate.maxy.max())] if len(candidate) else None
        summary['date_from']=candidate.observation_date.min() if len(candidate) else None;summary['date_to']=candidate.observation_date.max() if len(candidate) else None
        payload['scopes'][scope]=summary
    (directory/'irrigation_stress.json').write_text(json.dumps(payload,ensure_ascii=False,allow_nan=False))
    lookup={r.cadastre_code:{'state':r.state,'date':r.observation_date,'previous_date':r.previous_observation_date} for r in df.itertuples()}
    (directory/'irrigation_stress_lookup.json').write_text(json.dumps(lookup,allow_nan=False))
    p=SOURCE/'src/App.jsx';s=p.read_text(encoding='utf-8')
    s='import IrrigationStressView, { appendIrrigationStressInfo, useIrrigationStress } from "./IrrigationStressView";\n'+s
    s=replace(s,'? "degradation" : "activity_2026";', '? "degradation"\n  : new URLSearchParams(window.location.search).get("view") === "irrigation-stress" ? "irrigation_stress" : "activity_2026";')
    s=replace(s,'  } else if (parcel.mode === "degradation") {','  } else if (parcel.mode === "irrigation_stress") {\n    appendIrrigationStressInfo(content, parcel.irrigationStress);\n  } else if (parcel.mode === "degradation") {')
    s=replace(s,'  const degradation = useDegradation(', '  const irrigationStress = useIrrigationStress({map: mapRef.current, ready: mapReady, active: landMode === "irrigation_stress", scope: landScope, closePanel: () => setLayerPanelOpen(false)});\n  const degradation = useDegradation(')
    s=s.replace('"activity_change", "degradation"]','"activity_change", "degradation", "irrigation_stress"]')
    s=replace(s,'degradation: analytics.degradation,','degradation: analytics.degradation,\n          irrigationStress: analytics.irrigationStress,')
    s=replace(s,'{landMode === "degradation" && <DegradationView model={degradation} />}','{landMode === "degradation" && <DegradationView model={degradation} />}\n              {landMode === "irrigation_stress" && <IrrigationStressView model={irrigationStress} />}')
    p.write_text(s,encoding='utf-8')
    p=SOURCE/'src/landResourcesSchema.js';s=p.read_text(encoding='utf-8').rstrip();assert s.endswith('];');p.write_text(s[:-2]+f'  {{id:"irrigation_stress",label:"{TITLE}",classes:[]}},\n];\n',encoding='utf-8')
    p=SOURCE/'src/AgentPanel.jsx';s=p.read_text(encoding='utf-8');s=replace(s,'const topics={',f"const topics={{irrigation_stress:'{TITLE}',")
    s=replace(s,"    {result.query.topic==='history'", "    {result.query.topic==='irrigation_stress'&&<p className=\"agent-note\">Հնարավոր նշաններ՝ 05.09.2026-ի դրությամբ։ Եղանակը՝ ECMWF մոդելի գնահատականներ։ Անհրաժեշտ է տեղում ստուգում։ Հեկտարները ամբողջ հողամասերինն են, ոչ սթրեսի հատվածի չափում։ Չգնահատված՝ {format(s.assessment_unassessed_parcels,0)} հողամաս · {format(s.assessment_unassessed_official_area_ha)} հա։</p>}\n    {result.query.topic==='history'")
    p.write_text(s,encoding='utf-8')
    print('Prepared separate stress data, eight-section frontend and agent with Excel support')

if __name__=='__main__':main()
