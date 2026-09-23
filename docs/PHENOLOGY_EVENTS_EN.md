# Independent observation-event experiment

Approved scope, 2026-09-06: a separate private experiment on the existing 120
control parcels, using completed seasons 2021–2025. The owner approved writing
explicit rules and running deterministic Python, with protection from previous
classifications and efficient use of the local PC. Full-area calculation and
map publication remain outside this approval.

## Independence and retained measurement preparation

The engine is `wp_core/phenology_events.py`. Its complete numerical policy is
declared in `config/phenology_events.json`. It does not import, execute, read
parameters from, or initialize predictions from any previous classifier.

The runner passes only continuous index statistics, valid-observation masks,
spatial support diagnostics and dates into the engine. Cadastral identifiers
are used to join data and attach outputs, not to decide a class. Previous types,
cycles, events, reasons and review labels are excluded. The old derived
`vegetation_fraction`, `bare_fraction` and `vegetated_mask_10m` fields are also
excluded because they contain previous threshold decisions.

The existing 120-parcel selection and measurement preparation are retained:
cached, radiometrically corrected Sentinel-2 observations, SCL 4/5 masking,
same-date scene selection and native-grid area weights. The sample was selected
using earlier candidates and is not an independent probability sample. This
experiment does not undo that selection or the observation preprocessing.

Conservative observation-support, coverage, spatial and spectral guard values
are explicitly restated in the new configuration to keep the experiment
bounded. They remain uncalibrated settings. No predecessor configuration is
loaded as the effective policy. Numerical similarity of a declared guard is
not a dependency on an old classifier.

Historical output/code bytes are checksum-verified for preservation. Old class
tables are opened for comparison only after the new predictions are calculated
and saved. They are never treated as reference truth or training labels.

## Rules applied by Python

1. **Validate the observation.** Require dated, finite, concordant NDVI, EVI2,
   NDMI, BSI and NDRE measurements with sufficient valid area on both native
   grids. Decode masks using their actual packed-byte order and parcel weights.
   Missing values do not become zero, bare soil, inactivity or a class.
2. **Check the season and spatial interpretation.** Keep early/late coverage,
   four seasonal phases, the number of usable dates and the largest gap explicit.
   Keep small/narrow parcels, heterogeneous signals and edge sensitivity in
   review. Never modify cadastral geometry, codes, official area or exclusions.
3. **Find observed soil intervals before qualifying growth.** Repeated low
   NDVI/EVI2 with positive BSI must share sufficient valid ground with adjoining
   observations. A supported interval separates earlier and later growth even
   when the earlier fragment is too short to qualify as a complete cycle.
4. **Retain all growth fragments.** Record start, peak, observed decline/end,
   preceding/following soil dates, duration, area support and evidence gaps.
   Incomplete, left-censored, right-censored and gap-interrupted episodes remain
   visible. Two short fragments cannot be joined merely to meet a duration rule.
5. **Qualify a complete cycle separately.** Require sustained observed growth,
   an observed rise, a repeated soil end, sufficient spectral change and common
   spatial support. An unobserved start/end or gap remains unresolved.
6. **Separate quick recovery from independent cycles.** Record rapid regrowth
   as a competing explanation. Measure its interval from the onset of observed
   soil exposure, not the last soil image, because dense observations must not
   artificially shorten the duration. A rebound does not prove a new sowing.
7. **Assign only provisional annual/perennial hypotheses.** A sustained profile
   without observed internal resets or unresolved interruptions can support a
   perennial candidate; it does not prove trees or a specific crop. A complete
   growth episode can support an annual candidate. Competing incomplete episodes
   prevent an exact annual-cycle count. Low signal alone remains undetermined.
8. **Keep five separate annual records.** The predominant type requires at least
   three covered years and a strict majority of all covered years, including
   ambiguous covered years in the denominator. Apply the same denominator to a
   predominant cycle count. No preference is given to 2025.
9. **Describe annual variation.** Preserve counts of single-cycle and two-cycle
   candidate years and a separate cycle-history explanation. Variation across
   candidate years is not verified management history or a new public class.
10. **Test without promoting.** Recalculate two alternating-date thinning probes
    for every parcel-season. Report all state changes, resolved disagreements,
    and transitions into/out of uncertainty separately. All outputs remain
    `accepted=false` and `training_eligible=false` where parcel labels are stored.

The March–November coverage window remains an explicit limitation of this
experiment. Year boundaries and winter growth are not reconstructed. Removing
old binary fractions changes the available evidence, so class-count differences
cannot all be attributed to the event-splitting correction alone.

## Execution and preservation

Run only from `WORKING_PRODUCT` with its own environment:

```powershell
.\.venv\Scripts\python.exe -B run_phenology_events.py --workers auto
.\.venv\Scripts\python.exe -B verify_phenology_events.py
```

The runner reads 87,360 cached observation rows once. It benchmarks representative
parcels in serial and parallel, requires identical results, then chooses the
faster execution path. It caps workers according to available CPU/memory and
uses one numerical thread per worker to avoid oversubscription. Worker count
does not alter numerical results. Timing is measured and written into the report.

Output: `data/analysis/observation_screening/events_20260906_v1/`:

- `seasons.parquet`, `parcels.parquet`, `events.parquet`, `probes.parquet`:
  newly calculated results and evidence;
- `comparison_seasons.parquet`, `comparison_parcels.parquet`, `focus18.parquet`:
  subsequent private comparisons with v3;
- `manifest.json`, `source/`, `report.json`, `complete.json`: explicit policy,
  input/code hashes, copied source, timing, results and completion hashes.

An operating-system lock prevents duplicate writers. A completed version is
verified without rewriting. Changed inputs/code cannot resume into that version.
An interrupted incomplete calculation can rerun the same small control with the
same pins; it never overwrites a prior completed experiment. The collector,
cached imagery, releases, UI, public API, ports and cancelled monitoring
automation are outside this runner. Weather is not read by this experiment.

## Evaluation and research basis

Independent synthetic tests cover short-fragment separation, valid single/two
cycles, rapid regrowth, gaps, spatial mismatch, missing evidence, year voting,
input immutability and attempted contamination by old labels/features. An
additional verifier checks saved identities, scope, hashes, version preservation,
majority arithmetic and deterministic replay against actual cached inputs.

The rules draw on the distinction between growth cycles, exposed-soil intervals
and mowing/regrowth discussed in:

- [Huang et al. (2024), bare-soil occurrence and cropping intensity](https://doi.org/10.1016/j.compag.2024.109025).
- [De Vroey et al. (2022), mowing detection with Sentinel-1/2](https://doi.org/10.1016/j.rse.2022.113145).
- [Chen et al. (2018), mixed-pixel effects on phenology](https://doi.org/10.1016/j.rse.2018.04.030).

These sources do not validate our thresholds or establish local accuracy.
Technical tests, temporal consistency and changes in class totals are not an
independent accuracy assessment. The original 18 changed seasons provide a
diagnostic focus; their former classes are not assumed correct. Dated independent
reference review is still required before claims of improved accuracy, expansion
to 22,802 eligible parcels or publication.
