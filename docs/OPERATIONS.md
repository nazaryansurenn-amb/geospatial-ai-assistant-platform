# Water and Land Resources Working Product

The application, selected analytical inputs, preparation utilities, tests and
dependency lock are local to this folder. Legacy directories are not runtime
or preparation dependencies. This is technical isolation, not approval of the
current use-type classification or a change to analytical methods.

## Preserved releases

`config/releases.json` selects complete, SHA-256-checked copies of the existing
server, frontend, tiles and SQLite databases. `working` preserves the 8520
baseline; `use_type_review` preserves the separate 8521 review. Their original
files and ports have not been replaced. Rejected use-type results remain
preserved pending a separate analytical decision.

```powershell
python run_product.py --release working --verify-only
python run_product.py --release working --port 8522
```

The launcher refuses ports 8520/8521 and never stops another process. If the
chosen port is occupied, choose another free port. It ignores ambient analytical
profile settings and serves only the selected release's public dist directory.
Use a fresh URL query when reusing a port previously used by another local app:
http://127.0.0.1:8522/?technical=standalone-20260905

Top-level app.py, dist and dist_use_type_v2 are untouched historical launch paths.
Use run_product.py for verified isolated operation going forward. Never manually
edit releases or combine files from different releases.

## Independent analytical environment

Python 3.12 is required for the pinned analytical packages; the viewing server
uses the standard library only. Recreate the environment after relocating this
folder; virtual environments themselves are not portable.

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install --only-binary=:all: -r requirements-analytics.lock.txt
.\.venv\Scripts\python.exe -m pip check
.\.venv\Scripts\python.exe -B -m pytest -q
.\.venv\Scripts\python.exe -I -B scripts/check_standalone.py
```

The last command denies legacy-project file access and checks 12 existing
parcels against saved seasonal features. It does not download imagery,
recalculate the whole EO dataset, or write analytical results.

## Frontend preparation

Use Node.js and pnpm install --frozen-lockfile in this folder. Build into a new
directory, never into a preserved release. Existing output directories are
rejected before Vite can overwrite them.

```powershell
$env:WORKING_PRODUCT_DIST = 'output/my_new_frontend'
$env:LAND_ANALYTICS_DELIVERY_SLUG = 'v2_2026_09_05'
pnpm run build
```

Only the explicit public asset allowlist in vite.config.js enters a frontend
build. Internal inputs, reference documents and review material do not.
The development port is 8524; it is not a replacement for the Python API server.

## Analytical inputs and outputs

- data/source/cadastre: byte-identical input; geometry, codes and areas unchanged.
- data/source/activity_2026: preserved 10 m activity cache and prior context flags.
- data/source/scene_manifests: selected historical scene records.
- data/source/halo_inputs: internal reconstruction input, not public delivery.
- data/analysis/parcel_eo/current_halo_v1: current immutable parcel universe.
- data/analysis/parcel_eo/v5_full_halo_2021_2025: retained observations and features.
- wp_core: local numerical functions and narrowly extracted EO access helpers.
- server_data/review: unapproved drafts, no automatic promotion.

No analytical script is scheduled or called automatically by the viewing server.
Re-running analyses requires an explicit task. The delivery builder requires a
new LAND_ANALYTICS_DELIVERY_SLUG and rejects reuse of any existing tile directory,
summary or parcel database.

## Boundaries and archives

config/component_catalog.json records active, review-only and archived pieces.
config/isolation_provenance.json records input copies and their hashes.
The pre-migration code backup and eight unused intermediate datasets remain in
the original project's LOCAL_ARCHIVE, outside product dependencies.

Esri imagery and new Copernicus downloads still require their external services.
AI was not connected as part of isolation. Changing classification methods,
public labels, approved layers or the working port needs a separate decision.
