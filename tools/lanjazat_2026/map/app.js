"use strict";

const $ = (id) => document.getElementById(id);
const hectares = (n) => `${n.toLocaleString("en-US", {minimumFractionDigits: 2, maximumFractionDigits: 2})} ha`;
let summary;
let map;
let current = "active";
let satelliteFailure = false;

function selectedLayer() {
  if (!summary) return;
  const selected = summary.layers.find((layer) => layer.id === current);
  $("reading-title").textContent = selected ? selected.title : "Satellite imagery";
  $("description").textContent = selected ? selected.description : "Esri imagery is a visual reference and may have a different acquisition date from the 2026 EO observations.";
  if (!map) return;
  for (const layer of summary.layers) {
    if (map.getLayer(layer.id)) map.setLayoutProperty(layer.id, "visibility", layer.id === current ? "visible" : "none");
  }
  updateLoading();
}

function updateLoading() {
  if (!map || !summary) return;
  const ready = current === "none" || (map.getSource(current) && map.isSourceLoaded(current));
  $("loading-status").textContent = ready ? "" : "Loading selected layer...";
}

async function start() {
  const response = await fetch("/data/summary.json");
  if (!response.ok) throw new Error("Analysis summary unavailable");
  summary = await response.json();
  const dateFormat = new Intl.DateTimeFormat("en-GB", {day: "numeric", month: "short", timeZone: "UTC"});
  $("period").textContent = `${dateFormat.format(new Date(summary.start))} - ${dateFormat.format(new Date(summary.end))} 2026 | ${summary.observations} sampled dates`;
  $("boundary-area").textContent = hectares(summary.boundary_ha);
  $("coverage").textContent = `${hectares(summary.insufficient_observations_ha)} have insufficient seasonal observations. An uncoloured location is not proof of inactivity.`;
  for (const layer of summary.layers) {
    const label = document.createElement("label");
    label.className = "layer-option";
    const radio = document.createElement("input");
    radio.type = "radio"; radio.name = "layer"; radio.value = layer.id;
    radio.checked = layer.id === current;
    const swatch = document.createElement("span");
    swatch.className = "swatch"; swatch.style.backgroundColor = layer.color;
    const text = document.createElement("span"); text.className = "layer-text";
    text.textContent = layer.title;
    const value = document.createElement("strong"); value.textContent = hectares(layer.area_ha);
    text.append(value); label.append(radio, swatch, text);
    $("layer-options").append(label);
  }
  selectedLayer();
  map = new maplibregl.Map({
    container: "map", center: [44.61, 40.082], zoom: 13,
    attributionControl: false,
    style: {
      version: 8,
      sources: {satellite: {type: "raster", tileSize: 256,
        tiles: ["https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}"],
        attribution: "Imagery &copy; Esri, Maxar, Earthstar Geographics | Contains modified Copernicus Sentinel-2 data 2026 | ESA WorldCover 2021 | &copy; OpenStreetMap contributors"}},
      layers: [{id: "satellite", type: "raster", source: "satellite"}],
    },
  });
  map.addControl(new maplibregl.NavigationControl({showCompass: false}), "top-right");
  map.addControl(new maplibregl.ScaleControl({unit: "metric"}), "bottom-left");
  map.addControl(new maplibregl.AttributionControl({compact: true}), "bottom-right");
  map.on("error", (event) => {
    const message = event.error?.message || "Map source unavailable";
    satelliteFailure = satelliteFailure || event.sourceId === "satellite" || message.includes("arcgisonline");
    $("map-error").hidden = false;
    $("map-error").textContent = satelliteFailure
      ? "Satellite background could not load. The EO overlays are separate; check your internet connection."
      : "A map layer could not load. Reload to retry.";
    console.error(message);
  });
  map.on("load", () => {
    for (const layer of summary.layers) {
      map.addSource(layer.id, {type: "geojson", data: `/data/${layer.id}.geojson`, tolerance: 0, maxzoom: 18});
      map.addLayer({id: layer.id, type: "fill", source: layer.id,
        layout: {visibility: layer.id === current ? "visible" : "none"},
        paint: {"fill-color": layer.color, "fill-opacity": .65}});
    }
    map.addSource("boundary", {type: "geojson", data: "/data/boundary.geojson", tolerance: 0});
    const boundaryLayout = {visibility: $("boundary").checked ? "visible" : "none"};
    map.addLayer({id: "boundary-shadow", type: "line", source: "boundary", layout: boundaryLayout, paint: {"line-color": "#182820", "line-width": 5}});
    map.addLayer({id: "boundary", type: "line", source: "boundary", layout: boundaryLayout, paint: {"line-color": "#fff", "line-width": 2.5}});
    $("layers").disabled = false;
    map.fitBounds([[summary.bounds[0], summary.bounds[1]], [summary.bounds[2], summary.bounds[3]]], {padding: 55, duration: 0});
    updateLoading();
  });
  map.on("sourcedata", updateLoading);
  map.on("idle", updateLoading);
  $("layers").addEventListener("change", (event) => {
    if (event.target.name !== "layer") return;
    current = event.target.value;
    selectedLayer();
  });
  $("boundary").addEventListener("change", (event) => {
    for (const id of ["boundary", "boundary-shadow"]) {
      if (map.getLayer(id)) map.setLayoutProperty(id, "visibility", event.target.checked ? "visible" : "none");
    }
  });
  $("opacity").addEventListener("input", (event) => {
    $("opacity-value").textContent = `${event.target.value}%`;
    for (const layer of summary.layers) {
      if (map.getLayer(layer.id)) map.setPaintProperty(layer.id, "fill-opacity", Number(event.target.value) / 100);
    }
  });
}

start().catch((error) => {
  $("map-error").hidden = false;
  $("map-error").textContent = "The map could not start. Reload to retry.";
  $("loading-status").textContent = "Map unavailable";
  console.error(error);
});
