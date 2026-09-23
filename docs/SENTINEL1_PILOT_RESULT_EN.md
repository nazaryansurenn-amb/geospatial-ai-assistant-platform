# Sentinel-1 pilot: completed collection and observability analysis

Completed 6 September 2026, following the owner's approval to run the proposed
pilot. This is a private water-loss investigation, separate from the selected
Transitions v3 land-use classification.

## Actual result

- 96 eligible parcels: 32 in Ակնալիճ, 32 in Արշալույս, 16 in Նորակերտ and 16 in
  Բաղրամյան. The first two are priority areas; the latter two are comparisons.
- 72 parcels were selected with larger clear interiors; 24 smaller/narrower
  parcels were included to test boundary limitations. This compact sample is
  deliberately chosen for observability and does not estimate community rates.
- 57 distinct April–September 2025 radar acquisitions were collected from the
  Planetary Computer Sentinel-1 RTC archive: 26 on ascending track 72 and 31 on
  descending track 152. Three expected ascending acquisitions were absent from
  that RTC catalogue and remain recorded as missing.
- 228 cropped, two-band VV/VH GeoTIFF files, totaling **24,518,802 bytes on disk**.
  This is stored clip size, not a measurement of total network transfer.
- 5,472 parcel/acquisition records. There are 4,104 records passing the pilot
  interior-support check, covering all 72 larger-interior parcels. The 24
  boundary-challenge parcels remain spatially unresolved.
- 5,012 radar rows have a usable cached Sentinel-2 observation within three days.
  That temporal association is context, not a simultaneous moisture measurement.

Public raster access was verified with an actual numeric sample before launch.
No personal account credentials, new account, paid processing or bulk LLM calls
were used. The provider's older catalogue text still says an account is needed;
its maintainers removed that restriction in 2024, and public access worked here.
[Provider announcement](https://github.com/microsoft/PlanetaryComputer/discussions/347).
Transient public SAS access signatures were used in memory and not saved in
manifests or logs.

## What the observations establish

The stored radar series contain repeated rises and falls in backscatter. They
are numerical signal excursions, not established irrigation events. Exploratory
rise-and-fall checks produced 458, 309 and 118 excursions at 1.5, 2 and 3 dB,
respectively. These overlapping sensitivity results are not independent event
counts and none is an accepted irrigation threshold.

Same-track acquisition intervals are approximately six days; missing RTC dates
increase some track-72 gaps to 18 days. **No same-track excursion resolves a
drying interval of three days or less.** This is insufficient temporal evidence,
not evidence that rapid drying or frequent irrigation is absent.

Rises/falls can reflect rainfall, irrigation, vegetation, roughness or processing.
Different viewing directions were never combined into a drying curve. Platform
transitions remain flagged; no cross-platform moisture calibration is claimed.
The observation tables preserve full-parcel and buffered-interior VV/VH values
for subsequent investigation. No volume, soil hydraulic coefficient or excess
relative to a norm was inferred.

The owner's hypothesis—more frequent irrigation on rapidly losing soils—remains
useful, but cannot be tested simply by counting these radar excursions. Irrigation
and drying may both occur between visits, and a frequently irrigated field can
have few detected wetting events.

## Weather and remaining work

At the analysis timestamp, the existing ERA5-Land collection had completed
January–March 2025; April–September inputs needed by this pilot were not yet
present. Rainfall attribution and comparison under similar weather conditions
were therefore not performed.

The subsequent read-only checkpoint check found 51/61 weather jobs complete,
8 pending and 2 waiting; EO remained 762/762 complete. The existing weather
worker was present. It was neither restarted nor duplicated. No monitoring
automation was created or reactivated.

Next, the same cached pilot can be compared with rainfall after the required
weather arrives. That still cannot close six-day sampling gaps. Testing actual
irrigation frequency or diagnosing below-root drainage will need dated field
irrigation information and/or suitable soil-water measurements. The three
missing RTC dates are an additional coverage gap, not the sole limitation.

All 96 final high-water-demand fields remain null/unassessed. This is **not a
zero-candidate finding**. The requested single-colour section has not been
activated, because the pilot does not support parcel claims of above-normative
water demand. Existing 8525 and 8526 interfaces and map classes remain unchanged.

## Validation and preservation

- Six targeted tests passed, covering invalid power, absent interiors, temporal
  gaps, missing observations, mixed platforms and different radar tracks.
- The independent verifier checked all 228 clips, including bands, grid,
  source identity and checksums. It recalculated 16 parcel/acquisition rows
  directly from pixels in the first and last acquisitions.
- All 96 parcel identifiers, cadastral codes and official areas match the
  original included scope; household and road exclusions are preserved.
- The working release and every file in the existing Transitions review lock
  passed their hashes. Both ports returned ready health responses.
- A repeated collection command reused all completed checkpoints without network
  access or rewriting the completion timestamp.

## Files and commands

Run with the product environment, from WORKING_PRODUCT:

```powershell
.\.venv\Scripts\python.exe -X utf8 -B run_sentinel1_pilot.py prepare
.\.venv\Scripts\python.exe -X utf8 -B run_sentinel1_pilot.py collect
.\.venv\Scripts\python.exe -X utf8 -B run_sentinel1_pilot.py analyze
.\.venv\Scripts\python.exe -X utf8 -B verify_sentinel1_pilot.py
```

Versioned configuration: `config/sentinel1_pilot_20260906_v1.json`.
Code: `wp_core/sentinel1_pilot.py`; runner: `run_sentinel1_pilot.py`.
The completed dataset pins these source files; future method changes require
a new version. Do not edit this completed pipeline in place.

Outputs: `data/observations/sentinel1_pilot_20260906_v1/` contains the selection,
scene manifest, source snapshots, per-acquisition checkpoints, raster clips and
`analysis/{radar_observations,radar_excursions,parcels}.parquet` plus its report.
Independent validation is saved in
`server_data/review/sentinel1_pilot_20260906_v1/verification.json`.
