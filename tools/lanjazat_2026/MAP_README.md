# Lanjazat map

Separate local viewer: http://127.0.0.1:8531/

This viewer renders the saved `lanjazat_admin_2026_screen_v2` result, not a new
classification. No cadastral parcels, buffers, new satellite requests or
modifications to port 8526 are involved. The extent is OSM administrative
relation 15030684. Observations span 11 March to 2 September 2026.

## Local commands

From the project root, using the existing GIS Python environment:

```powershell
.\.venv\Scripts\python.exe -X utf8 -B WORKING_PRODUCT/tools/lanjazat_2026/build_map.py
.\.venv\Scripts\python.exe -X utf8 -B -m unittest discover -s WORKING_PRODUCT/tools/lanjazat_2026 -p 'test*.py' -v
.\.venv\Scripts\python.exe -X utf8 -B WORKING_PRODUCT/tools/lanjazat_2026/serve_map.py --port 8531
```

`build_map.py` uses only saved local arrays and context. It validates source
checksums and compares each exported layer's projected area to the saved total.
`output/map_verification.json` records the result and export checksums.
The actual map GeoJSON is valid WGS84; measurement uses UTM zone 38N.
Shapes retain the 20 m analytical support, clipped to the administrative area
and, where appropriate, the mapped agricultural context. They are not field
boundaries. No smoothing or parcel-level conclusions are added.

The server binds only to loopback. Its fixed allowlist serves the viewer,
MapLibre assets and sanitized `output/map_data` files. It does not serve raw
observations, weather series, analysis thresholds or arbitrary workspace files.
This map is not connected to the public product tunnel.

## Interpretation

- Green: repeated vegetation within mapped agricultural land, about 167 ha.
- Blue: irrigation-compatible agricultural growth, about 145 ha; actual
  irrigation is not confirmed.
- Yellow: all repeated vegetation, about 594 ha, including natural vegetation.
- Orange: recent observed vegetation, about 361 ha.
- Grey: mapped agricultural context, about 241 ha; it may be incomplete or old.

These categories overlap and must not be added. Blank areas do not necessarily
indicate inactivity. Esri background imagery may have a different acquisition
date from the analyzed Sentinel-2 scenes. The screening has not been field
validated; reported hectares are not official cadastral measurements.
