# Optical water-loss screening: completed private result

Version `optical_water_screening_20260906_v1`, completed 6 September 2026.
Rule: `docs/OPTICAL_WATER_SCREENING_RULE_EN.md`.

The requested run used stored Sentinel-2 optical measurements for the full
April–September seasons of 2021, 2022, 2023, 2024 and 2025. Radar measurements,
previous land-use classes and the 250 m stress dataset were not inputs.
There were no new imagery downloads or bulk LLM numerical calculations.

## Parcel results

All 22,802 eligible parcels remain in the register. Existing 20,096 household
and 1,086 road exclusions were retained. EO activity was observed in at least
one year on 17,884 parcels. The other 4,918 did not meet this activity rule;
that is not a conclusion that the land is inactive or unused.

| Evidence state | Parcels |
|---|---:|
| Recurrent pattern in multiple years, with mixed spatial support | 1 |
| One-season relative pattern | 66 |
| Isolated relative pattern | 1,915 |
| No recurrent pattern in assessable years | 11,078 |
| Active; insufficient comparable evidence | 4,824 |
| Activity not established by this EO rule | 4,918 |
| Total | 22,802 |

The inspection list contains **67 parcels** with at least one qualifying
season. Six have clearer spatial support, each with a signal in one season.
The other 61 have mixed spatial support: 60 one-season leads and one
multi-year lead. No parcel passed the multi-year recurrent rule with clearer
spatial support at any of the three tested moisture-change thresholds.

The six clearer one-season leads are:

| Community | Cadastral code | Official area, m² | Qualifying year |
|---|---|---:|---:|
| Ակնալիճ | 04-004-0115-0013 | 10,122.27 | 2024 |
| Աղավնատուն | 04-006-0239-0006 | 4,069.47 | 2023 |
| Աղավնատուն | 04-006-0296-0001 | 5,872.42 | 2023 |
| Ամբերդ | 04-008-0108-0026 | 7,268.99 | 2023 |
| Դողս | 04-038-0108-0014 | 8,225.53 | 2021 |
| Շահումյան | 04-077-0025-0017 | 2,833.46 | 2023 |

The multi-year mixed-support lead is Ամբերդ, **04-008-0108-0030**,
official area 2,066.61 m², with qualifying seasons in 2024 and 2025. It contains
only one completely internal 20 m pixel; it must not be promoted to the
clearer-support category merely because its pattern repeats.

**Every water-demand state remains Unassessed.** These are optical inspection
leads, not confirmed high-water-demand land. Neither actual irrigation
frequency nor water volume, rapid root-zone drainage, a crop type or a
water-consumption norm has been established. Rainfall, management, disease,
canopy effects and mixed pixels remain competing explanations. More leads or
more observations do not demonstrate improved accuracy.

## Actual coverage

| Year | EO-active parcels | Comparable triplets | Assessable parcels | Qualifying season signals |
|---|---:|---:|---:|---:|
| 2021 | 16,155 | 257,142 | 8,668 | 18 |
| 2022 | 14,497 | 222,399 | 8,698 | 7 |
| 2023 | 15,100 | 191,591 | 7,964 | 19 |
| 2024 | 16,551 | 232,824 | 8,986 | 22 |
| 2025 | 15,703 | 192,913 | 7,415 | 2 |

There are 1,096,869 comparable triplets and 12,953 distinct parcels with at
least one assessable season. Yearly parcel counts overlap. A parcel is
assessable only with enough comparable dates and temporal span; missing years
or dates are not assigned negative findings. 2025 has no priority in the
multi-year rule.

Weather is context only in this optical version. The frozen snapshot includes
March–September 2021–2024 and March–July 2025. July arrived before this run was
prepared. August–September 2025 remain missing in the snapshot and are marked
unknown for affected event intervals. These gaps did not exclude the full
2025 optical season. "Low recorded rain" is not proof of irrigation.

## Verification and preservation

The eight targeted unit tests passed. The independent checker passed after
handling the empty clearer-support recurrent CSV as an empty numeric table:
no analytical code, thresholds or results changed during that correction.

Verification covered all 22,802 immutable parcel identities and GeoPackage
geometries, 114,010 parcel-seasons, 342,030 seasonal metric rows, 200 sampled
triplets against actual cached EO, 400 three-date spatial-mask intersections,
50 independently selected real peer groups, and 7,644 event rows and weather
intervals across the three fixed sensitivities. This verifies implementation
and provenance, not scientific accuracy against field measurements.

The rule, code, inputs, weather and outputs have hash manifests. Completed
annual results are reusable and earlier versions remain preserved. Both
8525 (working) and 8526 (review) returned healthy status after completion;
neither map nor release was changed.

Sources/results:
- `data/analysis/rapid_water_loss/optical_water_screening_20260906_v1/`
- `server_data/review/optical_water_screening_20260906_v1/verification.json`
- `outputs/01a0756f-7ebb-7a02-a055-d253516fadd3/optical_water_screening_20260906_v1/`

The next bounded check is to inspect the six clearer one-season leads and
the single multi-year mixed-support lead against independent field evidence.
Changing thresholds or publishing a layer remains a separate decision.
