# Lower Hrazdan application of the selected first-attempt rule

Owner instruction, 2026-09-06: apply the selected Transitions v3 rule to the
Lower Hrazdan area of interest. This follows the approved parcel-level assignment
of annual/uncertain cycle counts to annual/single. Further method experiments
remain stopped.

The saved scope is Lower Hrazdan stages I and II: 43,984 cadastral parcels,
22,802 eligible, 20,096 household and 1,086 roads. The existing exclusions and
cadastral geometry, codes and official area remain unchanged.

Working product: **8525**, served from its verified preserved release by
`run_product.py --release working --port 8525`.
New classification review: **8526**, served after completion and validation by
`run_transitions_area_review.py`. The previous 8526 candidate, its launcher and
its pinned files remain preserved. The separate new launcher avoids changing
the old `wp_core/use_type_release.py` pointer, which earlier analysis versions pin.

## Calculation

Configuration: `config/transitions_area_20260906_v1.json`.
Runner: `run_transitions_area.py --workers 8`, using WORKING_PRODUCT's `.venv`.
Output: `data/analysis/observation_screening/transitions_area_20260906_v1/`.

The runner reads the unchanged v3 policy and transition policy from their
preserved manifest. It calls the existing observation screening/transition
functions and applies `wp_core/classification_selection.py` after predominant
five-year aggregation. It never imports either independent event experiment.

The 762 locally cached scenes are reused without network requests. The exact
existing neutral scene/spatial measurements for the 120 controls are reused;
their previous class labels are used only for a postcalculation replay check.
All 600 control parcel-seasons must reproduce the preserved v3 classifications,
reasons and coverage. The remaining parcels receive the same measurement
preparation from cached rasters and the same numerical rules.

The calculation saves scene, daily-year and classification-year checkpoints.
The operating-system lock prevents a duplicate run. Input, code and completed
partition hashes are checked on resume. Missing observations remain missing.
The original and owner-assigned parcel cycle categories are both retained.

## Local review delivery

`scripts/prepare_transitions_area_delivery.py` prepares a separate database,
summary and allowlisted vector tiles. It reuses the working tiles' geometry
commands exactly and changes only crop-type and cycle attributes. Every tile
and all other database attributes are checked against the working release.

The existing review UI is copied byte-for-byte, with the newly versioned tile
data. Raw observations, private diagnostics, thresholds and assignment flags
are excluded from public assets and APIs. Desktop/mobile review follows launch.

No change is made to `config/releases.json`, the working release on 8525,
the weather collector or the cancelled monitoring automation. The result is a
local owner review; replacing the working release remains a separate decision.
No independent ground-truth accuracy claim is made.
