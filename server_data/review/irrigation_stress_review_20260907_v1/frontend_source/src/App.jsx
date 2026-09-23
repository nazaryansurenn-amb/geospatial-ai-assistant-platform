import IrrigationStressView, { appendIrrigationStressInfo, useIrrigationStress } from "./IrrigationStressView";
import DegradationView, { appendDegradationInfo, useDegradation } from "./DegradationView";
import AgentPanel from "./AgentPanel";
import ActivityChangeView, { CHANGE_LABELS, useActivityChange } from "./ActivityChangeView";
import React, { useEffect, useRef, useState } from "react";
import maplibregl from "maplibre-gl";
import AreaMetric, { formatCountArea } from "./AreaMetric";
import { LAND_MODES, LAND_SCOPES } from "./landResourcesSchema";
import PotentialView, { POTENTIAL_LABELS } from "./PotentialView";
import ConsolidationView, { appendConsolidationInfo, useConsolidation } from "./ConsolidationView";

const STUDY_BOUNDS = [
  [44.1169, 40.0716],
  [44.4125, 40.2615],
];

const CADASTRE_BOUNDS = [44.11329279, 40.03213086, 44.44564097, 40.26746359];
const EMPTY_CODE = "__none__";
const INITIAL_LAND_MODE = new URLSearchParams(window.location.search).get("view") === "activity-change" ? "activity_change" : new URLSearchParams(window.location.search).get("view") === "use-type"
  ? "land_use_type"
  : new URLSearchParams(window.location.search).get("view") === "potential" ? "potential"
  : new URLSearchParams(window.location.search).get("view") === "consolidation" ? "consolidation"
  : new URLSearchParams(window.location.search).get("view") === "degradation" ? "degradation"
  : new URLSearchParams(window.location.search).get("view") === "irrigation-stress" ? "irrigation_stress" : "activity_2026";
const CADASTRE_LAYERS = [
  "cadastre-overview-low",
  "cadastre-overview",
  "cadastre-hit",
  "cadastre-line",
  "cadastre-selected-fill",
  "cadastre-selected-line",
];
const CANAL_LINE_LAYERS = ["lower-hrazdan-shadow", "lower-hrazdan-line"];
const CANAL_POINT_LAYERS = ["lower-hrazdan-point-shadow", "lower-hrazdan-points"];
const STAGE_ONE_HALO_LAYERS = [
  "lower-hrazdan-halo-stage-1-fill",
  "lower-hrazdan-halo-stage-1-glow",
  "lower-hrazdan-halo-stage-1-line",
];
const STAGE_TWO_HALO_LAYERS = [
  "lower-hrazdan-halo-stage-2-fill",
  "lower-hrazdan-halo-stage-2-glow",
  "lower-hrazdan-halo-stage-2-line",
];
const LAND_ANALYTICS_SOURCE = "land-analytics";
const LAND_ANALYTICS_SOURCE_LAYER = "land_analytics";
const HISTORY_CLASS_COLORS = {
  stable_active: "#249b6b",
  periodic: "#e2aa43",
  stable_no_activity: "#8d9691",
  insufficient: "#9b5de5",
};
const HISTORY_CLASS_LABELS = {
  stable_active: "Կայուն մշակվող",
  periodic: "Պարբերաբար մշակվող",
  stable_no_activity: "Կայուն առանց դիտվող ակտիվության",
  insufficient: "Պահանջվում է լրացուցիչ ստուգում",
  not_calculated: "EO պատմությունը դեռ հաշվարկված չէ",
};
const HISTORY_ANNUAL_LABELS = {
  0: "Տվյալ չկա",
  1: "Ակտիվ",
  2: "Մասամբ ակտիվ",
  3: "Ակտիվություն չի դիտվել",
};
const CROP_TYPE_CLASS_COLORS = {
  annual: "#e7bd46",
  perennial: "#1fa276",
  undetermined: "#8d9691",
};
const CROP_TYPE_LABELS = {
  annual: "Միամյա մշակաբույսեր",
  perennial: "Բազմամյա մշակաբույսեր",
  undetermined: "Տեսակը չի որոշվել",
};
const CROP_TYPE_YEAR_LABELS = {
  0: "EO դիտարկումները բավարար չեն",
  1: "Միամյա պրոֆիլ",
  2: "Բազմամյա պրոֆիլ",
  3: "Ակտիվություն չի դիտվել",
  4: "Տեսակը չի որոշվել",
};
const ANNUAL_CYCLE_CLASS_COLORS = {
  single_cycle: "#e7bd46",
  two_cycle_recurring: "#e8752e",
};
const ANNUAL_CYCLE_LABELS = {
  single_cycle: "Մեկ ցիկլ",
  two_cycle_recurring: "Երկու ցիկլ",
};
const ANNUAL_CYCLE_YEAR_LABELS = {
  0: "Չի գնահատվել",
  1: "Մեկ ցիկլ",
  2: "Երկու ցիկլ",
};
const ACTIVITY_REVIEW_COLOR = "#9b5de5";
const HOUSEHOLD_COLOR = "#d66d9e";
const ACTIVITY_COLOR_EXPRESSION = [
  "interpolate",
  ["linear"],
  ["/", ["coalesce", ["get", "activity_fraction_bp"], 0], 10000],
  0,
  "#df3f4a",
  0.1,
  "#e96a4f",
  0.3,
  "#f0c64a",
  0.55,
  "#9ed56c",
  0.8,
  "#31c982",
  1,
  "#15965e",
];
function formatArea(areaHa) {
  return new Intl.NumberFormat("hy-AM", {
    minimumFractionDigits: 2,
    maximumFractionDigits: 4,
  }).format(Number(areaHa || 0));
}

function formatCount(value) {
  return new Intl.NumberFormat("hy-AM").format(Number(value || 0));
}

function formatPercent(value) {
  return new Intl.NumberFormat("hy-AM", {
    minimumFractionDigits: 0,
    maximumFractionDigits: 1,
  }).format(Number(value || 0) * 100);
}

function parcelPopupContent(parcel) {
  const content = document.createElement("div");
  content.className = "parcel-popup";

  const label = document.createElement("span");
  label.textContent = "Կադաստրային հողամաս";
  content.appendChild(label);

  const code = document.createElement("strong");
  code.textContent = parcel.cadastreCode;
  content.appendChild(code);

  const area = document.createElement("p");
  area.textContent = `Մակերես՝ ${formatArea(parcel.areaHa)} հա`;
  content.appendChild(area);

  if (parcel.activity?.roadExcluded) {
    const road = document.createElement("p");
    road.className = "parcel-popup-review";
    road.textContent = "Ճանապարհային հողամաս․ բացառված է հողային վերլուծությունից";
    content.appendChild(road);
  } else if (parcel.mode === "irrigation_stress") {
    appendIrrigationStressInfo(content, parcel.irrigationStress);
  } else if (parcel.mode === "degradation") {
    appendDegradationInfo(content, parcel.degradation);
  } else if (parcel.mode === "consolidation") {
    appendConsolidationInfo(content, parcel.consolidation);
  } else if (parcel.mode === "potential" && parcel.potential) {
    const p = document.createElement("p");
    p.className = "parcel-popup-history-class";
    p.textContent = POTENTIAL_LABELS[parcel.potential.potentialClass] || "Ներուժի թեկնածու չի առանձնացվել";
    content.appendChild(p);
    const note = document.createElement("p");
    note.textContent = "Նախնական դիտարկում․ օգտագործման և ոռոգման հնարավորությունը ենթակա է տեղում ստուգման։";
    content.appendChild(note);
  } else if ((parcel.mode === "history_2021_2025" || parcel.mode === "activity_change") && parcel.history) {
    const historyClass = document.createElement("p");
    historyClass.className = "parcel-popup-history-class";
    historyClass.textContent = parcel.mode === "activity_change"
      ? CHANGE_LABELS[parcel.activityChange?.changeClass] || "Այս փոփոխությունը չի առանձնացվել"
      : HISTORY_CLASS_LABELS[parcel.history.historyClass];
    content.appendChild(historyClass);

    if (parcel.mode === "activity_change" && parcel.activityChange?.changeYear) {
      const changeYear = document.createElement("p");
      changeYear.textContent = `Փոփոխության առաջին դիտված տարին՝ ${parcel.activityChange.changeYear}`;
      content.appendChild(changeYear);
    }

    const timeline = document.createElement("div");
    timeline.className = "parcel-history-timeline";
    parcel.history.annualStateCodes.forEach((stateCode, index) => {
      const year = document.createElement("span");
      year.className = `parcel-history-year state-${stateCode}`;
      year.title = HISTORY_ANNUAL_LABELS[stateCode];
      const yearLabel = document.createElement("small");
      yearLabel.textContent = String(2021 + index);
      const stateMark = document.createElement("i");
      stateMark.setAttribute("aria-hidden", "true");
      year.append(yearLabel, stateMark);
      timeline.appendChild(year);
    });
    content.appendChild(timeline);

    const observed = document.createElement("p");
    observed.className = "parcel-popup-history-note";
    observed.textContent = `Դիտարկված սեզոններ՝ ${parcel.history.profileYearCount}/5`;
    content.appendChild(observed);
  } else if (parcel.mode === "activity_change") {
    const note = document.createElement("p");
    note.textContent = parcel.activityChange ? CHANGE_LABELS[parcel.activityChange.changeClass] : "Դիտարկումները դեռ բեռնվում են…";
    content.appendChild(note);
  } else if (parcel.mode === "land_use_type" && parcel.cropType) {
    const cropType = document.createElement("p");
    cropType.className = "parcel-popup-history-class";
    cropType.textContent = `Օգտագործման տեսակ՝ ${CROP_TYPE_LABELS[parcel.cropType.cropType] || parcel.cropType.cropType}`;
    content.appendChild(cropType);

    if (Array.isArray(parcel.cropType.yearTypeCodes)) {
      const typeTimeline = document.createElement("div");
      typeTimeline.className = "parcel-history-timeline parcel-use-type-timeline";
      parcel.cropType.yearTypeCodes.forEach((stateCode, index) => {
        const year = document.createElement("span");
        year.className = `parcel-history-year type-state-${stateCode}`;
        year.title = CROP_TYPE_YEAR_LABELS[stateCode];
        const yearLabel = document.createElement("small");
        yearLabel.textContent = String(2021 + index);
        const stateMark = document.createElement("i");
        stateMark.setAttribute("aria-hidden", "true");
        year.append(yearLabel, stateMark);
        typeTimeline.appendChild(year);
      });
      content.appendChild(typeTimeline);
    }

    if (parcel.cropType.cropType === "annual" && parcel.cropType.annualCycle) {
      const cycle = document.createElement("p");
      cycle.className = "parcel-popup-cycle-class";
      cycle.textContent = `Սեզոնային ցիկլ՝ ${ANNUAL_CYCLE_LABELS[parcel.cropType.annualCycle] || parcel.cropType.annualCycle}`;
      content.appendChild(cycle);

      if (Array.isArray(parcel.cropType.annualCycleYearCodes)) {
        const cycleTimeline = document.createElement("div");
        cycleTimeline.className = "parcel-history-timeline parcel-cycle-timeline";
        parcel.cropType.annualCycleYearCodes.forEach((stateCode, index) => {
          const year = document.createElement("span");
          year.className = `parcel-history-year cycle-state-${stateCode}`;
          year.title = ANNUAL_CYCLE_YEAR_LABELS[stateCode];
          const yearLabel = document.createElement("small");
          yearLabel.textContent = String(2021 + index);
          const stateMark = document.createElement("i");
          stateMark.setAttribute("aria-hidden", "true");
          year.append(yearLabel, stateMark);
          cycleTimeline.appendChild(year);
        });
        content.appendChild(cycleTimeline);
      }
    }

    const observed = document.createElement("p");
    observed.className = "parcel-popup-history-note";
    observed.textContent = `Դիտարկված սեզոններ՝ ${parcel.cropType.profileYearCount}/5`;
    content.appendChild(observed);
  } else if (parcel.activity?.household) {
    const household = document.createElement("p");
    household.className = "parcel-popup-household";
    household.textContent = "Հողօգտագործման տեսակ՝ տնամերձ գյուղատնտեսություն";
    content.appendChild(household);

    const activityShare = document.createElement("p");
    activityShare.textContent = `2026 դիտարկված ակտիվ բաժին՝ ${formatPercent(parcel.activity.fraction)}%`;
    content.appendChild(activityShare);
  } else if (parcel.activity?.previewState === "ready") {
    const activityShare = document.createElement("p");
    activityShare.textContent = `2026 ակտիվ բաժին՝ ${formatPercent(parcel.activity.fraction)}%`;
    content.appendChild(activityShare);

    const activityArea = document.createElement("p");
    activityArea.textContent = `Դիտարկված ակտիվ մակերես՝ ${formatArea(parcel.activity.areaHa)} հա`;
    content.appendChild(activityArea);
  } else if (parcel.activity?.previewState === "review") {
    const review = document.createElement("p");
    review.className = "parcel-popup-review";
    review.textContent = "Կարգավիճակը պարզելու համար անհրաժեշտ է լրացուցիչ ստուգում";
    content.appendChild(review);
  }
  return content;
}

export default function App() {
  const containerRef = useRef(null);
  const mapRef = useRef(null);
  const popupRef = useRef(null);
  const canalLabelElementsRef = useRef([]);
  const parcelRequestRef = useRef(0);
  const landModeRef = useRef(INITIAL_LAND_MODE);
  const [mapReady, setMapReady] = useState(false);
  const [cadastreVisible, setCadastreVisible] = useState(true);
  const [cadastreOpacity, setCadastreOpacity] = useState(70);
  const [stageOneVisible, setStageOneVisible] = useState(true);
  const [stageTwoVisible, setStageTwoVisible] = useState(true);
  const [stageOneHaloVisible, setStageOneHaloVisible] = useState(true);
  const [stageTwoHaloVisible, setStageTwoHaloVisible] = useState(true);
  const [landScope, setLandScope] = useState("lower_hrazdan");
  const [landMode, setLandMode] = useState(INITIAL_LAND_MODE);
  const [activityPreviewVisible, setActivityPreviewVisible] = useState(true);
  const [activityReviewVisible, setActivityReviewVisible] = useState(true);
  const [activityClassVisibility, setActivityClassVisibility] = useState({
    active: true,
    partial: true,
    no_current_activity: true,
  });
  const [householdVisible, setHouseholdVisible] = useState(true);
  const [activityOpacity, setActivityOpacity] = useState(60);
  const [activityPayload, setActivityPayload] = useState(null);
  const [activityStatus, setActivityStatus] = useState("idle");
  const [historyPayload, setHistoryPayload] = useState(null);
  const [potentialPayload, setPotentialPayload] = useState(null);
  const [historyStatus, setHistoryStatus] = useState("idle");
  const [historyOpacity, setHistoryOpacity] = useState(65);
  const [cropTypePayload, setCropTypePayload] = useState(null);
  const [annualCyclesPayload, setAnnualCyclesPayload] = useState(null);
  const [cropTypeStatus, setCropTypeStatus] = useState("idle");
  const [cropTypeOpacity, setCropTypeOpacity] = useState(65);
  const [annualExpanded, setAnnualExpanded] = useState(true);
  const [cropTypeClassVisibility, setCropTypeClassVisibility] = useState({
    annual: true,
    perennial: true,
    undetermined: true,
  });
  const [annualCycleVisibility, setAnnualCycleVisibility] = useState({
    single_cycle: true,
    two_cycle_recurring: true,
  });
  const [historyClassVisibility, setHistoryClassVisibility] = useState({
    stable_active: true,
    periodic: true,
    stable_no_activity: true,
    insufficient: true,
  });
  const [layerPanelOpen, setLayerPanelOpen] = useState(true);
  const [agentExpansion, setAgentExpansion] = useState(true);
  const [agentSelectedCode, setAgentSelectedCode] = useState(null);
  const [searchCode, setSearchCode] = useState("");
  const [searchStatus, setSearchStatus] = useState("");
  const [searching, setSearching] = useState(false);
  const irrigationStress = useIrrigationStress({map: mapRef.current, ready: mapReady, active: landMode === "irrigation_stress", scope: landScope, closePanel: () => setLayerPanelOpen(false)});
  const degradation = useDegradation({map: mapRef.current, ready: mapReady, active: landMode === "degradation", scope: landScope, closePanel: () => setLayerPanelOpen(false)});
  const consolidation = useConsolidation({ map: mapRef.current, ready: mapReady,
    active: landMode === "consolidation", scope: landScope, closePanel: () => setLayerPanelOpen(false) });
  const activityChange = useActivityChange({map: mapRef.current, ready: mapReady,
    active: landMode === "activity_change", scope: landScope,
    onSelect: (parcel, coordinates) => selectParcel(parcel, coordinates), closePanel: () => setLayerPanelOpen(false)});

  useEffect(() => {
    if ([landModeRef.current, landMode].some(mode => ["consolidation", "activity_change", "degradation", "irrigation_stress"].includes(mode))) popupRef.current?.remove();
    landModeRef.current = landMode;
  }, [landMode]);

  const resetParcelSelection = () => {
    setAgentSelectedCode(null);
    const map = mapRef.current;
    if (!map) return;
    ["cadastre-selected-fill", "cadastre-selected-line"].forEach((id) => {
      if (map.getLayer(id)) {
        map.setFilter(id, ["==", ["get", "cadastre_code"], EMPTY_CODE]);
      }
    });
  };

  const selectParcel = async (parcel, coordinates) => {
    const map = mapRef.current;
    if (!map) return;
    const requestId = parcelRequestRef.current + 1;
    parcelRequestRef.current = requestId;
    popupRef.current?.remove();
    ["cadastre-selected-fill", "cadastre-selected-line"].forEach((id) => {
      if (map.getLayer(id)) {
        map.setFilter(id, ["==", ["get", "cadastre_code"], parcel.cadastreCode]);
      }
    });

    const popup = new maplibregl.Popup({
      closeButton: true,
      closeOnClick: false,
      maxWidth: "280px",
      offset: 12,
    })
      .setLngLat(coordinates)
      .setDOMContent(parcelPopupContent({ ...parcel, mode: landModeRef.current }))
      .addTo(map);
    popup.on("close", () => {
      if (popupRef.current === popup) {
        popupRef.current = null;
        parcelRequestRef.current += 1;
        resetParcelSelection();
      }
    });
    popupRef.current = popup;
    setAgentSelectedCode(parcel.cadastreCode);

    try {
      const response = await fetch(
        `/api/land/parcel?code=${encodeURIComponent(parcel.cadastreCode)}`,
      );
      if (response.status === 404) return;
      if (!response.ok) throw new Error("Parcel analytics could not be loaded");
      const analytics = await response.json();
      if (parcelRequestRef.current !== requestId || popupRef.current !== popup) return;
      popup.setDOMContent(
        parcelPopupContent({
          ...parcel,
          activity: analytics.activity,
          history: analytics.history,
          cropType: analytics.cropType,
          potential: analytics.potential,
          consolidation: analytics.consolidation,
          degradation: analytics.degradation,
          irrigationStress: analytics.irrigationStress,
          activityChange: analytics.activityChange,
          mode: landModeRef.current,
        }),
      );
    } catch (error) {
      console.error(error);
    }
  };

  useEffect(() => {
    const communityMarkers = [];
    const canalMarkers = [];
    let updateLabelVisibility = () => {};
    const map = new maplibregl.Map({
      container: containerRef.current,
      style: {
        version: 8,
        sources: {
          esriSatellite: {
            type: "raster",
            tiles: [
              "https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}",
            ],
            tileSize: 256,
            attribution: "Imagery &copy; Esri",
          },
        },
        layers: [
          {
            id: "esri-satellite",
            type: "raster",
            source: "esriSatellite",
          },
        ],
      },
      bounds: STUDY_BOUNDS,
      fitBoundsOptions: { padding: 48 },
      maxZoom: 19,
      minZoom: 8,
    });
    mapRef.current = map;

    map.addControl(
      new maplibregl.NavigationControl({ showCompass: false }),
      "bottom-right",
    );

    map.on("load", async () => {
      map.addSource("communities", {
        type: "geojson",
        data: "/data/communities.geojson",
      });
      map.addSource("wua-boundary", {
        type: "geojson",
        data: "/data/echmiadzin_wua_boundary.geojson",
      });

      map.addLayer({
        id: "community-fill",
        type: "fill",
        source: "communities",
        paint: {
          "fill-color": "#dff6ea",
          "fill-opacity": 0.06,
        },
      });
      map.addLayer({
        id: "community-boundary-shadow",
        type: "line",
        source: "communities",
        paint: {
          "line-color": "#10251d",
          "line-width": 2.8,
          "line-opacity": 0.68,
        },
      });
      map.addLayer({
        id: "community-boundary",
        type: "line",
        source: "communities",
        paint: {
          "line-color": "#f7fff9",
          "line-width": 1.2,
          "line-opacity": 0.95,
        },
      });

      map.addSource("lower-hrazdan-halos", {
        type: "geojson",
        data: "/data/lower_hrazdan_halos.geojson",
      });
      map.addLayer({
        id: "lower-hrazdan-halo-stage-1-fill",
        type: "fill",
        source: "lower-hrazdan-halos",
        filter: ["==", ["get", "stage"], "stage_1"],
        paint: {
          "fill-color": "#16c7dd",
          "fill-opacity": 0.3,
        },
      });
      map.addLayer({
        id: "lower-hrazdan-halo-stage-1-glow",
        type: "line",
        source: "lower-hrazdan-halos",
        filter: ["==", ["get", "stage"], "stage_1"],
        paint: {
          "line-color": "#16c7dd",
          "line-width": ["interpolate", ["linear"], ["zoom"], 8, 10, 14, 16],
          "line-opacity": 0.5,
          "line-blur": 3,
        },
      });
      map.addLayer({
        id: "lower-hrazdan-halo-stage-1-line",
        type: "line",
        source: "lower-hrazdan-halos",
        filter: ["==", ["get", "stage"], "stage_1"],
        paint: {
          "line-color": "#c5fbff",
          "line-width": ["interpolate", ["linear"], ["zoom"], 8, 3.2, 14, 5],
          "line-opacity": 0.98,
        },
      });
      map.addLayer({
        id: "lower-hrazdan-halo-stage-2-fill",
        type: "fill",
        source: "lower-hrazdan-halos",
        filter: ["==", ["get", "stage"], "stage_2"],
        paint: {
          "fill-color": "#f2a81d",
          "fill-opacity": 0.27,
        },
      });
      map.addLayer({
        id: "lower-hrazdan-halo-stage-2-glow",
        type: "line",
        source: "lower-hrazdan-halos",
        filter: ["==", ["get", "stage"], "stage_2"],
        paint: {
          "line-color": "#f2a81d",
          "line-width": ["interpolate", ["linear"], ["zoom"], 8, 10, 14, 16],
          "line-opacity": 0.5,
          "line-blur": 3,
        },
      });
      map.addLayer({
        id: "lower-hrazdan-halo-stage-2-line",
        type: "line",
        source: "lower-hrazdan-halos",
        filter: ["==", ["get", "stage"], "stage_2"],
        paint: {
          "line-color": "#ffe6a6",
          "line-width": ["interpolate", ["linear"], ["zoom"], 8, 3.2, 14, 5],
          "line-opacity": 0.98,
        },
      });

      map.addSource("cadastre-overview-source", {
        type: "image",
        url: "/data/cadastre_overview.png",
        coordinates: [
          [CADASTRE_BOUNDS[0], CADASTRE_BOUNDS[3]],
          [CADASTRE_BOUNDS[2], CADASTRE_BOUNDS[3]],
          [CADASTRE_BOUNDS[2], CADASTRE_BOUNDS[1]],
          [CADASTRE_BOUNDS[0], CADASTRE_BOUNDS[1]],
        ],
      });
      map.addSource("cadastre-overview-low-source", {
        type: "image",
        url: "/data/cadastre_overview_low.png",
        coordinates: [
          [CADASTRE_BOUNDS[0], CADASTRE_BOUNDS[3]],
          [CADASTRE_BOUNDS[2], CADASTRE_BOUNDS[3]],
          [CADASTRE_BOUNDS[2], CADASTRE_BOUNDS[1]],
          [CADASTRE_BOUNDS[0], CADASTRE_BOUNDS[1]],
        ],
      });
      map.addLayer({
        id: "cadastre-overview-low",
        type: "raster",
        source: "cadastre-overview-low-source",
        maxzoom: 12.3,
        paint: {
          "raster-opacity": [
            "interpolate",
            ["linear"],
            ["zoom"],
            8,
            0.76,
            10.5,
            0.66,
            12.2,
            0.04,
          ],
          "raster-resampling": "linear",
          "raster-fade-duration": 80,
        },
      });
      map.addLayer({
        id: "cadastre-overview",
        type: "raster",
        source: "cadastre-overview-source",
        minzoom: 10.5,
        maxzoom: 14.25,
        paint: {
          "raster-opacity": [
            "interpolate",
            ["linear"],
            ["zoom"],
            8,
            0.3,
            11,
            0.5,
            13.5,
            0.68,
            14.2,
            0.05,
          ],
          "raster-resampling": "linear",
          "raster-fade-duration": 80,
        },
      });
      map.addSource("cadastre", {
        type: "vector",
        tiles: [`${window.location.origin}/data/cadastre/{z}/{x}/{y}.pbf`],
        minzoom: 14,
        maxzoom: 15,
        bounds: CADASTRE_BOUNDS,
        promoteId: "cadastre_code",
      });
      map.addLayer({
        id: "cadastre-hit",
        type: "fill",
        source: "cadastre",
        "source-layer": "parcels",
        minzoom: 13.5,
        paint: { "fill-color": "#ffffff", "fill-opacity": 0.001 },
      });
      map.addLayer({
        id: "cadastre-line",
        type: "line",
        source: "cadastre",
        "source-layer": "parcels",
        minzoom: 13.5,
        paint: {
          "line-color": "#ffffff",
          "line-opacity": 0.92,
          "line-width": ["interpolate", ["linear"], ["zoom"], 13.5, 0.65, 18, 1.45],
        },
      });
      map.addLayer({
        id: "cadastre-selected-fill",
        type: "fill",
        source: "cadastre",
        "source-layer": "parcels",
        minzoom: 13.5,
        filter: ["==", ["get", "cadastre_code"], EMPTY_CODE],
        paint: { "fill-color": "#ff4f93", "fill-opacity": 0.42 },
      });
      map.addLayer({
        id: "cadastre-selected-line",
        type: "line",
        source: "cadastre",
        "source-layer": "parcels",
        minzoom: 13.5,
        filter: ["==", ["get", "cadastre_code"], EMPTY_CODE],
        paint: { "line-color": "#ffd5e6", "line-width": 4.2 },
      });

      map.addLayer({
        id: "wua-boundary-shadow",
        type: "line",
        source: "wua-boundary",
        paint: {
          "line-color": "#081e16",
          "line-width": 7,
          "line-opacity": 0.9,
        },
      });
      map.addLayer({
        id: "wua-boundary",
        type: "line",
        source: "wua-boundary",
        paint: {
          "line-color": "#27d59b",
          "line-width": 4,
          "line-opacity": 1,
        },
      });

      map.addSource("lower-hrazdan", {
        type: "geojson",
        data: "/data/lower_hrazdan.geojson",
      });
      map.addSource("lower-hrazdan-points", {
        type: "geojson",
        data: "/data/lower_hrazdan_points.geojson",
      });
      map.addLayer({
        id: "lower-hrazdan-shadow",
        type: "line",
        source: "lower-hrazdan",
        paint: {
          "line-color": "#071b17",
          "line-width": ["interpolate", ["linear"], ["zoom"], 8, 5, 13, 8, 17, 12],
          "line-opacity": 0.96,
        },
      });
      map.addLayer({
        id: "lower-hrazdan-line",
        type: "line",
        source: "lower-hrazdan",
        paint: {
          "line-color": [
            "match",
            ["get", "stage"],
            "stage_1",
            "#16c7dd",
            "stage_2",
            "#f2a81d",
            "#ffffff",
          ],
          "line-width": ["interpolate", ["linear"], ["zoom"], 8, 2.8, 13, 5, 17, 8],
          "line-opacity": 1,
        },
      });
      map.addLayer({
        id: "lower-hrazdan-point-shadow",
        type: "circle",
        source: "lower-hrazdan-points",
        paint: {
          "circle-radius": ["interpolate", ["linear"], ["zoom"], 8, 4.8, 13, 7.2, 17, 9],
          "circle-color": "#10231d",
          "circle-opacity": 0.96,
        },
      });
      map.addLayer({
        id: "lower-hrazdan-points",
        type: "circle",
        source: "lower-hrazdan-points",
        paint: {
          "circle-radius": ["interpolate", ["linear"], ["zoom"], 8, 3.1, 13, 4.8, 17, 6.2],
          "circle-color": "#ffffff",
          "circle-stroke-color": [
            "match",
            ["get", "nearest_stage"],
            "stage_1",
            "#16c7dd",
            "stage_2",
            "#f2a81d",
            "#ffffff",
          ],
          "circle-stroke-width": 1.4,
        },
      });

      map.on("click", "cadastre-hit", (event) => {
        const feature = event.features?.[0];
        if (!feature) return;
        const cadastreCode = String(feature.properties?.cadastre_code || "");
        selectParcel(
          {
            cadastreCode,
            areaHa: Number(feature.properties?.area_ha || 0),
          },
          event.lngLat.toArray(),
        );
      });
      map.on("mouseenter", "cadastre-hit", () => {
        map.getCanvas().style.cursor = "pointer";
      });
      map.on("mouseleave", "cadastre-hit", () => {
        map.getCanvas().style.cursor = "";
      });
      map.on("zoomend", () => {
        if (map.getZoom() < 13.5) popupRef.current?.remove();
      });

      const canalLabelResponse = await fetch("/data/lower_hrazdan_labels.geojson");
      if (!canalLabelResponse.ok) {
        throw new Error("Lower Hrazdan labels could not be loaded");
      }
      const canalLabels = await canalLabelResponse.json();
      canalLabels.features.forEach((feature) => {
        const element = document.createElement("div");
        element.className = `canal-label ${feature.properties.stage}`;
        element.textContent = feature.properties.display_name_hy;
        const marker = new maplibregl.Marker({ element, anchor: "center" })
          .setLngLat(feature.geometry.coordinates)
          .addTo(map);
        canalMarkers.push(marker);
        canalLabelElementsRef.current.push({
          element,
          stage: feature.properties.stage,
        });
      });

      const response = await fetch("/data/community_labels.geojson");
      if (!response.ok) {
        throw new Error("Community labels could not be loaded");
      }
      const labels = await response.json();
      labels.features
        .sort(
          (left, right) =>
            right.properties.display_priority - left.properties.display_priority,
        )
        .forEach((feature) => {
          const element = document.createElement("div");
          element.className = "community-label";
          element.textContent = feature.properties.name_hy;
          const marker = new maplibregl.Marker({ element, anchor: "center" })
            .setLngLat(feature.geometry.coordinates)
            .addTo(map);
          communityMarkers.push({
            marker,
            element,
            coordinates: feature.geometry.coordinates,
          });
        });

      updateLabelVisibility = () => {
        const placed = [];
        const canvas = map.getCanvas();
        const zoom = map.getZoom();
        const gap = zoom < 10.5 ? 10 : 5;

        communityMarkers.forEach(({ element, coordinates }) => {
          const point = map.project(coordinates);
          const width = Math.max(element.offsetWidth, 54);
          const height = Math.max(element.offsetHeight, 18);
          const box = {
            left: point.x - width / 2 - gap,
            right: point.x + width / 2 + gap,
            top: point.y - height / 2 - gap,
            bottom: point.y + height / 2 + gap,
          };
          const outside =
            box.right < 0 ||
            box.left > canvas.clientWidth ||
            box.bottom < 0 ||
            box.top > canvas.clientHeight;
          const overlaps = placed.some(
            (other) =>
              box.left < other.right &&
              box.right > other.left &&
              box.top < other.bottom &&
              box.bottom > other.top,
          );
          const visible = !outside && !overlaps;
          element.style.visibility = visible ? "visible" : "hidden";
          if (visible) placed.push(box);
        });
      };

      requestAnimationFrame(updateLabelVisibility);
      setMapReady(true);
    });

    map.on("move", updateLabelVisibility);
    map.on("resize", updateLabelVisibility);
    map.on("error", (event) => console.error("MapLibre:", event?.error?.message || event));

    return () => {
      communityMarkers.forEach(({ marker }) => marker.remove());
      canalMarkers.forEach((marker) => marker.remove());
      canalLabelElementsRef.current = [];
      popupRef.current?.remove();
      map.remove();
      mapRef.current = null;
    };
  }, []);

  useEffect(() => {
    const map = mapRef.current;
    if (!map || !mapReady) return undefined;
    let cancelled = false;

    const addAnalyticsLayer = (layer) => {
      if (!map.getLayer(layer.id)) map.addLayer(layer, "cadastre-hit");
    };

    const loadLandDelivery = async () => {
      setActivityStatus("loading");
      setHistoryStatus("loading");
      setCropTypeStatus("loading");
      try {
        const response = await fetch("/api/land/delivery");
        if (!response.ok) throw new Error("Land analytics delivery could not be loaded");
        const payload = await response.json();
        if (cancelled) return;
        const delivery = payload.tile_delivery;

        if (!map.getSource(LAND_ANALYTICS_SOURCE)) {
          map.addSource(LAND_ANALYTICS_SOURCE, {
            type: "vector",
            tiles: [`${window.location.origin}${delivery.url}`],
            minzoom: delivery.min_zoom,
            maxzoom: delivery.max_zoom,
            bounds: delivery.bounds,
          });
        }

        addAnalyticsLayer({
          id: "land-activity-preview-fill",
          type: "fill",
          source: LAND_ANALYTICS_SOURCE,
          "source-layer": LAND_ANALYTICS_SOURCE_LAYER,
          minzoom: delivery.min_zoom,
          layout: { visibility: "none" },
          paint: { "fill-color": ACTIVITY_COLOR_EXPRESSION, "fill-opacity": 0 },
        });
        addAnalyticsLayer({
          id: "land-activity-review-fill",
          type: "fill",
          source: LAND_ANALYTICS_SOURCE,
          "source-layer": LAND_ANALYTICS_SOURCE_LAYER,
          minzoom: delivery.min_zoom,
          layout: { visibility: "none" },
          paint: { "fill-color": ACTIVITY_REVIEW_COLOR, "fill-opacity": 0 },
        });
        addAnalyticsLayer({
          id: "land-activity-review-line",
          type: "line",
          source: LAND_ANALYTICS_SOURCE,
          "source-layer": LAND_ANALYTICS_SOURCE_LAYER,
          minzoom: delivery.min_zoom,
          layout: { visibility: "none" },
          paint: {
            "line-color": "#f0dcff",
            "line-width": 1.5,
            "line-opacity": 0,
          },
        });
        addAnalyticsLayer({
          id: "land-household-fill",
          type: "fill",
          source: LAND_ANALYTICS_SOURCE,
          "source-layer": LAND_ANALYTICS_SOURCE_LAYER,
          minzoom: delivery.min_zoom,
          layout: { visibility: "none" },
          paint: { "fill-color": HOUSEHOLD_COLOR, "fill-opacity": 0 },
        });
        addAnalyticsLayer({
          id: "land-household-line",
          type: "line",
          source: LAND_ANALYTICS_SOURCE,
          "source-layer": LAND_ANALYTICS_SOURCE_LAYER,
          minzoom: delivery.min_zoom,
          layout: { visibility: "none" },
          paint: {
            "line-color": "#ffd1e6",
            "line-width": 1.4,
            "line-opacity": 0,
          },
        });
        Object.entries(HISTORY_CLASS_COLORS).forEach(([historyClass, color]) => {
          addAnalyticsLayer({
            id: `land-history-${historyClass}-fill`,
            type: "fill",
            source: LAND_ANALYTICS_SOURCE,
            "source-layer": LAND_ANALYTICS_SOURCE_LAYER,
            minzoom: delivery.min_zoom,
            layout: { visibility: "none" },
            filter: ["==", ["get", "history_class"], historyClass],
            paint: { "fill-color": color, "fill-opacity": 0 },
          });
          addAnalyticsLayer({
            id: `land-history-${historyClass}-line`,
            type: "line",
            source: LAND_ANALYTICS_SOURCE,
            "source-layer": LAND_ANALYTICS_SOURCE_LAYER,
            minzoom: delivery.min_zoom,
            layout: { visibility: "none" },
            filter: ["==", ["get", "history_class"], historyClass],
            paint: {
              "line-color": color,
              "line-width": 1.15,
              "line-opacity": 0,
            },
          });
        });
        Object.entries(CROP_TYPE_CLASS_COLORS).forEach(([cropType, color]) => {
          addAnalyticsLayer({
            id: `land-crop-type-${cropType}-fill`,
            type: "fill",
            source: LAND_ANALYTICS_SOURCE,
            "source-layer": LAND_ANALYTICS_SOURCE_LAYER,
            minzoom: delivery.min_zoom,
            layout: { visibility: "none" },
            filter: ["==", ["get", "crop_type"], cropType],
            paint: { "fill-color": color, "fill-opacity": 0 },
          });
          addAnalyticsLayer({
            id: `land-crop-type-${cropType}-line`,
            type: "line",
            source: LAND_ANALYTICS_SOURCE,
            "source-layer": LAND_ANALYTICS_SOURCE_LAYER,
            minzoom: delivery.min_zoom,
            layout: { visibility: "none" },
            filter: ["==", ["get", "crop_type"], cropType],
            paint: {
              "line-color": color,
              "line-width": 1.15,
              "line-opacity": 0,
            },
          });
        });
        Object.entries(ANNUAL_CYCLE_CLASS_COLORS).forEach(([annualCycle, color]) => {
          addAnalyticsLayer({
            id: `land-annual-cycle-${annualCycle}-fill`,
            type: "fill",
            source: LAND_ANALYTICS_SOURCE,
            "source-layer": LAND_ANALYTICS_SOURCE_LAYER,
            minzoom: delivery.min_zoom,
            layout: { visibility: "none" },
            filter: ["==", ["get", "annual_cycle"], annualCycle],
            paint: { "fill-color": color, "fill-opacity": 0 },
          });
          addAnalyticsLayer({
            id: `land-annual-cycle-${annualCycle}-line`,
            type: "line",
            source: LAND_ANALYTICS_SOURCE,
            "source-layer": LAND_ANALYTICS_SOURCE_LAYER,
            minzoom: delivery.min_zoom,
            layout: { visibility: "none" },
            filter: ["==", ["get", "annual_cycle"], annualCycle],
            paint: {
              "line-color": color,
              "line-width": 1.15,
              "line-opacity": 0,
            },
          });
        });

        setActivityPayload(payload.activity);
        setHistoryPayload(payload.history);
        setPotentialPayload(payload.potential || null);
        setCropTypePayload(payload.land_use_type);
        setAnnualCyclesPayload(payload.annual_cycles || null);
        setActivityStatus("ready");
        setHistoryStatus("ready");
        setCropTypeStatus("ready");
      } catch (error) {
        console.error(error);
        if (!cancelled) {
          setActivityStatus("error");
          setHistoryStatus("error");
          setCropTypeStatus("error");
        }
      }
    };

    loadLandDelivery();
    return () => {
      cancelled = true;
    };
  }, [mapReady]);

  useEffect(() => {
    const map = mapRef.current;
    if (!map || !mapReady) return;
    const showPreview =
      landMode === "activity_2026" &&
      activityStatus === "ready" &&
      activityPreviewVisible &&
      landScope !== "wua";
    const showReview =
      landMode === "activity_2026" &&
      activityStatus === "ready" &&
      activityReviewVisible &&
      landScope !== "wua";
    const showHousehold =
      landMode === "land_use_type" &&
      activityStatus === "ready" &&
      householdVisible &&
      landScope !== "wua";
    const showCropType =
      landMode === "land_use_type" &&
      cropTypeStatus === "ready" &&
      landScope !== "wua";
    const showHistory =
      landMode === "history_2021_2025" &&
      historyStatus === "ready" &&
      landScope !== "wua";

    const withScope = (conditions) => {
      const scoped = [...conditions];
      if (landScope === "stage_1" || landScope === "stage_2") {
        scoped.push(["==", ["get", "stage"], landScope]);
      }
      return ["all", ...scoped];
    };
    const setLayerState = (id, visible, filter, opacityProperty, opacity) => {
      if (!map.getLayer(id)) return;
      map.setFilter(id, filter);
      map.setLayoutProperty(id, "visibility", visible ? "visible" : "none");
      map.setPaintProperty(id, opacityProperty, visible ? opacity : 0);
    };

    const enabledActivityClasses = Object.entries(activityClassVisibility)
      .filter(([, visible]) => visible)
      .map(([activityClass]) => activityClass);
    const activityFilter = withScope([
      ["==", ["get", "activity_state"], "ready"],
      ["==", ["get", "household"], false],
      ["==", ["get", "road_excluded"], false],
      ["in", ["get", "activity_class"], ["literal", enabledActivityClasses]],
    ]);
    setLayerState(
      "land-activity-preview-fill",
      showPreview && enabledActivityClasses.length > 0,
      activityFilter,
      "fill-opacity",
      activityOpacity / 100,
    );

    const reviewFilter = withScope([
      ["==", ["get", "activity_state"], "review"],
      ["==", ["get", "household"], false],
      ["==", ["get", "road_excluded"], false],
    ]);
    setLayerState(
      "land-activity-review-fill",
      showReview,
      reviewFilter,
      "fill-opacity",
      0.72,
    );
    setLayerState(
      "land-activity-review-line",
      showReview,
      reviewFilter,
      "line-opacity",
      0.95,
    );

    const householdFilter = withScope([
      ["==", ["get", "household"], true],
      ["==", ["get", "road_excluded"], false],
    ]);
    setLayerState(
      "land-household-fill",
      showHousehold,
      householdFilter,
      "fill-opacity",
      0.72,
    );
    setLayerState(
      "land-household-line",
      showHousehold,
      householdFilter,
      "line-opacity",
      0.95,
    );

    Object.keys(CROP_TYPE_CLASS_COLORS).forEach((cropType) => {
      const annualUsesCycleLayers = cropType === "annual" && annualCyclesPayload;
      const visible =
        showCropType && cropTypeClassVisibility[cropType] && !annualUsesCycleLayers;
      const filter = withScope([["==", ["get", "crop_type"], cropType]]);
      setLayerState(
        `land-crop-type-${cropType}-fill`,
        visible,
        filter,
        "fill-opacity",
        cropTypeOpacity / 100,
      );
      setLayerState(
        `land-crop-type-${cropType}-line`,
        visible,
        filter,
        "line-opacity",
        0.9,
      );
    });

    Object.keys(ANNUAL_CYCLE_CLASS_COLORS).forEach((annualCycle) => {
      const visible =
        showCropType &&
        Boolean(annualCyclesPayload) &&
        cropTypeClassVisibility.annual &&
        annualCycleVisibility[annualCycle];
      const filter = withScope([
        ["==", ["get", "crop_type"], "annual"],
        ["==", ["get", "annual_cycle"], annualCycle],
      ]);
      setLayerState(
        `land-annual-cycle-${annualCycle}-fill`,
        visible,
        filter,
        "fill-opacity",
        cropTypeOpacity / 100,
      );
      setLayerState(
        `land-annual-cycle-${annualCycle}-line`,
        visible,
        filter,
        "line-opacity",
        0.9,
      );
    });

    Object.keys(HISTORY_CLASS_COLORS).forEach((historyClass) => {
      const visible = showHistory && historyClassVisibility[historyClass];
      const filter = withScope([
        ["==", ["get", "history_class"], historyClass],
      ]);
      setLayerState(
        `land-history-${historyClass}-fill`,
        visible,
        filter,
        "fill-opacity",
        historyOpacity / 100,
      );
      setLayerState(
        `land-history-${historyClass}-line`,
        visible,
        filter,
        "line-opacity",
        0.9,
      );
    });
  }, [
    activityOpacity,
    activityClassVisibility,
    activityPreviewVisible,
    activityReviewVisible,
    activityStatus,
    householdVisible,
    annualCyclesPayload,
    annualCycleVisibility,
    cropTypeClassVisibility,
    cropTypeOpacity,
    cropTypeStatus,
    historyClassVisibility,
    historyOpacity,
    historyStatus,
    landMode,
    landScope,
    mapReady,
  ]);

  useEffect(() => {
    const map = mapRef.current;
    if (!map || !mapReady) return;
    const thematicLayerIsPrimary =
      landScope !== "wua" &&
      ((landMode === "activity_2026" && activityPreviewVisible) ||
        (landMode === "land_use_type" && cropTypeStatus === "ready") ||
        (landMode === "history_2021_2025" && historyStatus === "ready") ||
        (landMode === "potential" && Boolean(potentialPayload)) || ["consolidation", "activity_change", "degradation", "irrigation_stress"].includes(landMode));
    if (map.getLayer("lower-hrazdan-halo-stage-1-fill")) {
      map.setPaintProperty(
        "lower-hrazdan-halo-stage-1-fill",
        "fill-opacity",
        thematicLayerIsPrimary ? 0.04 : 0.3,
      );
    }
    if (map.getLayer("lower-hrazdan-halo-stage-2-fill")) {
      map.setPaintProperty(
        "lower-hrazdan-halo-stage-2-fill",
        "fill-opacity",
        thematicLayerIsPrimary ? 0.04 : 0.27,
      );
    }
  }, [activityPreviewVisible, cropTypeStatus, historyStatus, landMode, landScope, mapReady, potentialPayload]);

  useEffect(() => {
    const map = mapRef.current;
    if (!map || !mapReady) return;
    const visibleStages = [
      stageOneVisible ? "stage_1" : null,
      stageTwoVisible ? "stage_2" : null,
    ].filter(Boolean);
    const stageFilter = (property) => [
      "in",
      ["get", property],
      ["literal", visibleStages],
    ];
    CANAL_LINE_LAYERS.forEach((id) => {
      if (map.getLayer(id)) map.setFilter(id, stageFilter("stage"));
    });
    CANAL_POINT_LAYERS.forEach((id) => {
      if (map.getLayer(id)) map.setFilter(id, stageFilter("nearest_stage"));
    });
    canalLabelElementsRef.current.forEach(({ element, stage }) => {
      element.style.display = visibleStages.includes(stage) ? "block" : "none";
    });
  }, [mapReady, stageOneVisible, stageTwoVisible]);

  useEffect(() => {
    const map = mapRef.current;
    if (!map || !mapReady) return;
    STAGE_ONE_HALO_LAYERS.forEach((id) => {
      if (map.getLayer(id)) {
        map.setLayoutProperty(id, "visibility", stageOneHaloVisible ? "visible" : "none");
      }
    });
  }, [mapReady, stageOneHaloVisible]);

  useEffect(() => {
    const map = mapRef.current;
    if (!map || !mapReady) return;
    STAGE_TWO_HALO_LAYERS.forEach((id) => {
      if (map.getLayer(id)) {
        map.setLayoutProperty(id, "visibility", stageTwoHaloVisible ? "visible" : "none");
      }
    });
  }, [mapReady, stageTwoHaloVisible]);

  useEffect(() => {
    const map = mapRef.current;
    if (!map || !mapReady) return;
    CADASTRE_LAYERS.forEach((id) => {
      if (map.getLayer(id)) {
        map.setLayoutProperty(id, "visibility", cadastreVisible ? "visible" : "none");
      }
    });
    if (!cadastreVisible) popupRef.current?.remove();
  }, [cadastreVisible, mapReady]);

  useEffect(() => {
    const map = mapRef.current;
    if (!map || !mapReady) return;
    const opacity = cadastreOpacity / 100;
    if (map.getLayer("cadastre-line")) {
      map.setPaintProperty("cadastre-line", "line-opacity", opacity);
    }
    if (map.getLayer("cadastre-overview")) {
      map.setPaintProperty("cadastre-overview", "raster-opacity", [
        "interpolate",
        ["linear"],
        ["zoom"],
        8,
        opacity * 0.72,
        11,
        opacity * 0.88,
        13.5,
        opacity * 0.94,
        14.2,
        opacity * 0.06,
      ]);
    }
    if (map.getLayer("cadastre-overview-low")) {
      map.setPaintProperty("cadastre-overview-low", "raster-opacity", [
        "interpolate",
        ["linear"],
        ["zoom"],
        8,
        opacity,
        10.5,
        opacity * 0.88,
        12.2,
        opacity * 0.05,
      ]);
    }
  }, [cadastreOpacity, mapReady]);

  const handleSearch = async (event) => {
    event.preventDefault();
    const code = searchCode.trim().replace(/[–—]/g, "-");
    if (!/^\d{2}-\d{3}-\d{4}-\d{4}$/.test(code)) {
      setSearchStatus("Մուտքագրեք ամբողջական կադաստրային ծածկագիրը։");
      return;
    }

    setSearching(true);
    setSearchStatus("");
    try {
      const response = await fetch(`/api/cadastre/search?code=${encodeURIComponent(code)}`);
      if (response.status === 404) {
        setSearchStatus("Այս ծածկագրով հողամաս չի գտնվել։");
        return;
      }
      if (!response.ok) throw new Error("Cadastre search failed");
      const parcel = await response.json();
      const map = mapRef.current;
      if (!map) return;
      setCadastreVisible(true);
      if (window.innerWidth <= 640) setLayerPanelOpen(false);
      setSearchCode(parcel.cadastre_code);
      setSearchStatus("");
      selectParcel(
        {
          cadastreCode: parcel.cadastre_code,
          areaHa: parcel.area_ha,
        },
        parcel.center,
      );
      map.fitBounds(
        [
          [parcel.bbox[0], parcel.bbox[1]],
          [parcel.bbox[2], parcel.bbox[3]],
        ],
        {
          padding: window.innerWidth > 720
            ? { top: 110, right: 350, bottom: 90, left: 90 }
            : { top: 190, right: 42, bottom: 70, left: 42 },
          maxZoom: 17,
          duration: 750,
        },
      );
    } catch (error) {
      console.error(error);
      setSearchStatus("Որոնումը ժամանակավորապես անհասանելի է։");
    } finally {
      setSearching(false);
    }
  };

  const activeLandMode = LAND_MODES.find(({ id }) => id === landMode) || LAND_MODES[0];
  const activitySummary = activityPayload?.summaries?.[landScope] || null;
  const historySummary = historyPayload?.summaries?.[landScope] || null;
  const cropTypeSummary = cropTypePayload?.summaries?.[landScope] || null;
  const annualCyclesSummary = annualCyclesPayload?.summaries?.[landScope] || null;

  return (
    <main
      className="product"
      aria-label="Ջրային և հողային ռեսուրսների կառավարման համակարգ"
    >
      <div ref={containerRef} className="map" aria-label="Esri satellite map" />

      <AgentPanel map={mapRef.current} ready={mapReady} scope={landScope} mode={landMode} includeExpansion={agentExpansion} selectedCode={agentSelectedCode} />

      <header className="identity-bar">
        <strong>Ջրային և հողային ռեսուրսների կառավարման համակարգ</strong>
        <span>Ստեղծող՝ Suren Nazaryan</span>
      </header>

      <aside className={`layer-panel ${layerPanelOpen ? "is-open" : ""}`} aria-label="Քարտեզի շերտեր">
        <button
          className="layer-panel-toggle"
          type="button"
          aria-expanded={layerPanelOpen}
          onClick={() => setLayerPanelOpen((open) => !open)}
        >
          <span>Շերտեր</span>
          <span aria-hidden="true">{layerPanelOpen ? "−" : "+"}</span>
        </button>

        {layerPanelOpen && (
          <div className="layer-panel-body">
            <section className="layer-group" aria-labelledby="canal-layers-title">
              <h2 id="canal-layers-title">Ջրանցքներ</h2>
              <p className="layer-subgroup-title">Ստորին Հրազդան</p>

              <label className="layer-option layer-option--nested">
                <input
                  type="checkbox"
                  checked={stageOneVisible}
                  onChange={(event) => setStageOneVisible(event.target.checked)}
                />
                <span className="layer-visual layer-visual--line stage-one" aria-hidden="true" />
                <span>Ստորին Հրազդան I հերթ</span>
              </label>
              <label className="layer-option layer-option--nested">
                <input
                  type="checkbox"
                  checked={stageOneHaloVisible}
                  onChange={(event) => setStageOneHaloVisible(event.target.checked)}
                />
                <span className="layer-visual layer-visual--halo stage-one" aria-hidden="true" />
                <span>I հերթի տարածքի ուրվագիծ</span>
              </label>
              <label className="layer-option layer-option--nested">
                <input
                  type="checkbox"
                  checked={stageTwoVisible}
                  onChange={(event) => setStageTwoVisible(event.target.checked)}
                />
                <span className="layer-visual layer-visual--line stage-two" aria-hidden="true" />
                <span>Ստորին Հրազդան II հերթ</span>
              </label>
              <label className="layer-option layer-option--nested">
                <input
                  type="checkbox"
                  checked={stageTwoHaloVisible}
                  onChange={(event) => setStageTwoHaloVisible(event.target.checked)}
                />
                <span className="layer-visual layer-visual--halo stage-two" aria-hidden="true" />
                <span>II հերթի տարածքի ուրվագիծ</span>
              </label>
            </section>

            <section className="layer-group" aria-labelledby="land-layers-title">
              <h2 id="land-layers-title">Հողային ռեսուրսներ</h2>

              <div className="land-control-block">
                <p className="layer-subgroup-title">Տարածք</p>
                <div className="land-button-grid" role="group" aria-label="Վերլուծության տարածք">
                  {LAND_SCOPES.map((scope) => (
                    <button
                      key={scope.id}
                      type="button"
                      className={landScope === scope.id ? "is-active" : ""}
                      aria-pressed={landScope === scope.id}
                      disabled={scope.available === false}
                      title={scope.available === false ? "Տվյալները դեռ չեն հաշվարկվել" : undefined}
                      onClick={() => setLandScope(scope.id)}
                    >
                      {scope.label}
                    </button>
                  ))}
                </div>
              </div>

              <div className="land-control-block">
                <p className="layer-subgroup-title">Վերլուծություն</p>
                <div className="land-mode-grid" role="tablist" aria-label="Հողային վերլուծություն">
                  {LAND_MODES.map((mode) => (
                    <button
                      key={mode.id}
                      type="button"
                      role="tab"
                      id={`land-tab-${mode.id}`}
                      data-mode={mode.id}
                      className={landMode === mode.id ? "is-active" : ""}
                      aria-selected={landMode === mode.id}
                      onClick={() => setLandMode(mode.id)}
                    >
                      {mode.label}
                    </button>
                  ))}
                </div>
              </div>

              <p className="parcel-area-note">Հողամասերի մակերեսը՝ ըստ կադաստրի</p>
              {landMode === "activity_2026" && (
                <div className="activity-preview" aria-label="2026 ակտիվության նախնական դիտարկում">
                  <label className="layer-option layer-option--activity-preview">
                    <input
                      type="checkbox"
                      checked={activityPreviewVisible}
                      disabled={activityStatus !== "ready" || landScope === "wua"}
                      onChange={(event) => setActivityPreviewVisible(event.target.checked)}
                    />
                    <span className="activity-gradient-swatch" aria-hidden="true" />
                    <span>Դիտարկված ակտիվ բաժին</span>
                  </label>

                  <div className="activity-gradient" aria-label="Ակտիվության բաժին՝ 0-ից 100 տոկոս">
                    <span>0%</span>
                    <span aria-hidden="true" />
                    <span>100%</span>
                  </div>

                  <label className="layer-option layer-option--activity-review">
                    <input
                      type="checkbox"
                      checked={activityReviewVisible}
                      disabled={activityStatus !== "ready" || landScope === "wua"}
                      onChange={(event) => setActivityReviewVisible(event.target.checked)}
                    />
                    <span className="activity-review-swatch" aria-hidden="true" />
                    <span>Կարգավիճակը պարզելու համար անհրաժեշտ է լրացուցիչ ստուգում</span>
                  </label>

                  <label className="opacity-control opacity-control--activity">
                    <span>Թափանցիկություն</span>
                    <output>{activityOpacity}%</output>
                    <input
                      type="range"
                      min="20"
                      max="85"
                      step="5"
                      value={activityOpacity}
                      disabled={!activityPreviewVisible || activityStatus !== "ready"}
                      onChange={(event) => setActivityOpacity(Number(event.target.value))}
                    />
                  </label>

                  {activityStatus === "loading" && (
                    <p className="activity-preview-status">Տվյալները բեռնվում են…</p>
                  )}
                  {activityStatus === "error" && (
                    <p className="activity-preview-status activity-preview-status--error">
                      Շերտը ժամանակավորապես անհասանելի է
                    </p>
                  )}
                  {activityStatus === "ready" && activitySummary && (
                    <dl className="activity-summary">
                      <div>
                        <dt>Բաց դաշտերի հողամասեր</dt>
                        <dd><AreaMetric count={activitySummary.open_field_parcel_count} area={activitySummary.official_areas_ha?.open_field}/></dd>
                      </div>
                      <div>
                        <dt>Դիտարկված ակտիվ մակերես</dt>
                        <dd>{formatArea(activitySummary.observed_active_area_ha)} հա</dd>
                      </div>
                      <div>
                        <dt>Լրացուցիչ ստուգում</dt>
                        <dd><AreaMetric count={activitySummary.activity_review_parcel_count} area={activitySummary.official_areas_ha?.review}/></dd>
                      </div>
                    </dl>
                  )}
                </div>
              )}

              {landMode === "history_2021_2025" && (
                <div className="activity-preview" aria-label="2021–2025 հողօգտագործման պատմություն">
                  <label className="opacity-control opacity-control--activity">
                    <span>Թափանցիկություն</span>
                    <output>{historyOpacity}%</output>
                    <input
                      type="range"
                      min="20"
                      max="85"
                      step="5"
                      value={historyOpacity}
                      disabled={historyStatus !== "ready" || landScope === "wua"}
                      onChange={(event) => setHistoryOpacity(Number(event.target.value))}
                    />
                  </label>

                  {historyStatus === "loading" && (
                    <p className="activity-preview-status">Պատմությունը բեռնվում է…</p>
                  )}
                  {historyStatus === "error" && (
                    <p className="activity-preview-status activity-preview-status--error">
                      Պատմական շերտը ժամանակավորապես անհասանելի է
                    </p>
                  )}
                  {historyStatus === "ready" && historySummary && (
                    <dl className="activity-summary">
                      <div>
                        <dt>Վերլուծվող հողամասեր</dt>
                        <dd><AreaMetric count={historySummary.eligible_parcel_count} area={historySummary.official_areas_ha?.eligible}/></dd>
                      </div>
                      <div>
                        <dt>EO մշակված հողամասեր</dt>
                        <dd><AreaMetric count={historySummary.processed_parcel_count} area={historySummary.official_areas_ha?.processed}/></dd>
                      </div>
                      <div>
                        <dt>Ավտոմատ դասակարգված</dt>
                        <dd><AreaMetric count={historySummary.automatic_classified_count} area={historySummary.official_areas_ha?.automatic}/></dd>
                      </div>
                      <div>
                        <dt>Պահանջվում է լրացուցիչ ստուգում</dt>
                        <dd><AreaMetric count={historySummary.review_count} area={historySummary.official_areas_ha?.review}/></dd>
                      </div>
                    </dl>
                  )}
                </div>
              )}

              {landMode === "land_use_type" && (
                <div className="activity-preview" aria-label="Հողօգտագործման տեսակի նախնական դասակարգում">
                  <label className="opacity-control opacity-control--activity">
                    <span>Թափանցիկություն</span>
                    <output>{cropTypeOpacity}%</output>
                    <input
                      type="range"
                      min="20"
                      max="85"
                      step="5"
                      value={cropTypeOpacity}
                      disabled={cropTypeStatus !== "ready" || landScope === "wua"}
                      onChange={(event) => setCropTypeOpacity(Number(event.target.value))}
                    />
                  </label>

                  {cropTypeStatus === "loading" && (
                    <p className="activity-preview-status">Տեսակները բեռնվում են…</p>
                  )}
                  {cropTypeStatus === "error" && (
                    <p className="activity-preview-status activity-preview-status--error">
                      Տեսակների շերտը ժամանակավորապես անհասանելի է
                    </p>
                  )}
                  {cropTypeStatus === "ready" && cropTypeSummary && (
                    <dl className="activity-summary">
                      <div>
                        <dt>Ժամանակահատված</dt>
                        <dd>2021–2025</dd>
                      </div>
                      <div>
                        <dt>Վերլուծվող հողամասեր</dt>
                        <dd><AreaMetric count={cropTypeSummary.eligible_parcel_count} area={cropTypeSummary.official_areas_ha?.eligible}/></dd>
                      </div>
                      <div>
                        <dt>Միամյա մշակաբույսեր</dt>
                        <dd><AreaMetric count={cropTypeSummary.class_counts.annual} area={cropTypeSummary.official_areas_ha?.classes?.annual}/></dd>
                      </div>
                      <div>
                        <dt>Բազմամյա մշակաբույսեր</dt>
                        <dd><AreaMetric count={cropTypeSummary.class_counts.perennial} area={cropTypeSummary.official_areas_ha?.classes?.perennial}/></dd>
                      </div>
                      <div>
                        <dt>Տեսակը չի որոշվել</dt>
                        <dd>
                          <AreaMetric count={cropTypeSummary.class_counts.undetermined} area={cropTypeSummary.official_areas_ha?.classes?.undetermined}/>
                        </dd>
                      </div>
                    </dl>
                  )}
                  {cropTypeStatus === "ready" && annualCyclesSummary && (
                    <dl className="activity-summary annual-cycle-summary">
                      <div>
                        <dt>Միամյա · մեկ ցիկլ</dt>
                        <dd><AreaMetric count={annualCyclesSummary.class_counts.single_cycle} area={annualCyclesSummary.official_areas_ha?.classes?.single_cycle}/></dd>
                      </div>
                      <div>
                        <dt>Միամյա · երկու ցիկլ</dt>
                        <dd>
                          <AreaMetric count={annualCyclesSummary.class_counts.two_cycle_recurring} area={annualCyclesSummary.official_areas_ha?.classes?.two_cycle_recurring}/>
                        </dd>
                      </div>
                    </dl>
                  )}
                </div>
              )}

              {landMode === "consolidation" && <ConsolidationView model={consolidation} />}
              {landMode === "degradation" && <DegradationView model={degradation} />}
              {landMode === "irrigation_stress" && <IrrigationStressView model={irrigationStress} />}
              {landMode === "activity_change" && <ActivityChangeView model={activityChange} />}
              <PotentialView onExpansionChange={setAgentExpansion} map={mapRef.current} ready={mapReady} payload={potentialPayload} scope={landScope} active={landMode === "potential"} />
              <div className="land-class-list" aria-label={activeLandMode.label} style={["potential", "consolidation", "activity_change", "degradation", "irrigation_stress"].includes(landMode) ? {display: "none"} : undefined}>
                {activeLandMode.classes.map((landClass) => {
                  const activityControl =
                    landMode === "activity_2026" &&
                    Object.hasOwn(activityClassVisibility, landClass.id);
                  const householdControl =
                    landMode === "land_use_type" && landClass.id === "household";
                  const cropTypeControl =
                    landMode === "land_use_type" &&
                    Object.hasOwn(cropTypeClassVisibility, landClass.id);
                  const historyControl =
                    landMode === "history_2021_2025" &&
                    Object.hasOwn(historyClassVisibility, landClass.id);
                  const enabledControl =
                    activityControl || householdControl || cropTypeControl || historyControl;
                  const checked = activityControl
                    ? activityClassVisibility[landClass.id]
                    : householdControl
                      ? householdVisible
                      : cropTypeControl
                        ? cropTypeClassVisibility[landClass.id]
                      : historyControl
                        ? historyClassVisibility[landClass.id]
                        : false;
                  const controlReady = cropTypeControl
                    ? cropTypeStatus === "ready"
                    : historyControl
                      ? historyStatus === "ready"
                      : activityStatus === "ready";
                  const classCount = activityControl ? activitySummary?.activity_class_counts?.[landClass.id]
                    : householdControl ? activitySummary?.household_parcel_count
                    : cropTypeControl ? cropTypeSummary?.class_counts?.[landClass.id]
                    : historyControl ? historySummary?.class_counts?.[landClass.id] : null;
                  const classArea = activityControl ? activitySummary?.official_areas_ha?.classes?.[landClass.id]
                    : householdControl ? activitySummary?.official_areas_ha?.household
                    : cropTypeControl ? cropTypeSummary?.official_areas_ha?.classes?.[landClass.id]
                    : historyControl ? historySummary?.official_areas_ha?.classes?.[landClass.id] : null;
                  const classOption = (
                    <label className="layer-option layer-option--land">
                      <input
                        type="checkbox"
                        checked={checked}
                        disabled={
                          !enabledControl ||
                          !controlReady ||
                          landScope === "wua" ||
                          (activityControl && !activityPreviewVisible)
                        }
                        onChange={
                          activityControl
                            ? (event) =>
                                setActivityClassVisibility((current) => ({
                                  ...current,
                                  [landClass.id]: event.target.checked,
                                }))
                            : householdControl
                              ? (event) => setHouseholdVisible(event.target.checked)
                              : cropTypeControl
                                ? (event) =>
                                    setCropTypeClassVisibility((current) => ({
                                      ...current,
                                      [landClass.id]: event.target.checked,
                                    }))
                              : historyControl
                                ? (event) =>
                                    setHistoryClassVisibility((current) => ({
                                      ...current,
                                      [landClass.id]: event.target.checked,
                                    }))
                                : undefined
                        }
                      />
                      <span
                        className="land-class-swatch"
                        style={{ backgroundColor: landClass.color }}
                        aria-hidden="true"
                      />
                      <span>{landClass.label}{classCount != null && <small className="parcel-class-metric">{formatCountArea(classCount, classArea)}</small>}</span>
                    </label>
                  );

                  if (
                    landMode === "land_use_type" &&
                    landClass.id === "annual" &&
                    annualCyclesPayload
                  ) {
                    return (
                      <div className="annual-cycle-branch" key={landClass.id}>
                        <div className="annual-cycle-parent">
                          {classOption}
                          <button
                            type="button"
                            className="annual-cycle-expand"
                            aria-expanded={annualExpanded}
                            aria-label={annualExpanded ? "Փակել ցիկլերը" : "Բացել ցիկլերը"}
                            onClick={() => setAnnualExpanded((expanded) => !expanded)}
                          >
                            {annualExpanded ? "−" : "+"}
                          </button>
                        </div>
                        {annualExpanded && (
                          <div className="annual-cycle-children">
                            {Object.entries(ANNUAL_CYCLE_LABELS).map(
                              ([annualCycle, label]) => (
                                <label
                                  className="layer-option layer-option--land layer-option--annual-cycle"
                                  key={annualCycle}
                                >
                                  <input
                                    type="checkbox"
                                    checked={annualCycleVisibility[annualCycle]}
                                    disabled={cropTypeStatus !== "ready" || landScope === "wua"}
                                    onChange={(event) => {
                                      const visible = event.target.checked;
                                      setAnnualCycleVisibility((current) => ({
                                        ...current,
                                        [annualCycle]: visible,
                                      }));
                                      if (visible) {
                                        setCropTypeClassVisibility((current) => ({
                                          ...current,
                                          annual: true,
                                        }));
                                      }
                                    }}
                                  />
                                  <span
                                    className="land-class-swatch"
                                    style={{
                                      backgroundColor: ANNUAL_CYCLE_CLASS_COLORS[annualCycle],
                                    }}
                                    aria-hidden="true"
                                  />
                                  <span>
                                    {label}
                                    {annualCyclesSummary
                                      ? ` · ${formatCountArea(annualCyclesSummary.class_counts[annualCycle], annualCyclesSummary.official_areas_ha?.classes?.[annualCycle])}`
                                      : ""}
                                  </span>
                                </label>
                              ),
                            )}
                          </div>
                        )}
                      </div>
                    );
                  }

                  return <React.Fragment key={landClass.id}>{classOption}</React.Fragment>;
                })}
              </div>

              <div className="land-schema-status" role="status" hidden={["consolidation", "activity_change", "degradation", "irrigation_stress"].includes(landMode)}>
                {landMode === "potential" ? "Նախնական թեկնածուներ․ ոռոգման հնարավորությունը դեռ հաստատված չէ" : landMode === "activity_2026"
                  ? "Նախնական դիտարկում․ դասերի սահմանները դեռ հաստատված չեն"
                  : landMode === "land_use_type" && cropTypeStatus === "ready" && activitySummary
                    ? `Տնամերձ գյուղատնտեսություն՝ ${formatCount(activitySummary.household_parcel_count)} հողամաս (${formatArea(activitySummary.official_areas_ha?.household)} հա) · 2021–2025 EO դասակարգումը դեռ ենթակա է տեսողական հաստատման`
                    : landMode === "history_2021_2025" && historyStatus === "ready" && historySummary
                      ? `EO պատմությունը դեռ հաշվարկված չէ ${formatCountArea(historySummary.class_counts.not_calculated, historySummary.official_areas_ha?.classes?.not_calculated)} հողամասի համար · լրացուցիչ ստուգում՝ ${formatCountArea(historySummary.class_counts.insufficient, historySummary.official_areas_ha?.classes?.insufficient)}`
                    : "Վերլուծական շերտերը դեռ միացված չեն"}
              </div>

              <p className="layer-subgroup-title layer-subgroup-title--separated">Կադաստրային հիմք</p>
              <label className="layer-option">
                <input
                  type="checkbox"
                  checked={cadastreVisible}
                  onChange={(event) => setCadastreVisible(event.target.checked)}
                />
                <span className="layer-swatch" aria-hidden="true" />
                <span>Կադաստրային քարտեզ</span>
              </label>

              <label className="opacity-control">
                <span>Թափանցիկություն</span>
                <output>{cadastreOpacity}%</output>
                <input
                  type="range"
                  min="20"
                  max="100"
                  step="5"
                  value={cadastreOpacity}
                  disabled={!cadastreVisible}
                  onChange={(event) => setCadastreOpacity(Number(event.target.value))}
                />
              </label>

              <form className="parcel-search" onSubmit={handleSearch}>
                <label htmlFor="parcel-code">Հողամասի որոնում</label>
                <div className="parcel-search-row">
                  <input
                    id="parcel-code"
                    type="search"
                    value={searchCode}
                    autoComplete="off"
                    spellCheck="false"
                    placeholder="04-013-0122-0013"
                    onChange={(event) => {
                      setSearchCode(event.target.value);
                      setSearchStatus("");
                    }}
                  />
                  <button type="submit" disabled={searching}>
                    {searching ? "…" : "Գտնել"}
                  </button>
                </div>
                <p className="search-status" aria-live="polite">{searchStatus}</p>
              </form>
            </section>
          </div>
        )}
      </aside>

      <div className="place-label" aria-label="Study area">
        <span aria-hidden="true" />
        Էջմիածին ջրօգտագործողների ընկերություն
      </div>
    </main>
  );
}
