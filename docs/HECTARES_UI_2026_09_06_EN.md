# Hectares beside parcel counts

Owner requested official hectares alongside quantities in every analytical section.
The change is live on 8526 using `run_hectares_review_v2.py --port 8526`.
The frontend is `hectares_review_20260906_v1`, based exactly on consolidation v2.

All five tabs retain their behavior. Summaries and class legends now pair parcel
counts with the sum of those parcels' unchanged official cadastral area. The
existing observed-active-area measurement remains separately labelled. I/II
scope changes, potential's 1 km checkbox, annual cycles and consolidation totals
use the corresponding areas. No numerical classification or exclusion changed.

Verification matched 129 count/area pairs to the prior delivery, compared eight
existing API responses (ignoring only the new area fields), verified unchanged
map assets and 8525 HTML, and checked ten desktop/mobile analysis views, scope
changes and panel collapse. Screenshots were inspected. The external Esri
basemap was excluded from headless checks. Receipts are retained under
`server_data/review/hectares_review_20260906_v1/` and
`output/playwright/hectares_review_20260906_v1/`.

The initial preview exposed a startup-order issue that omitted some existing
parcel fields. The v2 launcher sets the inherited analytics profile before the
app import. The corrected preview passed all prior-response comparisons before
activation. The first launcher and its seal are preserved; use v2.
