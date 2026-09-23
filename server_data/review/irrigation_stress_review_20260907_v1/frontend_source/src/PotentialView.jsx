import React, { useEffect, useState } from "react";
import AreaMetric, { formatCountArea } from "./AreaMetric";
export const POTENTIAL_LABELS={gravity_candidate:"Ինքնահոս ոռոգման նախնական թեկնածուներ",mechanical_candidate:"Մեխանիկական ոռոգման նախնական թեկնածուներ",review:"Լրացուցիչ ստուգման ենթակա",excluded:"Բացառված է ներուժի գնահատումից",not_candidate:"Ներուժի թեկնածու չի առանձնացվել"};
const COLORS={gravity_candidate:"#5b8ff9",mechanical_candidate:"#d65c91",review:"#a3a9ae"};
const count=n=>new Intl.NumberFormat("hy-AM").format(n||0);

export default function PotentialView({map,ready,payload,scope,active,onExpansionChange}){
  const [visibility,setVisibility]=useState({gravity_candidate:true,mechanical_candidate:true,review:false});
  const [opacity,setOpacity]=useState(65);
  const [includeExpansion,setIncludeExpansion]=useState(true);
  useEffect(()=>{onExpansionChange?.(includeExpansion);},[includeExpansion,onExpansionChange]);
  const expansion=payload?.expansion;
  useEffect(()=>{
    if(!map||!ready||!payload||!map.getSource("land-analytics"))return;
    if(expansion&&!map.getSource("potential-expansion")){
      const d=expansion.tile_delivery;
      map.addSource("potential-expansion",{type:"vector",tiles:[`${window.location.origin}${d.url}`],minzoom:d.min_zoom,maxzoom:d.max_zoom,bounds:d.bounds});
    }
    const sources=[{source:"land-analytics",layer:"land_analytics",prefix:"land-potential",enabled:true}];
    if(expansion)sources.push({source:"potential-expansion",layer:"potential_expansion",prefix:"land-potential-expansion",enabled:includeExpansion});
    for(const s of sources){
      for(const [kind,color] of Object.entries(COLORS)){
        for(const type of ["fill","line"]){
          const id=`${s.prefix}-${kind}-${type}`;
          if(!map.getLayer(id))map.addLayer({id,type,source:s.source,"source-layer":s.layer,minzoom:10,layout:{visibility:"none"},paint:type==="fill"?{"fill-color":color,"fill-opacity":0}:{"line-color":color,"line-width":1.3,"line-opacity":0}},"cadastre-hit");
          const filter=["all",["==",["get","potential_class"],kind]];
          if(scope==="stage_1"||scope==="stage_2")filter.push(["==",["get","stage"],scope]);
          const show=active&&scope!=="wua"&&visibility[kind]&&s.enabled;
          map.setFilter(id,filter);map.setLayoutProperty(id,"visibility",show?"visible":"none");
          map.setPaintProperty(id,`${type}-opacity`,show?(type==="fill"?opacity/100:.95):0);
        }
      }
    }
  },[map,ready,payload,scope,active,visibility,opacity,includeExpansion,expansion]);
  if(!active)return null;
  const inner=payload?.summaries?.[scope];
  const outer=expansion?.summaries?.[scope];
  const summary=includeExpansion&&expansion?expansion.combined_summaries[scope]:inner;
  return <div className="activity-preview" aria-label="Ներուժի նախնական գնահատում">
    <label className="opacity-control opacity-control--activity"><span>Թափանցիկություն</span><output>{opacity}%</output><input aria-label="Ներուժի թափանցիկություն" type="range" min="20" max="85" step="5" value={opacity} onChange={e=>setOpacity(Number(e.target.value))}/></label>
    {expansion&&<label className="layer-option layer-option--land" style={{display:"flex",gap:8,alignItems:"center"}}><input type="checkbox" checked={includeExpansion} onChange={e=>setIncludeExpansion(e.target.checked)}/><span>Ներառել հարակից 1 կմ գոտին</span></label>}
    {!summary?<p>Տվյալները բեռնվում են…</p>:<>
      <dl className="activity-summary">
        <div><dt>Ուսումնասիրված հողամասեր</dt><dd><AreaMetric count={summary.eligible_parcel_count} area={summary.official_areas_ha?.eligible}/></dd></div>
        <div><dt>Ներուժի նախնական թեկնածուներ</dt><dd><AreaMetric count={summary.candidate_count} area={summary.official_areas_ha?.candidate}/></dd></div>
        <div><dt>Հիմնական տարածքում</dt><dd><AreaMetric count={inner?.candidate_count} area={inner?.official_areas_ha?.candidate}/></dd></div>
        {includeExpansion&&outer&&<div><dt>Հարակից 1 կմ գոտում</dt><dd><AreaMetric count={outer.candidate_count} area={outer.official_areas_ha?.candidate}/></dd></div>}
        <div><dt>Ինքնահոս</dt><dd><AreaMetric count={summary.class_counts.gravity_candidate} area={summary.official_areas_ha?.classes?.gravity_candidate}/></dd></div>
        <div><dt>Մեխանիկական</dt><dd><AreaMetric count={summary.class_counts.mechanical_candidate} area={summary.official_areas_ha?.classes?.mechanical_candidate}/></dd></div>
        <div><dt>Լրացուցիչ ստուգում</dt><dd><AreaMetric count={summary.class_counts.review} area={summary.official_areas_ha?.classes?.review}/></dd></div>
      </dl>
      <p className="activity-preview-status">2021–2025 պատմություն · 2026 դիտարկում՝ մինչև 23.08</p>
      <div className="land-class-list" aria-label="Ներուժի դասեր">{Object.entries(COLORS).map(([kind,color])=><label key={kind} className="layer-option layer-option--land"><input type="checkbox" checked={visibility[kind]} onChange={e=>setVisibility({...visibility,[kind]:e.target.checked})}/><span className="land-class-swatch" style={{backgroundColor:color}} aria-hidden="true"/><span>{POTENTIAL_LABELS[kind]} · {formatCountArea(summary.class_counts[kind], summary.official_areas_ha?.classes?.[kind])}</span></label>)}</div>
      <p className="activity-preview-status">{includeExpansion?"Ստորին Հրազդան I + II և հարակից 1 կմ որոնման գոտի։":"Ստորին Հրազդան I + II սահմաններում։"} Տնամերձ և ճանապարհային հողամասերը բացառված են։</p>
    </>}
  </div>;
}
