# Available-weather preliminary comparison

Owner authorization: use weather already downloaded and go ahead, 6 September
2026. This is a separate private version, `sentinel1_peer_partial_20260906_v1`.
It reuses the frozen matching and sensitivity rules described in
`SENTINEL1_PEER_COMPARISON_EN.md` without changing their thresholds or the
prepared full-season run. No map or classification changes are included.

At launch, snapshot only completed, checksum-verified March–September 2025
weather batches. March supplies predecessor hours where available. Filter the
existing 96-parcel pilot radar table to acquisition months present in that
snapshot. Never substitute 2021–2024 weather for missing 2025 dates. New weather
arriving later cannot silently change this sealed preliminary result.

Matching remains separate by radar track and platform, with up to 14-day gaps,
the same geometry/area/weather-cell constraints, optical support and peer rules.
Hourly precipitation remains gap-safe: unknown or negative increments are not
zero rain. Any comparison window lacking a complete precipitation total cannot
enter the low-rainfall signal counts. Observed changes and matched peer medians
are retained privately even when rainfall prevents irrigation interpretation.

Short coverage may not contain the six comparable triplets required for a
repetition assessment. Such assessments stay null. All final high-water-demand
fields stay null; no parcel water volume, irrigation frequency or root-zone
drainage is established. The output reports matched comparisons before the
weather screen, low-rainfall comparisons and whether repetition is assessable,
so insufficient evidence cannot be presented as a zero-candidate finding.

From the independent product directory, using its environment:

```powershell
.\.venv\Scripts\python.exe -X utf8 -B run_sentinel1_peer_partial.py run
.\.venv\Scripts\python.exe -X utf8 -B verify_sentinel1_peer_partial.py
```

Private output: `data/analysis/rapid_water_loss/sentinel1_peer_partial_20260906_v1/`.
The run snapshots input hashes and sources, uses an OS lock, preserves dated
attempts, seals completed output and verifies/reuses it on repeat. The checker
independently recalculates radar changes, peer medians and rainfall totals from
raw accumulations and checks coverage and unchanged parcel identities.

The existing weather collector continues independently. The five-minute
full-season follow-up and its prepared version remain available for the later
March–September run. The cancelled 15-minute EO/weather monitor stays paused.
