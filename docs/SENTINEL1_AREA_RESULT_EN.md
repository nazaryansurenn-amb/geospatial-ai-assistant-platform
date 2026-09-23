# Full-area water-loss screening — 6 September 2026

The owner authorized extending the existing deterministic Python comparison to
all 22,802 eligible parcels in the saved Lower Hrazdan stages I/II selection.
This supersedes the earlier 96-parcel scope restriction for this private run.
Existing household (20,096) and road (1,086) exclusions remain unchanged.
There is no new public map layer or release promotion.

## Verified result

Version: `sentinel1_area_20260906_v1`.

| Evidence state | Parcels |
|---|---:|
| Relative radar signal at the main setting | 13 |
| Signal only at another sensitivity setting | 61 |
| Comparable observations without a main-setting relative signal | 597 |
| Insufficient comparable observations | 1,062 |
| Insufficient spatial support | 21,069 |
| Total | 22,802 |

There are 74 unique inspection leads, including 13 at the main setting. Each
of those 13 has only one main-setting signal, in either 2021 or 2022. Four of
the 74 have signals in two years when all sensitivity settings are considered.
No parcel has enough comparable triplets to assess the existing repetition
rule within a single year, orbit and satellite platform. Thus zero repetition
passes is an **unassessable result**, not evidence of normal water demand.

All 22,802 water-demand states remain **Unassessed**. Neither an irrigation
event, irrigation volume, rapid soil drainage nor above-normative demand has
been established. No independent ground-truth accuracy is available.

## Coverage and limitations

The 174 completed acquisitions cover April–September 2021–2024 and
April–June 2025. Frozen ERA5-Land inputs cover March–September 2021–2024 and
March–June 2025. July–September 2025 weather was unavailable at preparation;
later arrivals do not modify this sealed snapshot.

The original 204-scene cached catalogue was checked locally: every native
scene footprint fully contains the study selection. Public raster reads used
known scene assets; no new catalogue query containing study coordinates was
sent. These are the existing orbits 72/152 and inventory dates, not a claim
that every available archive acquisition or track was searched.

All 1,733 parcels passing the existing interior-area and width requirements
have 174 radar rows, giving 301,542 parcel/acquisition measurements. The other
21,069 parcels remain in the full register with insufficient-spatial-support
states and blank analytical counts. Their geometries, cadastral codes and
official areas are preserved exactly.

The rules still require three same-platform, same-orbit observations with
supported optical context, at least three matched peers and little rainfall
over the surrounding intervals. These long intervals remove many otherwise
matched comparisons. Observed counts cannot be treated as actual irrigation
frequency, and absent evidence cannot be treated as inactivity.

| Year | Radar acquisitions | Main-setting comparable triplets | Main-setting signal parcels | Any-setting signal parcels |
|---|---:|---:|---:|---:|
| 2021 | 58 | 1,810 | 4 | 42 |
| 2022 | 30 | 2,351 | 9 | 25 |
| 2023 | 30 | 0 | 0 | 0 |
| 2024 | 29 | 0 | 0 | 11 |
| 2025, partial | 27 | 0 | 0 | 0 |

Yearly counts overlap; they must not be added to obtain unique parcels.
The expanded comparison pool can change matched peers relative to the
96-parcel pilot even though thresholds and measurements remain the same.

## Reproducible implementation

- Configuration: `config/sentinel1_area_20260906_v1.json`.
- Data runner: `run_sentinel1_area_data.py` (prepare, collect, extract).
- Finite concurrent extraction: `run_sentinel1_area_stream.py`.
- Analysis runner: `run_sentinel1_area_analysis.py`.
- Array implementation: `wp_core/sentinel1_area_compare.py`.
- Independent checker: `verify_sentinel1_area_analysis.py`.

Use only this product's `.venv\Scripts\python.exe`. The download used eight
threads, extraction six processes, and the five independent yearly analyses
ran concurrently. No bulk LLM numerical calls or land-use classification
inputs were used. The original comparison thresholds and the separately
versioned 0.0001 mm precipitation precision policy were retained.

Data, sources, weather, code and results have local hash manifests. Completed
downloads and earlier versions are preserved. The sealed result is under
`data/analysis/rapid_water_loss/sentinel1_area_20260906_v1/`.
The complete register, main-setting list, sensitivity-only list, combined
inspection list, community counts and original-geometry GeoPackage are there.

Verification passed: all 174 image hashes/grids, exact cadastral identity and
GeoPackage roundtrip for 22,802 parcels, reproduction of 12,528 old pilot
radar/optical rows, 80 independently extracted pixel statistics, 249 rainfall
windows, 218,358 metric rows and two real positive triplets with all 702 peers
in their weather cell retained. Both real cases reproduced the frozen
reference implementation. The 19 relevant unit tests passed; pandas emitted
future-compatibility warnings, not failed checks.

Verification evidence:
`server_data/review/sentinel1_area_20260906_v1/verification.json`.
Both 8525 and 8526 returned healthy status after completion. No map classes,
releases, collector sources, credentials or cancelled monitoring automation
were changed by this full-area run.

## Bounded next decision

Inspect a small selection from the 13 main-setting leads against comparable
non-leads and obtain independent irrigation-frequency or field observations.
Use that evidence to decide whether to revise the temporal/rainfall rule.
No automatic threshold relaxation or new full-area recalculation is approved
by this recommendation.
