"""Seal checked additive UI without altering any prior version."""
from pathlib import Path
import hashlib,json,sys
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from wp_core.agent_service_v7 import credentials
SLUG='irrigation_stress_review_20260907_v1'

def sha(p):return hashlib.sha256(p.read_bytes()).hexdigest()

def main():
    lock=ROOT/'config'/f'{SLUG}.review.lock.json';assert not lock.exists()
    for name in ['analysis_verification','http_verification','browser_verification']:
        assert json.loads((ROOT/'server_data/agent_v7'/f'{name}.json').read_text())['passed']
    assert '429 passed' in (ROOT/'server_data/agent_v7/full_tests.log').read_text()
    previous=[ROOT/'config'/f'agent_review_20260906_v{n}.review.lock.json' for n in range(1,7)]
    previous += [ROOT/'config'/f'{s}.review.lock.json' for s in ['activity_change_history_review_20260906_v1','activity_change_area50_20260907_v1','readability_20260907_v1']]
    for p in previous:
        for name,r in json.loads(p.read_text())['files'].items():assert sha(ROOT/name)==r['sha256'],name
    source=ROOT/'server_data/review'/SLUG/'frontend_source';dist=ROOT/'output'/f'frontend_{SLUG}'
    base=ROOT/'output/frontend_activity_change_area50_20260907_v1/data'
    for p in base.rglob('*'):
        if p.is_file():assert sha(p)==sha(dist/'data'/p.relative_to(base)),p
    assert (dist/'assets/readability-20260907-v1.css').read_bytes()==(ROOT/'ui/readability-20260907-v1.css').read_bytes()
    key,_=credentials()
    for p in dist.rglob('*'):
        if p.is_file():
            assert p.suffix in ['.html','.js','.css','.pbf','.json','.geojson','.png','.svg','.webp','.jpg','.jpeg','.woff','.woff2'],p
            if key:assert key.encode() not in p.read_bytes(),'Credential in frontend'
    names=['run_irrigation_stress_review.py','wp_core/agent_service_v7.py','wp_core/agent_queries_v7.py','wp_core/agent_knowledge_v7.py','wp_core/agent_presentation_v7.py','wp_core/agent_excel_v7.py','wp_core/activity_change_history_agent_v7.py','wp_core/activity_change_area50_agent_v7.py','server_data/agent_v7/parcels.sqlite3','server_data/agent_v7/irrigation_stress.json','server_data/agent_v7/irrigation_stress_lookup.json','docs/IRRIGATION_STRESS_2026_09_07_EN.md','verify_current_irrigation_stress.py','scripts/verify_irrigation_stress_http.py','scripts/prepare_irrigation_stress_review.py','tests/test_current_irrigation_stress.py','tests/test_irrigation_stress_delivery.py','data/analysis/irrigation_stress/irrigation_stress_20260907_v2/complete.json']
    files=[ROOT/n for n in names]+previous+[p for folder in [source,dist] for p in folder.rglob('*') if p.is_file()]
    records={p.relative_to(ROOT).as_posix():{'bytes':p.stat().st_size,'sha256':sha(p)} for p in files}
    lock.write_text(json.dumps({'version':SLUG,'base':'readability_20260907_v1 on activity_change_area50_20260907_v1','desktop_mobile_verified':True,'files':records},indent=2))
    print(json.dumps({'sealed':True,'files':len(records),'existing_map_tiles_unchanged':True,'prior_versions_preserved':True}))
if __name__=='__main__':main()
