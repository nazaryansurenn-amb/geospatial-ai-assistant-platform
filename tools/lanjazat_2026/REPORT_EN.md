# Lanjazat: Rapid 2026 Agricultural and Vegetation Screening

Prepared 7 September 2026. Administrative area only: no buffer, no substitution with the settlement outline, and no cadastral parcel classification.

## Results

| Indicator | Approximate area |
| --- | ---: |
| OSM administrative territory | 2,428 ha |
| Mapped agricultural context: farmland, orchards and vineyards plus historical cropland reference | 241 ha |
| Sustained 2026 vegetation inside that agricultural context | **167 ha** |
| Summer vegetation inside agricultural context compatible with irrigation support | **145 ha** |
| Repeated vegetation across the whole administrative area, including natural vegetation | **594 ha** |
| Recent vegetation across the whole administrative area, using each pixel's latest sampled clear observation | **361 ha** |

The 145 ha is a subset of the 167 ha, not additional land. Similarly, agricultural vegetation contributes to the all-vegetation total. Do not add these figures together.

**Interpretation:** about 167 ha showed sustained vegetation on land mapped as agricultural. About 145 ha of that also remained green and moisture-bearing during several dry summer periods. This is an irrigation-compatible signal, not proof of irrigation or an estimate of water volume. **Confirmed irrigated hectares cannot be established by this screening.**

## Boundary

[OpenStreetMap administrative relation 15030684](https://www.openstreetmap.org/relation/15030684), tagged Lanjazat, administrative level 7. The retrieved geometry is valid. An independent reconstruction from the current OSM relation matched the downloaded boundary with zero geometric difference. Its projected area is 2,428.1368 ha in EPSG:32638.

This is the administrative boundary mapped in OSM, not a certified cadastral/legal survey. The smaller OSM village outline, way 636025750, was not used for the calculation.

## Observations

Thirteen actual Copernicus Sentinel-2 surface-reflectance scenes were sampled: 11 and 18 March; 15 and 17 April; 10 and 27 May; 6 and 29 June; 14 and 26 July; 5 and 23 August; and 2 September 2026.

The quick selection takes one low-scene-cloud product per half-month. Actual local cloud, shadow, snow and invalid observations are screened at pixel level. For example, the 18 March scene had very little usable local coverage; its missing pixels were not counted as inactive. No interpolation or forward fill was used.

The figures describe a March-to-early-September sample, not every 2026 acquisition, not the full calendar year, and not a same-day survey. Two observation dates in one month are not automatically evidence of two crop cycles.

## Method

- All area calculations use exact intersections between 20 m analysis cells and the administrative boundary. The old 250 m grid and Echmiadzin parcel classifications are not inputs.
- NDVI screens repeated green vegetation. NDMI provides moisture-related optical context. Clear observations must recur over time; one green image is insufficient.
- Agricultural context combines 41 intersecting OSM farmland/orchard/vineyard ways with the historical cropland class from [ESA WorldCover 2021](https://esa-worldcover.org/en/data-access). Overlaps are unioned, not double-counted, and historical built-up/water classes are excluded.
- The initial historical-cropland-only screen is preserved separately. The reported result adds mapped orchards and vineyards without repeating satellite downloads.
- [ECMWF IFS estimates through Open-Meteo](https://open-meteo.com/en/docs/historical-weather-api) provide rainfall and reference evapotranspiration context from the preceding days. They are coarse weather-model estimates, not local weather-station or soil-water measurements. Only antecedent days are used for each observation.

## Coverage and Uncertainty

Approximately 2,380 ha, or 98.0% of the administrative area, met the temporal observation requirements. About 48 ha did not. Within mapped agricultural context, about 0.64 ha lacked sufficient observations. Adequate observations do not establish accurate land-use classification.

A modest change in the vegetation screen changes the active-agricultural-context estimate from approximately **150 to 183 ha**. This is a rule-sensitivity range, not a statistical confidence interval or a ground-truth accuracy measurement.

Important limitations:

- OSM land use and the 2021 WorldCover reference can be incomplete or outdated. The 241 ha context is not a complete official agricultural inventory.
- Unmapped new fields, some orchards, household plots, grazing land and recent conversions may be missed or misrepresented. The result focuses on mapped crop/orchard/vineyard context, not every agricultural use.
- Green growth in a mapped orchard can include weeds or understory. It does not prove active management or harvested production.
- Persistent moisture-bearing vegetation during dry periods can reflect irrigation, groundwater access, deep-rooted plants or stored moisture. The 145 ha must not be presented as confirmed irrigated land.
- The sampled dates may miss short growing cycles. Mixed 20 m pixels limit fine-scale interpretation, particularly near buildings, roads and small plots.
- Rounded hectares are appropriate. The extra decimal places retained in machine-readable results are computational precision, not real-world measurement accuracy.

## Verification and Files

Six focused tests passed. A separate checker reproduced the main totals from the saved observations, verified area reconciliation, valid geometry, source hashes, no area outside the boundary, and no duplicated overlap area. These are computational checks; field accuracy and actual irrigation remain unverified.

- `output/result_v2.json`: final screening totals, provenance and limitations.
- `output/result.json`: preserved initial cropland-reference-only result.
- `output/verification.json`: independent computational checks.
- `output/dates.csv`: per-date all-area observation coverage.
- `output/boundary.geojson`: the administrative area used.

Everything is isolated under `WORKING_PRODUCT/tools/lanjazat_2026`. The working application on 8526, its public tunnel, cadastral records, analytical releases and weather collection were not changed.

Source attribution: OpenStreetMap contributors (ODbL); ESA WorldCover project 2021 / contains modified Copernicus Sentinel data (2021), processed by the ESA WorldCover consortium; Copernicus Sentinel-2 data distributed through Earth Search; Open-Meteo / ECMWF weather estimates.
