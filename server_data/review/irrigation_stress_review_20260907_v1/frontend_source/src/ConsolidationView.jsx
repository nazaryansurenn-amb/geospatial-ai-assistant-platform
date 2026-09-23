import React, { useEffect, useRef, useState } from "react";
import maplibregl from "maplibre-gl";
import AreaMetric from "./AreaMetric";
import "./consolidation.css";

export const CONSOLIDATION_TITLE = "Հողատարածքների կոնսոլիդացիայի հնարավորություն";
const SOURCE = "consolidation-candidates";
const LAYERS = ["consolidation-fill", "consolidation-line", "consolidation-selected"];
const COLOR = "#bd83f3";
const count = n => new Intl.NumberFormat("hy-AM").format(n || 0);
const area = n => new Intl.NumberFormat("hy-AM", { maximumFractionDigits: 2 }).format(n || 0);

export function appendConsolidationInfo(content, group) {
  const heading = document.createElement("p");
  heading.className = "parcel-popup-history-class";
  heading.textContent = group ? `Կոնսոլիդացիայի հնարավոր խումբ №${group.group_number}` : "Կոնսոլիդացիայի թեկնածու չի առանձնացվել";
  content.appendChild(heading);
  if (!group) return;
  for (const text of [
    `${count(group.parcel_count)} հողամաս · ${area(group.area_ha)} հա ընդհանուր մակերես`,
    "Նախնական թեկնածու․ գործնական հնարավորությունը հաստատված չէ։",
  ]) {
    const p = document.createElement("p");
    p.textContent = text;
    content.appendChild(p);
  }
}

export function useConsolidation({ map, ready, active, scope, closePanel }) {
  const [payload, setPayload] = useState(null);
  const [status, setStatus] = useState("idle");
  const [attempt, setAttempt] = useState(0);
  const [visible, setVisible] = useState(true);
  const [opacity, setOpacity] = useState(65);
  const [selected, setSelected] = useState(0);
  const [tileError, setTileError] = useState(false);
  const popup = useRef(null);
  const activeRef = useRef(active);
  activeRef.current = active && visible;
  const groups = (payload?.groups || []).filter(g => scope === "lower_hrazdan" || g.stage === scope);

  useEffect(() => {
    if (!active || payload) return;
    const controller = new AbortController();
    setStatus("loading");
    fetch("/api/land/consolidation", { signal: controller.signal })
      .then(r => { if (!r.ok) throw new Error("Consolidation unavailable"); return r.json(); })
      .then(data => { setPayload(data); setStatus("ready"); })
      .catch(error => { if (error.name !== "AbortError") setStatus("error"); });
    return () => controller.abort();
  }, [active, payload, attempt]);

  useEffect(() => {
    if (!map || !ready || !payload) return;
    const delivery = payload.tile_delivery;
    if (!map.getSource(SOURCE)) {
      map.addSource(SOURCE, { type: "vector", tiles: [`${window.location.origin}${delivery.url}`],
        minzoom: delivery.min_zoom, maxzoom: delivery.max_zoom, bounds: delivery.bounds });
      for (const layer of [
        { id: LAYERS[0], type: "fill", paint: { "fill-color": COLOR, "fill-opacity": 0 } },
        { id: LAYERS[1], type: "line", paint: { "line-color": COLOR, "line-width": 2.2, "line-opacity": .95 } },
        { id: LAYERS[2], type: "fill", paint: { "fill-color": "#ffffff", "fill-opacity": .16 } },
      ]) map.addLayer({ ...layer, source: SOURCE, "source-layer": "consolidation", layout: { visibility: "none" } }, "cadastre-hit");
    } else map.getSource(SOURCE).reload();
    const failed = event => {
      if (event.sourceId === SOURCE || String(event.error?.message || "").includes("/data/consolidation/")) setTileError(true);
    };
    map.on("error", failed);
    const selectGroup = event => {
      if (!activeRef.current) return;
      const group = event.features?.[0]?.properties;
      if (!group) return;
      setSelected(Number(group.group_number));
      popup.current?.remove();
      if (map.queryRenderedFeatures(event.point, { layers: ["cadastre-hit"] }).length) return;
      const content = document.createElement("div");
      content.className = "parcel-popup";
      appendConsolidationInfo(content, group);
      popup.current = new maplibregl.Popup({ maxWidth: "280px", closeOnClick: false }).setLngLat(event.lngLat).setDOMContent(content).addTo(map);
    };
    const hover = () => { if (activeRef.current) map.getCanvas().style.cursor = "pointer"; };
    const leave = () => { map.getCanvas().style.cursor = ""; };
    map.on("click", LAYERS[0], selectGroup);
    map.on("mouseenter", LAYERS[0], hover);
    map.on("mouseleave", LAYERS[0], leave);
    return () => {
      map.off("click", LAYERS[0], selectGroup);
      map.off("mouseenter", LAYERS[0], hover);
      map.off("mouseleave", LAYERS[0], leave);
      map.off("error", failed);
      popup.current?.remove();
    };
  }, [map, ready, payload]);

  useEffect(() => {
    if (!map || !ready || !payload || !map.getSource(SOURCE)) return;
    const numbers = groups.map(g => g.group_number);
    for (const id of LAYERS) {
      map.setFilter(id, ["all", ["in", ["get", "group_number"], ["literal", numbers]],
        ...(id === LAYERS[2] ? [["==", ["get", "group_number"], selected]] : [])]);
      map.setLayoutProperty(id, "visibility", active && visible && numbers.length ? "visible" : "none");
    }
    map.setPaintProperty(LAYERS[0], "fill-opacity", opacity / 100);
    if (!active || !visible || !numbers.includes(selected)) {
      popup.current?.remove();
      if (selected) setSelected(0);
    }
  }, [map, ready, payload, active, scope, visible, opacity, selected]);

  const focus = number => {
    const rows = number ? groups.filter(g => g.group_number === number) : groups;
    if (!map || !ready || !rows.length) return;
    const bounds = new maplibregl.LngLatBounds();
    rows.forEach(g => { bounds.extend(g.bbox.slice(0, 2)); bounds.extend(g.bbox.slice(2)); });
    setSelected(number);
    popup.current?.remove();
    if (window.innerWidth <= 640) closePanel();
    map.fitBounds(bounds, { maxZoom: 16.5, duration: 550, padding: window.innerWidth > 720
      ? { top: 120, right: 360, bottom: 70, left: 80 } : { top: 180, right: 35, bottom: 90, left: 35 } });
  };
  return { payload, status, tileError, retry: () => { setPayload(null); setTileError(false); setAttempt(n => n + 1); }, visible, setVisible, opacity, setOpacity,
    selected, groups, focus, canFocus: ready && visible };
}

export default function ConsolidationView({ model }) {
  const { groups, visible, setVisible, status } = model;
  return <div className="consolidation-section" role="tabpanel" aria-labelledby="land-tab-consolidation">
      {status === "loading" && <p role="status">Տվյալները բեռնվում են…</p>}
      {(status === "error" || model.tileError) && <div role="alert"><p>Շերտը ժամանակավորապես անհասանելի է։</p><button type="button" onClick={model.retry}>Կրկին փորձել</button></div>}
      {status === "ready" && <>
        <label className="layer-option layer-option--land consolidation-class">
          <input type="checkbox" checked={visible} onChange={e => setVisible(e.target.checked)} />
          <span className="land-class-swatch" style={{ background: COLOR }} aria-hidden="true" />
          <span>Կոնսոլիդացիայի հնարավոր խմբեր</span>
        </label>
        <dl className="activity-summary" aria-label="Կոնսոլիդացիայի արդյունք">
          <div><dt>Խմբեր</dt><dd><AreaMetric count={groups.length} area={groups.reduce((n, g) => n + g.area_ha, 0)}/></dd></div>
          <div><dt>Հողամասեր</dt><dd><AreaMetric count={groups.reduce((n, g) => n + g.parcel_count, 0)} area={groups.reduce((n, g) => n + g.area_ha, 0)}/></dd></div>
          <div><dt>Ընդհանուր մակերես</dt><dd>{area(groups.reduce((n, g) => n + g.area_ha, 0))} հա</dd></div>
        </dl>
        <label className="opacity-control"><span>Թափանցիկություն</span><output>{model.opacity}%</output>
          <input type="range" aria-label="Կոնսոլիդացիայի թափանցիկություն" min="20" max="85" step="5" value={model.opacity} onChange={e => model.setOpacity(Number(e.target.value))} /></label>
        <button className="consolidation-focus" type="button" disabled={!groups.length || !model.canFocus} onClick={() => model.focus(0)}>Ցույց տալ խմբերը</button>
        <label className="consolidation-picker">Խումբ
          <select aria-label="Կոնսոլիդացիայի խումբ" value={model.selected} disabled={!groups.length || !model.canFocus} onChange={e => model.focus(Number(e.target.value))}>
            <option value="0">Բոլոր խմբերը</option>
            {groups.map(g => <option key={g.group_number} value={g.group_number}>№{g.group_number} · {g.stage === "stage_1" ? "I" : "II"} · {area(g.area_ha)} հա</option>)}
          </select>
        </label>
        {!groups.length && <p role="status">Այս ընտրությամբ խմբեր չեն ցուցադրվում։</p>}
        <p className="activity-preview-status">2021–2025 · Նախնական թեկնածուներ․ գործնական հնարավորությունը հաստատված չէ։</p>
      </>}
  </div>;
}
