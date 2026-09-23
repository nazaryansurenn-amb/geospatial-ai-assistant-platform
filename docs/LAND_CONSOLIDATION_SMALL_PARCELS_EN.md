# Current consolidation screening rule

Owner decision, 2026-09-06: each participating parcel must have an official
cadastral area of **0.5 ha or less**. A connected group must still total **at
least 5 ha**. Apply the member-size gate before grouping, not after forming
large blocks. Do not round hectares before testing. At least ten members are
therefore necessary, and more when their average area is below 0.5 ha.

This is an approved screening rule, not approval of any candidate for publication.
Current private result: `consolidation_small_parcels_20260906_v1`.
Configuration: `config/consolidation_small_parcels_20260906_v1.json`.
Runner: `run_land_consolidation_small_parcels.py run` (sealed results are verified,
not overwritten). Verification: the same runner with `verify`.

## Preserved methods

- Active, ready parcels come from the preserved 2026 working activity release.
  Similarity uses completed 2021-2025 Sentinel-2 parcel time series.
- Household, roads, conflicting masks, substantive overlaps and mapped barriers
  remain excluded or in explicit review states. Cadastral geometry, code and
  official area are immutable.
- The earlier four-index screen uses NDVI, EVI2, NDMI and BSI and requires native
  spatial support at both 10 m and 20 m.
- The separate additional 10 m screen targets parcels lacking a wholly contained
  20 m pixel but having width >=10 m and at least three pure 10 m pixels. NDVI
  and EVI2 are primary; mixed NDMI/BSI observations are context only. NDVI and
  EVI2 share red/NIR bands and are not independent confirmations.
- Each screen retains its existing adjacency, shared-pixel, actual-date coverage,
  seasonal contrast and similarity rules. Connected groups must satisfy every
  member-pair comparison in at least three common years; similarity cannot be
  inferred by chaining neighbors.
- The two evidence-quality populations remain separate. Cross-screen groups have
  not been tested or ruled out. This limitation must accompany results.
- The original four blocks are retained as historical output but **none meets
  the new member-size rule**. They must not be presented as current candidates.

## Verified result

| Item | Count |
| --- | ---: |
| Active parcels retained in register | 16,258 |
| Above the 0.5 ha member limit | 3,788 |
| Within limit but retained for spatial/mask review | 6,581 |
| Size-eligible with support for the four-index screen | 1,818 |
| Size-eligible in the separate additional 10 m screen | 4,071 |
| Similar groups below 5 ha | 23 |
| Parcels in those groups | 46 |
| Qualifying groups of at least 5 ha | 0 |

The largest similar group is **0.860019 ha**. This does not establish that
consolidation is impossible: it describes the current conservative, separate
screens. No ownership, willingness, common management or feasibility is inferred.

The preceding unrestricted additional 10 m run screened all 4,875 targets,
including 3,159 parcels with a usable target-to-target boundary. It read
2,299,752 cached parcel/date rows and found 20 similar groups, all below 5 ha;
the largest was 1.850874 ha. Both earlier result sets are sealed and preserved.

## Evidence and boundaries

- Full test suite: **270 passed**, five existing warnings in unrelated optical
  water-screening and radar-comparison tests.
- `verify_land_consolidation_10m_replay.py` independently reproduced primary
  quality/date/contamination and numerical decisions from saved observations:
  46 pairs, including all 22 positive 10 m pairs, 170 pair-years and 84 parcels.
  This verifies implementation, not independent field accuracy.
- All final size-filtered groups passed identity, exact member area, connectivity,
  disjoint membership and common-year all-pair checks. Input and historical
  output hashes are unchanged. Source geometry was not edited.
- Both 8525 and 8526 returned HTTP 200 on `/api/health`. The new block GeoJSON
  and 10 m parcel register returned HTTP 404 on both public servers.
- No map release, application, EO raster, weather process, household/road mask or
  prior classification was changed. No new downloads or AI API calls were used.
- No 250 m data is an input. All EO input hashes trace to saved native-resolution
  Sentinel-2 parcel observations; version manifests contain the full private lineage.

Private report and verification receipt:
`server_data/review/consolidation_small_parcels_20260906_v1/`.
The result is not accepted, not training truth and not a public map layer.
