# Current-season possible irrigation-stress signs

Owner request: use the recent ECMWF estimates (Option 1), add a separate UI
section and use one red colour. Title: **Ոռոգման սթրեսի հնարավոր նշաններ**.
The section is a current 2026 screening as of **5 September**, not an irrigation
instruction, a seven-day forecast, measured water consumption or soil degradation.

## Inputs and scope

All 22,802 existing non-household, non-road parcels in the saved Lower Hrazdan I/II
observed zone are retained in the output. No potential expansion is included.
Cadastral codes, geometry, official hectares and exclusions are unchanged.
Earlier use-type, activity, potential, cultivation-change and degradation classes
are not numerical inputs to this stress rule. The existing index supplies only
identities, coordinates, stage/community membership and official area.

- Current Sentinel-2 cache: `current_stress_inputs_20260907_v2`, 22 scenes from
  1 August through 5 September 2026, 501,644 parcel observations. Scene outputs and
  checksums are complete. The older activity layer still ends on 23 August.
- Historical comparison: completed locally cached Sentinel-2 observations over
  the same August/early-September interval in 2021–2025. No archive redownload.
- Weather: `current_stress_weather_20260907_v1`, explicit ECMWF IFS model estimates
  from Open-Meteo, sampled at nine public coarse locations. No automatic Best Match
  mixture, ERA5 replacement, elevation downscaling, private records or credentials
  are transmitted. All required hourly fields are present for the requested
  1 August–6 September windows in 2024, 2025 and 2026. Native provider grid coordinates
  and units are retained. Only hours preceding each actual EO observation are used.
- Temperature, humidity/dewpoint, wind, pressure, radiation, precipitation and the
  provider's FAO reference ET0 are cached. Rainfall and reference ET0 provide the
  classification's weather gates. ET0 is not measured crop ET or irrigation volume.
  No radar, thermal CWSI, measured root-zone moisture or 250 m product is used.

## Frozen local screening rule

Runner: `run_current_irrigation_stress_v2.py`; shared deterministic rules:
`wp_core/current_irrigation_stress.py`; configuration:
`config/irrigation_stress_20260907_v2.json`. These settings are provisional local
screening choices, not universal FAO thresholds or independently calibrated accuracy.

1. Use native 10 m vegetation and 20 m moisture measurements with at least 95%
   clear/valid coverage, at most 5% water, valid reflectance handling and finite
   values. Require at least 1,200 m2 of geometric sampling support and an effective
   count of three 20 m pixels, calculated as `(sum area)^2 / sum(area^2)`.
   Boundary pixels may be shared across parcels. Their signals are joint screening
   evidence, not independent confirmation for every parcel. Two accepted dates
   have a conservative 90% common-area bound, not measured pixelwise persistence.
   The saved SCL mask includes unclassified SCL 7; a stricter new mask is not claimed.
2. Select one coherent scene per parcel/date by greatest valid support with a
   deterministic scene-ID tie break. Baseline: at least three valid dates spanning
   seven days during 1–20 August. Recent: the latest clear observation within three
   days of 5 September and a preceding observation at least three days earlier,
   both in 27 August–5 September. Never replace a clear harvested latest observation
   with an older greener one. Missing observations are not filled.
3. Both recent observations retain NDVI >=0.40, EVI2 >=0.20 and vegetation share
   >=0.50. Baseline vegetation also meets those values. Require retention of at least
   75% of baseline NDVI/EVI2, vegetation-share loss <=0.20, bare-share growth <=0.15,
   and no recent-pair NDVI/EVI2 drop exceeding 0.15/0.12. Larger changes stay under
   review because harvest/senescence cannot safely be separated. This excludes
   some genuine severe stress and does not eliminate all management ambiguity.
4. NDMI must fall at least 0.08 below its early-August median on both recent dates.
   Do not count MSI as an independent signal: it is algebraically related to NDMI.
5. Compare the mean recent NDMI change with at least 20 nearby parcels within 5 km,
   excluding self, matched on observed baseline NDVI, EVI2, vegetation fraction and
   recent NDVI/date. Require an additional decline of 0.05 below their median.
   This matches vegetation context, not known identical crops, soils or supply.
   Neighbouring observations may share pixels and are not independent replicates.
6. Require at least two earlier comparable seasons for the same parcel, with
   similar early/recent NDVI and adequate temporal/vegetation support. The current
   moisture change must be at least 0.04 more negative than their median. Historical
   checks use only years preceding the assessed year, not future reference data.
7. For both recent EO dates, require all 336 preceding hourly weather rows, at least
   25 mm reference ET0 over those 14 days, precipitation <=half of ET0, and <5 mm rain
   in the preceding 48 hours. These indicate dry atmospheric conditions; they do
   not measure effective rain, irrigation, root-zone storage or actual water deficit.

Only the conjunction is red. All evidence requirements must be met to call a parcel
assessed. Unassessed and not-flagged are distinct; neither means certified stress-free.
The current-vegetation exclusion is a screening result, not a new inactivity class.

## Spatial review and results

The first preserved strict run, `irrigation_stress_20260907_v1`, required three
nearly whole 20 m pixels covering >=70% of each parcel. Only 952 parcels met that
spatial criterion; three reached complete 2026 assessment, with zero candidates.
This was inadequate coverage for the local small/narrow-parcel pattern. It is
preserved as a private diagnostic and is not the proposed map result.

Version 2 uses the area-weighted sampling rule above, with its explicit shared-pixel
limitation. Every temporal, vegetation, moisture, peer, historical and weather
threshold is unchanged. Thresholds were not relaxed to obtain a candidate count.

| 2026 state | Parcels | Whole official ha |
| --- | ---: | ---: |
| Possible signs | 43 | 15.092812 |
| Assessed, no sign flagged | 1,810 | 779.565855 |
| Unassessed | 10,421 | 2,025.438820 |
| Current growing vegetation not established by the recent screen | 10,528 | 7,519.211579 |
| Total | 22,802 | 10,339.309066 |

1,853 parcels /794.658667 ha are assessed. Candidate latest dates are 4–5 September.
Stage I: 13 /5.247871 ha; Stage II: 30 /9.844941 ha.
The historical checks found 10 possible candidates among 1,584 assessed parcels in
2024 and 15 among 1,700 in 2025. These are reproducibility/plausibility checks, not
measured true positives or classification accuracy. No field validation is available.

## UI and validation

Additive launcher: `run_irrigation_stress_review.py`; review build:
`irrigation_stress_review_20260907_v1`. It retains the accepted >=50% cultivation-change
version and the approved readability stylesheet. The stress section is eighth, one
red `#dc3038` class, with counts and official hectares, coverage and actual EO dates.
No existing section or map tile is recalculated. The new agent index adds only
stress state/date fields; all previous index values are equal. It supports community
tables, charts, Excel and the existing reversible, user-confirmed map selection.
Private metrics, weather grids, thresholds and local source paths are not served.

Independent checker: identities, union/count/area reconciliation, candidate evidence,
all 43 IDs in existing vector tiles, antecedent weather recomputation and no future
historical reference pass. Seven prior HTTP summaries and eight parcel profiles are
unchanged after removing only the added stress field. Private-path and origin checks
pass; 8525 retains its original response hash.

Full suite: 429 tests and 14 subtests passed, with five existing dependency warnings.
The initial default temporary-directory run had Windows permission errors; the full
rerun in a fresh product-owned test directory passed. Desktop 1280x720 and mobile
390x844 browser checks show eight tabs, one red control, actual rendered red parcels,
scope counts, toggling, focus and no horizontal overflow. The approved agent input
font remains 16 px. A real agent response returned 43 /15.09 ha and 12 community rows.
The Excel button received HTTP 200, and workbook tests verify all 43 dated parcel
rows and numeric tables. Browser download-event capture timed out, so OS file
placement is not asserted. No cancelled automation or weather worker was restarted.

Rollback: `run_readability_review.py --port 8526` with its preserved area50 backend.
Activation status is recorded separately in `server_data/agent_v7/activation.json`.

## Method references

- FAO56 soil-water stress and the field parameters needed for a true root-zone
  balance: https://www.fao.org/4/x0490e/x0490e0e.htm
- Sentinel-2 band/resolution specifications:
  https://sentiwiki.copernicus.eu/web/s2-mission
- Weather source, units, reference ET0 and ECMWF model-estimate construction:
  https://open-meteo.com/en/docs/historical-weather-api

These support the interpretation and input limitations; they do not validate the
specific local numerical cutoffs or establish that a red parcel needs irrigation.
