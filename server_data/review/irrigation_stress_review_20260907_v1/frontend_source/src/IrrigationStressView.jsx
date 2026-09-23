import React, {useEffect,useRef,useState} from "react";
import AreaMetric from "./AreaMetric";
import "./irrigationStress.css";
const COLOR="#dc3038", SOURCE="irrigation-stress-candidates";
const LAYERS=["irrigation-stress-fill","irrigation-stress-line"];
export function appendIrrigationStressInfo(content,result) {
  const texts={candidate:"Ոռոգման սթրեսի հնարավոր նշաններ",unassessed:"Տվյալները բավարար չեն գնահատման համար",not_flagged:"Գնահատմամբ նշան չի առանձնացվել",not_current_vegetation:"Վերջին դիտարկումներով աճող բուսականությունը չի հաստատվել",outside_mask:"Այս գնահատման շրջանակից դուրս"};
  const state=result?.state||"outside_mask";
  const add=text=>{const p=document.createElement("p");p.textContent=text;content.appendChild(p);};
  add(texts[state]||texts.outside_mask);
  if(result?.date)add(`Վերջին դիտարկում՝ ${result.date}`);
  if(state!=="outside_mask")add("Նախնական գնահատում․ ջրի պակասը հաստատված չէ։ Եղանակը՝ ECMWF մոդելի գնահատականներ։ Հարևան հողամասերը կարող են կիսել արբանյակային ազդանշանը։ Անհրաժեշտ է տեղում ստուգում։");
}
export function useIrrigationStress({map,ready,active,scope,closePanel}) {
  const [payload,setPayload]=useState(null),[status,setStatus]=useState("idle"),[attempt,setAttempt]=useState(0);
  const [visible,setVisible]=useState(true),[opacity,setOpacity]=useState(70),[tileError,setTileError]=useState(false);
  const [zoom,setZoom]=useState(Infinity);
  const activeRef=useRef(active);activeRef.current=active&&visible;
  const summary=payload?.scopes[scope];
  useEffect(()=>{if(!map||!ready)return;const update=()=>setZoom(map.getZoom());update();map.on("zoomend",update);return()=>map.off("zoomend",update);},[map,ready]);
  useEffect(()=>{
    if(!active||payload)return;
    const control=new AbortController();setStatus("loading");
    Promise.all(["/api/land/irrigation-stress","/api/land/delivery"].map(url=>fetch(url,{signal:control.signal}).then(r=>{if(!r.ok)throw Error("Unavailable");return r.json();})))
      .then(([data,delivery])=>{setPayload({...data,tile_delivery:delivery.tile_delivery});setStatus("ready");})
      .catch(e=>{if(e.name!=="AbortError")setStatus("error");});
    return()=>control.abort();
  },[active,payload,attempt]);
  useEffect(()=>{
    if(!map||!ready||!payload)return;
    if(!map.getSource(SOURCE)){
      const d=payload.tile_delivery;
      map.addSource(SOURCE,{type:"vector",tiles:[`${window.location.origin}${d.url}`],minzoom:d.min_zoom,maxzoom:d.max_zoom,bounds:d.bounds});
      for(const layer of [
        {id:LAYERS[0],type:"fill",paint:{"fill-color":COLOR,"fill-opacity":0}},
        {id:LAYERS[1],type:"line",paint:{"line-color":COLOR,"line-width":1.8,"line-opacity":.95}}
      ])map.addLayer({...layer,source:SOURCE,"source-layer":"land_analytics",layout:{visibility:"none"}},"cadastre-hit");
    }else map.getSource(SOURCE).reload();
    const fail=e=>{if(e.sourceId===SOURCE)setTileError(true);};
    const click=e=>{if(activeRef.current && map.getZoom()<14)map.flyTo({center:e.lngLat,zoom:14.6,duration:500});};
    const hover=()=>{if(activeRef.current)map.getCanvas().style.cursor="pointer";};
    const leave=()=>{map.getCanvas().style.cursor="";};
    map.on("error",fail);map.on("click",LAYERS[0],click);map.on("mouseenter",LAYERS[0],hover);map.on("mouseleave",LAYERS[0],leave);
    return()=>{map.off("error",fail);map.off("click",LAYERS[0],click);map.off("mouseenter",LAYERS[0],hover);map.off("mouseleave",LAYERS[0],leave);};
  },[map,ready,payload]);
  useEffect(()=>{
    if(!map||!ready||!summary||!map.getLayer(LAYERS[0]))return;
    for(const id of LAYERS){map.setFilter(id,["in",["id"],["literal",summary.ids]]);map.setLayoutProperty(id,"visibility",active&&visible&&summary.ids.length?"visible":"none");}
    map.setPaintProperty(LAYERS[0],"fill-opacity",opacity/100);
  },[map,ready,summary,active,visible,opacity]);
  const focus=()=>{
    if(!ready||!map||!summary?.bbox||!visible)return;
    if(window.innerWidth<=640)closePanel();
    const b=summary.bbox;const camera=map.cameraForBounds([b.slice(0,2),b.slice(2)],{padding:window.innerWidth>720?{top:110,bottom:70,left:70,right:380}:20,maxZoom:15});
    if(camera)map.easeTo({...camera,zoom:Math.max(camera.zoom,payload.tile_delivery.min_zoom),duration:500});
  };
  return {summary,status,visible,setVisible,opacity,setOpacity,tileError,focus,canFocus:ready&&visible,zoomTooLow:zoom<(payload?.tile_delivery.min_zoom||10),
    retry:()=>{setPayload(null);setTileError(false);setAttempt(n=>n+1);}};
}

export default function IrrigationStressView({model:m}) {
  return <div className="irrigation-stress-section" role="tabpanel" aria-labelledby="land-tab-irrigation_stress">
    {m.status==="loading"&&<p role="status">Տվյալները բեռնվում են…</p>}
    {(m.status==="error"||m.tileError)&&<div role="alert"><p>Շերտը ժամանակավորապես անհասանելի է։</p><button type="button" onClick={m.retry}>Կրկին փորձել</button></div>}
    {m.status==="ready"&&m.summary&&<>
      <p className="activity-preview-status">2026 թ․ · Գնահատում՝ սեպտեմբերի 5-ի դրությամբ։ Տնամերձներն ու ճանապարհները բացառված են։</p>
      <label className="layer-option layer-option--land irrigation-stress-class"><input type="checkbox" checked={m.visible} onChange={e=>m.setVisible(e.target.checked)}/><span className="land-class-swatch" style={{background:COLOR}} aria-hidden="true"/><span>Ոռոգման սթրեսի հնարավոր նշաններ</span></label>
      <dl className="activity-summary" aria-label="Ոռոգման սթրեսի գնահատում">
        {[["candidate","Հնարավոր նշաններով"],["included","Ստուգման տարածքի հողամասեր"],["assessed","Բավարար համեմատությամբ գնահատված"],["unassessed","Չգնահատված"],["not_flagged","Գնահատմամբ նշան չի առանձնացվել"],["not_current_vegetation","Վերջին դիտարկումներով աճող բուսականությունը չի հաստատվել"]].map(([key,title])=><div key={key} data-metric={key}><dt>{title}</dt><dd><AreaMetric count={m.summary[key].count} area={m.summary[key].area_ha}/></dd></div>)}
      </dl>
      {m.summary.date_from&&<p className="activity-preview-status">Առանձնացված հողամասերի վերջին դիտարկումները՝ {m.summary.date_from} – {m.summary.date_to}։</p>}
      <label className="opacity-control"><span>Թափանցիկություն</span><output>{m.opacity}%</output><input type="range" aria-label="Ոռոգման սթրեսի շերտի թափանցիկություն" min="20" max="85" step="5" value={m.opacity} onChange={e=>m.setOpacity(Number(e.target.value))}/></label>
      <button className="consolidation-focus irrigation-stress-focus" type="button" disabled={!m.canFocus||!m.summary.ids.length} onClick={m.focus}>Ցույց տալ հողամասերը</button>
      {m.zoomTooLow&&<p role="status">Հողամասերը տեսնելու համար մոտեցրեք քարտեզը կամ սեղմեք «Ցույց տալ հողամասերը»։</p>}
      <p className="activity-preview-status">Աճող բուսականության խոնավության կրկնվող նվազման հնարավոր նշաններ։ Եղանակը՝ ECMWF մոդելի գնահատականներ։ Ջրի պակասը և դրա պատճառը հաստատված չեն․ անհրաժեշտ է տեղում ստուգում։</p>
      <p className="activity-preview-status">Հեկտարները ամբողջ կադաստրային մակերեսն են, ոչ սթրեսի ենթարկված հատվածի չափում։ Հարևան հողամասերը կարող են կիսել արբանյակային ազդանշանը։ Չգունավորված հողամասը չի նշանակում սթրեսի բացակայություն։</p>
    </>}
  </div>;
}
