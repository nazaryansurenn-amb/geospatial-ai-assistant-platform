"""Preserve old seals and seal the additive Excel-download version."""
from pathlib import Path
import hashlib
import json
import sys
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from wp_core.agent_service_v5 import credentials

if __name__=='__main__':
    lock=ROOT/'config/agent_review_20260906_v5.review.lock.json'
    assert not lock.exists(),'Preserve the existing seal'
    for report in ['server_data/agent_v5/http_verification.json','output/playwright/agent_excel_v5/verification.json']:
        assert json.loads((ROOT/report).read_text(encoding='utf-8'))['passed'],report
    old_locks=[ROOT/'config'/f'agent_review_20260906_v{n}.review.lock.json' for n in [1,2,3,4]]
    for old in old_locks:
        for name,record in json.loads(old.read_text())['files'].items():
            assert hashlib.sha256((ROOT/name).read_bytes()).hexdigest()==record['sha256'],name
    source=ROOT/'server_data/review/agent_review_20260906_v5/frontend_source'
    frontend=ROOT/'output/frontend_agent_review_20260906_v5'
    for p in (ROOT/'output/frontend_agent_review_20260906_v2/data').rglob('*'):
        if p.is_file():assert p.read_bytes()==(frontend/'data'/p.relative_to(ROOT/'output/frontend_agent_review_20260906_v2/data')).read_bytes()
    key,_=credentials()
    for p in frontend.rglob('*'):
        if p.is_file():assert key.encode() not in p.read_bytes(),'Credential must never appear in frontend'
    files=[ROOT/'run_agent_review_v5.py',ROOT/'wp_core/agent_service_v5.py',ROOT/'wp_core/agent_knowledge_v5.py',ROOT/'wp_core/agent_excel.py',ROOT/'server_data/agent_v5/export_template.xlsx']+old_locks
    files += [p for base in [source,frontend] for p in base.rglob('*') if p.is_file()]
    records={str(p.relative_to(ROOT)).replace('\\','/'):{'bytes':p.stat().st_size,'sha256':hashlib.sha256(p.read_bytes()).hexdigest()} for p in files}
    lock.write_text(json.dumps({'version':'agent_review_20260906_v5','purpose':'Excel tables, native charts and parcel details; CSV removed from visible UI','desktop_mobile_verified':True,'files':records},indent=2),encoding='utf-8')
    print(json.dumps({'sealed':True,'prior_versions_preserved':True,'map_data_unchanged':True,'files':len(records)}))
