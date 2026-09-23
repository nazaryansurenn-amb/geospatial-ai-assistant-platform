"""Pin the complete candidate only after its data and frontend exist."""
from pathlib import Path
import json
import sys
from datetime import datetime, timezone

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from release_tools import sha256

from wp_core.use_type_release import CANDIDATE_SLUG as SLUG


def seal():
    target=ROOT/'config'/f'{SLUG}.lock.json'
    if target.exists():
        raise FileExistsError('Candidate already sealed; create another version')
    dist=ROOT/'output'/f'frontend_{SLUG}'
    required=[ROOT/'app.py',ROOT/'run_use_type_candidate.py',ROOT/'wp_core/use_type_release.py',ROOT/'server_data/cadastre_search.sqlite3',
        ROOT/'server_data'/f'land_analytics_{SLUG}.sqlite3',
        ROOT/'server_data'/f'land_analytics_summary_{SLUG}.json',dist/'index.html']
    if not all(path.is_file() for path in required):
        raise ValueError('Candidate is not complete')
    files=set(required)|set(path for path in dist.rglob('*') if path.is_file())
    payload={'version':SLUG,'port':8526,'host':'127.0.0.1','approval':'awaiting_Suren_visual_review',
             'created_at':datetime.now(timezone.utc).isoformat(),
             'files':{path.relative_to(ROOT).as_posix():{'bytes':path.stat().st_size,'sha256':sha256(path)} for path in sorted(files)}}
    target.write_text(json.dumps(payload,indent=2)+'\n',encoding='utf-8')
    print(f'Sealed {len(files)} candidate files; working release catalog unchanged')


if __name__=='__main__':
    seal()
