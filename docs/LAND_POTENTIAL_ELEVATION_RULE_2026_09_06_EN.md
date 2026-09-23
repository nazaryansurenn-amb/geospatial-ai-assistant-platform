# Owner-selected preliminary elevation rule

Decision date: 2026-09-06. This follows the owner's instruction:
"above canal is mechanical down canal is gravity".

For unused-land candidates in **Ներուժ**, interpret above/below as elevation
relative to the relevant canal segment:

| Relative elevation | Preliminary class |
| --- | --- |
| Parcel higher than canal reference | Mechanical irrigation candidate |
| Parcel lower than canal reference | Gravity irrigation candidate |
| Equal, uncertain, missing or conflicting elevation evidence | Unassigned / review |

This authorizes the simple elevation-based screening rule. It supersedes the
proposal to leave all irrigation-method candidates unassigned in
`LAND_POTENTIAL_PLAN_2026_09_06_EN.md`. The earlier decision to defer hydraulic
design still applies: these candidate classes do not establish a working supply
connection, route, capacity or confirmed irrigation feasibility.

First identify unused-land candidates; then apply the elevation comparison.
Use the existing Armenian gravity/mechanical candidate labels and their existing
colors when a resulting review layer is approved for activation. The earlier
single-color suggestion is superseded for this two-category screening.

## Local data verified

- `data/source/activity_2026/lower_hrazdan_eo_terrain_grid_2026.npz` contains
  `elevation` and `slope_percent` arrays, each 2,106 by 2,521.
- Its JSON metadata records EPSG:32638 and 10 m output-grid spacing. The
  recorded source is in the Copernicus DEM 90 m bucket; output spacing must
  not be described as native 10 m elevation accuracy.
- `data/source/lower_hrazdan.geojson` contains the two stage lines.
- `data/source/lower_hrazdan_points.geojson` contains 70 points.
- Both geometry files are two-dimensional and provide no recorded canal
  water-level elevations. A DEM sample along the canal is terrain context,
  not a measured operating water level.

The implementation must retain the selected stage/reference point, parcel and
canal elevation estimates, their signed difference, source resolution and review
reason. Reference selection is an analytical comparison and must not create a
confirmed service link. Missing data, elevation differences unresolved by source
quality, and parcels spanning both sides of the reference elevation must not be
silently forced into a class. No numerical uncertainty threshold was selected
in this discussion.

Use only the independent product's stored inputs and `.venv`. No parent inputs,
250 m stress dataset, SQL extraction or ERA5 wait are needed for this preliminary
elevation comparison. Preserve cadastral geometry, code, official area and
household/road exclusions. No calculation, UI activation or release replacement
was performed while recording this decision.
