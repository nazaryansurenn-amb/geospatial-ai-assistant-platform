# Geospatial AI Assistant Platform

[![CI](https://github.com/nazaryansurenn-amb/geospatial-ai-assistant-platform/actions/workflows/ci.yml/badge.svg)](https://github.com/nazaryansurenn-amb/geospatial-ai-assistant-platform/actions/workflows/ci.yml)

A map-based decision-support platform for parcel-level land and water analysis. It combines:

- a tool-using LLM assistant;
- a Sentinel-2 and ERA5-Land data pipeline;
- deterministic parcel analytics;
- a React and MapLibre vector-tile map.

The assistant handles language and tool selection. Every number it reports comes from Python
functions over prepared tables.

Case study: about 44,000 cadastral parcels in the Echmiadzin area and the Lower Hrazdan zone,
Armenia, with 22,802 open-field parcels in the main analytical scope and five seasons of satellite
history (2021–2025).

Code, tests and method documents only. The parcel data, satellite archive, analysis outputs and
built releases are not included (see [Data](#data)).

## Analytical sections

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

## AI assistant: LLM function calling

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

## Earth-observation pipeline

`wp_core/data_collector/` is a standalone, restartable collector (`run_data_collector.py`).

- **Sentinel-2 L2A.** Per-pixel reflectance indices, aggregated to the unchanged parcel geometry at
  their native 10 m or 20 m resolution. Results are stored as Parquet partitions by year, with
  atomic writes and a catalogue.
- **18 indices** (`indices.py`, each with its formula): NDVI, EVI, EVI2, GNDVI, SAVI, MSAVI2, NDRE,
  CI red-edge, MTCI, IRECI, NDMI, MSI, BSI, NDWI, MNDWI, NBR, NBR2, PSRI.
- **Scale, 2021–2025:** 762 scenes and 17,375,124 parcel-scene records for 22,802 parcels. These
  are repeated observations, not unique parcels.
- **ERA5-Land weather** through the Copernicus CDS API (`weather.py`): hourly records, local-day
  summaries and reference evapotranspiration. The current-season stress section separately uses
  ECMWF model estimates from Open-Meteo.
- **Sentinel-1 radar pilots** (`run_sentinel1_*`) are exploratory and feed no map layer.

## Architecture

- **Server:** Python standard library only (`http.server`), serving JSON APIs and the built
  frontend. Analytical code lives in `wp_core/`.
- **Frontend:** React 18, Vite 6 and MapLibre GL 5. Analysis classes arrive as versioned Mapbox
  Vector Tiles, and parcel details through small API calls, so the browser never loads the whole
  archive.
- **Sealed, additive releases.** Each reviewed version is a `run_*_review.py` layer that wraps the
  previous one. The current product is `run_irrigation_stress_review.py`, which wraps
  `run_hectares_review.py`, then `run_consolidation_review.py`, then `app.py`. Every review layer
  checks its files against a SHA-256 lock in `config/*.lock.json` and refuses to start on any change.
  Accepted results are never overwritten; new analyses ship as new versions.
- **Frontend source per release:** `server_data/review/<version>/frontend_source/` is copied from
  the previous release, extended by `scripts/prepare_*.py` and sealed by `scripts/seal_*.py`. The
  current UI source is
  [`server_data/review/irrigation_stress_review_20260907_v1/frontend_source/src`](server_data/review/irrigation_stress_review_20260907_v1/frontend_source/src).
  The top-level `src/` is the earlier base version.

## Repository layout

| Path | Contents |
|---|---|
| `run_irrigation_stress_review.py` | Current product entry point |
| `run_*.py`, `verify_*.py` | Versioned analysis runs, review servers and their verification scripts |
| `wp_core/` | Analytics, assistant service and tools, data collector, release helpers |
| `scripts/` | Data preparation, tile building, frontend preparation and sealing |
| `tools/lanjazat_2026/` | A separate single-community analysis and map |
| `config/` | Release locks, catalogues, method configuration |
| `docs/` | Method and result write-ups for each analysis, and [operations](docs/OPERATIONS.md) |
| `tests/` | 429 tests |

## Running

Python 3.12.

```bash
python -m venv .venv
.venv/Scripts/python -m pip install --only-binary=:all: -r requirements-analytics.lock.txt
.venv/Scripts/python -m pytest -q
```

In a plain checkout, 389 tests pass and 40 are skipped because they need the private inputs
(`tests/conftest.py`). With those inputs present, all of them run.

Serving the product needs the private `server_data/` and `output/` folders:

```bash
python run_irrigation_stress_review.py --port 8526
```

The assistant needs `OPENAI_API_KEY` (see `.env.example`). Everything else works without it.

## Data

The following are **not** in the repository:

- cadastral geometry;
- the satellite and weather archive (about 40 GB);
- analysis outputs, built frontends and preserved releases;
- the map assets;
- credentials.

The docs and tests contain a few parcel codes as examples. Screening results describe observed
signals, not verified conditions on the ground, as stated in the table above.

## Verification

- Automated tests cover data contracts, API responses, tile delivery, release sealing, the
  assistant's tool contracts and the Excel exports.
- Releases were checked visually on desktop and mobile before activation.
- None of this is ground-truth accuracy. The classifications and screening results have not been
  validated against field observations.

## Stack

Python · NumPy · pandas · GeoPandas · Shapely · Rasterio · PyArrow · pytest · OpenAI Responses API ·
Copernicus Sentinel-2 · ERA5-Land (CDS API) · React · Vite · MapLibre GL JS · Mapbox Vector Tiles
