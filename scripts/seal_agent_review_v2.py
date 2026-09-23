from pathlib import Path
import hashlib
import json
import sys
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from wp_core.agent_service_v2 import connection_enabled,credentials

if __name__=='__main__':
    slug='agent_review_20260906_v2';lock=ROOT/'config'/f'{slug}.review.lock.json'
    assert not lock.exists(),'Preserve the existing seal'
    assert connection_enabled()
    for report in ['server_data/agent_v1/live_model_verification_v2.json','server_data/agent_v1/http_verification.json','output/playwright/agent_review_20260906_v2/verification.json']:
        assert json.loads((ROOT/report).read_text(encoding='utf-8'))['passed'],report
    old=json.loads((ROOT/'config/agent_review_20260906_v1.review.lock.json').read_text())
    for name,r in old['files'].items():assert hashlib.sha256((ROOT/name).read_bytes()).hexdigest()==r['sha256'],name
    frontend=ROOT/'output'/('frontend_'+slug);source=ROOT/'server_data/review'/slug/'frontend_source'
    for p in (ROOT/'output/frontend_hectares_review_20260906_v1/data').rglob('*'):
        if p.is_file():assert p.read_bytes()==(frontend/'data'/p.relative_to(ROOT/'output/frontend_hectares_review_20260906_v1/data')).read_bytes()
    key,_=credentials()
    for p in frontend.rglob('*'):
        if p.is_file():assert key.encode() not in p.read_bytes(),'Credential must never be in frontend'
    files=[ROOT/'run_agent_review_v2.py',ROOT/'wp_core/agent_service_v2.py',ROOT/'wp_core/agent_knowledge_v2.py',ROOT/'config/agent_review_20260906_v1.review.lock.json']
    files += [p for base in [source,frontend] for p in base.rglob('*') if p.is_file()]
    records={str(p.relative_to(ROOT)).replace('\\','/'):{'bytes':p.stat().st_size,'sha256':hashlib.sha256(p.read_bytes()).hexdigest()} for p in files}
    lock.write_text(json.dumps({'version':slug,'live_gpt_verified':True,'owner_approved_external_payload':True,'previous_agent_configuration':'not loaded; credential/model reused only','files':records},indent=2),encoding='utf-8')
    print(json.dumps({'sealed':slug,'files':len(records),'live_checks_passed':True,'prior_seals_preserved':True,'map_data_unchanged':True}))
