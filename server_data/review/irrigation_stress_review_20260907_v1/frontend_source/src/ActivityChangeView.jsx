import React, { useEffect, useRef, useState } from "react";
import maplibregl from "maplibre-gl";
import "./activityChange.css";

const SOURCE = "activity-change";
const LAYERS = ["activity-change-fill", "activity-change-line"];
export const CHANGE_LABELS = {
  became_active: "Ակտիվացած հողամասեր",
  became_inactive: "Ակտիվությունն այլևս չի դիտվում",
  insufficient: "Հինգ տարվա դիտարկումները բավարար չեն",
  excluded: "Բացառված է այս վերլուծությունից",
  not_selected: "Այս փոփոխությունը չի առանձնացվել",
};
const COLORS = { became_active: "#318cf0", became_inactive: "#ef454f" };
const number = (n, digits = 0) => new Intl.NumberFormat("hy-AM", {maximumFractionDigits: digits}).format(n || 0);

export function useActivityChange({map, ready, active, scope, onSelect, closePanel}) {
  const [payload, setPayload] = useState(null);
  const [status, setStatus] = useState("idle");
  const [attempt, setAttempt] = useState(0);
  const [tileError, setTileError] = useState(false);
  const [visible, setVisible] = useState({became_active: true, became_inactive: true});
  const [opacity, setOpacity] = useState(65);
  const current = useRef({active, onSelect});
  current.current = {active, onSelect};
  const selected = Object.keys(COLORS).filter(key => visible[key]);
  const summary = payload?.summaries[scope];

  useEffect(() => {
    if (!active || payload) return;
    const controller = new AbortController();
    setStatus("loading");
    fetch("/api/land/activity-change", {signal: controller.signal})
      .then(response => {if (!response.ok) throw new Error("Activity change unavailable"); return response.json();})
      .then(data => {setPayload(data); setStatus("ready");})
      .catch(error => {if (error.name !== "AbortError") setStatus("error");});
    return () => controller.abort();
  }, [active, payload, attempt]);

  useEffect(() => {
    if (!map || !ready || !payload) return;
    const delivery = payload.tile_delivery;
    if (!map.getSource(SOURCE)) {
      map.addSource(SOURCE, {type: "vector", tiles: [`${window.location.origin}${delivery.url}`],
        minzoom: delivery.min_zoom, maxzoom: delivery.max_zoom, bounds: delivery.bounds});
      const color = ["match", ["get", "change_class"], "became_active", COLORS.became_active, "became_inactive", COLORS.became_inactive, "transparent"];
      map.addLayer({id: LAYERS[0], type: "fill", source: SOURCE, "source-layer": "activity_change",
        layout: {visibility: "none"}, paint: {"fill-color": color, "fill-opacity": .65}}, "cadastre-hit");
      map.addLayer({id: LAYERS[1], type: "line", source: SOURCE, "source-layer": "activity_change",
        layout: {visibility: "none"}, paint: {"line-color": color, "line-width": 1.3}}, "cadastre-hit");
    } else map.getSource(SOURCE).reload();
    const click = event => {
      if (!current.current.active || !event.features?.length) return;
      if (map.queryRenderedFeatures(event.point, {layers: ["cadastre-hit"]}).length) return;
      const p = event.features[0].properties;
      current.current.onSelect({cadastreCode: p.cadastre_code, areaHa: p.area_ha}, event.lngLat);
    };
    const error = event => {if (event.sourceId === SOURCE || String(event.error?.message || "").includes("/data/activity_change/")) setTileError(true);};
    map.on("click", LAYERS[0], click);
    map.on("error", error);
    return () => {map.off("click", LAYERS[0], click); map.off("error", error);};
  }, [map, ready, payload]);

  useEffect(() => {
    if (!map || !ready || !payload || !map.getSource(SOURCE)) return;
    const filter = ["all", ["in", ["get", "change_class"], ["literal", selected]]];
    if (scope !== "lower_hrazdan") filter.push(["==", ["get", "stage"], scope]);
    for (const id of LAYERS) {
      map.setFilter(id, filter);
      map.setLayoutProperty(id, "visibility", active && selected.length ? "visible" : "none");
    }
    map.setPaintProperty(LAYERS[0], "fill-opacity", opacity / 100);
  }, [map, ready, payload, active, scope, visible, opacity]);

  const count = selected.reduce((n, key) => n + (summary?.classes[key].parcel_count || 0), 0);
  const area = selected.reduce((n, key) => n + (summary?.classes[key].area_ha || 0), 0);
  const focus = () => {
    if (!map || !ready || !count) return;
    const bounds = new maplibregl.LngLatBounds();
    selected.forEach(key => {const b = payload.bounds[scope][key]; if (b) {bounds.extend(b.slice(0,2)); bounds.extend(b.slice(2));}});
    if (window.innerWidth <= 640) closePanel();
    map.fitBounds(bounds, {maxZoom: 16.5, duration: 500, padding: window.innerWidth > 720
      ? {top:120, right:360, bottom:70, left:80} : {top:180, right:35, bottom:90, left:35}});
  };
  return {status, tileError, summary, visible, setVisible, opacity, setOpacity, count, area, focus, canFocus: ready && count > 0,
    retry: () => {setTileError(false); setPayload(null); setAttempt(n => n + 1);}};
}

export default function ActivityChangeView({model}) {
  return <div className="activity-change-section" role="tabpanel" aria-labelledby="land-tab-activity_change">
    <p className="activity-change-period">2021–2025</p>
    {model.status === "loading" && <p role="status">Տվյալները բեռնվում են…</p>}
    {(model.status === "error" || model.tileError) && <div role="alert"><p>Շերտը ժամանակավորապես անհասանելի է։</p><button type="button" onClick={model.retry}>Կրկին փորձել</button></div>}
    {model.status === "ready" && <>
      {Object.entries(COLORS).map(([key, color]) => <label className="layer-option layer-option--land activity-change-class" key={key}>
        <input type="checkbox" checked={model.visible[key]} onChange={e => model.setVisible(value => ({...value, [key]: e.target.checked}))} />
        <span className="land-class-swatch" style={{background: color}} aria-hidden="true" />
        <span>{CHANGE_LABELS[key]}<small>{number(model.summary.classes[key].parcel_count)} հողամաս · {number(model.summary.classes[key].area_ha, 2)} հա</small></span>
      </label>)}
      <dl className="activity-summary" aria-label="Մշակման փոփոխության արդյունք">
        <div><dt>Ցուցադրվող հողամասեր</dt><dd>{number(model.count)}</dd></div>
        <div><dt>Կադաստրային մակերես</dt><dd>{number(model.area, 2)} հա</dd></div>
      </dl>
      <label className="opacity-control"><span>Թափանցիկություն</span><output>{model.opacity}%</output>
        <input type="range" min="20" max="85" step="5" aria-label="Մշակման փոփոխության թափանցիկություն" value={model.opacity} onChange={e => model.setOpacity(Number(e.target.value))} /></label>
      <button className="activity-change-focus" type="button" disabled={!model.canFocus} onClick={model.focus}>Ցույց տալ հողամասերը</button>
      {!model.count && <p role="status">Այս ընտրությամբ հողամասեր չեն ցուցադրվում։</p>}
      <p className="activity-preview-status">Ոչ բավարար դիտարկումներ՝ {number(model.summary.insufficient.parcel_count)} հողամաս · {number(model.summary.insufficient.area_ha, 2)} հա։</p>
      <p className="activity-preview-status">Դիտվող փոփոխություն․ հողի մշտական լքված լինելը հաստատված չէ։</p>
    </>}
  </div>;
}
