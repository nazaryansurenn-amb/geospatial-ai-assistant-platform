"""Initialize the inherited analytical profile before importing the review server."""
import os
import hashlib
import json
from pathlib import Path
os.environ['LAND_ANALYTICS_PROFILE'] = 'use_type_v2'
from run_hectares_review import main

if __name__ == '__main__':
    root = Path(__file__).resolve().parent
    lock = json.loads((root / 'config/hectares_review_20260906_v2.review.lock.json').read_text())
    for name, record in lock['files'].items():
        assert hashlib.sha256((root / name).read_bytes()).hexdigest() == record['sha256'], name
    main()
