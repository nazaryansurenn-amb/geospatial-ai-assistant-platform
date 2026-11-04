"""Additive AlphaEarth groups tab on top of the live eight-section product. Unsealed preview: port 8527 only."""
import os
# app.py reads the analytics profile at import time; set it exactly as the live launcher does, first.
os.environ['LAND_ANALYTICS_PROFILE'] = 'use_type_v2'
os.environ['WORKING_PRODUCT_HOST'] = '127.0.0.1'
import json
import sys
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import app
import run_irrigation_stress_review as base

ROOT = Path(__file__).resolve().parent
SLUG = 'alphaearth_review_20261030_v1'


def main():
    if not (ROOT / 'config' / f'{SLUG}.review.lock.json').exists():
        assert '--port' in sys.argv and sys.argv[sys.argv.index('--port') + 1] == '8527', 'Unsealed AlphaEarth preview is 8527 only'
    review = ROOT / 'server_data/review' / SLUG
    summary = json.loads((review / 'alphaearth.json').read_text(encoding='utf-8'))
    lookup = json.loads((review / 'alphaearth_lookup.json').read_text())

    def with_alphaearth():
        class Handler(app.ProductRequestHandler):
            def do_GET(self):
                if urlsplit(self.path).path == '/api/land/alphaearth':
                    self._send_json(summary)
                    return
                super().do_GET()

            def _send_json(self, payload, status=200):
                request = urlsplit(self.path)
                if request.path == '/api/land/parcel' and status == 200 and 'error' not in payload:
                    code = parse_qs(request.query).get('code', [''])[0].strip()
                    payload = {**payload, 'alphaEarth': lookup.get(code)}
                super()._send_json(payload, status=status)
        app.ProductRequestHandler = Handler
        app.DIST_ROOT = ROOT / 'output' / f'frontend_{SLUG}'
        serve()
    serve = app.main
    app.main = with_alphaearth
    base.main()


if __name__ == '__main__':
    main()
