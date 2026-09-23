from pathlib import Path
import hashlib
import json
ROOT=Path(__file__).resolve().parents[1]

if __name__=='__main__':
    lock=ROOT/'config/agent_review_20260906_v3.review.lock.json'
    assert not lock.exists(),'Preserve the existing seal'
    for report in ['server_data/agent_v1/language_verification_v3.json','output/playwright/agent_language_v3/verification.json','server_data/agent_v1/http_verification.json']:
        assert json.loads((ROOT/report).read_text(encoding='utf-8'))['passed'],report
    old_locks=[ROOT/'config'/f'agent_review_20260906_v{n}.review.lock.json' for n in [1,2]]
    for old in old_locks:
        for name,record in json.loads(old.read_text())['files'].items():
            assert hashlib.sha256((ROOT/name).read_bytes()).hexdigest()==record['sha256'],name
    files=[ROOT/'run_agent_review_v3.py',ROOT/'wp_core/agent_service_v3.py',ROOT/'wp_core/agent_knowledge_v3.py',ROOT/'wp_core/agent_presentation.py']+old_locks
    records={str(p.relative_to(ROOT)).replace('\\','/'):{'bytes':p.stat().st_size,'sha256':hashlib.sha256(p.read_bytes()).hexdigest()} for p in files}
    lock.write_text(json.dumps({'version':'agent_review_20260906_v3','purpose':'plain-language replies; unchanged calculations and frontend','live_language_verified':True,'files':records},indent=2),encoding='utf-8')
    print(json.dumps({'sealed':True,'prior_versions_preserved':True,'frontend_unchanged':True,'files':len(records)}))
