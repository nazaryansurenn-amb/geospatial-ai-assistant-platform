# Land potential: owner review with the 1 km expansion

Owner authorization: run the Python screening and make it available for
observation on 8526; include reasonably nearby land, with an initial 1 km search.

## Result

| Scope | Eligible parcels screened | Gravity candidates | Mechanical candidates | Total candidates | Further review |
| --- | ---: | ---: | ---: | ---: | ---: |
| Saved Lower Hrazdan I + II | 22,802 | 786 | 352 | 1,138 | 435 |
| Outside register, initial 1 km search | 11,627 | 518 | 1,513 | 2,031 | 3,211 |
| Combined | 34,429 | 1,304 | 1,865 | 3,169 | 3,646 |

The outside inventory contains 26,123 cadastral parcels before exclusions.
The reused household rules exclude 13,874, and the existing road algorithm
excludes 622. Every outside parcel retains a result or an explicit review state.
The inner-area exclusions remain exactly those of the prior 8526 delivery.

The 1 km inventory selects whole cadastral parcels whose polygon is at most
1,000 m from the saved zone polygons. A parcel intersecting that search band
can extend beyond it. No parcel is clipped, divided or legally reassigned.
The official area of a candidate parcel is not a measurement of usable area
or of the portion inside the search band.

## Rules and sources

The inner run reuses the sealed history and current-activity results, requires
no activity in the two latest completed years, and checks recent Transitions v3
observation quality and vegetation evidence. Undetermined crop type is not used
as proof of non-use. Existing geometry-cache household flags overlap 160 road
parcels; the active, disjoint 8526 exclusion fields are preserved.

For the expansion, cached current-activity measurements and the existing
household/road functions first identify the parcels requiring historical work.
Python then processes 2,507 parcels against 593 locally cached March–November
Sentinel-2 scenes: 1,486,651 parcel-scene records, 1,416,455 distinct parcel-dates,
and 12,535 parcel-seasons. No imagery is downloaded. The cache checksums and
recorded reflectance scale/offset are checked before reading each source band.

The expansion uses the existing parcel optical aggregation functions with the
stricter SCL 4/5 mask. It retains one best-covered observation per parcel/date,
uses the existing seasonal coverage function (including gaps and season edges),
and applies the declared repeated bare/low-vegetation history rule. It does not
run a new crop classifier or treat its output as crop identification. The
prepared outside history is separate from the preserved inner history; its
measurement preparation is documented in `history_policy.json`.

Open-cover and built/water checks use the cached land-cover context. Its date
must not be presented as a new 2026 land-cover observation. Known greenhouse
review cases remain under review; this is not a comprehensive greenhouse survey.
Current activity is the saved 2026 observation window ending 2026-08-23, not a
completed 2026 season or a live field inspection.

Both scopes use the owner-selected elevation rule. Higher than canal terrain
is a mechanical candidate; lower is a gravity candidate. The inner scope uses
its recorded stage; the outside scope uses the nearest canal-stage line as an
analytical reference, without assigning a confirmed service relationship.

The saved DEM is a nominal 90 m source on a 10 m output grid. The parcel's full
sampled elevation range is compared with the terrain range in a 90 m square
around the canal reference. Overlapping ranges or missing coverage stay in
review. No measured operating canal water level is available in these inputs.

## Meaning and limitations

These are preliminary land-potential candidates for owner observation.
The screening does not establish legal availability, absence of grazing or
other unobserved use, agricultural/soil suitability, water availability,
canal connectivity or hydraulic feasibility. No independent ground-truth
accuracy has been measured. Candidate counts are not accuracy scores.

## Delivery and verification

Review: `http://127.0.0.1:8526/?view=potential`.
The checkbox **Ներառել հարակից 1 կմ գոտին** includes/excludes the expansion.
Blue identifies gravity candidates, pink mechanical candidates, and the optional
gray category contains parcels needing further review.

Final UI version: `land_potential_1km_review_20260906_v2` (display adjustment only).
Launcher: `run_land_potential_extended_review_v2.py`.
Analytical delivery: `land_potential_1km_review_20260906_v1`.
Initial inner-only review and every earlier release remain preserved.

Private results:

- `data/analysis/land_potential/land_potential_20260906_v1/` — inner analysis.
- `data/analysis/land_potential/expansion_inventory_20260906_v1/` — distance inventory.
- `data/analysis/land_potential/land_potential_expansion_1km_20260906_v1/` — outside prefilter, observations, seasons, results and source audits.
- `server_data/review/land_potential_1km_review_20260906_v1/all_candidates.csv` — combined candidate list.

Verification includes immutable geometry/code/official-area and exclusion checks,
all inner database fields and 370 original MVT tiles, outside tile IDs/classes,
source/output checksums, three focused numerical tests, and 60 independent
raw-pixel parcel checks on 2024/2025 scenes. Desktop/mobile browser checks cover
both sources, category toggles, the 1 km toggle, stage filters, selected-parcel
popups and the preserved use-type mode. External Esri requests were disabled in
the headless checks; those checks validate local layers and interaction, not
external basemap availability. Review screenshots are retained privately.

No SQL analysis, weather collector, cancelled automation, working release on
8525, map use-type rules or original cadastral data are changed by this work.
