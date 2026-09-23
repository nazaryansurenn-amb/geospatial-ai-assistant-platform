# Cultivation change: partial-season area gate

Owner approval: partially active seasons participate only at >=50%; this applies
to every partial season during 2021-2025, not just 2025. Failed or unavailable
evidence excludes a parcel from the change layer, never turns it inactive/red.

## Preserved rules

Five usable seasonal states, exactly one active/partial versus no-activity
transition, no reversal through 2025. Retain its actual year (2022-2025).
Roads and household land remain excluded. The existing seasonal classifications,
all other analyses, cadastral IDs/codes, geometry and official areas are unchanged.

## Area evidence

Historical scene-level Copernicus Sentinel-2 L2A tables are the only new inputs.
Their vegetation fraction uses valid pixels as denominator. Recover conservative
integer vegetation counts from the stored four-decimal fraction, then divide by
ALL parcel-sampling pixels, including cloud-hidden pixels. Cloud-hidden area is
not counted as observed vegetation. Exact 50% passes; below 50% does not.

A partial season passes if at least two distinct usable dates each show >=50%.
The second-largest daily lower area-share bound represents repeated evidence.
Same-day scenes are not counted twice; prefer the greatest valid coverage, then
scene ID. No interpolation, forward fill, 2026 data or 250 m grid is used.

This is conservative observed vegetation over whole 10 m pixel-center sampling
support, not an exact cadastral survey of cultivated area. It does not reconstruct
the 2026 per-pixel temporal activity algorithm from aggregate historical tables.
Cloud gaps, narrow parcels and sampling edges can omit genuine activity. This
gate and structural tests do not establish field-verified classification accuracy.

## Version and result

Version: activity_change_area50_20260907_v1.
Register: 43,984 parcels, all original identities and geometry retained.
Area-evidence population: 22,802 parcels x 5 years = 114,010 annual rows.
Source inventory: 94 dates and 4,134,496 rows across the five scene tables.
Thirteen hashed source files are listed in the analytical manifest.

| Result | Parcels | Official hectares |
| --- | ---: | ---: |
| Blue, activity appeared | 129 | 63.101813 |
| Red, activity ceased | 448 | 150.760592 |
| Total | 577 | 213.862405 |

Previous candidate count: 1,968. Removed: 1,391. No newly introduced candidates
and no opposite-colour reassignments. Official hectares are whole parcel areas,
not measured cultivated-area gain or loss.

## Delivery and checks

Launcher: run_activity_change_area50_review.py. Own preview: 8528; working: 8526.
Rollback: sealed run_agent_review_v6.py. The seven-section v6 production frontend
is copied byte-for-byte; no cosmetic or analytical changes to its other sections.
Added map delivery: 575 MVT tiles, 847,472 bytes, all 577 candidate IDs retained.
Only code, class, stage and official area are tile properties. Summary is 3,895
bytes; private registers, thresholds and dated evidence remain server-side.
Agent context replaces old cohort totals but preserves v6 tools and Excel support.
Existing limitation: general agent tools cannot export/filter this change cohort.

Verification evidence: server_data/review/activity_change_area50_20260907_v1.
407 Python tests and 7 subtests passed (5 existing dependency warnings).
Seven unrelated API endpoints match sealed v6; 14 representative parcel responses
retain every unrelated field, including degradation. Invalid/private paths fail
closed; sampled new tiles match disk. The delivery seal checks every added MVT,
source hashes, original UI bytes and the deployment allowlist.

Observed browser checks: desktop 1440x900, mobile 390x844 with no horizontal
overflow, close zoom, blue checkbox totals, I/II scope counts, retained 2024
change-year popup, removed parcel not labelled inactive, all seven sections,
and preserved 299-parcel degradation result. No browser console errors observed.
Agent context replacement was tested in HY/RU/EN; this update did not make a
new external model call. No independent new translation work was performed.

Known inherited UI issue, unchanged here: selecting a second parcel while the
first popup remains open can leave its loading message indefinitely. The old
popup close callback increments the request sequence after the new request ID
is assigned (v6 App.jsx selectParcel). Close the previous popup before another
search; the resulting profile and new change status display correctly. This is
separate from EO completeness and the area gate; do not interpret it as no data.

8525, weather collection, and cancelled automations are not changed. Visual
acceptance of the narrower map remains the owner's decision.
