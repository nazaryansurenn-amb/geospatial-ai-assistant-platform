# Consolidation fifth analysis tab

Owner-requested UI correction, 2026-09-06. Active on
`http://127.0.0.1:8526/?view=consolidation`.

- Consolidation is the fifth tab after activity, use type, history and potential,
  inside the existing land-resource analysis selector. No separate top section.
- Full Armenian title: Հողատարածքների կոնսոլիդացիայի հնարավորություն.
- One checkbox and one purple class for all 31 groups. No narrow/other labels,
  separate colors or category controls. Internal analysis remains preserved.
- Retains 262 parcel / 120.36 ha totals, opacity, group navigation and parcel
  details. The existing I/II territory selector controls this tab as well.
- Hiding the class keeps the tab and its totals visible; switching to another
  analysis hides the consolidation map fill. Only one primary theme is active.

## Preservation and checks

Version: `consolidation_review_20260906_v2`. Start with
`.venv\Scripts\python.exe -B run_consolidation_review_v2.py --port 8526`.
Previous `run_consolidation_review.py` and v1 output remain sealed and unchanged.
No EO recalculation, geometry/code/area changes, release-pointer change or 8525
replacement. All map files, summary and parcel lookup are byte-identical to v1.

- 309 pytest tests passed; five existing unrelated pandas warnings.
- Production build passed; existing large MapLibre chunk warning remains.
- Before/after checks passed for 20 public API responses, invalid/unknown codes,
  private-route blocking and unchanged 8525 HTML.
- Browser checked at 1440x900 and 390x844: all five tabs, one checkbox, I/II
  totals, group navigation and mobile panel collapse. No text overflow in tabs
  or horizontal page overflow; preview warning/error log empty.
- Live 8526 reloaded and verified with the fifth tab and 31/262/120.36 totals.

Receipts: `server_data/review/consolidation_review_20260906_v2/`.
Seal: `config/consolidation_review_20260906_v2.review.lock.json`.
These remain preliminary opportunities, not verified feasible consolidations.
