from __future__ import annotations

import json
import mimetypes
import os
import re
import sqlite3
from functools import partial
from functools import lru_cache
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit


PRODUCT_ROOT = Path(__file__).resolve().parent
ANALYTICS_PROFILE = os.getenv("LAND_ANALYTICS_PROFILE", "v2")
DIST_ROOT = PRODUCT_ROOT / os.getenv("WORKING_PRODUCT_DIST", "dist")
CADASTRE_INDEX = PRODUCT_ROOT / "server_data" / "cadastre_search.sqlite3"
if ANALYTICS_PROFILE == "use_type_v2":
    LAND_ANALYTICS_SUMMARY = (
        PRODUCT_ROOT / "server_data" / "land_analytics_summary_v3_2026_09_05.json"
    )
    LAND_ANALYTICS_INDEX = (
        PRODUCT_ROOT / "server_data" / "land_analytics_v3_2026_09_05.sqlite3"
    )
else:
    LAND_ANALYTICS_SUMMARY = PRODUCT_ROOT / "server_data" / "land_analytics_summary.json"
    LAND_ANALYTICS_INDEX = (
        PRODUCT_ROOT / "server_data" / "land_analytics_v2_2026_09_05.sqlite3"
    )
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8520
CADASTRE_CODE_RE = re.compile(r"^\d{2}-\d{3}-\d{4}-\d{4}$")

mimetypes.add_type("application/javascript", ".js")
mimetypes.add_type("application/geo+json", ".geojson")
mimetypes.add_type("application/vnd.mapbox-vector-tile", ".pbf")


class ProductRequestHandler(SimpleHTTPRequestHandler):
    server_version = "WaterLandProduct/1.0"

    def do_GET(self) -> None:
        request = urlsplit(self.path)
        request_path = request.path
        if request_path == "/health":
            self._send_json(
                {
                    "status": "ready",
                    "application": "water_land_resources_working_product",
                }
            )
            return

        if request_path == "/api/cadastre/search":
            self._search_cadastre(parse_qs(request.query))
            return

        if request_path == "/api/land/activity-2026":
            self._land_activity_2026()
            return

        if request_path == "/api/land/history-2021-2025":
            self._land_history_2021_2025()
            return

        if request_path == "/api/land/use-type":
            self._land_use_type()
            return

        if request_path == "/api/land/delivery":
            self._land_delivery()
            return

        if request_path == "/api/land/parcel":
            self._land_parcel(parse_qs(request.query))
            return

        requested = (DIST_ROOT / request_path.lstrip("/")).resolve()
        if DIST_ROOT.resolve() not in requested.parents and requested != DIST_ROOT.resolve():
            self.send_error(403)
            return

        if request_path != "/" and not requested.exists() and "." not in Path(request_path).name:
            self.path = "/index.html"
        super().do_GET()

    def end_headers(self) -> None:
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "strict-origin-when-cross-origin")
        request_path = urlsplit(self.path).path
        if request_path in {
            "/api/land/delivery",
            "/api/land/activity-2026",
            "/api/land/history-2021-2025",
            "/api/land/use-type",
        }:
            self.send_header("Cache-Control", "public, max-age=300")
        elif request_path.startswith("/api/"):
            self.send_header("Cache-Control", "no-store")
        elif request_path.startswith(
            ("/assets/", "/data/cadastre/", "/data/land_analytics/")
        ):
            self.send_header("Cache-Control", "public, max-age=31536000, immutable")
        else:
            self.send_header("Cache-Control", "no-cache")
        super().end_headers()

    def _search_cadastre(self, query: dict[str, list[str]]) -> None:
        code = query.get("code", [""])[0].strip()
        if not CADASTRE_CODE_RE.fullmatch(code):
            self._send_json({"error": "invalid_cadastre_code"}, status=400)
            return
        if not CADASTRE_INDEX.exists():
            self._send_json({"error": "cadastre_index_unavailable"}, status=503)
            return

        with sqlite3.connect(CADASTRE_INDEX, timeout=5.0) as connection:
            row = connection.execute(
                """
                SELECT cadastre_code, area_ha, minx, miny, maxx, maxy,
                       center_x, center_y
                FROM parcels
                WHERE cadastre_code = ?
                """,
                (code,),
            ).fetchone()
        if row is None:
            self._send_json({"error": "cadastre_code_not_found"}, status=404)
            return

        self._send_json(
            {
                "cadastre_code": row[0],
                "area_ha": row[1],
                "bbox": [row[2], row[3], row[4], row[5]],
                "center": [row[6], row[7]],
            }
        )

    def _land_activity_2026(self) -> None:
        try:
            payload = _load_land_analytics_summary()["activity"]
        except (OSError, KeyError, json.JSONDecodeError):
            self._send_json({"error": "activity_preview_invalid"}, status=503)
            return
        self._send_json(payload)

    def _land_history_2021_2025(self) -> None:
        try:
            payload = _load_land_analytics_summary()["history"]
        except (OSError, KeyError, json.JSONDecodeError):
            self._send_json({"error": "land_history_preview_invalid"}, status=503)
            return
        self._send_json(payload)

    def _land_use_type(self) -> None:
        try:
            summary = _load_land_analytics_summary()
            payload = dict(summary["land_use_type"])
            if summary.get("annual_cycles"):
                payload["annual_cycles"] = summary["annual_cycles"]
        except (OSError, KeyError, json.JSONDecodeError):
            self._send_json({"error": "land_use_type_invalid"}, status=503)
            return
        self._send_json(payload)

    def _land_delivery(self) -> None:
        try:
            payload = _load_land_analytics_summary()
        except (OSError, KeyError, json.JSONDecodeError):
            self._send_json({"error": "land_analytics_delivery_unavailable"}, status=503)
            return
        self._send_json(payload)

    def _land_parcel(self, query: dict[str, list[str]]) -> None:
        code = query.get("code", [""])[0].strip()
        if not CADASTRE_CODE_RE.fullmatch(code):
            self._send_json({"error": "invalid_cadastre_code"}, status=400)
            return
        if not LAND_ANALYTICS_INDEX.exists():
            self._send_json({"error": "land_analytics_index_unavailable"}, status=503)
            return

        with sqlite3.connect(LAND_ANALYTICS_INDEX, timeout=5.0) as connection:
            if ANALYTICS_PROFILE == "use_type_v2":
                query = """
                    SELECT activity_stage, activity_fraction, observed_active_area_ha,
                           activity_state, household, activity_class, road_excluded,
                           history_class, annual_state_codes, profile_year_count,
                           used_year_count, crop_type, crop_profile_year_count,
                           crop_type_year_codes, annual_cycle,
                           annual_cycle_year_codes, cycle_assessed_year_count
                    FROM parcel_analytics
                    WHERE cadastre_code = ?
                """
            else:
                query = """
                    SELECT activity_stage, activity_fraction, observed_active_area_ha,
                           activity_state, household, activity_class, road_excluded,
                           history_class, annual_state_codes, profile_year_count,
                           used_year_count, crop_type, crop_profile_year_count
                    FROM parcel_analytics
                    WHERE cadastre_code = ?
                """
            row = connection.execute(query, (code,)).fetchone()
        if row is None:
            self._send_json({"error": "land_analytics_not_available"}, status=404)
            return

        history = None
        if row[7]:
            history = {
                "historyClass": row[7],
                "annualStateCodes": json.loads(row[8]),
                "profileYearCount": int(row[9]),
                "usedYearCount": int(row[10]),
            }
        crop_type = None
        if row[11]:
            crop_type = {
                "cropType": row[11],
                "profileYearCount": int(row[12]),
            }
            if ANALYTICS_PROFILE == "use_type_v2":
                crop_type.update(
                    {
                        "yearTypeCodes": json.loads(row[13]) if row[13] else [],
                        "annualCycle": row[14],
                        "annualCycleYearCodes": json.loads(row[15]) if row[15] else [],
                        "cycleAssessedYearCount": int(row[16]) if row[16] is not None else 0,
                    }
                )
        self._send_json(
            {
                "cadastreCode": code,
                "activity": {
                    "stage": row[0],
                    "fraction": float(row[1]),
                    "areaHa": float(row[2]),
                    "previewState": row[3],
                    "household": bool(row[4]),
                    "activityClass": row[5],
                    "roadExcluded": bool(row[6]),
                },
                "history": history,
                "cropType": crop_type,
            }
        )

    def _send_json(self, payload: dict, status: int = 200) -> None:
        encoded = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)


class ProductServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


@lru_cache(maxsize=1)
def _load_land_analytics_summary() -> dict:
    if not LAND_ANALYTICS_SUMMARY.exists():
        raise OSError("Land analytics summary is unavailable")
    return json.loads(LAND_ANALYTICS_SUMMARY.read_text(encoding="utf-8"))


def main() -> None:
    if not (DIST_ROOT / "index.html").exists():
        raise SystemExit("Build is missing. Run the frontend build before starting app.py.")

    host = os.getenv("WORKING_PRODUCT_HOST", DEFAULT_HOST)
    port = int(os.getenv("WORKING_PRODUCT_PORT", str(DEFAULT_PORT)))
    handler = partial(ProductRequestHandler, directory=str(DIST_ROOT))
    server = ProductServer((host, port), handler)
    print(f"Working product: http://{host}:{port}/", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
