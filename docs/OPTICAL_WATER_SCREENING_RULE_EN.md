# Optical-only water-loss screening rule

Owner authorization, 6 September 2026: replace radar as an input for this new
experiment, write a rule from the stored EO data, and run 2021–2025 on active
land excluding household parcels. Prior road exclusions remain unchanged.
This is a separate private result; no existing analytical version is replaced.

## Meaning

Look for unusually repeated canopy-moisture decline and recovery during stable
green vegetation, relative to similar nearby parcels observed on the same
three dates. This is a lead for investigating rapid water loss or recurring
water stress. It does not measure irrigation events, volumes, root-zone
drainage, soil texture or an applicable water norm.

NDMI is sensitive to vegetation water content, not a direct root-zone moisture
measurement: https://www.usgs.gov/landsat-missions/normalized-difference-moisture-index
Canopy state, soil evaporation and weather affect water demand and its optical
context: https://www.fao.org/4/x0490E/x0490e0a.htm
These sources support the interpretation, not the numerical thresholds below.
The thresholds are explicit, uncalibrated screening choices frozen before the
run. No ground-truth accuracy or improvement over radar is claimed.

## Frozen rule

1. Keep all 22,802 eligible parcel identities in the register. Determine EO
   activity independently for each April–September season: at least four
   quality dates spanning 20 days with NDVI >= 0.35, EVI2 >= 0.20 and vegetation
   fraction >= 0.45. Missing activity evidence is not proof of inactivity.
   No annual/perennial/cycle classification is an input.
2. Reuse the cached same-scene daily indices and actual area-weighted masks.
   Each date needs >=80% support. Three consecutive quality dates need >=80%
   common visible area on BOTH 10 m and 20 m grids. No temporal interpolation
   or radar-resolution rule is used. Steps must be 2–15 days, at most 25 days
   for the whole triplet.
3. Require active vegetation on all three dates, NDVI range <=0.12, EVI2 range
   <=0.10, BSI range <=0.10 and each date's spatial NDVI p90-p10 <=0.40. These
   screens reduce harvest, senescence, canopy growth and mixed-cover effects;
   they do not identify a crop or prove those confounders absent.
4. Keep small/mixed parcels in the numerical analysis, in a separate support
   stratum. Clearer spatial support means >=3 fully internal 20 m pixels and
   minimum parcel width >=20 m. This does not alter cadastral geometry.
   Mixed-support patterns are reported separately from the clearer shortlist.
5. Match up to 20 peers from the nearest 256 eligible static neighbours within
   5 km, same nearest ERA5 grid cell and same spatial-support stratum, with area
   and width ratios <=3. Require at least five. Peers must have the SAME actual
   observation dates and comparable NDVI, EVI2 and BSI on all three dates;
   initial NDMI must be within 0.10. Matching uses no prior land-use labels.
6. At the main setting, NDMI must fall >=0.04 then recover >=0.04. Both changes
   must exceed peer medians by >=0.02. The decline/day must exceed the peer
   median by max(0.001, twice 1.4826*MAD). Repeat at 0.03 and 0.05 only to report
   threshold sensitivity, without changing the main rule after viewing counts.
7. Count distinct relative patterns greedily in date order: troughs >=14 days
   apart and no overlapping event intervals (sharing an endpoint is allowed).
   A season requires >=8 comparable triplets spanning >=60 days, >=2 distinct
   relative patterns and an observed raw-pattern fraction at least 0.05 above
   the matched-peer mean. The fraction denominator is comparable observations,
   never calendar days with missing data. This is not actual irrigation rate.
8. A recurrent candidate needs the seasonal rule in >=2 different completed
   years. Report all active/assessable/positive years; 2025 receives no priority.
   One-season or single-pattern leads are separate review states. A candidate
   with mixed spatial support stays a mixed-support review candidate.
9. Freeze completed local weather months and annotate recovery intervals with
   their available precipitation. Include complete calendar days touching an
   interval. Rain <=2 mm is an explicitly chosen context flag, not a norm.
   Missing weather stays unknown. Rain is not a gate for the optical run, so
   all five optical seasons, including full 2025, are analyzed. Matched dates
   and local peers control common conditions imperfectly; rain, management,
   soil, disease and canopy effects remain competing explanations.

Only local cached tables, weights, cadastral identity, community geography and
available ERA5-Land weather are inputs. No Sentinel-1 measurements, downloads,
250 m stress products, past classifier outputs, LLM numerical calls or private
operational records enter the calculation.

Config: `config/optical_water_screening_20260906_v1.json`.
Runner: `run_optical_water_screening.py`.
Rules: `wp_core/optical_water_screening.py`.
Checker: `verify_optical_water_screening.py`.
Use this product's `.venv\Scripts\python.exe`. Preserve every completed source,
annual checkpoint and earlier version. Maps 8525/8526 require a separate action.
