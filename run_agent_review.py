"""Add the local owner assistant to the sealed five-tab hectare review."""
import os
os.environ['LAND_ANALYTICS_PROFILE']='use_type_v2'
os.environ['WORKING_PRODUCT_HOST']='127.0.0.1'
from pathlib import Path
import hashlib
import json
import app
from wp_core.agent_service import AgentService, install_handler

ROOT=Path(__file__).resolve().parent
SLUG='agent_review_20260906_v1'

def main():
    lock=ROOT/'config'/f'{SLUG}.review.lock.json'
    if lock.exists():
        for name,r in json.loads(lock.read_text())['files'].items():
            assert hashlib.sha256((ROOT/name).read_bytes()).hexdigest()==r['sha256'],name
    elif '--port' not in __import__('sys').argv or __import__('sys').argv[__import__('sys').argv.index('--port')+1]!='8527':
        raise SystemExit('Unsealed agent build may run only on preview port 8527.')
    import run_hectares_review
    serve=app.main
    def with_agent():
        app.ProductRequestHandler=install_handler(app.ProductRequestHandler,AgentService())
        app.DIST_ROOT=ROOT/'output'/('frontend_'+SLUG)
        serve()
    app.main=with_agent
    run_hectares_review.main()

if __name__=='__main__':main()
