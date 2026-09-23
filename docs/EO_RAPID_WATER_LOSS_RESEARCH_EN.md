# EO feasibility: rapid soil-water loss

Research and local input check, 6 September 2026. Decision support only; no new
parcel classifications or map layer have been activated.

## Conclusion

EO can support a screening layer for repeated rapid surface drying and possible
water-retention problems. It cannot, by itself, establish that a small parcel
loses an excessive volume below the root zone during irrigation. The defensible
route is Sentinel-1 radar plus the existing Sentinel-2 series and weather, with
independent soil/event checks before attributing the cause or estimating excess
water relative to a norm.

The readiness run is not an EO feasibility classifier: its unassessed records
mean the evidence for the owner's full causal claim is absent, not that EO
screening is impossible. The draft evidence rule in RAPID_SOIL_WATER_LOSS_EN.md
describes that stronger claim. A preliminary EO screening rule would need its
own version, validation and explicit candidate interpretation.

## What the primary research supports

| Evidence | What it supports | Transfer limit for this project |
| --- | --- | --- |
| USGS NDMI documentation | Vegetation water-content monitoring | NDMI decline is not a measurement of soil drainage or applied irrigation volume. |
| Bazzi et al., 2022, irrigation-event detection with Sentinel-1 | Detection of some wetting events; shallow soil response | C-band mainly senses the first few centimetres; canopy and revisit gaps hide events. Surface drying does not prove root-zone depletion. |
| Ouaadi et al., 2025, modelled root-zone moisture | Surface radar moisture plus a recursive filter can estimate moisture at 15–20 cm | Tested on 12 fields at roughly 50 m resolution; performance varied substantially. The filter's time constant depends on soil, climate and depth. It is not a direct root-zone measurement. |
| Laluet et al., 2024, Sentinel-1 assimilation into SAMIR | Radar can constrain an explicit water-balance model | The strong reported result concerns district weekly irrigation. It requires model parameters and validation; it does not demonstrate parcel drainage accuracy here. |
| Ferreira et al., 2022, EO-assisted deep-percolation mapping | EO vegetation timing can support drainage hotspot modelling | The approach also used soil types, irrigation systems and calendars, and a soil-water-balance model validated against measurements. |
| Xu et al., 2025, satellite drydown and soil characteristics | Multi-year moisture declines can inform hydrological characteristics | Its global product is 36 km. The rapid drainage phase can occur at hourly scales and was not resolved by that satellite method. |

Sources: [USGS NDMI](https://www.usgs.gov/landsat-missions/normalized-difference-moisture-index);
[Bazzi et al.](https://doi.org/10.3390/agronomy12112725);
[Ouaadi et al.](https://doi.org/10.1016/j.agwat.2025.109507), with the
[authors' university record](https://research.slu.se/en/publications/root-zone-soil-moisture-mapping-at-very-high-spatial-resolution-u/);
[Laluet et al., full paper](https://horizon.documentation.ird.fr/exl-doc/pleins_textes/2024-05/010089727.pdf);
[Ferreira et al., university record](https://repositorio.ulisboa.pt/entities/publication/6a02b42b-c869-4a46-96e4-f30f2071a19a);
[Xu et al.](https://www.nature.com/articles/s41597-025-05048-y).

Another directly relevant study estimated drainage as a root-zone water-balance
residual, using climate, NDVI-based coefficients, soil hydraulic parameters and
manager-provided irrigation. This supports EO-assisted modelling, not an
EO-only drainage measurement. [Nassah et al., 2022](https://www.actahort.org/books/1335/1335_46.htm).

## Verified fit to the local data

- The prepared scene manifest contains 762 Sentinel-2 acquisitions from two
  collections. This baseline does not contain Sentinel-1 radar observations.
  Existing EO and weather will be reused; the full optical archive need not be
  downloaded again.
- There are 22,802 eligible parcels. Median official area is 0.2234855 ha;
  17,390 are below 0.5 ha and 5,109 below 0.1 ha. These are descriptive area
  counts, not new spatial eligibility thresholds.
- Sentinel-1 IW high-resolution GRD has approximately 20 × 22 m spatial
  resolution, despite 10 × 10 m pixel spacing. A 50 m retrieval cell covers
  0.25 ha, already larger than the median parcel before boundary contamination.
  Upsampling or copying a cell value to many parcels adds no independent
  information. [Copernicus product specification](https://sentiwiki.copernicus.eu/web/s1-products).
- Radar acquisition dates, consistent orbit coverage, usable parcel interiors
  and vegetation limitations must be checked before claiming a time series
  can resolve drying. No local radar archive coverage count has yet been made.
- All nine requested names match saved community polygons. Review-point
  association finds 1,384 eligible parcels in Ակնալիճ and 1,007 in Արշալույս.
  These are spatial review associations, not official community assignments.
- Մուսալեռ has no parcel representative point in the saved scope, and only one
  eligible parcel geometry intersects its saved boundary. This scope does not
  represent that community adequately. Do not silently enlarge the study area.
- No calibrated root-zone/event/soil-hydraulic inputs or applicable norm are
  configured. The completed readiness pass retains all 43,984 scope records,
  preserves exclusions and leaves all 22,802 eligible records unassessed.

## Proposed EO screening logic — not yet executed

1. **Observation gate.** Use a stable parcel interior with sufficient independent
   radar resolution elements. Correct and co-register acquisitions; compare
   consistent relative orbit, direction and incidence geometry. Do not smooth
   across parcel boundaries. Reject dense-canopy, standing-water, roughness/
   tillage-change and mixed-boundary cases where moisture attribution fails.
2. **Wetting candidates.** Detect a coherent surface-moisture increase above
   estimated measurement variability. Compare rainfall and neighbouring
   responses. Localized unexplained wetting is a possible irrigation event,
   not confirmed delivery. Regional ERA5-Land rainfall cannot rule out every
   local shower; unresolved events remain unresolved.
3. **Drying response.** Estimate the subsequent decline only where observations
   genuinely resolve it. Never treat a change in radar dB as a calibrated
   volumetric soil-moisture change without an appropriate retrieval. A wet
   observation followed by a dry one after a long gap does not establish a
   rapid drying rate. Rewetting interrupts an interval rather than becoming
   part of its drying fit.
4. **Comparable conditions.** Compare similar vegetation cover and observed
   growth phase, initial wetness, weather demand and actual elapsed interval.
   Repeated common-rainfall responses could provide a useful comparison where
   unknown irrigation does not contaminate the interval. This is a proposed
   diagnostic design, not proof of equal water supply to every parcel.
5. **Repeatability.** Require repeated usable events, test sensitivity to gaps,
   alternate valid dates and plausible retrieval uncertainty. Validate on
   held-out events and parcels. Set numerical thresholds after these checks;
   do not import published cutoffs from different sensors, soils and climates.
6. **Interpretation.** Repeated unusually fast surface drying can create a
   candidate for soil-water-retention review. It cannot automatically become
   measured deep drainage, a volume per irrigation, overwatering, or excess
   above an official norm. Soil and field evidence is needed for those claims.

Annual/perennial classification can support comparisons but cannot supply
exact water-demand coefficients. The owner assignment of uncertain annual
cycles to single cycle must not shorten the observed growing period in this
new analysis. Classification rules remain unchanged. Priority communities
affect sampling and review order only; comparison parcels outside them are
essential. No specific crops are identified.

Low storage capacity may call for smaller, more frequent irrigations rather
than a larger correct depth per event. Therefore rapid drying and above-norm
volume are separate quantities. [FAO scheduling guidance](https://www.fao.org/4/t7202e/t7202e06.htm).

## Bounded next step

Start with a **radar coverage and parcel-observability pilot** for one completed
season. Give Ակնալիճ and Արշալույս priority, include the other represented focus
communities and comparison parcels elsewhere. First list available acquisitions
and assess parcel interiors. Select the season and sample using actual coverage;
do not assume a nominal revisit interval. Only if that gate passes, obtain the
small radar subset needed to test wetting/drying repeatability while reusing
cached optical data and completed weather months.

Deliver a private table of observable/unobservable parcels and candidate
wetting/drying intervals, with uncertainty and competing explanations. This
pilot would test whether the evidence can support a meaningful one-colour
screening layer. It would not produce or publish fabricated water volumes.
Current map classes, 8525, 8526 and the selected Transitions v3 result remain
unchanged while this research decision is reviewed.
