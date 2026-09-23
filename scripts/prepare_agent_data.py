"""Create an additive private query index from the pinned review data."""
from pathlib import Path
import hashlib
import json
import sqlite3
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def digest(path):
    h = hashlib.sha256()
    with path.open('rb') as f:
        for block in iter(lambda: f.read(4 * 1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()


def main():
    import geopandas as gpd
    import numpy as np
    import pandas as pd
    import shapely
    start = time.monotonic()
    out = ROOT / 'server_data/agent_v1'
    out.mkdir(exist_ok=True)
    index = out / 'parcels.sqlite3'
    if index.exists():
        raise SystemExit('Preserve the existing agent index; use its manifest to verify it.')
    inner_path = ROOT / 'data/analysis/parcel_eo/current_halo_v1/current_parcels.parquet'
    outside_path = ROOT / 'data/analysis/land_potential/land_potential_expansion_1km_20260906_v1/all_parcels.parquet'
    community_path = ROOT / 'data/source/wua_areas.geojson'
    source_index = ROOT / 'server_data/land_analytics_land_potential_1km_review_20260906_v1.sqlite3'
    consolidation_path = ROOT / 'server_data/review/consolidation_review_20260906_v2/parcel_lookup.json'
    sources = [inner_path, outside_path, community_path, source_index, consolidation_path]
    before = {str(p.relative_to(ROOT)): digest(p) for p in sources}
    inner = gpd.read_parquet(inner_path)
    outside = gpd.read_parquet(outside_path)
    with sqlite3.connect(source_index.as_uri() + '?mode=ro', uri=True) as c:
        analytics = pd.read_sql_query('SELECT * FROM parcel_analytics', c)
    fields = ['cadastre_code', 'public_parcel_id', 'area_official_m2', 'geometry']
    inner = inner[fields].merge(analytics, on='cadastre_code', validate='one_to_one')
    inner['scope'] = 'inside_I_II'
    outside = outside[fields + ['stage', 'potential_class', 'household_agriculture', 'road_excluded']].rename(
        columns={'stage': 'activity_stage', 'household_agriculture': 'household'})
    outside['scope'] = 'nearby_1km'
    frame = gpd.GeoDataFrame(pd.concat([inner, outside], ignore_index=True), crs=4326)
    assert len(frame) == 70107 and frame.cadastre_code.is_unique
    assert frame.public_parcel_id.is_unique
    communities = gpd.read_file(community_path).to_crs(32638)
    projected = frame.to_crs(32638)
    pg = np.asarray(projected.geometry.array)
    cg = np.asarray(communities.geometry.array)
    assert shapely.is_valid(pg).all() and shapely.is_valid(cg).all()
    pairs = shapely.STRtree(cg).query(pg, predicate='intersects')
    intersections = shapely.area(shapely.intersection(pg[pairs[0]], cg[pairs[1]]))
    candidates = pd.DataFrame({'parcel': pairs[0], 'community_index': pairs[1], 'intersection_m2': intersections})
    candidates = candidates[candidates.intersection_m2 > 0].sort_values(
        ['parcel', 'intersection_m2', 'community_index'], ascending=[True, False, True])
    best = candidates.drop_duplicates('parcel').set_index('parcel')
    community_names = communities.name_hy.to_dict()
    frame['community'] = frame.index.map(best.community_index.map(community_names)).fillna('unassigned')
    areas = shapely.area(pg)
    frame['community_overlap_fraction'] = frame.index.map(best.intersection_m2).fillna(0) / areas
    frame['boundary_crossing'] = (frame.community_overlap_fraction < 0.999999).astype(int)
    # Exact ties remain unresolved rather than being assigned by arbitrary row order.
    tied = candidates.groupby('parcel').head(2).groupby('parcel').intersection_m2.agg(list)
    ties = tied[tied.map(lambda x: len(x) == 2 and abs(x[0] - x[1]) <= 1e-6)].index
    frame.loc[ties, 'community'] = 'unassigned'
    frame['official_area_ha'] = frame.area_official_m2 / 10000
    bbox = frame.geometry.bounds
    for field in ['minx', 'miny', 'maxx', 'maxy']:
        frame[field] = bbox[field]
    lookup = json.loads(consolidation_path.read_text())
    frame['consolidation_group'] = frame.cadastre_code.map(lambda code: (lookup.get(code) or {}).get('group_number'))
    frame['household'] = frame.household.astype(int)
    frame['road_excluded'] = frame.road_excluded.astype(int)
    frame['public_parcel_id'] = frame.public_parcel_id.map(int).astype('int64')
    fields_out = [c for c in frame.columns if c not in ('geometry', 'area_official_m2')]
    with sqlite3.connect(index) as c:
        frame[fields_out].to_sql('parcels', c, index=False)
        c.execute('CREATE UNIQUE INDEX parcel_code ON parcels(cadastre_code)')
        c.execute('CREATE INDEX parcel_scope ON parcels(scope, activity_stage, community)')
        c.execute('CREATE INDEX parcel_community ON parcels(community)')
    assert before == {str(p.relative_to(ROOT)): digest(p) for p in sources}
    report = {'version': 'agent_v1', 'source_ui': 'consolidation_review_20260906_v2',
              'rows': len(frame), 'inner_rows': len(inner), 'outside_rows': len(outside),
              'community_membership': 'greatest polygon intersection; exact ties unassigned; whole parcel areas',
              'community_counts': frame.community.value_counts().to_dict(),
              'boundary_crossing_count': int(frame.boundary_crossing.sum()),
              'source_sha256': before, 'index_sha256': digest(index),
              'preparation_seconds': round(time.monotonic() - start, 2)}
    (out / 'manifest.json').write_text(json.dumps(report, indent=2, ensure_ascii=False) + '\n', encoding='utf-8')
    print(json.dumps({k: report[k] for k in ['rows', 'inner_rows', 'outside_rows', 'boundary_crossing_count', 'preparation_seconds']}))


if __name__ == '__main__':
    main()
