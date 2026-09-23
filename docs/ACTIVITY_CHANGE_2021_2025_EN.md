# Cultivation changes 2021-2025

Owner-approved rule: all five seasons must be usable; exactly one binary transition,
with no reversal through 2025. Preserved seasonal states 1 (active) and 2 (partial)
count as active; state 3 means no observed activity. Unknown states and incomplete
coverage are not converted to inactivity. Household parcels and roads are excluded
using the active analytical index, not the older flags in the geometry archive.

Blue shows activity appearing; red shows activity ceasing. The transition may occur
in 2022, 2023, 2024 or 2025. Its first observed year is retained. For example,
active in 2021 and inactive throughout 2022-2025 is red, change year 2022.
Alternating activity, or no transition, does not appear in either class.

## Results

| First year of new state | Blue parcels | Blue official ha | Red parcels | Red official ha |
| --- | ---: | ---: | ---: | ---: |
| 2022 | 158 | 86.165626 | 529 | 307.446097 |
| 2023 | 55 | 31.412555 | 546 | 544.978166 |
| 2024 | 63 | 63.954703 | 88 | 64.015589 |
| 2025 | 35 | 44.745731 | 494 | 232.039941 |
| Total | 311 | 226.278615 | 1,657 | 1,148.479793 |

The register covers all 43,984 immutable cadastral parcels. There are 21,182
household/road exclusions, 1,551 incomplete histories and 19,283 other histories.
The 1,968 selected parcels were independently replayed against the saved Copernicus
seasonal formulas for every year. No cadastral geometry, code, ID or official area
was changed. The 250 m branch and 2026 activity are not classification inputs.

Inputs: `data/analysis/parcel_eo/current_halo_v1/current_parcels.parquet`, the current
`server_data/land_analytics_land_potential_1km_review_20260906_v1.sqlite3`, and five
`seasonal_features_YEAR.parquet` files in `data/analysis/parcel_eo/v5_full_halo_2021_2025`.
Their seven SHA-256 digests are recorded in the new analysis manifest. No EO download
or raw time-series recalculation was performed.

## Delivery

Launcher: `run_activity_change_history_review.py --port 8526`.
Version: `activity_change_history_review_20260906_v1`.
New sixth tab: `Մշակման փոփոխություն 2021–2025`.
It has two class controls, stage I/II scope, official-hectare totals, opacity and
map focus. Parcel cards retain the full five-year timeline and show the change year.
679 vector tiles, 3,083,165 bytes total, supply only visible map features; the browser
does not receive the 43,984-row register. Detailed parcel results are requested singly.
Tile source bounds match the candidate extent, preventing requests outside the build.

`/api/land/activity-change` returns the public summary including year breakdowns.
`/api/land/parcel` adds only `activityChange: {changeClass, changeYear}`.
The current agent-v5 frontend and service, Excel downloads, five previous tabs,
potential expansion, consolidation and old map data remain preserved. The agent can
read the new tab's supplied scope totals and selected parcel state. Its general tools
do not yet filter this transition cohort for community tables/Excel/map selections;
the adapter explicitly prevents substituting yearly totals for this cohort.

The previous strict-2025 candidate remains separately preserved and inactive.
Rollback is the preserved `run_agent_review_v5.py`; 8525 is not changed.

## Verification and limitations

24 focused tests include all 1,024 possible five-year state patterns. Full suite:
375 passed, 7 subtests passed, 5 pre-existing pandas future warnings. An initial run
hit 19 Windows temp-directory setup errors; rerunning with a new product-local
temporary directory resolved them without changing application or test logic.
Production build passed; the existing MapLibre vendor-size warning remains.

Desktop 1440x900 and mobile 390x844: both filters, empty selection, I/II totals,
opacity, focus, tab switching, parcel search, 2022 popup and zoom were checked.
The layer stays visible when the mobile panel closes. Browser console had no errors.
Server logs contain expected connection-aborted messages from cancelled map requests.
Six existing API responses and 11 representative parcel responses were compared.
Private paths reject access. Evidence is in the version's `server_data/review` folder:
`pytest.xml`, `http_preview.json`, `browser_verification.json`, `delivery_verification.json`
and, after switching the service, `http_activation.json`.

These are observed EO state changes, not proof of first-ever cultivation, permanent
abandonment or exact cultivated-area gain/loss. Partial activity retains the existing
broad residual seasonal definition. Some qualifying historical seasons have as few
as four usable dates; formula consistency is not independent ground-truth validation.
No persistence beyond 2025 is established, particularly for changes first seen in 2025.
Visual approval of the resulting map is still an owner decision.

Activation verified on 8526: all six pre-existing API responses and 11 representative
parcel responses remain identical after removing only the new response field.
The public summary is 3,953 bytes. The 8525 HTML hash is unchanged. The temporary
8527 preview was stopped after verification, and the user's existing browser tab
was opened at `http://127.0.0.1:8526/?view=activity-change`.
