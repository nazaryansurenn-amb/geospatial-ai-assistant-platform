# Land potential: inspection and proposed next step

Status: private planning draft, 2026-09-06. No analytical method, map class,
release, UI, SQL record or cadastral feature changed by this inspection.

Subsequent owner decision: see `LAND_POTENTIAL_ELEVATION_RULE_2026_09_06_EN.md`.
It selects higher-than-canal = mechanical candidate and lower-than-canal =
gravity candidate, superseding this draft's all-unassigned method proposal and
single-color suggestion. The original inspection and unused-land proposal below
are retained as the record of the preceding discussion.

## Verified current product

Both 8525 and 8526 returned HTTP 200 from `/health`. The 8526 delivery is
`lower_hrazdan_land_analytics_mvt_transitions_area_20260906_v1`.
Its app, frontend bundle and analytical SQLite file match their review-lock hashes.

`src/landResourcesSchema.js` contains the **Ներուժ** tab and two labels:

- **Ինքնահոս ոռոգման նախնական թեկնածուներ** (`gravity_candidate`).
- **Մեխանիկական ոռոգման նախնական թեկնածուներ** (`mechanical_candidate`).

They are disabled placeholders. `src/App.jsx` enables class controls and map
fills only for activity, use type and history. `app.py` and the live delivery
provide no potential calculation or hydraulic candidate dataset. The sealed
served frontend also contains these labels and the layers-not-connected message.

The dated decision record in `docs/reference/AI_AGENT_DEMO_DECISIONS_2026-09-02_RU.md`,
sections 25 and 26, defines development candidates as land for further assessment
and defers gravity/pumping, route and head calculations. The current request
reopens planning and inspection; no hydraulic classification was calculated.

## Starting evidence, not final potential counts

Read-only Python/SQLite grouping of
`server_data/land_analytics_transitions_area_20260906_v1.sqlite3` found:

| Existing saved result | Parcels |
| --- | ---: |
| Eligible, excluding existing household and road masks | 22,802 |
| Historical stable no observed activity | 2,664 |
| Of those, current 2026 activity is active | 427 |
| Of those, current 2026 activity is partial | 609 |
| Of those, current 2026 activity is no observed activity, state ready | 1,628 |

The 2026 layer reports latest parcel observation 2026-08-23 and an incomplete
season. These results describe the saved observation period, not present-day
field verification. History uses `lower_hrazdan_land_history_v1_4_2021_2025`;
it was preserved by the Transitions v3 delivery, not recomputed by that method.
The current preparation script has a later version and must not be substituted
for the sealed history's provenance.

All 2,664 historical candidates have five recorded profile years: 1,757 have
zero used years and 907 have one. All 1,628 in the current intersection have
undetermined use type; this does not establish non-use. Of the 1,628, 1,283
have no-activity codes in all five historical years. Some others have a recent
active/partial year, so recent-year checks still matter.

## Proposed rule for review

Purpose: identify **land worth assessing for renewed agricultural use**.
It must not claim legal availability, agricultural suitability, a confirmed
water supply or a feasible irrigation method.

1. Keep the same 22,802-parcel scope and immutable cadastral geometry, code
   and official area. Preserve household and road exclusions.
2. Reuse the cached 2021–2025 observations and their quality information.
   Start with repeated absence of observed activity over at least three
   adequately observed completed seasons; examine the existing history rule
   (at least 75% no-activity years and no more than one used year) against the
   sealed source before adopting it. No new NDVI threshold is approved here.
3. For a persistent candidate, require the two latest completed years,
   2024 and 2025, to be adequately observed and show no activity. Keep recent
   cessation, rotating/fallow land and conflicting evidence in separate review
   states rather than forcing them into persistent non-use.
4. Use dated 2026 observations as an additional check. Current active or
   partially active parcels must not be presented as wholly unused. Missing
   or uncertain current evidence remains unresolved; it does not pass the test.
5. Check for built cover, standing water, greenhouse structures, natural
   vegetation and other incompatible or ambiguous land cover using available
   dated evidence. Low vegetation alone does not pass these checks. Unknown
   constraints remain visible in private review rather than being assumed absent.
6. Use surrounding agricultural activity, observable open area and parcel form
   to prioritize inspection. They cannot prove suitability, availability or water
   access. Any observed usable-area estimate stays separate from official area.
7. Keep irrigation method **unassessed**. Neither distance to a canal nor the
   fact that a parcel is unused can select gravity or mechanical irrigation.

## One bounded next step

After owner selection of the rule, prepare one private deterministic Python
screening version using the saved tables. Run the inexpensive eligibility and
history checks across all 22,802 parcels, recording pass/fail/unknown reasons;
use the 1,628 intersection only as a starting review pool, not the entire test
population or the promised final count. Check a small varied sample against
dated cached imagery before considering map activation. Use the product's
`.venv`; do not recollect EO or wait for ERA5 for this initial activity screening.

A later approved map view can use one candidate color under **Ներուժ**.
Preserve existing releases while preparing any separate review version.
Gravity and mechanical categories remain unassigned until a separately agreed
hydraulic evidence and calculation workflow exists. SQL analysis remains deferred.
