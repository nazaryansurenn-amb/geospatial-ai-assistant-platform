# Sentinel-1 pilot and irrigation-frequency hypothesis

6 September 2026. Additional owner observation: the target lands are usually
irrigated more often. This is a hypothesis to investigate, not a label for all
parcels in the named communities. No new classification or map change is made.

## Live catalogue check

A public Copernicus STAC metadata query covered the bounding box of the existing
22,802 eligible parcels and the completed calendar year 2025. All returned pages
were retrieved: 137 products, 92 distinct calendar dates, three relative orbits.
No radar image was downloaded. Saved request, response and summaries:
`server_data/review/rapid_water_loss_20260906_v1/sentinel1_catalogue_20260906T083546Z/`.

| Relative orbit | Direction | Products in 2025 | Full study bounding-box coverage | April–September acquisitions | Gaps within that period |
| --- | --- | --- | --- | --- | --- |
| 72 | Ascending | 52 | All 52 footprints | 29 | 26 six-day gaps; two twelve-day gaps |
| 152 | Descending | 54 | All 54 footprints | 31 | 30 six-day gaps |
| 174 | Ascending | 31 | Partial coverage | 15 | Twelve-day gaps |

Full-cover tracks include Sentinel-1A and Sentinel-1C. Inter-platform consistency
must be checked before treating them as one time series. Different viewing
directions and relative orbits remain separate retrievals; their samples cannot
simply be concatenated into a backscatter drying curve. The combined timing
summary is an acquisition opportunity count, not a calibrated moisture series.
Catalogue footprints do not guarantee usable pixels for every small parcel.

The 60 full-cover acquisitions are mostly paired morning/evening on the same
day: 29 combined gaps are about 12 hours, 28 about 132 hours (5.5 days), and two
about 144 hours. Combining those tracks does **not** provide regular three-day
coverage. This limits true irrigation-event counting even after inter-track
calibration. The paired observations may help test short surface responses,
but their different viewing geometry prevents direct raw-backscatter comparison.

## Bounded acquisition plan

1. Use 2025 April–September to test the main irrigation period, explicitly not
   the full annual growing-season demand. Begin with eligible parcel interiors
   in Ակնալիճ and Արշալույս, plus comparison parcels in other communities.
2. Obtain clipped VV/VH radar data and required quality/geometry information for
   the two fully covering tracks: at most 60 acquisitions for this first period.
   Prefer a provider-processed subset if suitable account access and quota are
   available. Otherwise assess selected-product download and local processing.
   Download volume and processing time require an actual subset/access check.
3. Apply consistent calibration, thermal-noise handling and terrain/geometric
   correction. Keep per-acquisition metadata, orbit, platform and processing
   options; never use a multi-date display mosaic as an irrigation observation.
4. Store these in a new versioned radar dataset with resumable acquisition jobs,
   input/output hashes and a manifest. Reuse cached Sentinel-2 and available
   weather. Leave the ERA5 collector and previous outputs unchanged.
5. Verify local access before launching jobs. The ERA5 CDS credentials are for
   a different service and must not be assumed to grant CDSE/Sentinel Hub access.
   Any required sign-in takes place locally; no credentials belong in chat.

Copernicus documents public catalogue querying and authenticated data services:
[STAC](https://documentation.dataspace.copernicus.eu/APIs/STAC.html),
[Sentinel-1 processing](https://documentation.dataspace.copernicus.eu/APIs/SentinelHub/Data/S1GRD.html),
[data download](https://dataspace.copernicus.eu/ecosystem/services/data-download).

## Incorporating more frequent irrigation

The proposed supporting signal is unusually frequent *observed wetting*
associated with repeated rapid drying, after comparing equivalent weather,
vegetation development, irrigation method where known, and observation coverage.
Event frequency alone is insufficient. Longer growing periods, management,
shallow rooting, irrigation method and water availability also affect it.

Compare within observed active periods, not raw annual event totals. Do not
shorten a growing period because the owner display rule assigns uncertain
annual cycles to single cycle. Compare parcels using the same usable radar
opportunities and account for event detectability. Dividing detections by days
does not fully correct missed-event bias.

Some fast-drying soils may be irrigated and become dry again between two radar
visits. Such fields can show *fewer* detected events despite more actual events.
Keep recorded wetting detections separate from estimated true event frequency.
An unobserved event is neither zero irrigation nor evidence of low demand.

FAO supports the link between low storage and frequent small applications;
it does not imply that every application should contain more water.
[FAO irrigation-method guidance](https://www.fao.org/4/s8684e/s8684e08.htm).
Radar-event research identifies shallow sensing and revisit gaps as important
limitations. [Bazzi et al., full paper](https://horizon.documentation.ird.fr/exl-doc/pleins_textes/2023-01/010086494.pdf).

The pilot must first establish whether observations resolve wetting and drying
often enough. If they do, test the combined frequency/drying hypothesis privately.
If they do not, use the result to identify where a small set of dated irrigation
records or root-zone measurements is needed. Do not fabricate event counts,
per-event volume or above-normative water demand.
