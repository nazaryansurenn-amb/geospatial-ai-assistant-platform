import React, { useEffect, useRef, useState } from 'react';
import './agent.css';
import AgentMessage from './AgentMessage';

const format=(n,digits=2)=>n==null?'—':new Intl.NumberFormat('hy-AM',{maximumFractionDigits:digits}).format(n);
const topics={irrigation_stress:'Ոռոգման սթրեսի հնարավոր նշաններ',degradation:'Հողերի դեգրադացիայի հնարավոր նշաններ',activity:'2026 ակտիվություն',history:'Պատմություն՝ ըստ տարիների',history_summary:'2021–2025 պատմություն',use_type:'Օգտագործման տեսակ',cycles:'Տարեկան ցիկլեր',potential:'Ներուժ',consolidation:'Կոնսոլիդացիայի հնարավորություն',parcel:'Հողամաս'};
const scopes={lower_hrazdan:'Ստորին Հրազդան I + II',stage_1:'Ստորին Հրազդան I',stage_2:'Ստորին Հրազդան II'};
const phases={understanding:'Հարցը դիտարկվում է…',calculating:'Տվյալները հաշվարկվում են…',explaining:'Պատասխանը պատրաստվում է…'};
const overlayIds=['agent-selection-inner','agent-selection-expansion','agent-selection-cadastre'];

function ResultCard({result,onMap,onExport,ready}) {
  const [metric,setMetric]=useState('official_area_ha');
  const [chart,setChart]=useState(false);
  const s=result.summary;
  const rows=result.rows||[];
  const chartRows=rows.slice(0,12);
  const max=Math.max(1,...chartRows.map(r=>r[metric]||0));
  return <article className="agent-result" aria-label="Հաշվարկի արդյունք">
    <strong>{topics[result.query.topic]||'Արդյունք'}</strong>
    <p className="agent-result-scope">{result.query.code||scopes[result.query.scope]}{result.query.communities?.length>0?' · '+result.query.communities.join(', '):''}{result.query.include_expansion?' · հարակից 1 կմ':''}</p>
    <p className="agent-period">{result.observation_date?`Դիտարկում՝ ${result.observation_date}`:`${result.years?.join(', ')}`}</p>
    <div className="agent-metrics">
      <span><b>{format(s.parcel_count,0)}</b> հողամաս</span>
      <span><b>{format(s.official_area_ha)}</b> հա ըստ կադաստրի</span>
      {s.observed_active_area_ha!=null&&<span><b>{format(s.observed_active_area_ha)}</b> հա դիտվող ակտիվություն</span>}
      {s.group_count!=null&&<span><b>{format(s.group_count,0)}</b> խումբ</span>}
    </div>
    <div className="agent-result-actions">
      <button type="button" disabled={!ready||!result.map_available} onClick={()=>onMap(result)}>Ցույց տալ քարտեզում</button>
      {rows.length>0&&<button type="button" aria-expanded={chart} onClick={()=>setChart(!chart)}>Գծապատկեր</button>}
      <button type="button" onClick={()=>onExport(result)}>Ներբեռնել Excel</button>
    </div>
    {chart&&<div className="agent-chart">
      <select aria-label="Գծապատկերի չափանիշ" value={metric} onChange={e=>setMetric(e.target.value)}>
        <option value="official_area_ha">Կադաստրային մակերես (հա)</option><option value="parcel_count">Հողամասերի քանակ</option>
        {s.observed_active_area_ha!=null&&<option value="observed_active_area_ha">Դիտվող ակտիվ մակերես (հա)</option>}
      </select>
      {chartRows.map(r=><div className="agent-bar-row" key={r.key}><span>{r.label}</span><div className="agent-bar-track"><i style={{width:`${(r[metric]||0)/max*100}%`}} /></div><b>{format(r[metric],metric==='parcel_count'?0:2)}</b></div>)}
      {rows.length>12&&<small>Առաջին 12 տողերը․ ամբողջը՝ աղյուսակում։</small>}
    </div>}
    {rows.length>0&&<details><summary>Տվյալների աղյուսակ ({rows.length})</summary><div className="agent-table-wrap"><table>
      <thead><tr><th>Խումբ / տարի</th><th>Հողամաս</th><th>Կադաստրային հա</th>{s.observed_active_area_ha!=null&&<th>Ակտիվ հա</th>}</tr></thead>
      <tbody>{rows.map(r=><tr key={r.key}><td>{r.label}</td><td>{format(r.parcel_count,0)}</td><td>{format(r.official_area_ha)}</td>{s.observed_active_area_ha!=null&&<td>{format(r.observed_active_area_ha)}</td>}</tr>)}</tbody>
    </table></div></details>}
    {result.query.topic==='degradation'&&<p className="agent-note">Հնարավոր նշաններ․ հողի դեգրադացիան հաստատված չէ։ Չգնահատված՝ {format(s.assessment_unassessed_parcels,0)} հողամաս · {format(s.assessment_unassessed_official_area_ha)} հա։</p>}
    {result.query.topic==='irrigation_stress'&&<p className="agent-note">Հնարավոր նշաններ՝ 05.09.2026-ի դրությամբ։ Եղանակը՝ ECMWF մոդելի գնահատականներ։ Անհրաժեշտ է տեղում ստուգում։ Հեկտարները ամբողջ հողամասերինն են, ոչ սթրեսի հատվածի չափում։ Չգնահատված՝ {format(s.assessment_unassessed_parcels,0)} հողամաս · {format(s.assessment_unassessed_official_area_ha)} հա։</p>}
    {result.query.topic==='history'&&<p className="agent-note">Տարեկան մակերեսները չեն գումարվում․ ամփոփումը եզակի հողամասերն է։</p>}
    {s.unresolved_parcels>0&&<p className="agent-note">Չորոշված կամ լրացուցիչ ստուգման ենթակա՝ {format(s.unresolved_parcels,0)} հողամաս · {format(s.unresolved_official_area_ha)} կադաստրային հա։</p>}
    {['potential','consolidation'].includes(result.query.topic)&&<p className="agent-note">Նախնական թեկնածուներ․ իրագործելիությունը հաստատված չէ։</p>}
    {result.query.communities?.length>0&&<p className="agent-note">Միայն ընտրված դիտարկվող տարածքի ներսում։ Հողամասերի ամբողջ կադաստրային մակերեսը։</p>}
  </article>;
}

export default function AgentPanel({map,ready,scope,mode,includeExpansion,selectedCode}) {
  const [open,setOpen]=useState(false),[text,setText]=useState(''),[messages,setMessages]=useState([]),[job,setJob]=useState(null),[phase,setPhase]=useState(''),[error,setError]=useState(''),[selected,setSelected]=useState(false),[sending,setSending]=useState(false);
  const token=useRef(null),camera=useRef(null),end=useRef(null),input=useRef(null);
  const api=async(path,body)=>{
    const response=await fetch('/api/agent/'+path,{method:body===undefined?'GET':'POST',headers:{...(body===undefined?{}:{'Content-Type':'application/json'}),...(token.current?{'X-Agent-Session':token.current}:{})},body:body===undefined?undefined:JSON.stringify(body)});
    if(!response.ok){const v=await response.json().catch(()=>({}));if(response.status===403)token.current=null;throw new Error(v.error||'Կապի սխալ։ Փորձեք կրկին։');}
    return response;
  };
  useEffect(()=>{if(open)input.current?.focus();},[open]);
  useEffect(()=>{end.current?.scrollIntoView({block:'nearest'});},[messages,phase,open]);
  useEffect(()=>{
    if(!job)return;
    let stopped=false, timer;
    async function poll(){
      try{
        const value=await (await api('job/'+job)).json();if(stopped)return;
        setPhase(value.phase);
        if(value.state==='running'){timer=setTimeout(poll,900);return;}
        setJob(null);setPhase('');
        if(value.state==='complete')setMessages(m=>[...m,{role:'assistant',text:value.answer,results:value.results}]);
        else if(value.state==='error'){setError(value.error);if(value.results?.length)setMessages(m=>[...m,{role:'assistant',text:'Հաշվարկված տվյալներ',results:value.results}]);}
      }catch(e){if(!stopped){setError(e.message);setJob(null);setPhase('');}}
    }
    poll();return()=>{stopped=true;clearTimeout(timer);};
  },[job]);
  async function send(e){
    e.preventDefault();if(!text.trim()||job||sending)return;
    const question=text.trim();setSending(true);setError('');
    try{
      if(!token.current){const s=await(await api('session',{})).json();token.current=s.token;if(!s.configured)throw new Error('AI կապը դեռ կարգավորված չէ։');}
      const language=/[Ա-ֆ]/.test(question)?'hy':/[А-Яа-яЁё]/.test(question)?'ru':'en';
      const value=await(await api('message',{text:question,context:{scope,mode,include_expansion:includeExpansion,selected_code:selectedCode||null,language}})).json();
      setMessages(m=>[...m,{role:'user',text:question}]);setText('');setJob(value.job_id);setPhase('understanding');
    }catch(e){setError(e.message);}finally{setSending(false);}
  }
  async function cancel(){try{await api('cancel',{job_id:job});setJob(null);setPhase('');}catch(e){setError(e.message);}}
  function clearSelection(){
    if(map){for(const id of overlayIds)if(map.getLayer(id))map.removeLayer(id);if(camera.current)map.jumpTo(camera.current);}
    camera.current=null;setSelected(false);
  }
  async function showMap(result){
    try{
      setError('');if(!map||!ready)throw new Error('Քարտեզը դեռ բեռնվում է։');
      const data=await(await api('result/'+result.result_id+'/map')).json();
      if(!data.ids.length)return;
      if(!camera.current)camera.current={center:map.getCenter(),zoom:map.getZoom(),bearing:map.getBearing(),pitch:map.getPitch()};
      for(const id of overlayIds)if(map.getLayer(id))map.removeLayer(id);
      const sources=[['agent-selection-inner','land-analytics','land_analytics'],['agent-selection-expansion','potential-expansion','potential_expansion'],['agent-selection-cadastre','cadastre','parcels']];
      for(const [id,source,layer] of sources){
        if(!map.getSource(source))continue;
        map.addLayer({id,type:'line',source,'source-layer':layer,minzoom:source==='cadastre'?14:10,filter:['in',source==='cadastre'?['get','cadastre_code']:['id'],['literal',source==='cadastre'?data.codes:data.ids]],paint:{'line-color':'#e9fcff','line-width':2.8,'line-opacity':1}},'cadastre-hit');
      }
      setSelected(true);map.fitBounds([[data.bbox[0],data.bbox[1]],[data.bbox[2],data.bbox[3]]],{padding:window.innerWidth>900?{left:440,right:350,top:105,bottom:65}:50,maxZoom:17,duration:600});
    }catch(e){setError(e.message);}
  }
  async function exportExcel(result){try{setError('');const response=await api('result/'+result.result_id+'/xlsx');const url=URL.createObjectURL(await response.blob());const a=document.createElement('a');a.href=url;a.download='land-analysis.xlsx';a.click();setTimeout(()=>URL.revokeObjectURL(url),1000);}catch(e){setError(e.message);}}
  return <>
    {!open&&<button className="agent-launch" type="button" onClick={()=>setOpen(true)}>✦ AI օգնական</button>}
    {selected&&!open&&<button className="agent-clear-floating" type="button" onClick={clearSelection}>Մաքրել AI ընտրությունը</button>}
    {open&&<aside className="agent-panel" aria-label="AI օգնական">
      <header><div><strong>✦ AI օգնական</strong><small>{scopes[scope]}</small></div><button type="button" aria-label="Փակել AI օգնականը" onClick={()=>setOpen(false)}>×</button></header>
      <div className="agent-conversation" aria-live="polite">
        {messages.length===0&&<div className="agent-welcome"><h2>Հարցրեք ձեր տարածքի մասին</h2><p>Հողամասեր ու հեկտարներ, համեմատություններ, գծապատկերներ և քարտեզային ընտրություն։</p><p>Հաշվարկները՝ պատրաստված տվյալներից։</p>{['Ակնալճում քանի՞ հեկտար է ակտիվ 2026-ին։','Բացատրի՛ր այս հարթակի կառուցվածքն ու բաժինները։','Համեմատիր համայնքների ներուժը։'].map(q=><button type="button" key={q} onClick={()=>{setText(q);input.current?.focus();}}>{q}</button>)}</div>}
        {messages.map((m,i)=><div key={i} className={'agent-message agent-message-'+m.role}><AgentMessage text={m.text}/>{m.results?.map(r=><ResultCard key={r.result_id} result={r} ready={ready} onMap={showMap} onExport={exportExcel}/>)}</div>)}
        {phase&&<p className="agent-progress" role="status">{phases[phase]||'Սպասեք…'}</p>}
        <div ref={end}/>
      </div>
      {selected&&<button className="agent-clear" type="button" onClick={clearSelection}>Մաքրել AI ընտրությունը և վերադարձնել տեսքը</button>}
      {error&&<p className="agent-error" role="alert">{error}</p>}
      <form onSubmit={send}><textarea ref={input} aria-label="Հարց AI օգնականին" placeholder="Հարցրեք հայերեն, English, русский…" value={text} maxLength={4000} rows={2} onChange={e=>setText(e.target.value)} onKeyDown={e=>{if(e.key==='Enter'&&!e.shiftKey){e.preventDefault();send(e);}}}/>{job?<button type="button" onClick={cancel}>Դադարեցնել</button>:<button type="submit" disabled={sending||!text.trim()}>{sending?'…':'Ուղարկել'}</button>}</form>
      <footer>Ստուգեք տարածքը, ժամանակահատվածը և դիտարկման սահմանափակումները։</footer>
    </aside>}
  </>;
}
