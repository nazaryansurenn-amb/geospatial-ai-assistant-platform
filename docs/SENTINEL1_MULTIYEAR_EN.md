# Private water-loss comparison across 2021–2025

Owner request, 6 September 2026: extend the current analysis to 2021–2025.
The scope remains the same 96 pilot parcels: Aknalich 32, Arshaluys 32, Norakert
16, Baghramyan 16. No full-area expansion or map activation is included.

This version extends temporal coverage only. It reuses the exact pixel summaries,
optical matching and numerical peer/rainfall rules from the sealed 2025 pilot
and `SENTINEL1_PEER_COMPARISON_EN.md`. Earlier classification rules and classes
are not inputs. All high-water-demand parcel fields remain unassessed: repeated
relative radar signals are private screening evidence, not measured water use.

## Data and interpretation

- Analyze April–September growing-season radar separately for each completed
  year 2021–2025, using March–September weather from that same year. This is not
  a full-calendar water balance. Never use another year's rain for missing dates.
- Reuse all 57 cached 2025 acquisitions. Public catalogue queries identified
  58 acquisitions in 2021, 30 in 2022, 30 in 2023 and 29 in 2024 on tracks 72/152.
  Collect only four small VV/VH windows per missing acquisition, reusing the same
  native 10 m grid. Catalogue coverage is not a guarantee of usable parcel pixels.
- Preserve the 72 larger-interior and 24 boundary-challenge parcels. Challenge
  parcels are retained as unassessed wherever they lack spatial support.
- Reuse the cached 2021–2025 daily optical tables, with support at least 0.6,
  finite observations and nearest optical date within three days of radar.
  NDVI/BSI/NDMI values are observations, not prior land-use labels. No optical
  archive is redownloaded, and no 250 m dataset is used.
- Platforms A/B in 2021, A in 2022–2024 and A/C in 2025 remain separate. So do
  radar tracks and years. Never construct a drying curve across those boundaries.
- Per-year weather is checked against complete collector jobs, hashes, row counts,
  hourly timestamps and the saved grid. Missing or inconsistent precipitation
  remains unknown. Only radar months present in that weather snapshot are used.
- Compare using unchanged geometry/area/weather-cell and optical matching rules;
  preserve rain thresholds 0.5/1/2 mm and radar thresholds 1.5/2/3 dB as overlapping
  exploratory sensitivity settings. Exposure denominators and missing states
  are reported explicitly. Do not relax thresholds to obtain a desired community.
- A multi-year table counts years with assessable repetition and years with a
  positive relative signal at any sensitivity. The latter is permissive review
  context, not independent validation or an accepted parcel class. No new
  predominant water-demand label is assigned. No crop identification is made.

## Reproducible stages and later weather

From `C:\Projects\Echmiadzin_IrrigationTool\WORKING_PRODUCT`:

```powershell
.\.venv\Scripts\python.exe -X utf8 -B run_sentinel1_multiyear.py prepare
.\.venv\Scripts\python.exe -X utf8 -B run_sentinel1_multiyear.py collect
.\.venv\Scripts\python.exe -X utf8 -B run_sentinel1_multiyear.py extract
.\.venv\Scripts\python.exe -X utf8 -B run_sentinel1_multiyear.py analyze
.\.venv\Scripts\python.exe -X utf8 -B verify_sentinel1_multiyear.py
```

Preparation pins code, configuration, catalogues, source optical tables and the
original pilot. Collection is checkpointed with file hashes and uses four small
parallel downloads. A shared OS lock prevents duplicate workers for this version.
Tokens for the public raster archive are transient and are never stored or logged.

Yearly analyses are keyed by the exact completed weather-source hashes. Completed
year results are immutable and reused. When additional 2025 weather arrives,
`analyze` creates a new 2025 result and a new summary snapshot; it reuses earlier
years and preserves every previous snapshot. `latest.json` is a private pointer.
The older 2025 full-season and April-only versions remain unchanged.

Radar output: `data/observations/sentinel1_pilot_2021_2025_20260906_v1/`.
Analysis: `data/analysis/rapid_water_loss/sentinel1_peer_2021_2025_20260906_v1/`.
The independent checker verifies all clip checkpoints, native grids/bands, sample
pixel statistics and optical matches, parcel identities and rainfall arithmetic.

Same-platform revisit is roughly twelve days. Surface changes do not measure
rapid root-zone drainage, actual irrigation frequency, water volume or normative
excess. Weather is coarse reanalysis; local showers, canopy and management remain
alternative explanations. The compact sample does not estimate community rates.
Working port 8525, review port 8526, cadastral geometry/code/area and current
household/road exclusions remain preserved. The cancelled monitor stays paused.
