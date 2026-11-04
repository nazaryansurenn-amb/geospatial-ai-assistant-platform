# Geospatial AI Assistant Platform

[![CI](https://github.com/nazaryansurenn-amb/geospatial-ai-assistant-platform/actions/workflows/ci.yml/badge.svg)](https://github.com/nazaryansurenn-amb/geospatial-ai-assistant-platform/actions/workflows/ci.yml)

A map-based decision-support platform for parcel-level land and water analysis in an irrigated
area of Armenia. It has three parts:

1. **Land and water analytics.** A Sentinel-2 and ERA5 pipeline, deterministic parcel analytics
   and a React/MapLibre map, with a tool-using LLM assistant (OpenAI function calling).
2. **Land types.** A parcel land-type map built from satellite embeddings and reviewed photo
   samples, with a two-level legend.
3. **Irrigation Assistant (in progress).** A multi-agent system on the Claude Agent SDK that
   simulates water sharing on a canal: plans, rotations and explanations on a shared map.

The same rule holds everywhere: the language models handle language, routing and tool choice.
Every number they report comes from fixed Python code over prepared tables.

Case study: about 44,000 cadastral parcels in the Echmiadzin area and the Lower Hrazdan zone, with
22,802 open-field parcels in the main analytical scope and five seasons of satellite history
(2021–2025).

Code, tests and method documents only. The parcel data, satellite archive, analysis outputs and
built releases are not included (see [Data](#data)).

## 1. Land and water analytics

| Section | What it shows | What it does not establish |
|---|---|---|
| Current land activity | 2026 activity per parcel: active, partial, none observed | No observation is not proof of abandonment |
| Land-use type | Annual and perennial candidate profiles from multi-year phenology, households separate | Classification accuracy is not independently validated |
| Five-year history | Parcel states across the 2021–2025 seasons | Missing observations are kept apart from inactivity |
| Land potential | Parcels near the canal zone that merit further assessment | Not confirmed available or irrigable land |
| Consolidation | Connected groups of small active parcels with similar phenology: 31 groups, 262 parcels, 120.36 ha | No ownership, consent or legal feasibility |
| Cultivation change | Sustained activation or cessation during 2021–2025, with the year of change | Whole-parcel area, not measured cultivated area |
| Possible degradation | Historical deterioration or persistently weak patterns | Not a diagnosis of soil degradation or salinity |
| Possible irrigation stress | Recent optical signal against history, neighbours and weather context | Not a measure of irrigation volume or demand |

The map also has satellite imagery, community boundaries, cadastral search, parcel cards with
official area, canal sections and analytical-zone controls. The interface is in Armenian.

### Map assistant: LLM function calling

The assistant runs server-side through the OpenAI Responses API (`wp_core/agent_service_v7.py`).
The model is set with `OPENAI_COPILOT_MODEL`.

1. The user asks a question in Armenian, Russian, English or transliterated text. The current map
   scope and language come with it.
2. The model chooses one of three tools:
   - `analyze_land`: filters, groupings and comparisons across territories and sections;
   - `measure_active_area`: observed active area, as opposed to official parcel area;
   - `get_parcel_profile`: one parcel's full analytical record.
3. The server validates the arguments and runs the tool as deterministic Python against prepared
   tables. The model receives a limited, sanitised result.
4. Up to 8 tool rounds are allowed per answer. Tool calls run one at a time, each model request
   times out after 55 s, and output is capped at 2,400 tokens.
5. One result feeds the answer text, a table, a chart, an Excel export (`wp_core/agent_excel*.py`)
   and, where supported, a proposed map selection. The user applies the selection and can reverse
   it.

The assistant has no shell, SQL, filesystem or web access, and it cannot reclassify parcels or
publish a release. Questions outside the platform's scope are redirected.

### Earth-observation pipeline

`wp_core/data_collector/` is a standalone, restartable collector (`run_data_collector.py`).

- **Sentinel-2 L2A.** Per-pixel reflectance indices, aggregated to the unchanged parcel geometry at
  their native 10 m or 20 m resolution. Results are stored as Parquet partitions by year, with
  atomic writes and a catalogue.
- **18 indices** (`indices.py`, each with its formula): NDVI, EVI, EVI2, GNDVI, SAVI, MSAVI2, NDRE,
  CI red-edge, MTCI, IRECI, NDMI, MSI, BSI, NDWI, MNDWI, NBR, NBR2, PSRI.
- **Scale, 2021–2025:** 762 scenes and 17,375,124 parcel-scene records for 22,802 parcels. These
  are repeated observations, not unique parcels.
- **Weather.** ERA5-Land through the Copernicus CDS API (`weather.py`): hourly records, local-day
  summaries and reference evapotranspiration. Daily ERA5 series from 1961 onward
  (`scripts/build_season_weather_era5_v1.py`) feed the season replay and the water planner.
- **Sentinel-1 radar pilots** (`run_sentinel1_*`) are exploratory and feed no map layer.

## 2. Land types

Parcels get a land type from 64-dimensional annual satellite embeddings (Google AlphaEarth),
clustered and then labelled from reviewed high-resolution photo samples.

- **Pipeline:** `scripts/alphaearth_download.py`, `alphaearth_cluster.py` and
  `alphaearth_parcels.py` prepare the embeddings and per-parcel summaries. The decision rules are
  in `wp_core/land_types.py` and `config/alphaearth_types_*.json`.
- **Review:** photo review pages and a command-line reviewer (`scripts/alphaearth_photo_review.py`,
  `alphaearth_review_cli.py`). Review answers are reconciled, blind-scored and audited
  (`alphaearth_review_*.py`). Accuracy estimates use stratified (Stehman) estimators.
- **Legend:** two levels.
  - Field land: crops, no crop, use not specified.
  - Perennial: orchard, vineyard, type not specified.
  - Greenhouse, not farmland, unclassified, not classifiable.
- **Map:** `scripts/alphaearth_landtypes_map.py` builds the parcel map with the legend and popups.

A land type is a classification from imagery, not a field survey or a cadastral land category.

## 3. Irrigation Assistant (in progress)

A simulation of water sharing on the Lower Hrazdan canal, stages I and II, for a water users'
association. It is a planning and explanation tool. It gives no water orders and changes nothing
in the field.

### The canal model

- **Network.** The canal line is real. The distribution network below it is **synthetic**: a
  rule-built tree of main-canal gates, secondary canals, tertiary canals, gates, direct outlets and
  tail escapes, reaching every farm parcel in the Lower Hrazdan observed zone
  (`scripts/canal_ops_network_v3.py`). Terrain screening sorts gate clusters into gravity,
  pumping and "terrain uncertain" areas. No head or pump design figures are produced.
- **Water sharing.** The head flow is capped at an assumed 12 m³/s main canal capacity.
  `wp_core/canal_tree_allocation.py` shares it level by level down the tree within capacities,
  losses and minimum flows. Shortage rules protect perennials, and no unit goes without water
  twice in a row. The water balance closes within 0.1% per dekad.
- **Rotations.** `wp_core/canal_rotation.py` turns allocations into turn schedules: canal inflows
  change only at 07:00 and 19:00, with travel time and ramp limits; hand streams move between
  outlets on quarter hours; pumps run whole units.
- **Demand.** Crop water need per parcel and dekad: a satellite crop coefficient times ET0, less
  effective rain, with a soil-water reserve and field efficiency (`scripts/canal_ops_demand_v1.py`).
- **Water planner** (`wp_core/canal_planner_*.py`): a daily soil-water state per parcel, days to
  stress, an urgency score and plans for a day, a week or a dekad, on any simulated date of 2025
  with no hindsight. The engine is built and tested; it is not on the page yet.

Every assumed value is labelled "assumed", kept in a versioned config (`config/canal_*.json`) and
can be replaced by a real figure later.

### Agents

Twelve agents run on the Claude Agent SDK (`scripts/irrigation_assistant.py`,
`scripts/canal_ops_live.py`):

- the main agent, which answers and routes;
- the specialists: planner, reviewer, agronomy, data auditor, season monitor, season preparation
  and report writer;
- staff questions, request intake, a watchdog, and a public Q&A agent with no map tools.

The safeguards are all fixed code:

| Safeguard | What it does |
|---|---|
| Tools | Agents only reach the canal model through read and simulation tools (`wp_core/canal_ops_tools_v6.py`), with per-agent allowlists (`wp_core/canal_ops_policy_v6.py`). Results are capped in size and paged. |
| Router | `wp_core/canal_ops_router.py`: only the main agent asks other agents, at most three per question; plain questions skip them. |
| Placeholders | Agents write numbers as `{{R1.key}}` references to tool results. The Checker fills them in, so a model cannot type a number of its own. |
| Checker | `wp_core/canal_ops_checker.py` rejects stray digits, operational orders ("open the gate now"), wrong comparisons, restricted patterns (cadastral codes, coordinates, paths) and wording such as "command area". A public profile blocks all parcel detail. |
| Payload approval | Live mode is refused unless the hash of the exact data schema sent to the model matches one a person approved after reading a sample (`scripts/canal_ops_payload_review.py`). |
| Read guard | `wp_core/read_guard.py` records every file a build reads and refuses archive-like paths, files outside the product and SQL drivers. Tests prove what each build read. |
| Map actions | An agent can propose a map highlight. It appears as a card; the user confirms it and can undo it. |
| Ledger | Plans, approvals and deficits go to a local ledger (`wp_core/canal_ops_ledger.py`). |

Three run modes: `fixture` (scripted agents, real tools and Checker, no model call), `recorded`
(replays a saved run and refuses if any hash changed) and `live`.

### Page

`scripts/canal_ops_view.py` serves a loopback-only page with one satellite map and the chat.
The layers are cadastral parcels coloured by land type, community and association boundaries, the
canal with its gates, the observed zone, the synthetic tree and the pumping areas. Clicking an
object and asking about it puts it into the chat. `scripts/season_replay_view.py` replays the
2025 season dekad by dekad.

### Status

- Simulation only, on a synthetic network. Nothing here is a hydraulic design or a confirmed
  service area.
- The live acceptance test (20 questions and plans, with time and cost limits) has **not passed
  yet**. In the latest run, 12 of 20 answers passed the Checker and 7 were withheld; one plan was
  over the time limit.
- Next: the water planner on the page, then another live test.

## Architecture

- **Server:** Python standard library only (`http.server`), serving JSON APIs and the built
  frontend. Analytical code lives in `wp_core/`.
- **Frontend:** React 18, Vite 6 and MapLibre GL 5. Analysis classes arrive as versioned Mapbox
  Vector Tiles, and parcel details through small API calls, so the browser never loads the whole
  archive.
- **Sealed, additive releases.** Each reviewed version is a `run_*_review.py` layer that wraps the
  previous one. Every review layer checks its files against a SHA-256 lock in `config/*.lock.json`
  and refuses to start on any change. Accepted results are never overwritten; new analyses ship as
  new versions.
- **Frontend source per release:** `server_data/review/<version>/frontend_source/` is copied from
  the previous release, extended by `scripts/prepare_*.py` and sealed by `scripts/seal_*.py`. The
  current UI source is
  [`server_data/review/irrigation_stress_review_20260907_v1/frontend_source/src`](server_data/review/irrigation_stress_review_20260907_v1/frontend_source/src).
  The top-level `src/` is the earlier base version.

## Repository layout

| Path | Contents |
|---|---|
| `run_irrigation_stress_review.py` | Land and water analytics entry point |
| `run_*.py`, `verify_*.py` | Versioned analysis runs, review servers and their verification scripts |
| `wp_core/` | Analytics, both assistants and their tools, the canal model, data collector, release helpers |
| `scripts/` | Data preparation, land types, canal network and pages, tile building, sealing |
| `tools/lanjazat_2026/` | A separate single-community analysis and map |
| `config/` | Release locks, catalogues, method and model configuration |
| `docs/` | Method and result write-ups, and [operations](docs/OPERATIONS.md) |
| `tests/` | Unit and contract tests |

## Running

Python 3.12.

```bash
python -m venv .venv
.venv/Scripts/python -m pip install --only-binary=:all: -r requirements-analytics.lock.txt
.venv/Scripts/python -m pytest -q
```

In a plain checkout, tests that need the private inputs are skipped (`tests/conftest.py`). With
those inputs present, all of them run.

Serving needs the private `data/`, `server_data/` and `output/` folders:

```bash
python run_irrigation_stress_review.py --port 8526
python scripts/canal_ops_view.py serve
```

The map assistant needs `OPENAI_API_KEY`, the Irrigation Assistant's live mode needs
`ANTHROPIC_API_KEY` and an approved payload (see `.env.example`). Everything else works without
them.

## Data

The following are **not** in the repository:

- cadastral geometry and parcel records;
- the satellite and weather archive (about 40 GB);
- analysis outputs, built frontends and preserved releases;
- review answers, approvals and ledgers;
- the map assets;
- credentials.

Parcel codes in tests and examples are placeholders. Screening results describe observed signals,
not verified conditions on the ground.

## Verification

- Automated tests cover data contracts, API responses, tile delivery, release sealing, both
  assistants' tool contracts, the Checker, the read guard, the canal tree, water balance,
  rotations and the Excel exports.
- Releases were checked visually on desktop and mobile before activation.
- None of this is ground-truth accuracy. The classifications and screening results have not been
  validated against field observations, and the canal network is synthetic.

## Stack

Python · NumPy · pandas · GeoPandas · Shapely · Rasterio · PyArrow · pytest · OpenAI Responses API ·
Claude Agent SDK · Copernicus Sentinel-2 · ERA5 · Google AlphaEarth embeddings · React · Vite ·
MapLibre GL JS · Mapbox Vector Tiles
