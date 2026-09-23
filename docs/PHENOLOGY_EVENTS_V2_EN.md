# Bounded soil-interval correction — private control

Scope approved on 2026-09-06: inspect the remaining merged growth episode and
three specific annual/perennial thinning contradictions, then test one targeted
correction on the existing 120 parcels. The calculation uses completed seasons
2021–2025 and the existing continuous observation cache.

## Single method change

Version `events_20260906_v2` changes only the grouping of observed soil witnesses.
All numerical policy values, annual/perennial decisions, coverage requirements,
spatial exclusions, growth qualification and five-year voting remain as in the
preserved independent event experiment v1.

Previously, any usable observation with nonpositive BSI ended a soil run, even
when NDVI and EVI2 remained low. That allowed an early short growth fragment and
later growth to remain inside one episode across repeatedly observed soil.

The corrected grouping:

1. Find consecutive usable observations with low NDVI and low EVI2. A non-low
   observation or a gap beyond the existing local limit breaks the interval.
2. Require repeated positive-BSI soil witnesses inside that low interval, with
   the existing minimum count, minimum span and maximum witness spacing.
3. Bound the separating interval by the first and last soil witnesses. Require
   sufficient common valid ground across the entire interval and adjacent
   observed growth, on both native grids.
4. An intermediate low observation with nonpositive BSI remains a low-vegetation
   observation. It is never promoted to a soil witness or used to satisfy the
   repeated-soil ending requirement.
5. Separate growth fragments before deciding whether either is a complete
   cycle. Short growth, missing boundaries, gaps and rapid recovery keep their
   existing restrictions. A split does not establish two annual cycles.

This is a correction to observation grouping, not a calibration of the spectral
thresholds. Previously assigned classes do not enter inference.

## Diagnosed but unchanged

The three jointly resolved annual/perennial contradictions have a complete
episode and a terminal November soil decline in both full and thinned data.
Thinning changes green duration or green fraction across the perennial cutoffs;
the existing branch order then chooses perennial or falls back to annual.

There are also ten comparable annual/perennial type switches involving an
uncertain annual cycle count. Report all type switches separately from fully
specified disagreements. They must not disappear from reporting merely because
the annual cycle count is unresolved.

Explicitly retaining competing sustained-profile and completed-episode evidence
is a proposed further correction. It is not included in this version, so the
effect of the soil-grouping change can be assessed separately. Neither temporal
consistency nor increased uncertainty establishes classification accuracy.

## Execution and preservation

Use WORKING_PRODUCT's own environment:

```powershell
.\.venv\Scripts\python.exe -B run_phenology_events_v2.py --workers auto
.\.venv\Scripts\python.exe -B verify_phenology_events_v2.py
```

The independent v2 engine is `wp_core/phenology_events_v2.py`; the explicit
configuration is `config/phenology_events_v2.json`. Neither imports earlier
classifiers. The runner preserves the existing input allowlist and CPU/memory
limits, verifies serial/parallel equality and calculates only the fixed control.

All v1 code, configuration and completed outputs remain intact. The v2 runner
adds their hashes to its protected set. New predictions are saved before old
class tables are opened for comparisons with transitions v3 and event v1.

Output: `data/analysis/observation_screening/events_20260906_v2/`.
Private diagnosis, verification and result report:
`server_data/review/events_20260906_v2/`.

All outputs remain provisional, unaccepted and ineligible as training truth.
No full-area classification, public interface, map release, weather collector or
monitoring automation is changed. Dated independent reference review is still
needed to establish accuracy.
