"""Copy the preserved hectare UI into a new additive agent review."""
from pathlib import Path
import hashlib
import json
import shutil

ROOT = Path(__file__).resolve().parents[1]
BASE = 'hectares_review_20260906_v1'
SLUG = 'agent_review_20260906_v1'
source = ROOT / 'server_data/review' / SLUG / 'frontend_source'
dest = ROOT / 'output' / ('frontend_' + SLUG)
if __name__ == '__main__':
    assert not source.exists() and not dest.exists(), 'Preserve existing preparation'
    for name, record in json.loads((ROOT/'config/hectares_review_20260906_v2.review.lock.json').read_text())['files'].items():
        assert hashlib.sha256((ROOT/name).read_bytes()).hexdigest() == record['sha256'], name
    shutil.copytree(ROOT/'server_data/review'/BASE/'frontend_source', source)
    shutil.copytree(ROOT/'output'/('frontend_'+BASE)/'data', dest/'data')
    config=source/'vite.config.mjs'
    config.write_text(config.read_text().replace(BASE, SLUG), encoding='utf-8')
    print('Prepared new agent frontend; existing UI and data preserved.')
