# Active-land consolidation: current private screening rule

Owner-approved on 2026-09-06. This supersedes the earlier member <=0.5 ha / group
>=5 ha rule; previous outputs remain sealed historical comparisons.

- Each member's unchanged official area must be positive and <=1 ha.
- A connected group must total >=3 ha of unchanged official member areas.
- Use the active/ready population, retaining household, road, mapped-barrier,
  substantive overlap and conflicting-mask review exclusions.
- Use saved 2021-2025 Sentinel-2 NDVI/EVI2 profiles. Shared 10 m pixels are
  allowed as joint EO evidence. NDMI/BSI at 20 m remain separate context.
- Every pair within a group must pass in at least two of the same years.
  Retain real-date coverage and phase checks; no interpolation or forward fill.
- Long/narrow priority: estimated length >=100 m, width <=40 m, elongation >=4,
  single-part geometry and oriented-rectangle coverage >=55%. Dimensions use
  EPSG:32638 without changing cadastral geometry. A group receives priority
  when such strips contribute >=50% of its total official area. Other qualifying
  groups remain in the full candidate set.
- These are uncalibrated screening choices, not crop identity, independent
  parcel evidence, ownership, legal availability or consolidation feasibility.

The version is `consolidation_strip_priority_20260906_v1`. Reproduce/verify with
`run_consolidation_priority.py run` / `verify`, followed by
`verify_consolidation_priority.py`. A completed version verifies instead of
overwriting itself. Changed logic requires a new version.

[Result and verification](../server_data/review/consolidation_strip_priority_20260906_v1/RESULT_EN.md)
contain exact counts, source lineage, road-review example and limitations.
No map action, release promotion, new download or external AI call was made.
