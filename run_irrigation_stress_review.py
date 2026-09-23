"""Versioned additive current-stress UI. Preserves the accepted area50 results."""
import os
os.environ['LAND_ANALYTICS_PROFILE']='use_type_v2'
os.environ['WORKING_PRODUCT_HOST']='127.0.0.1'
from pathlib import Path
import hashlib,json,sys
from urllib.parse import urlsplit,parse_qs
import app
from wp_core.agent_service_v7 import AgentService,install_handler
from wp_core import agent_service_v7
from wp_core.activity_change_area50_agent_v7 import install
ROOT=Path(__file__).resolve().parent
SLUG='irrigation_stress_review_20260907_v1'

def main():
    names=[f'agent_review_20260906_v{n}' for n in range(1,7)]+['activity_change_history_review_20260906_v1','activity_change_area50_20260907_v1','readability_20260907_v1',SLUG]
    for name in names:
        lock=ROOT/'config'/f'{name}.review.lock.json'
        if lock.exists():
            for path,r in json.loads(lock.read_text())['files'].items():assert hashlib.sha256((ROOT/path).read_bytes()).hexdigest()==r['sha256'],path
        else:assert name==SLUG and '--port' in sys.argv and sys.argv[sys.argv.index('--port')+1]=='8527','Unsealed stress preview is 8527 only'
    old=ROOT/'server_data/agent_v6';directory=ROOT/'server_data/agent_v7';change=ROOT/'server_data/review/activity_change_area50_20260907_v1'
    degradation=json.loads((old/'degradation.json').read_text());degradation_lookup=json.loads((old/'degradation_lookup.json').read_text())
    change_summary=json.loads((change/'summary.json').read_text());change_lookup=json.loads((change/'parcel_lookup.json').read_text())
    stress=json.loads((directory/'irrigation_stress.json').read_text());lookup=json.loads((directory/'irrigation_stress_lookup.json').read_text())
    install(agent_service_v7,change_summary,change_lookup)
    import run_hectares_review
    serve=app.main
    def with_agent():
        class Handler(app.ProductRequestHandler):
            def do_GET(self):
                payload={'/api/land/degradation':degradation,'/api/land/activity-change':change_summary,'/api/land/irrigation-stress':stress}.get(urlsplit(self.path).path)
                if payload is not None:self._send_json(payload);return
                super().do_GET()
            def _send_json(self,payload,status=200):
                request=urlsplit(self.path)
                if request.path=='/api/land/parcel' and status==200 and 'error' not in payload:
                    code=parse_qs(request.query).get('code',[''])[0].strip()
                    payload={**payload,'degradation':degradation_lookup.get(code),'activityChange':change_lookup.get(code),'irrigationStress':lookup.get(code)}
                super()._send_json(payload,status=status)
        app.ProductRequestHandler=install_handler(Handler,AgentService())
        app.DIST_ROOT=ROOT/'output'/f'frontend_{SLUG}'
        serve()
    app.main=with_agent
    run_hectares_review.main()

if __name__=='__main__':main()
