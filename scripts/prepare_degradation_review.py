"""Create additive v6 code and data; never modify sealed v5 artifacts."""
from pathlib import Path
import hashlib,json,shutil,sqlite3
import pandas as pd
ROOT=Path(__file__).resolve().parents[1]
OLD='activity_change_history_review_20260906_v1';NEW='agent_review_20260906_v6'
TITLE='Հողերի դեգրադացիայի հնարավոր նշաններ'
SOURCE=ROOT/'server_data/review'/NEW/'frontend_source'

def replace(text,old,new):
    assert old in text,old
    return text.replace(old,new)

def main():
    assert not (ROOT/'config'/f'{NEW}.review.lock.json').exists(),'Sealed build'
    SOURCE.parent.mkdir(parents=True,exist_ok=True)
    shutil.copytree(ROOT/'server_data/review'/OLD/'frontend_source',SOURCE,dirs_exist_ok=True)
    dist=ROOT/'output'/f'frontend_{NEW}';dist.mkdir(parents=True,exist_ok=True)
    shutil.copytree(ROOT/'output'/f'frontend_{OLD}'/'data',dist/'data',dirs_exist_ok=True)
    (SOURCE/'vite.config.mjs').write_text((ROOT/'server_data/review'/OLD/'frontend_source/vite.config.mjs').read_text().replace(OLD,NEW))
    for a,b in [('agent_service_v5','agent_service_v6'),('agent_knowledge_v5','agent_knowledge_v6'),('agent_queries','agent_queries_v6'),('agent_presentation','agent_presentation_v6'),('agent_excel','agent_excel_v6')]:
        text=(ROOT/'wp_core'/f'{a}.py').read_text(encoding='utf-8')
        if a=='agent_service_v5':
            for x,y in [('agent_knowledge_v5','agent_knowledge_v6'),('agent_queries','agent_queries_v6'),('agent_presentation','agent_presentation_v6'),('agent_excel','agent_excel_v6'),('server_data/agent_v1/results','server_data/agent_v6/results')]:text=text.replace(x,y)
            text=replace(text,"'potential','consolidation']:","'potential','consolidation','degradation']:")
        elif a=='agent_queries':
            text=text.replace('server_data/agent_v1/','server_data/agent_v6/')
            text=replace(text,"'potential', 'consolidation']","'potential', 'consolidation', 'degradation']")
            text=replace(text,"'cycles','potential','consolidation']","'cycles','potential','consolidation','degradation']")
            text=replace(text,"    'consolidation': ['candidate', 'not_candidate'],","    'consolidation': ['candidate', 'not_candidate'],\n    'degradation': ['possible_signs', 'not_flagged', 'unassessed'],")
            text=replace(text,"LABELS = {","LABELS = {\n    'possible_signs': ['Դեգրադացիայի հնարավոր նշաններ', 'Possible signs of degradation', 'Возможные признаки деградации'],\n    'not_flagged': ['Գնահատված ցուցանիշներով նշան չի առանձնացվել', 'No sign flagged in assessed indicators', 'Признаки не выделены по оцененным показателям'],\n    'outside_mask': ['Այս գնահատման շրջանակից դուրս', 'Outside this assessment', 'Вне этой оценки'],")
            text=replace(text,"else ['candidate']","else (['possible_signs'] if q['topic']=='degradation' else ['candidate'])")
            text=replace(text,"    if q['communities']:\n        clauses.append", "    if q['topic']=='degradation':\n        clauses.append('degradation_included=1')\n    if q['communities']:\n        clauses.append")
            text=replace(text,"    elif topic == 'potential':\n", "    elif topic == 'degradation':\n        frame['class'] = frame.degradation_state.replace({'candidate':'possible_signs'})\n    elif topic == 'potential':\n")
            text=replace(text,"    if topic=='potential':\n        notes", "    if topic=='degradation':\n        summary['assessment_unassessed_parcels']=int(denominator.degradation_state.eq('unassessed').sum())\n        summary['assessment_unassessed_official_area_ha']=area_sum(denominator[denominator.degradation_state.eq('unassessed')])\n        notes += ['Possible vegetation signs only, not confirmed soil degradation, salinity or measured damaged hectares. This first five-year screen has no independent accuracy validation.', 'Historically active or partly active in at least one 2021-2025 year, inside the selected Lower Hrazdan study area, excluding households and roads. All five years are examined. Missing support stays unassessed; absence of a flag does not certify healthy land.', 'Vegetation deterioration and persistent low performance are distinct indicators displayed with one colour. Climate, water access and management remain possible explanations.']\n    if topic=='potential':\n        notes")
            text=replace(text,"    if row.activity_state!='ready':", "    profile['degradation_state']=scalar(row.degradation_state)\n    profile['vegetation_deterioration']=bool(row.degradation_deterioration)\n    profile['persistent_low_performance']=bool(row.degradation_low_performance)\n    if row.activity_state!='ready':")
        elif a=='agent_knowledge_v5':
            text=text.replace('8526-agent-20260906-v5','8526-agent-20260906-v6').replace('five visible sections','seven visible sections').replace('five analysis tabs','seven analysis tabs').replace('active five','active seven')
            text=replace(text,"'Հողատարածքների կոնսոլիդացիայի հնարավորություն'],",f"'Հողատարածքների կոնսոլիդացիայի հնարավորություն', 'Մշակման փոփոխություն 2021–2025', '{TITLE}'],")
            text=replace(text,"    'sections': {", "    'sections': {\n        'degradation': 'Possible signs of land degradation / Հողերի դեգրադացիայի հնարավոր նշաններ. One orange map class combines vegetation deterioration and persistent low vegetation performance, kept distinct as explanations. First 2021-2025 screening of historically active or partly active parcels in any of those years, inside Lower Hrazdan I/II only, excluding households and roads. All five years are examined, including later no-activity years. No adjacent expansion. Inadequate evidence remains unassessed and must not be called healthy. Use analyze_land topic degradation; default possible_signs; request all three classes for coverage. Query results, not memorized totals, supply counts and hectares. Whole official parcel hectares are not measured damaged-soil hectares. No field-confirmed degradation, salinity, causal diagnosis or independently measured accuracy. Weather, crop rotation, fallow, water access and management remain explanations. Known broad annual/perennial context supports nearby comparisons; this does not establish identical soil potential. Show possible signs wording even if the user says degraded land.',")
            text=text.replace('potential and consolidation; discuss','potential, consolidation and degradation; discuss').replace('Potential and consolidation always','Potential, consolidation and degradation always').replace('activity, potential or consolidation','activity, potential, consolidation or degradation')
        elif a=='agent_presentation':
            text=replace(text,"'consolidation':'Հողատարածքների կոնսոլիդացիայի հնարավորություն'",f"'consolidation':'Հողատարածքների կոնսոլիդացիայի հնարավորություն','activity_change':'Մշակման փոփոխություն 2021–2025','degradation':'{TITLE}'")
            text=replace(text,"'consolidation':'Land consolidation opportunity'","'consolidation':'Land consolidation opportunity','activity_change':'Cultivation changes 2021-2025','degradation':'Possible signs of land degradation'")
            text=replace(text,"'consolidation':'Возможность консолидации земель'","'consolidation':'Возможность консолидации земель','activity_change':'Изменения обработки 2021–2025','degradation':'Возможные признаки деградации земель'")
        elif a=='agent_excel':
            text=text.replace('from wp_core.agent_queries import','from wp_core.agent_queries_v6 import')
            text=replace(text,"'parcel':'Հողամաս'",f"'parcel':'Հողամաս','degradation':'{TITLE}'")
            text=replace(text,"    if active:summary.append(['Դիտարկման սահման'", "    if q['topic']=='degradation':summary.extend([['Մեկնաբանություն','Հնարավոր նշաններ․ հողի դեգրադացիան հաստատված չէ։ Հեկտարները վնասված հատվածի չափում չեն։'],['Գնահատման շրջանակում չգնահատված հողամաս',s.get('assessment_unassessed_parcels')],['Չգնահատված կադաստրային հա',s.get('assessment_unassessed_official_area_ha')]])\n    if active:summary.append(['Դիտարկման սահման'")
        (ROOT/'wp_core'/f'{b}.py').write_text(text,encoding='utf-8')
    # Copy the read-only numerical index and add separate attributes only.
    directory=ROOT/'server_data/agent_v6';directory.mkdir(exist_ok=True)
    target=directory/'parcels.sqlite3'
    if not target.exists():
        shutil.copy2(ROOT/'server_data/agent_v1/parcels.sqlite3',target)
        with sqlite3.connect(target) as conn:
            for name,kind in [('degradation_included','INTEGER NOT NULL DEFAULT 0'),('degradation_state',"TEXT NOT NULL DEFAULT 'outside_mask'"),('degradation_deterioration','INTEGER NOT NULL DEFAULT 0'),('degradation_low_performance','INTEGER NOT NULL DEFAULT 0')]:conn.execute(f'ALTER TABLE parcels ADD COLUMN {name} {kind}')
            df=pd.read_parquet(ROOT/'data/analysis/degradation/degradation_screening_20260906_v1/parcels.parquet')
            conn.executemany('UPDATE parcels SET degradation_included=?,degradation_state=?,degradation_deterioration=?,degradation_low_performance=? WHERE cadastre_code=?',[(int(r.included),r.state,int(r.deterioration),int(r.low_performance),r.cadastre_code) for r in df.itertuples()])
    df=pd.read_parquet(ROOT/'data/analysis/degradation/degradation_screening_20260906_v1/parcels.parquet')
    payload={'period':'2021–2025','scopes':{}}
    for scope in ['lower_hrazdan','stage_1','stage_2']:
        part=df[df.included & (True if scope=='lower_hrazdan' else df.activity_stage.eq(scope))]
        candidates=part[part.candidate]
        summary={}
        for name,condition in [('included',pd.Series(True,index=part.index)),('assessed',part.assessed),('candidate',part.candidate),('not_flagged',part.state.eq('not_flagged')),('unassessed',part.state.eq('unassessed'))]:
            selected=part[condition];summary[name]={'count':len(selected),'area_ha':float(selected.official_area_ha.sum())}
        summary['ids']=[int(v) for v in candidates.public_parcel_id]
        summary['bbox']=[float(candidates.minx.min()),float(candidates.miny.min()),float(candidates.maxx.max()),float(candidates.maxy.max())] if len(candidates) else None
        payload['scopes'][scope]=summary
    (directory/'degradation.json').write_text(json.dumps(payload,ensure_ascii=False,allow_nan=False),encoding='utf-8')
    lookup={r.cadastre_code:{'state':r.state,'deterioration':bool(r.deterioration),'low_performance':bool(r.low_performance)} for r in df.itertuples()}
    (directory/'degradation_lookup.json').write_text(json.dumps(lookup),encoding='utf-8')
    app=(ROOT/'server_data/review'/OLD/'frontend_source/src/App.jsx').read_text(encoding='utf-8')
    app='import DegradationView, { appendDegradationInfo, useDegradation } from "./DegradationView";\n'+app
    app=replace(app,'? "consolidation" : "activity_2026";', '? "consolidation"\n  : new URLSearchParams(window.location.search).get("view") === "degradation" ? "degradation" : "activity_2026";')
    app=replace(app,'  } else if (parcel.mode === "consolidation") {','  } else if (parcel.mode === "degradation") {\n    appendDegradationInfo(content, parcel.degradation);\n  } else if (parcel.mode === "consolidation") {')
    app=replace(app,'  const consolidation = useConsolidation(', '  const degradation = useDegradation({map: mapRef.current, ready: mapReady, active: landMode === "degradation", scope: landScope, closePanel: () => setLayerPanelOpen(false)});\n  const consolidation = useConsolidation(')
    app=replace(app,'["consolidation", "activity_change"].includes(mode)','["consolidation", "activity_change", "degradation"].includes(mode)')
    app=replace(app,'consolidation: analytics.consolidation,','consolidation: analytics.consolidation,\n          degradation: analytics.degradation,')
    app=replace(app,'["consolidation", "activity_change"].includes(landMode)','["consolidation", "activity_change", "degradation"].includes(landMode)')
    app=replace(app,'{landMode === "consolidation" && <ConsolidationView model={consolidation} />}','{landMode === "consolidation" && <ConsolidationView model={consolidation} />}\n              {landMode === "degradation" && <DegradationView model={degradation} />}')
    app=replace(app,'["potential", "consolidation", "activity_change"].includes(landMode)','["potential", "consolidation", "activity_change", "degradation"].includes(landMode)')
    (SOURCE/'src/App.jsx').write_text(app,encoding='utf-8')
    schema=(ROOT/'server_data/review'/OLD/'frontend_source/src/landResourcesSchema.js').read_text(encoding='utf-8')
    schema=schema.rstrip()[:-2]+f'  {{id: "degradation", label: "{TITLE}", classes: []}},\n];\n'
    (SOURCE/'src/landResourcesSchema.js').write_text(schema,encoding='utf-8')
    panel=(ROOT/'server_data/review'/OLD/'frontend_source/src/AgentPanel.jsx').read_text(encoding='utf-8')
    panel=replace(panel,"const topics={",f"const topics={{degradation:'{TITLE}',")
    panel=replace(panel,"    {result.query.topic==='history'", "    {result.query.topic==='degradation'&&<p className=\"agent-note\">Հնարավոր նշաններ․ հողի դեգրադացիան հաստատված չէ։ Չգնահատված՝ {format(s.assessment_unassessed_parcels,0)} հողամաս · {format(s.assessment_unassessed_official_area_ha)} հա։</p>}\n    {result.query.topic==='history'")
    (SOURCE/'src/AgentPanel.jsx').write_text(panel,encoding='utf-8')
    print('Prepared separate v6 data, queries, agent and frontend integration')

if __name__=='__main__':main()
