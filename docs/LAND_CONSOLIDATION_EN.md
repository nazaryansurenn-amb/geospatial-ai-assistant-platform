# Active-land consolidation screening

Owner instruction: neighboring land already classified as active, totaling at
least 5 hectares, with repeated similar EO phenology. This is an independent
private screening branch; it does not modify the working release or the other
task's water-loss analysis, classification or weather collector.

## Scope and interpretation

The activity selector reads the preserved working release in read-only mode:
`activity_class=active`, `activity_state=ready`, not household and not road.
This is the **2026 activity population**, compared against **2021-2025** completed
EO seasons. It is not a claim about simultaneous current cultivation.

The original 43,984-parcel cadastral geometry, code and official area are used
without repair or changes. All 16,258 selected active parcels remain in an
explicit register, including exclusions and cases with insufficient support.
Partially active parcels are not silently included.

The current cadastral analytical household flag differs from the preserved
working release for 160 parcels in the wider scope. Affected active parcels
remain in `household_road_mask_conflict_review`; neither mask is overwritten.

## Rules

- At least two parcels and at least 5 ha, using summed official areas once per ID.
- A meaningful shared boundary, not corner-only contact or proximity alone.
- A 25 cm adjacency tolerance accommodates small numerical cadastral slivers,
  capped at 1% of the smaller parcel and wholly within both boundary bands.
  Substantial overlaps remain review cases. The original polygons are unchanged.
- Existing mapped roads and the Lower Hrazdan canal prevent connections. Lines
  crossing parcel interiors also trigger review. Smaller unmapped barriers,
  buildings and water require visual assessment; the available barrier inventory
  is not claimed complete.
- Narrow/poorly resolved parcels and excessive shared satellite pixels cannot
  provide automatic positive evidence.
- Compare NDVI, EVI2, NDMI and BSI on actual common valid dates. No interpolation,
  calendar shifting, new raster processing, downloads or LLM calls occur.
- Compare seasonal variation, absolute index differences, correlated NDVI/EVI2
  curves, and agreement of green/bare phases. Flat profiles are not evidence of
  common cultivation. Remove the existing isolated-excursion flags.
- Require agreement in at least three adequately observed seasons, March-November.
- Every pair in a group must agree in the same three or more years. Similarity
  is not propagated merely because A resembles B and B resembles C.
- Groups are connected and have disjoint parcel membership. A deterministic
  longest-boundary-first greedy grouping is used, not an optimal land-allocation
  solution. Alternative valid groupings and overlooked opportunities can exist.

All numerical similarity settings are initial uncalibrated screening parameters
in `config/consolidation_active_20260906_v1.json`, not validated agronomic cutoffs.
Similarity does not establish common ownership, common management, landowner
willingness, access, financial benefit, irrigation supply or legal feasibility.

## Run and review

From WORKING_PRODUCT, using its existing virtual environment:

```powershell
.\.venv\Scripts\python.exe -B run_land_consolidation.py run
.\.venv\Scripts\python.exe -B verify_land_consolidation.py
```

The output is `data/analysis/land_consolidation/consolidation_active_20260906_v1/`.
It contains an active-parcel register, candidate membership, block GeoParquet
and GeoJSON, date-level pair diagnostics, topology checks, report and a static
overview image. There is no public deployment or new server port.

Inputs, code snapshots and outputs are SHA-256 pinned. A completed rerun verifies
the existing result rather than rewriting it. Initial interrupted attempts are
retained under `server_data/review/consolidation_active_20260906_v1/`.

The separate checker replays a deterministic spread of candidate pair-years
against the source EO tables. This verifies arithmetic, not ground-truth accuracy.
Map publication still requires Suren's visual approval.

## First result, 2026-09-06

Four candidate blocks, all in stage II, contain eight parcels and 35.459322 ha.
Their individual areas are 12.570133, 8.216336, 8.037319 and 6.635534 ha. Each
pair agrees in three completed seasons; the supporting years differ by block.
There are no automatic candidates in stage I under this initial strict policy;
that is not evidence that stage I lacks consolidation opportunities.

Of 16,258 active parcels: 10,761 are spatial-support review, 881 overlap review,
481 mapped-barrier review, 82 household-mask conflict review, 95 belong to similar
groups below 5 ha, 3,950 have no qualifying group, and eight are candidates.
The no-qualifying-group state includes isolated parcels and insufficient or
nonmatching pair evidence; it does not mean unsuitable or inactive land.

The spatial screen yielded 1,806 eligible adjacency pairs. Group-wide checks
evaluated 1,809 pairs in total: 1,366 shared-pixel review, 381 without repeated
similarity, and 62 repeatedly similar. Sparse/ambiguous evidence is therefore a
major coverage limitation. Do not extrapolate the four results to all land.

The final calculation took 324.39 seconds, including full input checks and
verification. It used 1,790,880 saved parcel-date rows for 2,460 participating
parcels. No imagery was downloaded or reprocessed. All four candidate pairs and
their twelve supporting pair-years passed separate arithmetic replay.
Ground-truth accuracy and practical consolidation benefits remain unverified.
