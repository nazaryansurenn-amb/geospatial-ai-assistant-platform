# Rapid soil-water loss and above-normative demand

Owner clarification, 6 September 2026: the intended target is irrigated land
needing more water because it loses water quickly through the soil. This is
separate from overwatering. The requested section title is
**Գերնորմատիվային ջրապահանջարկ ունեցող հողատարածքներ**, using one fill colour.

Priority review communities: Ակնալիճ and Արշալույս. Other focus communities:
Ֆերիկ, Ծաղկալանջ, Նորակերտ, Բաղրամյան (Էջմ.), Մուսալեռ, Այգեկ, Մերձավան.
The same eventual rule must apply to every eligible parcel in the saved Lower
Hrazdan I/II scope. A community name affects review order only; it is never a
positive label, training label, soil class or threshold adjustment.

## Proposed evidence rule

1. Retain existing cadastral identity, geometry, official area, household and
   road exclusions. Confirm irrigation independently: agricultural activity,
   annual/perennial type and canal proximity do not prove irrigation.
2. Establish root-zone depth, water retention and drainage using a relevant
   soil profile or field measurements. Fast surface infiltration alone does
   not establish rapid loss below the roots. Texture alone is insufficient.
3. Check repeated post-irrigation root-zone water balances. Account for rain,
   evapotranspiration, runoff, drainage, capillary contribution and measurement
   uncertainty over the actual event interval. EO supports vegetation timing
   and competing explanations; NDMI does not measure soil drainage or volume.
4. Compare like-for-like conditions and an applicable approved norm or validated
   reference. Separate low storage capacity from deep drainage losses. Define
   numerical cutoffs and repetition requirements only after inspecting suitable
   measurements and validating them. No numerical thresholds are accepted yet.
5. Mark a candidate only when the required evidence agrees. Missing inputs mean
   unassessed, not normal demand and not a positive case. Additional water for
   salt management must not automatically be treated as avoidable loss.

Low water storage can require smaller, more frequent irrigations. It does not
automatically mean a larger appropriate depth per event or greater crop ET.
The requested title is an analytical objective, not a claim that any parcel
has already been shown to exceed a legal/agronomic allowance.

For future measured volume comparisons, 1 mm on 1 ha equals 10 m³. Use the
verified irrigated event area as an additional analytical field; do not change
or silently substitute cadastral official area. Without the event area, a
whole-parcel equivalent depth must be explicitly distinguished.

## First executable step and actual limitation

`run_rapid_water_loss_readiness.py` reads the preserved scope, cadastral geometry,
saved community boundaries and EO table schema. It creates an immutable private
parcel readiness table and community review summary. It does **not** run a
calibrated soil-loss classifier. The current module has no configured irrigation
event/root-zone measurements, soil hydraulic inputs or applicable norm.

The local input inventory found EO, weather, cadastral and contextual products,
but no suitable parcel soil hydraulic or post-irrigation root-zone records.
An ontology entry describing water-delivery events is not evidence that a usable
dataset exists. No parent files, credentials, new imagery, 250 m dataset or LLM
bulk calculations are used. ERA5-Land weather alone cannot supply these missing
measurements.

Community association uses a representative point inside a saved polygon only
for review geography. It is not an official administrative/cadastral assignment.
Ambiguous or missing associations remain unresolved. All scope parcels remain
in the output regardless of community or existing classification.

The review section must show unassessed status and no parcel fill until suitable
evidence and a validated rule exist. Candidate count is null, not zero. Keep
existing Transitions v3 classification and working 8525 unchanged. A separate
versioned local frontend on 8526 is the owner review, not public publication.

## References

- [FAO: evaluation of irrigation field data](https://www.fao.org/4/t0231e/t0231e06.htm)
  distinguishes root-zone storage, deep percolation, runoff and adequacy.
- [FAO: irrigation scheduling](https://www.fao.org/4/t7202e/t7202e06.htm)
  relates irrigation depth and interval to the soil-water reservoir.
- [USGS: NDMI](https://www.usgs.gov/landsat-missions/normalized-difference-moisture-index)
  describes vegetation water content, not a soil drainage or applied-volume measurement.
