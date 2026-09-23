# Panel readability review

Owner request: make the right-hand layers panel and left-hand AI chat text more
comfortable to read. This version changes presentation only.

Preview: http://127.0.0.1:8529/?view=activity-change
Launcher: run_readability_review.py
Base: sealed activity_change_area50_20260907_v1, currently approved on 8526.
Status: preview awaiting visual approval; 8526 and 8525 are unchanged.

## Changes

- Right panel: 340 px instead of 304 px; class labels 15 px, tabs 14 px,
  figures 16 px, secondary text at least 13 px. More spacing and stronger contrast.
- AI panel: 420 px instead of 400 px; messages/input 16 px, headings 18-20 px,
  secondary information 13-14 px. Larger buttons and more comfortable line spacing.
- Scrollable panels retain fixed headers and visible chat controls. Responsive
  widths preserve mobile text size. The mobile AI launch button has reserved
  space below the list so that it no longer covers statistics or search controls.
- No changes to map classes, colors, calculations, API data, prompts, agent tools,
  cadastral information, popup logic or language content.

## Delivery

One allowlisted local CSS file and a matching HTML entry point load the exact
existing JS/CSS production bundles. The stylesheet is deliberately loaded last.
No npm build was needed: this is a static CSS addition, with no JavaScript changes.
Original assets and sealed files are not edited or duplicated. The new handler
serves only /, /index.html and /ui/readability-20260907-v1.css; all data and agent
routes delegate unchanged to the approved standalone working-product launcher.
No parent-project dependency or new external resource is introduced.

## Verification

- Full Python suite: 418 passed, 7 subtests passed; five existing warnings.
- Focused HTTP/asset tests: 11 passed.
- Eight API summaries/status endpoints plus 14 parcel responses are byte-identical
  to 8526. 8525 and 8526 HTML hashes remain unchanged. Private paths return 404.
- Browser: desktop 1440x900 and mobile 390x844 screenshots inspected.
- All seven analytical sections were checked for text size and horizontal
  overflow; no 9-12 px leaf text remains in the checked layers panel sections.
- Measured desktop text: labels 15 px, tabs 14 px, statistics 16 px, chat messages
  and input 16 px, footer 13 px. Long Armenian labels wrap without clipping.
- A local fixed Armenian identity reply verified real chat text; no new external
  AI request was made. Wider result tables/charts received CSS rules but were not
  visually retested with a generated analytical reply during this UI-only change.
- Mobile document width is 390 px, panel bottom 834 px in an 844 px viewport.
  Scroll list bottom 768.67 px and AI button top 776.27 px: no overlap.
- No browser console errors observed. Version evidence is under
  server_data/review/readability_20260907_v1/.

The inherited repeated-parcel-popup loading race remains outside this change.
Visual approval is required before moving this presentation to 8526. Rollback
is the unchanged run_activity_change_area50_review.py launcher.
