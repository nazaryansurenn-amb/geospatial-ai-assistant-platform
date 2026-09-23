# Private parcel delivery, 2021–2025

The owner's instruction to deliver the lands authorizes a concrete private parcel
list using the existing 96-parcel pilot and cached observations. This version
addresses the previously explained rainfall precision issue. Earlier code, rules,
outputs and map releases remain intact. There is no full-area expansion.

ERA5-Land precipitation is accumulated from forecast initialization. The cached
NetCDF metadata confirms `tp`, units m, `GRIB_stepType=accum`, float32 storage.
The converted NetCDF does not retain a per-step GRIB packing-error bound.
ECMWF explains that independently packed accumulations can produce tiny negative
differences; this is also documented in its ERA5-Land support response:

- https://confluence.ecmwf.int/display/UDOC/Why%2Bare%2Bthere%2Bsometimes%2Bsmall%2Bnegative%2Bprecipitation%2Baccumulations%2B-%2BecCodes%2BGRIB%2BFAQ
- https://forum.ecmwf.int/t/era5-land-precipitation-negative-values-after-deaccumulation/12765/2
- https://confluence.ecmwf.int/pages/viewpage.action?pageId=505384848

The new explicit numerical policy sets finite negative hourly differences no
smaller than -0.0001 mm to zero, recording every correction. Larger negatives,
missing predecessors and nonfinite values stay unknown. Reset remains 01 UTC;
00 UTC remains the last interval of the preceding forecast day. Positive values
and raw downloaded data are unchanged. The tolerance is our declared precision
policy, not an exact packing-error value recovered from the source.

All existing optical matching, peer selection, temporal, rainfall, excursion and
repetition criteria are reused unchanged. Weather is frozen at preparation from
completed, hash-checked monthly checkpoints. Each year uses only its available
weather months; observations are never borrowed across years. Python uses up to
four independent worker processes, one per year, for the bounded rerun.

The main inspection list contains parcels with an observed relative radar
excursion at the middle of the existing sensitivity settings: rain <=1 mm,
VV rise and fall >=2 dB, each >=1 dB above matched-peer median, with at least
three matched peers and all existing optical/temporal/spatial requirements.
Its purpose is to provide actual parcel identities for checking in the field.
This list does not apply a newly weakened repetition classifier: original
repetition pass counts and exposure are reported separately. Parcels with a
signal only at another existing sensitivity are in a separate table. Every pilot
parcel appears in the full register, including all insufficient-observation cases.

Multiple settings are overlapping sensitivity checks, not independent evidence.
Counts are observed radar triplets, not irrigation events. A single inspection
candidate does not establish persistent water demand, rapid root-zone drainage,
applied volume or above-normative use. High-water-demand states remain unassessed.
Communities and existing land-use classes are never used as training truth.

The delivered GeoPackage preserves exact pilot cadastral geometry, identifiers,
codes and official areas. Coordinates are interior representative points solely
for locating parcels. Lists and diagnostics are private, not a public map layer.

Run `run_sentinel1_parcel_delivery.py`, then `verify_sentinel1_parcel_delivery.py`
with the product's `.venv` Python. Preparation pins this specification, code,
configuration, input tables and exact completed weather sources. Repeated runs
reuse the sealed version. New weather requires a distinct later version.
