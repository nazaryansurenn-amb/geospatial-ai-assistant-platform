"""Seal the additive agent review after local verification; no API activation."""
from pathlib import Path
import hashlib
import json
import sys
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from wp_core.agent_service import credentials, connection_enabled

if __name__=='__main__':
    slug='agent_review_20260906_v1'
    lock=ROOT/'config'/f'{slug}.review.lock.json'
    assert not lock.exists(), 'Preserve the existing seal'
    assert not connection_enabled(), 'Live external connection remains disabled pending owner approval'
    for report in ['server_data/agent_v1/http_verification.json','output/playwright/agent_review_20260906_v1/verification.json']:
        assert json.loads((ROOT/report).read_text())['passed']
    records={}
    for version in ['hectares_review_20260906_v1','hectares_review_20260906_v2']:
        old=json.loads((ROOT/'config'/f'{version}.review.lock.json').read_text())
        for name,r in old['files'].items():
            assert hashlib.sha256((ROOT/name).read_bytes()).hexdigest()==r['sha256'],name
            records[name]=r
    source=ROOT/'server_data/review'/slug/'frontend_source'
    frontend=ROOT/'output'/('frontend_'+slug)
    baseline=ROOT/'output/frontend_hectares_review_20260906_v1/data'
    for p in baseline.rglob('*'):
        if p.is_file():assert p.read_bytes()==(frontend/'data'/p.relative_to(baseline)).read_bytes(),str(p)
    key,_=credentials()
    for p in frontend.rglob('*'):
        if p.is_file():
            value=p.read_bytes()
            assert not key or key.encode() not in value, 'A credential must never be in frontend assets'
    files=[ROOT/'run_agent_review.py',ROOT/'wp_core/agent_queries.py',ROOT/'wp_core/agent_knowledge.py',ROOT/'wp_core/agent_service.py',
           ROOT/'server_data/agent_v1/parcels.sqlite3',ROOT/'server_data/agent_v1/manifest.json']
    files += [p for base in [source,frontend] for p in base.rglob('*') if p.is_file()]
    for p in files:
        records[str(p.relative_to(ROOT)).replace('\\','/')]={'bytes':p.stat().st_size,'sha256':hashlib.sha256(p.read_bytes()).hexdigest()}
    lock.write_text(json.dumps({'version':slug,'local_checks_passed':True,'live_gpt_verified':False,'external_connection':'awaiting owner approval',
                                'files':records},indent=2),encoding='utf-8')
    print(json.dumps({'sealed':slug,'files':len(records),'existing_map_data_unchanged':True,'credential_scan_passed':True,'live_api_enabled':False}))
