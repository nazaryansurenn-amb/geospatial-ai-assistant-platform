"""Serve the additive inner-area plus 1 km search review on localhost."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import sqlite3
from urllib.parse import parse_qs,urlsplit
ROOT=Path(__file__).resolve().parent
SLUG='land_potential_1km_review_20260906_v2'
DATA_SLUG='land_potential_1km_review_20260906_v1'


def main():
    p=argparse.ArgumentParser();p.add_argument('--port',type=int,default=8526);args=p.parse_args()
    for name,record in json.loads((ROOT/'config'/f'{SLUG}.review.lock.json').read_text())['files'].items():
        path=(ROOT/name).resolve();assert ROOT in path.parents
        assert path.stat().st_size==record['bytes'] and hashlib.sha256(path.read_bytes()).hexdigest()==record['sha256'],name
    os.environ['LAND_ANALYTICS_PROFILE']='use_type_v2'
    os.environ['WORKING_PRODUCT_HOST']='127.0.0.1';os.environ['WORKING_PRODUCT_PORT']=str(args.port)
    import app
    app.DIST_ROOT=ROOT/'output'/f'frontend_{SLUG}'
    app.LAND_ANALYTICS_INDEX=ROOT/'server_data'/f'land_analytics_{DATA_SLUG}.sqlite3'
    app.LAND_ANALYTICS_SUMMARY=ROOT/'server_data'/f'land_analytics_summary_{DATA_SLUG}.json'
    class Handler(app.ProductRequestHandler):
        def _land_parcel(self,query):
            code=query.get('code',[''])[0].strip()
            if app.CADASTRE_CODE_RE.fullmatch(code):
                with sqlite3.connect(app.LAND_ANALYTICS_INDEX.as_uri()+'?mode=ro',uri=True) as c:
                    row=c.execute('SELECT potential_class,household_agriculture,road_excluded FROM potential_expansion WHERE cadastre_code=?',(code,)).fetchone()
                if row:
                    self._send_json({'potential':{'potentialClass':row[0],'scope':'nearby_1km'},'activity':{'household':bool(row[1]),'roadExcluded':bool(row[2])}})
                    return
            super()._land_parcel(query)
        def _send_json(self,payload,status=200):
            req=urlsplit(self.path)
            if req.path=='/api/land/parcel' and status==200 and 'error' not in payload and 'potential' not in payload:
                code=parse_qs(req.query).get('code',[''])[0]
                with sqlite3.connect(app.LAND_ANALYTICS_INDEX.as_uri()+'?mode=ro',uri=True) as c:
                    row=c.execute('SELECT potential_class FROM parcel_analytics WHERE cadastre_code=?',(code,)).fetchone()
                payload=dict(payload);payload['potential']={'potentialClass':row[0] if row else 'not_candidate','scope':'inside_I_II'}
            super()._send_json(payload,status=status)
    app.ProductRequestHandler=Handler
    app.main()


if __name__=='__main__':main()
