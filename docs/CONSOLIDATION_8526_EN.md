# Consolidation section on 8526

Owner-authorized 2026-09-06; activated at
`http://127.0.0.1:8526/?view=consolidation`.

Section: **Հողատարածքների կոնսոլիդացիայի հնարավորություն**.
The title and controls are Armenian, matching the current working interface.

## Delivery

- New UI version: `consolidation_review_20260906_v1`.
- Basis: preserved `land_potential_1km_review_20260906_v2` frontend and its
  unchanged `land_potential_1km_review_20260906_v1` analytical index/summary.
- New layer: the sealed `consolidation_strip_priority_20260906_v1` result.
- Two disjoint checkboxes: 20 long/narrow-priority groups and 11 other groups.
  Both selected: 31 groups / 262 parcels / 120.358241 ha. Priority only:
  20 groups / 133 parcels / 76.710634 ha. I/II filters update all totals.
- Opacity, group selector, fit-to-groups action, group highlight and cadastral
  popup details are functional. The main checkbox restores the previous land
  mode when disabled. Only one primary thematic fill is active at a time.
- New map data: 421 MVT files, 242,068 bytes total, zooms 8-15 with overzoom.
  The browser requests visible tiles only, not a full geometry dump or EO rows.
  Only display copies are clipped, simplified by at most a quarter tile unit,
  and quantized. Source cadastral geometries/codes/official areas are untouched.
- Public tile properties are limited to group number, stage, priority category,
  parcel count and official total area. `/api/land/consolidation` returns only
  these summaries, group bounds and delivery metadata. The existing parcel API
  adds a sanitized `consolidation` member summary or null.
- Existing map files are byte-identical. Old analytical versions and 8525 were
  not modified; no classification or phenology recalculation was performed.

## Verification

- Full WORKING_PRODUCT suite: 306 tests passed; five existing unrelated pandas
  warnings. Production build passed; its unchanged MapLibre vendor chunk still
  produces the existing >500 kB Vite advisory.
- Delivery verification decoded all new tiles and matched all 31 groups and
  public attributes. Existing map data and analytical/review seals verified.
- Before/after HTTP checks: 18 parcel samples including outside-potential land,
  both old land-delivery payloads and 8525 HTML are preserved. Candidate and
  non-candidate responses, malformed/unknown codes and private-route blocking
  passed. The selected road-review example remains outside the candidate set.
- Browser: desktop 1440x900, mobile 390x844 and normal 1280x720; no horizontal
  overflow. Verified category filters, I/II counts, empty selection, group
  navigation, panel collapse preserving the layer, zoom and cadastral popups.
  Disabling the section restored the activity tab; enabling restored the new
  section. Final live browser log check returned no warning/error entries.
- UI was visually checked in the browser. Numerical frame timing was not
  available from the browser tool; no FPS or first-load-time guarantee is made.
- Preparation caught and corrected escaped tile URL placeholders before
  activation. A regression test now checks their unescaped template form.

Receipts are in `server_data/review/consolidation_review_20260906_v1/`:
`delivery_verification.json`, `tile_verification.json`, `http_before.json`, and
`http_after.json`. The new lock is
`config/consolidation_review_20260906_v1.review.lock.json`.

## Startup / rollback

Start the new review with `.venv\Scripts\python.exe -B run_consolidation_review.py --port 8526`.
It verifies the new and previous seals and binds only to 127.0.0.1. Do not start
a second process on an occupied port. The temporary 8527 preview was stopped.

The previous 8526 version remains intact and can be restored, after the owner's
decision, with `run_land_potential_extended_review_v2.py --port 8526`.
Do not change the frozen files or `config/releases.json` to perform rollback.

These remain preliminary consolidation opportunities. Shared pixels are joint
EO evidence, not independent confirmation of every parcel; road alignment,
ownership and practical/legal feasibility still require separate evaluation.
