"""Additive possible-degradation review, preserving prior sections and releases."""
import os
os.environ['LAND_ANALYTICS_PROFILE']='use_type_v2'
os.environ['WORKING_PRODUCT_HOST']='127.0.0.1'
from pathlib import Path
import hashlib,json,sys
from urllib.parse import urlsplit,parse_qs
import app
from wp_core.agent_service_v6 import AgentService,install_handler
from wp_core import agent_service_v6
from wp_core.activity_change_history_agent_v6 import install
ROOT=Path(__file__).resolve().parent
SLUG='agent_review_20260906_v6'

def main():
    names=[f'agent_review_20260906_v{version}' for version in range(1,7)]+['activity_change_history_review_20260906_v1']
    for name in names:
        lock=ROOT/'config'/f'{name}.review.lock.json'
        if lock.exists():
            for path,record in json.loads(lock.read_text())['files'].items():
                assert hashlib.sha256((ROOT/path).read_bytes()).hexdigest()==record['sha256'],path
        elif '--port' not in sys.argv or sys.argv[sys.argv.index('--port')+1]!='8527':
            raise SystemExit('Unsealed build may run only on preview port 8527.')
    directory=ROOT/'server_data/agent_v6'
    summary=json.loads((directory/'degradation.json').read_text(encoding='utf-8'))
    lookup=json.loads((directory/'degradation_lookup.json').read_text())
    history=ROOT/'server_data/review/activity_change_history_review_20260906_v1'
    change_summary=json.loads((history/'summary.json').read_text())
    change_lookup=json.loads((history/'parcel_lookup.json').read_text())
    install(agent_service_v6,change_summary,change_lookup)
    import run_hectares_review
    serve=app.main
    def with_agent():
        class Handler(app.ProductRequestHandler):
            def do_GET(self):
                if urlsplit(self.path).path=='/api/land/degradation':
                    self._send_json(summary);return
                if urlsplit(self.path).path=='/api/land/activity-change':
                    self._send_json(change_summary);return
                super().do_GET()
            def _send_json(self,payload,status=200):
                request=urlsplit(self.path)
                if request.path=='/api/land/parcel' and status==200 and 'error' not in payload:
                    code=parse_qs(request.query).get('code',[''])[0].strip()
                    payload=dict(payload);payload['degradation']=lookup.get(code)
                    payload['activityChange']=change_lookup.get(code)
                super()._send_json(payload,status=status)
        app.ProductRequestHandler=install_handler(Handler,AgentService())
        app.DIST_ROOT=ROOT/'output'/f'frontend_{SLUG}'
        serve()
    app.main=with_agent
    run_hectares_review.main()

if __name__=='__main__':main()
