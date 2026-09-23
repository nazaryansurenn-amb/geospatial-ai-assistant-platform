"""Read-only verification of the candidate and preservation of the baseline."""
from pathlib import Path
import json
import sqlite3
import sys
import numpy as np
import pandas as pd
import mapbox_vector_tile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from release_tools import verify_release, read_release

from wp_core.use_type_release import CANDIDATE_SLUG as SLUG


def validate():
    baseline_check = verify_release(ROOT, 'working')
    baseline, manifest = read_release(ROOT, 'working')
    candidate = ROOT / 'server_data' / f'land_analytics_{SLUG}.sqlite3'
    with sqlite3.connect(candidate.as_uri()+'?mode=ro',uri=True) as con:
        rows = pd.read_sql_query('SELECT * FROM parcel_analytics', con).set_index('cadastre_code').sort_index()
        metadata = dict(con.execute('SELECT key,value FROM metadata'))
    with sqlite3.connect((baseline/manifest['index']).as_uri()+'?mode=ro',uri=True) as con:
        old = pd.read_sql_query('SELECT * FROM parcel_analytics',con).set_index('cadastre_code').sort_index()
    assert len(rows)==43984 and rows.index.is_unique and rows.index.equals(old.index)
    for column in old.columns:
        if not column.startswith('crop_'):
            pd.testing.assert_series_equal(old[column],rows[column],check_dtype=False)
    excluded = rows.household.astype(bool) | rows.road_excluded.astype(bool)
    assert rows.loc[excluded,'crop_type'].isna().all()
    fields = rows.loc[~excluded]
    assert fields.crop_type.notna().all()
    assert set(fields.crop_type) <= {'annual','perennial','undetermined'}
    assert set(fields.loc[fields.crop_type.eq('annual'),'annual_cycle']) <= {'single_cycle','two_cycle_recurring'}
    assert fields.loc[~fields.crop_type.eq('annual'),'annual_cycle'].isna().all()
    summary = json.loads((ROOT/'server_data'/f'land_analytics_summary_{SLUG}.json').read_text())
    assert summary['delivery_version']==metadata['delivery_version']
    assert summary['activity']==json.loads((baseline/manifest['summary']).read_text())['activity']
    for scope, section in summary['land_use_type']['summaries'].items():
        selected=fields if scope=='lower_hrazdan' else fields.loc[fields.activity_stage.eq(scope)]
        assert len(selected)==sum(section['class_counts'].values())==section['eligible_parcel_count']
    tile_root=ROOT/'public/data/land_analytics'/SLUG
    old_root=baseline/'dist'/manifest['tile_url'].lstrip('/').split('/{z}')[0]
    checked=0
    allowed={'stage','activity_class','activity_fraction_bp','activity_state','household','road_excluded','history_class','crop_type','annual_cycle'}
    for path in sorted(tile_root.rglob('*.pbf')):
        relative=path.relative_to(tile_root)
        decoded=mapbox_vector_tile.decode(path.read_bytes())['land_analytics']['features']
        previous={f['id']:f for f in mapbox_vector_tile.decode((old_root/relative).read_bytes())['land_analytics']['features']}
        assert len(decoded)==len(previous)
        for feature in decoded:
            old_feature=previous[feature['id']]
            assert feature['geometry']==old_feature['geometry']
            assert set(feature['properties'])==allowed
            for key,value in feature['properties'].items():
                if key not in ('crop_type','annual_cycle'):
                    assert value==old_feature['properties'][key]
            if feature['properties']['household'] or feature['properties']['road_excluded']:
                assert not feature['properties']['crop_type']
        checked+=1
    assert checked==370
    return {'baseline':baseline_check,'parcel_count':len(rows),'field_count':len(fields),
            'tiles_checked':checked,'preserved_geometry_and_other_attributes':True,'public_tile_schema':'passed'}


if __name__=='__main__':
    print(json.dumps(validate(),indent=2))
