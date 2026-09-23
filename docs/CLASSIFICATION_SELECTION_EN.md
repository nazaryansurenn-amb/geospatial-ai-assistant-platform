# Owner-selected first classification attempt — 2026-09-06

The owner selected **Transitions v3** and stopped further method experiments.
The additional approved rule is: **annual with uncertain cycle count goes to
annual single**.

Apply this assignment after the existing predominant-use parcel summary over
completed seasons 2021–2025. Do not rewrite seasonal observations or change the
five-year voting denominator. Perennial, annual two-cycle, household, road and
undetermined-type treatment stays as before.

Selected control totals: 42 annual single (25 existing + 17 assigned by the
owner's rule), 14 annual two, 21 perennial, 43 undetermined type; total 120.

Active analytical selection: `config/classification_selection.json`.
Reusable assignment: `wp_core/classification_selection.py`.
Materialize or verify the private control with:

```powershell
.\.venv\Scripts\python.exe -B run_classification_selection.py
```

Selected output:
`data/analysis/observation_screening/transitions_20260906_v3_owner_v1/`.
The original v3 cycle is retained in `annual_cycle_candidate_v3`; the assignment
flag and basis distinguish the owner's category rule from observed cycle evidence.
Prior outputs, event experiments, seasonal tables and v3 code remain preserved.
Selection does not establish reference truth or training labels.

This decision selects the first-attempt method and category rule. It does not
launch a full-area calculation or change releases on 8525/8526. Those existing
release pointers remain separate. Further method experiments require a new
owner request; do not resume the event v1/v2 development proposal automatically.
