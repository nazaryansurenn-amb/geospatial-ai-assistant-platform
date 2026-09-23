"""Serve the current five-tab product with additive official hectare displays."""
from pathlib import Path
import hashlib
import json
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parent
SLUG = 'hectares_review_20260906_v1'


def main():
    for name, record in json.loads((ROOT / 'config' / f'{SLUG}.review.lock.json').read_text())['files'].items():
        path = (ROOT / name).resolve()
        assert ROOT in path.parents
        assert path.stat().st_size == record['bytes'] and hashlib.sha256(path.read_bytes()).hexdigest() == record['sha256'], name
    import app
    import run_consolidation_review as base
    from wp_core.review_hectares import attach_hectares
    base.BASE_SLUG = base.SLUG
    base.SLUG = 'consolidation_review_20260906_v2'
    areas = json.loads((ROOT / 'server_data/review' / SLUG / 'official_areas.json').read_text())
    serve = app.main

    def with_hectares():
        base_handler = app.ProductRequestHandler
        class Handler(base_handler):
            def _send_json(self, payload, status=200):
                if status == 200:
                    payload = attach_hectares(payload, urlsplit(self.path).path, areas)
                super()._send_json(payload, status=status)
        app.ProductRequestHandler = Handler
        app.DIST_ROOT = ROOT / 'output' / f'frontend_{SLUG}'
        serve()
    app.main = with_hectares
    base.main()


if __name__ == '__main__':
    main()
