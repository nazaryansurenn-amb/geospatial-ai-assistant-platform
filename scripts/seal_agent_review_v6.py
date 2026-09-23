"""Seal the additive seven-section review, including the actual newer baseline."""
from pathlib import Path
import hashlib,json,sys
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from wp_core.agent_service_v6 import credentials

def main():
    slug='agent_review_20260906_v6';lock=ROOT/'config'/f'{slug}.review.lock.json'
    assert not lock.exists(),'Preserve the existing seal'
    for name in ['server_data/agent_v6/analysis_verification.json','server_data/agent_v6/http_verification.json','output/playwright/degradation_v6/verification.json']:
        assert json.loads((ROOT/name).read_text(encoding='utf-8'))['passed'],name
    old=[ROOT/'config'/f'agent_review_20260906_v{n}.review.lock.json' for n in range(1,6)]+[ROOT/'config/activity_change_history_review_20260906_v1.review.lock.json']
    for p in old:
        for name,r in json.loads(p.read_text())['files'].items():assert hashlib.sha256((ROOT/name).read_bytes()).hexdigest()==r['sha256'],name
    source=ROOT/'server_data/review'/slug/'frontend_source';dist=ROOT/'output'/f'frontend_{slug}'
    base=ROOT/'output/frontend_activity_change_history_review_20260906_v1/data'
    for p in base.rglob('*'):
        if p.is_file():assert p.read_bytes()==(dist/'data'/p.relative_to(base)).read_bytes(),p
    key,_=credentials()
    for p in dist.rglob('*'):
        if p.is_file() and key:assert key.encode() not in p.read_bytes(),'Credential in frontend'
    paths=['run_agent_review_v6.py','wp_core/agent_service_v6.py','wp_core/agent_knowledge_v6.py','wp_core/agent_queries_v6.py','wp_core/agent_presentation_v6.py','wp_core/agent_excel_v6.py','wp_core/activity_change_history_agent_v6.py','server_data/agent_v6/parcels.sqlite3','server_data/agent_v6/degradation.json','server_data/agent_v6/degradation_lookup.json','data/analysis/degradation/degradation_screening_20260906_v1/complete.json','docs/DEGRADATION_SCREENING_2026_09_06_EN.md','verify_degradation_screening.py','scripts/prepare_degradation_review.py']
    files=[ROOT/p for p in paths]+old+[p for folder in [source,dist] for p in folder.rglob('*') if p.is_file()]
    records={p.relative_to(ROOT).as_posix():{'bytes':p.stat().st_size,'sha256':hashlib.sha256(p.read_bytes()).hexdigest()} for p in files}
    lock.write_text(json.dumps({'version':slug,'purpose':'Possible degradation signs as seventh section; preserves cultivation changes and agent v5 behavior','desktop_mobile_verified':True,'files':records},indent=2),encoding='utf-8')
    print(json.dumps({'sealed':True,'prior_versions_preserved':True,'existing_map_tiles_unchanged':True,'files':len(records)}))
if __name__=='__main__':main()
