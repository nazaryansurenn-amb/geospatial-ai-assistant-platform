# Private Sentinel-1 comparison, version 20260906 v1

The owner authorized preparing a deterministic Python comparison with other
parcels, waiting for ERA5-Land, then launching the analysis. This version uses
only the existing 96-parcel April–September 2025 pilot: Aknalich, Arshaluys,
Norakert and Baghramyan. It does not expand collection or change Transitions v3.

## Question and scope

Which observed radar rise/fall patterns recur more often than in comparable
nearby pilot parcels under low modeled rainfall? These are private relative
signals for investigation, not measured irrigation counts, water volumes,
root-zone drainage, soil classes or above-normative water demand. Peers are not
verified normal-water controls. Community names and priority flags are never
used as labels, thresholds or evidence of abnormality.

The 72 parcels with adequate radar interiors can enter matching. The 24 boundary
challenge parcels remain in the result as spatially unassessed. The sample was
selected for observability, not statistical community prevalence. No 250 m data,
crop identifications, prior analytical classes or LLM numerical calls are used.

## Fixed exploratory rules

1. Potential peers must have a usable interior, the same nearest ERA5-Land cell,
   a distance of at most 15 km and official areas within a factor of two. Use
   geometry copies for centroids only; cadastral geometry/code/area stay immutable.
2. First separate each radar track and platform, then compare consecutive
   three-acquisition sequences within each. The actual archive alternates A/C
   platforms every six days, so same-platform intervals are about twelve days.
   Both intervals must be at most fourteen days. Never join different tracks or
   platforms into a drying curve or interpolate a missing acquisition. This is
   a relative comparison over coarser intervals, not rapid-drying measurement.
3. All three radar interiors and optical NDVI/BSI values must be usable. Require
   NDVI and BSI ranges at most 0.15, and middle NDVI between 0.25 and 0.70 as an
   exploratory canopy screen. Dense vegetation remains unresolved, not inactive.
   This is a proxy for similar observed vegetation state, not a crop/stage label.
4. Match three-date NDVI within 0.10, BSI within 0.15 and actual optical dates
   within two days. Existing EO associations are within three days of radar.
   Rank by optical differences, then distance and parcel ID; use 3–10 peers.
   Radar outcomes and NDMI do not influence peer selection. Store NDMI/VH changes
   separately as context; neither is a validated moisture measurement here.
5. Read completed March–September 2025 raw weather batches only. March supplies
   the predecessor for April's first accumulation. Require checkpoint checksums,
   row counts, complete hourly timestamps and an identical weather grid.
6. Standard `reanalysis-era5-land` precipitation accumulates from forecast 00 UTC.
   At 01 UTC use the value directly; otherwise subtract the immediately preceding
   hour, multiply meters by 1000. Missing/negative increments remain missing.
   Every hourly interval touching an acquisition window is counted in full;
   boundary-hour precipitation is not fractionally invented. This conservatively
   includes a little rain outside the precise radar interval. Missing hours make
   the total unknown. See [ECMWF conversion documentation](https://confluence.ecmwf.int/pages/viewpage.action?pageId=212460134).
   The cached March NetCDF was verified to store `tp` in meters with GRIB
   `stepType=accum`; this is not the already deaccumulated ARCO time-series product.
7. Test rainfall totals at 0.5, 1 and 2 mm for both acquisition intervals and
   the 48 hours preceding the middle acquisition. All three totals must pass.
   ERA5-Land is coarse reanalysis: passing does not rule out a local shower.
8. At each rainfall setting, test VV rise-and-fall magnitudes of 1.5, 2 and 3 dB.
   Compare each target's changes with its peers' medians. A relative excursion
   requires both changes to exceed their peer medians by at least 1 dB.
9. Summarize the fraction of observed excursions per comparable triplet, separately
   by track, platform and sensitivity. Compare with mean peer excursion fraction
   over those exact triplets. At least six comparable triplets, three relative
   excursions and excess fraction of at least 0.15 produce a private repeated
   relative signal flag. Sensitivity rows overlap and must not be added together.
   If exposure is insufficient, the flag is null. Counts describe observed signal
   triplets, never true irrigation frequency. Twelve-day same-platform intervals
   miss faster processes; even the original six-day mixed-platform cadence did.

Thresholds were declared before weather-conditioned results and are uncalibrated.
Do not tune them until the desired communities light up. All high-water-demand
candidate values remain null. Ground-truth accuracy and normative excess remain
unmeasured. Roughness, canopy, management and local rain remain rival explanations.

## Execution and readiness

Use the product's own environment, from `C:\Projects\Echmiadzin_IrrigationTool\WORKING_PRODUCT`:

```powershell
.\.venv\Scripts\python.exe -X utf8 -B run_sentinel1_peer_comparison.py prepare
.\.venv\Scripts\python.exe -X utf8 -B run_sentinel1_peer_comparison.py status
.\.venv\Scripts\python.exe -X utf8 -B run_sentinel1_peer_comparison.py run-if-ready
.\.venv\Scripts\python.exe -X utf8 -B verify_sentinel1_peer_comparison.py
```

`run-if-ready` returns `waiting_for_weather` without starting the comparison or
writing analysis outputs when any required batch is incomplete. It needs only
these seven monthly batches; it does not wait for unrelated missing years or
October–December. An OS lock prevents duplicate comparison workers. Completion
is sealed with source/output hashes; a repeated command verifies and reuses it.
Interrupted attempts are preserved in dated attempt directories. Checkpoint/hash
failures stop the run for inspection rather than erasing or repairing originals.

Preparation freezes code/configuration, the checker, this specification and
cached pilot inputs. Future changes require a distinct version. The collector
and its existing CDS requests are untouched. No new credentials are needed.

Outputs are private under
`data/analysis/rapid_water_loss/sentinel1_peer_comparison_20260906_v1/`.
`fields`, `potential_peers` and `weather_grid` are preparation artifacts.
`comparisons`, `matched_peers`, `metrics`, `parcels`, `report` and `complete` are
created only after the weather gate passes. Independent verification records
are under the matching `server_data/review/` folder.

The one-time launch follow-up is separate from the cancelled 15-minute EO/weather
monitor. It must remain quiet while waiting, run this fixed Python command once
ready, verify the result, report completion or a real blocker and then pause
itself. It must not reactivate the old monitor, alter methods, expand to all
eligible parcels, change either map or publish these diagnostics.
