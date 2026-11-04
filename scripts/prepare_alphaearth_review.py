"""Add an AlphaEarth groups tab on top of the live eight-section product (preview only).

Copies the live irrigation-stress frontend source and map data into new versioned folders, writes the
public summary and per-parcel lookup, and adds the ninth tab. One colour per parcel: the group that covers
at least half of the parcel's official-geometry area, otherwise "mixed". Public names are neutral
pattern names (no crop names), per the owner's decision of 2026-10-30. Never overwrites anything.
"""
from pathlib import Path
import json
import shutil
import sys

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
SLUG = "alphaearth_review_20261030_v1"
BASE = "irrigation_stress_review_20260907_v1"
CANDIDATE = json.loads((ROOT / "config/alphaearth_groups_20261030_v1.json").read_text())
SOURCE = ROOT / "server_data/review" / SLUG / "frontend_source"
DIST = ROOT / "output" / f"frontend_{SLUG}"
TITLE = "AlphaEarth խմբեր"
MAJORITY = .5
CLASSES = [
    {"id": "g0", "group": 0, "label": "Ամբողջ սեզոնին կանաչ", "label_en": "Green all season", "color": "#1f8a4c"},
    {"id": "g6", "group": 6, "label": "Սեզոնի մեծ մասում կանաչ շերտեր", "label_en": "Green strips most of the season", "color": "#e0663d"},
    {"id": "g1", "group": 1, "label": "Գարնանը կանաչ, ամռան սկզբին հավաքված", "label_en": "Green in spring, harvested by early summer", "color": "#e0a526"},
    {"id": "g4", "group": 4, "label": "Ուշ սեզոնին կանաչ", "label_en": "Late-season green", "color": "#3a7fc4"},
    {"id": "g2", "group": 2, "label": "Գարնանը թույլ կանաչ, ամռանը չոր", "label_en": "Weak spring green, dry in summer", "color": "#b5c25e"},
    {"id": "g3", "group": 3, "label": "Ամբողջ տարին մերկ հող", "label_en": "Bare land all year", "color": "#b08d6e"},
    {"id": "g5", "group": 5, "label": "Կառուցապատված միջավայր", "label_en": "Built-up surroundings", "color": "#7d5ba6"},
    {"id": "mixed", "group": None, "label": "Խառը՝ ոչ մի խումբ չի ծածկում հողամասի կեսը", "label_en": "Mixed: no group covers half the parcel", "color": "#9aa3a0"},
]


def replace(text, old, new):
    assert old in text, old
    return text.replace(old, new)


def data():
    parcels = pd.read_parquet(ROOT / CANDIDATE["outputs"]["data"] / "parcels_2025/parcels.parquet")
    ids = pd.read_parquet(ROOT / "data/analysis/parcel_eo/current_halo_v1/current_parcels.parquet",
                          columns=["internal_parcel_id", "public_parcel_id"])
    frame = parcels.merge(ids, on="internal_parcel_id", how="left", validate="one_to_one")
    if frame.public_parcel_id.isna().any() or len(frame) != CANDIDATE["expected_eligible"]:
        raise ValueError("Every eligible parcel needs its public map id")
    by_group = {c["group"]: c["id"] for c in CLASSES if c["group"] is not None}
    frame["map_class"] = np.where(frame.group_share >= MAJORITY, frame.group.map(by_group), "mixed")
    import sqlite3
    with sqlite3.connect((ROOT / "server_data/agent_v7/parcels.sqlite3").as_uri() + "?mode=ro", uri=True) as con:
        box = pd.read_sql_query("SELECT cadastre_code, minx, miny, maxx, maxy FROM parcels", con)
    frame = frame.merge(box, on="cadastre_code", how="left", validate="one_to_one")
    payload = {"version": SLUG, "candidate": CANDIDATE["version"], "year": 2025, "credit": CANDIDATE["credit"],
               "rule": "majority_of_parcel_area", "classes": CLASSES, "scopes": {}}
    for scope in ["lower_hrazdan", "stage_1", "stage_2"]:
        part = frame if scope == "lower_hrazdan" else frame[frame.activity_stage == scope]
        summary = {"included": {"count": len(part), "area_ha": round(float(part.area_official_m2.sum() / 1e4), 6)},
                   "classes": {}, "bbox": [float(part.minx.min()), float(part.miny.min()), float(part.maxx.max()), float(part.maxy.max())]}
        for c in CLASSES:
            member = part[part.map_class == c["id"]]
            summary["classes"][c["id"]] = {"count": len(member), "area_ha": round(float(member.area_official_m2.sum() / 1e4), 6),
                                           "ids": sorted(int(v) for v in member.public_parcel_id)}
        assert sum(v["count"] for v in summary["classes"].values()) == len(part)
        payload["scopes"][scope] = summary
    lookup = {r.cadastre_code: r.map_class for r in frame.itertuples()}
    return payload, lookup


VIEW = r'''import React, {useEffect,useRef,useState} from "react";
import AreaMetric from "./AreaMetric";
import "./alphaEarth.css";
const SOURCE="alphaearth-groups", LAYERS=["alphaearth-fill","alphaearth-line"];
export function appendAlphaEarthInfo(content,value,classes) {
  const add=(text,className)=>{const p=document.createElement("p");p.textContent=text;if(className)p.className=className;content.appendChild(p);};
  const found=(classes||[]).find(c=>c.id===value);
  if(!found){add("Այս հողամասը AlphaEarth խմբավորման շրջանակից դուրս է");return;}
  add(`AlphaEarth խումբ (2025)՝ ${found.label}`);
  add("Նախնական խմբավորում․ ճշգրտությունը դեռ ստուգված չէ։ Մանր հողամասերում ազդանշանը կարող է արտացոլել հարևան տարածքը։","parcel-popup-review");
}
export function useAlphaEarth({map,ready,active,scope,closePanel,onClasses}) {
  const [payload,setPayload]=useState(null),[status,setStatus]=useState("idle"),[attempt,setAttempt]=useState(0);
  const [hidden,setHidden]=useState([]),[opacity,setOpacity]=useState(70),[tileError,setTileError]=useState(false);
  const [zoom,setZoom]=useState(Infinity);
  const activeRef=useRef(active);activeRef.current=active;
  const summary=payload?.scopes[scope];
  useEffect(()=>{if(!map||!ready)return;const update=()=>setZoom(map.getZoom());update();map.on("zoomend",update);return()=>map.off("zoomend",update);},[map,ready]);
  useEffect(()=>{
    if(!active||payload)return;
    const control=new AbortController();setStatus("loading");
    Promise.all(["/api/land/alphaearth","/api/land/delivery"].map(url=>fetch(url,{signal:control.signal}).then(r=>{if(!r.ok)throw Error("Unavailable");return r.json();})))
      .then(([data,delivery])=>{setPayload({...data,tile_delivery:delivery.tile_delivery});onClasses?.(data.classes);setStatus("ready");})
      .catch(e=>{if(e.name!=="AbortError")setStatus("error");});
    return()=>control.abort();
  },[active,payload,attempt]);
  useEffect(()=>{
    if(!map||!ready||!payload)return;
    if(!map.getSource(SOURCE)){
      const d=payload.tile_delivery;
      map.addSource(SOURCE,{type:"vector",tiles:[`${window.location.origin}${d.url}`],minzoom:d.min_zoom,maxzoom:d.max_zoom,bounds:d.bounds});
      for(const layer of [
        {id:LAYERS[0],type:"fill",paint:{"fill-opacity":0}},
        {id:LAYERS[1],type:"line",paint:{"line-width":1.1,"line-opacity":.9}}
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
    const shown=payload.classes.filter(c=>!hidden.includes(c.id)&&summary.classes[c.id].ids.length);
    const color=["match",["id"]];
    for(const c of shown)color.push(summary.classes[c.id].ids,c.color);
    color.push("#000000");
    const ids=shown.flatMap(c=>summary.classes[c.id].ids);
    for(const id of LAYERS){
      map.setFilter(id,["in",["id"],["literal",ids]]);
      map.setLayoutProperty(id,"visibility",active&&ids.length?"visible":"none");
    }
    if(ids.length){map.setPaintProperty(LAYERS[0],"fill-color",shown.length>1?color:shown[0].color);map.setPaintProperty(LAYERS[1],"line-color",shown.length>1?color:shown[0].color);}
    map.setPaintProperty(LAYERS[0],"fill-opacity",opacity/100);
  },[map,ready,payload,summary,active,hidden,opacity]);
  const focus=()=>{
    if(!ready||!map||!summary?.bbox)return;
    if(window.innerWidth<=640)closePanel();
    const b=summary.bbox;const camera=map.cameraForBounds([b.slice(0,2),b.slice(2)],{padding:window.innerWidth>720?{top:110,bottom:70,left:70,right:380}:20,maxZoom:15});
    if(camera)map.easeTo({...camera,zoom:Math.max(camera.zoom,payload.tile_delivery.min_zoom),duration:500});
  };
  const toggle=id=>setHidden(h=>h.includes(id)?h.filter(v=>v!==id):[...h,id]);
  return {payload,summary,status,hidden,toggle,opacity,setOpacity,tileError,focus,canFocus:ready,zoomTooLow:zoom<(payload?.tile_delivery.min_zoom||10),
    retry:()=>{setPayload(null);setTileError(false);setAttempt(n=>n+1);}};
}

export default function AlphaEarthView({model:m}) {
  return <div className="alphaearth-section" role="tabpanel" aria-labelledby="land-tab-alphaearth">
    {m.status==="loading"&&<p role="status">Տվյալները բեռնվում են…</p>}
    {(m.status==="error"||m.tileError)&&<div role="alert"><p>Շերտը ժամանակավորապես անհասանելի է։</p><button type="button" onClick={m.retry}>Կրկին փորձել</button></div>}
    {m.status==="ready"&&m.summary&&<>
      <p className="activity-preview-status">2025 թ․ · Տնամերձներն ու ճանապարհները բացառված են։ Յուրաքանչյուր հողամաս ունի մեկ գույն՝ այն խումբը, որը ծածկում է նրա մակերեսի առնվազն կեսը։</p>
      <div className="alphaearth-classes">
        {m.payload.classes.map(c=>{const v=m.summary.classes[c.id];return <label key={c.id} className="layer-option layer-option--land alphaearth-class">
          <input type="checkbox" checked={!m.hidden.includes(c.id)} onChange={()=>m.toggle(c.id)}/>
          <span className="land-class-swatch" style={{background:c.color}} aria-hidden="true"/>
          <span className="alphaearth-class-name">{c.label}</span>
          <span className="alphaearth-class-metric"><AreaMetric count={v.count} area={v.area_ha}/></span>
        </label>;})}
      </div>
      <label className="opacity-control"><span>Թափանցիկություն</span><output>{m.opacity}%</output><input type="range" aria-label="AlphaEarth շերտի թափանցիկություն" min="20" max="85" step="5" value={m.opacity} onChange={e=>m.setOpacity(Number(e.target.value))}/></label>
      <button className="consolidation-focus alphaearth-focus" type="button" disabled={!m.canFocus} onClick={m.focus}>Ցույց տալ հողամասերը</button>
      {m.zoomTooLow&&<p role="status">Հողամասերը տեսնելու համար մոտեցրեք քարտեզը կամ սեղմեք «Ցույց տալ հողամասերը»։</p>}
      <p className="activity-preview-status">Հիմնված է Google DeepMind-ի AlphaEarth Foundations արբանյակային ներկայացումների (embeddings) վրա։ Համակարգիչը խմբավորել է հողամասերը ըստ ամբողջ տարվա արբանյակային նմանության․ անունները նկարագրում են տեսանելի օրինաչափությունը, ոչ թե հաստատված մշակաբույսը։</p>
      <p className="activity-preview-status">Նախնական թեկնածու․ ճշգրտությունը դեռ ստուգված չէ և չի փոխարինում «Օգտագործման տեսակ» բաժինը։ Հեկտարները ամբողջ կադաստրային մակերեսն են։ Մանր հողամասերում ազդանշանը կարող է արտացոլել հարևան տարածքը։</p>
      <p className="activity-preview-status alphaearth-credit">{m.payload.credit}</p>
    </>}
  </div>;
}
'''

CSS = """.land-mode-grid button[data-mode="alphaearth"] {grid-column:1 / -1; min-height:48px; white-space:normal; overflow-wrap:anywhere; line-height:1.4; letter-spacing:0;}
.alphaearth-classes {display:grid; gap:6px; margin:8px 0;}
.alphaearth-class {display:grid; grid-template-columns:18px 14px minmax(0,1fr); column-gap:8px; row-gap:2px; align-items:center; min-height:44px;}
.alphaearth-class input {width:18px; height:18px; margin:0;}
.alphaearth-class-name {min-width:0; overflow-wrap:anywhere; line-height:1.4;}
.alphaearth-class-metric {grid-column:3; font-size:.9em; opacity:.85;}
.alphaearth-credit {font-style:italic;}
"""


def main():
    review = ROOT / "server_data/review" / SLUG
    if review.exists() or DIST.exists():
        raise SystemExit("This review version already exists; it is never overwritten.")
    payload, lookup = data()
    shutil.copytree(ROOT / "server_data/review" / BASE / "frontend_source", SOURCE)
    DIST.mkdir(parents=True)
    shutil.copytree(ROOT / "output" / f"frontend_{BASE}" / "data", DIST / "data")
    (review / "alphaearth.json").write_text(json.dumps(payload, ensure_ascii=False, allow_nan=False), encoding="utf-8")
    (review / "alphaearth_lookup.json").write_text(json.dumps(lookup, allow_nan=False), encoding="utf-8")
    v = SOURCE / "vite.config.mjs"
    v.write_text(replace(v.read_text(), f"frontend_{BASE}", f"frontend_{SLUG}"))
    (SOURCE / "src/AlphaEarthView.jsx").write_text(VIEW, encoding="utf-8")
    (SOURCE / "src/alphaEarth.css").write_text(CSS, encoding="utf-8")
    p = SOURCE / "src/App.jsx"
    s = p.read_text(encoding="utf-8")
    s = 'import AlphaEarthView, { appendAlphaEarthInfo, useAlphaEarth } from "./AlphaEarthView";\n' + s
    s = replace(s, '=== "irrigation-stress" ? "irrigation_stress" : "activity_2026";',
                '=== "irrigation-stress" ? "irrigation_stress"\n  : new URLSearchParams(window.location.search).get("view") === "alphaearth" ? "alphaearth" : "activity_2026";')
    s = replace(s, '  } else if (parcel.mode === "irrigation_stress") {',
                '  } else if (parcel.mode === "alphaearth") {\n    appendAlphaEarthInfo(content, parcel.alphaEarth, alphaEarthClasses);\n  } else if (parcel.mode === "irrigation_stress") {')
    s = replace(s, 'const CADASTRE_LAYERS = [', 'let alphaEarthClasses = [];\nconst CADASTRE_LAYERS = [')
    s = replace(s, '  const irrigationStress = useIrrigationStress(',
                '  const alphaEarth = useAlphaEarth({map: mapRef.current, ready: mapReady, active: landMode === "alphaearth", scope: landScope, closePanel: () => setLayerPanelOpen(false), onClasses: classes => { alphaEarthClasses = classes; }});\n  const irrigationStress = useIrrigationStress(')
    s = s.replace('"activity_change", "degradation", "irrigation_stress"]', '"activity_change", "degradation", "irrigation_stress", "alphaearth"]')
    s = replace(s, 'irrigationStress: analytics.irrigationStress,', 'irrigationStress: analytics.irrigationStress,\n          alphaEarth: analytics.alphaEarth,')
    s = replace(s, '{landMode === "irrigation_stress" && <IrrigationStressView model={irrigationStress} />}',
                '{landMode === "irrigation_stress" && <IrrigationStressView model={irrigationStress} />}\n              {landMode === "alphaearth" && <AlphaEarthView model={alphaEarth} />}')
    # The unchanged assistant knows the eight approved sections; on this tab it keeps the use-type context.
    s = replace(s, 'scope={landScope} mode={landMode}', 'scope={landScope} mode={landMode === "alphaearth" ? "land_use_type" : landMode}')
    p.write_text(s, encoding="utf-8")
    p = SOURCE / "src/landResourcesSchema.js"
    s = p.read_text(encoding="utf-8").rstrip()
    assert s.endswith("];")
    p.write_text(s[:-2] + f'  {{id:"alphaearth",label:"{TITLE}",classes:[]}},\n];\n', encoding="utf-8")
    sys.stdout.reconfigure(encoding="utf-8")
    for scope, summary in payload["scopes"].items():
        print(scope, summary["included"], {k: v["count"] for k, v in summary["classes"].items()})


def finish():
    """After `vite build`: carry over the approved readability stylesheet exactly as the live release does."""
    css = (ROOT / "ui/readability-20260907-v1.css").read_bytes()
    if (ROOT / "output" / f"frontend_{BASE}" / "assets/readability-20260907-v1.css").read_bytes() != css:
        raise ValueError("Live readability stylesheet differs from the approved one")
    target = DIST / "assets/readability-20260907-v1.css"
    if target.exists():
        raise SystemExit("Already finished.")
    target.write_bytes(css)
    page = DIST / "index.html"
    page.write_text(replace(page.read_text(encoding="utf-8"), "</head>",
                            '  <link rel="stylesheet" href="/assets/readability-20260907-v1.css">\n</head>'), encoding="utf-8")
    print("readability stylesheet linked")


if __name__ == "__main__":
    finish() if sys.argv[1:] == ["finish"] else main()
