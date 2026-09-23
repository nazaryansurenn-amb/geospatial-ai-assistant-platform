# Possible signs of land degradation

The owner approved a separate section on 8526 titled
«Հողերի դեգրադացիայի հնարավոր նշաններ», with one colour for the union of two
separate indicators: vegetation deterioration and persistent low performance.
Salinity is not included. The mask is active or partially active in at least one
saved historical year, 2021–2025, in Lower Hrazdan I/II, excluding households and
roads: 19,421 unique parcels, 8,812.343734 whole-parcel official hectares.
All five years are examined after selection, including no-observed-activity years.
The nearby 1 km potential expansion is outside this first screen.

## Method and limits

The scientific starting point is UNCCD's Trend/State/Performance framework:
https://prais4-reporting-manual.unccd.int/en/2022/SO1.html#step-4-calculate-land-productivity-metrics
The reference trend method uses 16 years. This version is explicitly a five-year
local screening adaptation, not certified SDG 15.3.1 reporting. The local numerical
thresholds are provisional settings, not independently calibrated accuracy claims.

Independent code: `wp_core/degradation_rules.py`, runner
`run_degradation_screening.py`, versioned JSON configuration. It reuses completed
and checksum-verified cached Sentinel-2 parcel observations. No LLM calculation,
EO download, weather restart, 250 m input or previous event-classification rule is
run. The saved history determines inclusion only. Accepted broad use-type and
annual-cycle categories provide provisional peer context, never degradation labels.

1. Retain only parcel observations with at least 95% valid NDVI/EVI2 and clear
   coverage, at most 5% water, valid radiometry and finite indices. Require at least
   three nearly full 10 m pixels and 70% of parcel area in those pixels, using the
   existing exact-area sampling weights. The source mask excludes clouds, shadows
   and snow but includes SCL 7 (unclassified); a newly reprocessed SCL-4/5 mask is
   not claimed. Two accepted dates have a conservative at-least-90% common-area
   bound; exact pixel-by-pixel persistence is not measured from aggregate tables.
   Cadastral geometry, codes and official areas remain immutable.
2. Select one coherent scene per date by greatest valid support; break ties by
   scene identifier. Remove only isolated large two-index excursions with nearby
   agreeing neighbours. Require two usable dates in every March–November month and
   no gap over 35 days. Form day-weighted monthly-median vegetation scores without
   filling missing months. These are vegetation proxies, not measured NPP or yield.
3. Deterioration requires five covered seasons; adequate early vegetation; at least
   20% and absolute 0.07 NDVI / 0.04 EVI2 drops from 2021–22 to 2024–25; both recent
   years below the early baseline; negative robust slopes in both indices; and a
   decline of at least 0.15 relative to local annual medians in both indices.
4. Persistent low performance requires both indices below half their local cohort
   90th percentile in at least three covered years including 2024 and 2025. Peers
   exclude the parcel itself, lie within 5 km, and share the provisional broad
   use-type and annual-cycle grouping. Require 30 usable peers per year and a
   sufficient reference vegetation level. Unknown type or insufficient peers makes
   this indicator unassessed. This matches observable vegetation context, not
   measured identical soil productivity potential.
5. Local median adjustment for deterioration prefers matched peers; where that
   cohort is insufficient, it uses nearby historically used parcels. It reduces
   common regional-year effects, but does not causally separate weather, water
   access, crop rotation, fallow, changing cycle counts or management. It uses no
   measured weather adjustment. NDVI and EVI2 are correlated checks, not independent
   evidence of soil degradation. Persistently bare-patch geometry is not inferred.

Output retains all 22,802 eligible cadastral records with a separate inclusion
flag. Missing evidence stays unassessed. One passing indicator is enough for the
single map colour; overlapping indicators count a parcel only once. An unflagged
parcel is not certified healthy. Official hectares are whole candidate parcels,
not the measured area of damaged soil. Raw metrics, thresholds and provenance stay
private; the UI/agent expose only selected scope, counts/hectares and readable
indicator explanations with the possible-signs limitation.

The run is resumable by year and protected against duplicate workers. Completed
versions and their inputs/code/output checksums are sealed and never overwritten.
The map is additive on 8526; the six earlier tabs, Excel exports, agent identity,
8525, previous outputs and paused automations remain preserved.

## Verified result and current integration

The run completed in 68.61 seconds using cached observations and three year workers.
No SciPy dependency was introduced: existing Shapely spatial indexing finds neighbours.
An independent checker reconciled the inclusion mask, unchanged identities/official
areas, coverage, candidate union, indicator requirements and sanitized delivery.

| Status | Unique parcels | Whole official ha |
| --- | ---: | ---: |
| Historically active/partial inclusion mask | 19,421 | 8,812.343734 |
| At least one indicator assessed | 4,043 | 4,460.850123 |
| Possible signs, union | 299 | 286.820136 |
| Unassessed | 15,378 | 4,351.493611 |
| No sign flagged in assessed indicators | 3,744 | 4,174.029987 |

298 show vegetation deterioration; one shows persistent low performance; no overlap.
Stage I has 105 candidates / 137.311444 ha; Stage II has 194 / 149.508692 ha.
The large unassessed population is a material coverage limitation. Requiring complete
monthly seasons and adequate parcel spatial support particularly limits small parcels.
An assessed parcel can still lack support for the other indicator. No classification
accuracy or actual soil degradation has been established by these engineering checks.

The actual live baseline discovered during integration was the newer
`run_activity_change_history_review.py`, not agent v5 alone. Its cultivation-change
section, year information, tiles, parcel response and agent adapter are preserved.
Degradation is therefore the seventh section. A concurrent owner-approved >=50%
partial-season area gate belongs to the other task; it is not recalculated here.

New launcher: `run_agent_review_v6.py`. Its separate query index adds only degradation
attributes; every pre-existing index value is verified equal. The map reuses identical
existing vector tiles and filters candidate public IDs, with one orange fill/outline.
The focus action respects the existing minimum parcel-display zoom on mobile.
The new assistant topic supports deterministic scope/community counts, full Excel
tables with editable charts and session-owned reversible map selections.
It preserves the cultivation-change adapter's existing cohort/export limitations.

Validation: 384 tests and 7 subtests passed, with five pre-existing pandas warnings.
All six prior HTTP summaries and eight representative parcel profiles stayed equal
after removing only the new degradation field. Private paths reject access; 8525
retains its original HTML checksum. Desktop/mobile checks verified seven tabs,
scope filters, one orange control, actual rendered parcel IDs, toggling, focus,
live GPT numerical results, charts and Excel downloads with 22 community rows
and 299 parcel rows. Engineering checks are not field validation.
