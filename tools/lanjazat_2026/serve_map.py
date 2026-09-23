"""Loopback-only viewer with an explicit public-file allowlist."""
import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
from urllib.parse import urlsplit

HERE = Path(__file__).resolve().parent
WP = HERE.parents[1]
DATA = HERE / "output" / "map_data"
VENDOR = WP / "node_modules" / "maplibre-gl" / "dist"
ROUTES = {
    "/": (HERE / "map" / "index.html", "text/html; charset=utf-8"),
    "/app.js": (HERE / "map" / "app.js", "text/javascript; charset=utf-8"),
    "/style.css": (HERE / "map" / "style.css", "text/css; charset=utf-8"),
    "/vendor/maplibre.js": (VENDOR / "maplibre-gl.js", "text/javascript"),
    "/vendor/maplibre.css": (VENDOR / "maplibre-gl.css", "text/css"),
}
for name in ["summary.json", "boundary.geojson", "active.geojson", "irrigation.geojson",
             "vegetation.geojson", "recent.geojson", "context.geojson"]:
    ROUTES["/data/" + name] = (DATA / name, "application/json; charset=utf-8")


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        path = urlsplit(self.path).path
        if path == "/health":
            self.respond(json.dumps({"status": "ready", "viewer": "lanjazat-2026"}).encode(), "application/json")
            return
        if path not in ROUTES:
            self.send_error(404)
            return
        source, content_type = ROUTES[path]
        self.respond(source.read_bytes(), content_type)

    def respond(self, body, content_type):
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "strict-origin-when-cross-origin")
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; "
                         "style-src 'self' 'unsafe-inline'; img-src 'self' data: blob: https://server.arcgisonline.com; "
                         "connect-src 'self' https://server.arcgisonline.com; worker-src blob:; "
                         "object-src 'none'; base-uri 'none'; frame-ancestors 'none'")
        self.end_headers()
        self.wfile.write(body)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8531)
    args = parser.parse_args()
    assert all(p.is_file() for p, _ in ROUTES.values()), "Build map data before starting viewer"
    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    print(f"Lanjazat map: http://127.0.0.1:{args.port}/", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
