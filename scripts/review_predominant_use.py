"""Local owner-only dated RGB sheets and measured seasonal plots. No approval."""
from __future__ import annotations
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
import html
import json
from pathlib import Path
import sys
import argparse

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))
import geopandas as gpd
import numpy as np
import pandas as pd
from PIL import Image, ImageDraw, ImageFont
from rasterio.enums import Resampling
import rasterio
from rasterio.windows import from_bounds
from wp_core.eo_access import _grid_from_bounds, _read_asset_to_grid
from prepare_predominant_use import OUTPUT, EO_ROOT, PARCELS

REVIEW = OUTPUT / "visual_review_v2"
COLORS = {2021: '#268463', 2022: '#bb6b18', 2023: '#307cc1', 2024: '#b3456e', 2025: '#535d63'}


def stratified(frame, count):
    if len(frame) <= count:
        return frame.sort_values("cadastre_code")
    frame = frame.copy()
    frame["size"] = pd.qcut(frame.area_official_m2.rank(method="first"), 3, labels=False)
    center = frame.geometry.centroid
    frame["region"] = (center.x.gt(center.x.median()).astype(int) + 2*center.y.gt(center.y.median()).astype(int))
    buckets = [group.sort_values("cadastre_code") for _, group in frame.groupby(["stage", "size", "region"], sort=True)]
    selected = []
    for i in range(max(map(len, buckets))):
        for bucket in buckets:
            if i < len(bucket):
                selected.append(bucket.iloc[i:i+1])
                if len(selected) == count:
                    return pd.concat(selected)
    return pd.concat(selected)


def rgb_scene(scene, transform, width, height):
    bands = []
    with rasterio.Env(GDAL_HTTP_TIMEOUT="40", GDAL_HTTP_CONNECTTIMEOUT="15"):
        for color in ("red", "green", "blue"):
            asset = scene["assets"][color]
            raw = _read_asset_to_grid(asset["href"], transform, width, height, resampling=Resampling.bilinear)
            bands.append(raw.astype("float32") * asset["scale"] + asset["offset"])
    rgb = np.power(np.clip(np.stack(bands, axis=2)/.30, 0, 1), .82)
    return (rgb*255).astype("uint8")


def prefetch():
    frame = gpd.read_parquet(PARCELS).to_crs(32638)
    transform, width, height, _ = _grid_from_bounds(frame.total_bounds)
    cache = EO_ROOT / 'rgb_review_cache'
    cache.mkdir(parents=True, exist_ok=True)
    jobs = []
    for year in range(2021, 2026):
        manifest = json.loads((EO_ROOT/f'manifest_{year}.json').read_text())
        candidates = [s for s in manifest['scenes'] if float(s['properties'].get('eo:cloud_cover',100)) < 20]
        used = set()
        for month in (4,6,8,10):
            target = pd.Timestamp(year=year,month=month,day=25,tz='UTC')
            available = [s for s in candidates if s['id'] not in used]
            if available:
                scene = min(available,key=lambda s:abs(pd.Timestamp(s['properties']['datetime'])-target))
                used.add(scene['id'])
                jobs.append(scene)
    def read(scene):
        date = pd.Timestamp(scene['properties']['datetime']).date().isoformat()
        path = cache / f'rgb_{date}.npy'
        if not path.exists():
            np.save(path,rgb_scene(scene,transform,width,height))
        print(f'review RGB {date}',flush=True)
        return date, (scene,path)
    with ThreadPoolExecutor(max_workers=3) as pool:
        return dict(pool.map(read,jobs))


def build():
    if (REVIEW / "index.html").exists():
        raise FileExistsError("Review sheets already exist")
    REVIEW.mkdir(parents=True, exist_ok=True)
    frame = gpd.read_parquet(PARCELS)[["cadastre_code", "geometry"]].merge(
        pd.read_parquet(OUTPUT / "classification.parquet"), on="cadastre_code", validate="one_to_one").to_crs(32638)
    eligible = frame.loc[~frame.household_agriculture & ~frame.road_excluded]
    groups = {
        "annual_single": eligible.loc[eligible.annual_cycle.eq("single_cycle")],
        "double_cycle_or_boundary": eligible.loc[eligible.annual_cycle.eq("two_cycle_recurring") |
            eligible.annual_cycle_year_codes.map(lambda values: 2 in values)],
        "perennial": eligible.loc[eligible.crop_type_candidate.eq("perennial")],
        "undetermined": eligible.loc[eligible.crop_type_candidate.eq("undetermined")],
    }
    samples, counts = [], {}
    selected_codes = set()
    for name, population in groups.items():
        population = population.loc[~population.cadastre_code.isin(selected_codes)]
        sample = stratified(population, 30).copy()
        sample["review_group"] = name
        counts[name] = {"requested": 30, "available": len(population), "selected": len(sample)}
        samples.append(sample)
        selected_codes.update(sample.cadastre_code)
    sample = pd.concat(samples)
    # No invented members when the class population is smaller than the quota.
    diagnostics = {"status": "awaiting_visual_review", "groups": counts, "sample_count": len(sample),
                   "resolution_m": 10, "limitations": "RGB at 10 m cannot resolve all orchard rows or narrow parcels.",
                   "sheets": []}
    transform, width, height, _ = _grid_from_bounds(frame.total_bounds)
    scenes = []
    for year in range(2021, 2026):
        manifest = json.loads((EO_ROOT / f"manifest_{year}.json").read_text())
        candidates = [s for s in manifest["scenes"] if float(s["properties"].get("eo:cloud_cover", 100)) < 20]
        used = set()
        for month in (4, 6, 8, 10):
            target = pd.Timestamp(year=year, month=month, day=25, tz="UTC")
            available = [s for s in candidates if s["id"] not in used]
            if not available:
                continue
            selected = min(available, key=lambda s: abs(pd.Timestamp(s['properties']['datetime'])-target))
            used.add(selected['id'])
            scenes.append((year, selected))
    rgb_paths = {}
    def read(job):
        year, scene = job
        date = pd.Timestamp(scene['properties']['datetime']).date().isoformat()
        path = EO_ROOT / 'rgb_review_cache' / f"rgb_{date}.npy"
        path.parent.mkdir(parents=True,exist_ok=True)
        if not path.exists():
            np.save(path, rgb_scene(scene, transform, width, height))
        print(f"review RGB {date}", flush=True)
        return date, scene, path
    with ThreadPoolExecutor(max_workers=3) as pool:
        for date, scene, path in pool.map(read, scenes):
            rgb_paths[date] = (scene, path)
    series = {}
    for year in range(2021, 2026):
        data = pd.read_parquet(EO_ROOT / f"observations_{year}.parquet",
            columns=['cadastre_code','observation_date','ndvi_mean','valid_fraction'])
        for code, group in data.loc[data.cadastre_code.isin(sample.cadastre_code)].groupby('cadastre_code'):
            series.setdefault(code, {})[year] = group.sort_values('observation_date')
    font = ImageFont.load_default(size=14)
    small = ImageFont.load_default(size=12)
    cards = []
    for row in sample.itertuples():
        canvas = Image.new('RGB', (1240, 1570), '#ffffff')
        draw = ImageDraw.Draw(canvas)
        draw.text((20,12), f'{row.cadastre_code} | {row.review_group} | result: {row.crop_type_candidate} | {row.stage} | {row.area_official_m2/10000:.4f} ha', fill='#17251f', font=font)
        draw.text((20,35), 'UNVERIFIED EO SCREENING | Copernicus RGB 10 m | white outline: immutable cadastre', fill='#58645e', font=small)
        for year in range(2021, 2026):
            y0 = 66 + (year-2021)*292
            year_dates = sorted(date for date in rgb_paths if date.startswith(str(year)))
            for j,date in enumerate(year_dates):
                _, path = rgb_paths[date]
                rgb = np.load(path, mmap_mode='r')
                geom = row.geometry
                cx = (geom.bounds[0]+geom.bounds[2])/2
                cy = (geom.bounds[1]+geom.bounds[3])/2
                span = max(geom.bounds[2]-geom.bounds[0],geom.bounds[3]-geom.bounds[1],100)*1.4
                window = from_bounds(cx-span/2,cy-span/2,cx+span/2,cy+span/2,transform).round_offsets().round_lengths()
                left,top=max(0,int(window.col_off)),max(0,int(window.row_off))
                right,bottom=min(width,int(window.col_off+window.width)),min(height,int(window.row_off+window.height))
                patch=np.array(rgb[top:bottom,left:right])
                x0=20+j*240
                draw.text((x0,y0),date,fill='#17251f',font=font)
                if patch.size:
                    canvas.paste(Image.fromarray(patch).resize((228,196),Image.Resampling.NEAREST),(x0,y0+22))
                    def pt(x,y):
                        col,rr=(~transform)*(x,y)
                        return x0+(col-left)/(right-left)*228,y0+22+(rr-top)/(bottom-top)*196
                    for polygon in geom.geoms if geom.geom_type=='MultiPolygon' else [geom]:
                        draw.line([pt(x,y) for x,y in polygon.exterior.coords],fill='white',width=2)
            x0,yc=1000,y0+22
            draw.rectangle((x0,yc,x0+220,yc+196),outline='#b2bab5')
            for value in (0,.5,1):
                yy=yc+176-value*156
                draw.line((x0,yy,x0+220,yy),fill='#e2e8e4')
                draw.text((x0+3,yy-14),f'{value:g}',fill='#64736c',font=small)
            for day,label in ((75,'Mar'),(165,'Jun'),(255,'Sep'),(345,'Dec')):
                draw.text((x0+day/366*220-12,yc+181),label,fill='#64736c',font=small)
            points=series[row.cadastre_code][year]
            prior=None
            for obs in points.itertuples():
                day=pd.Timestamp(obs.observation_date).dayofyear
                if not np.isfinite(obs.ndvi_mean) or obs.valid_fraction<.6:
                    prior=None
                    continue
                point=(x0+day/366*220,yc+176-float(obs.ndvi_mean)*156)
                if prior and day-prior[0]<=35:
                    draw.line((prior[1],point),fill=COLORS[year],width=2)
                draw.ellipse((point[0]-2,point[1]-2,point[0]+2,point[1]+2),fill=COLORS[year])
                prior=(day,point)
            idx=year-2021
            label={0:'coverage gap',1:'annual',2:'perennial',3:'no crop activity observed',4:'undetermined'}[row.crop_type_year_codes[idx]]
            draw.text((20,y0+231),f'{year}: {label} | cycles {row.annual_cycle_year_codes[idx] or "not assessed"} | measured NDVI, Jan-Dec, gaps not joined',fill='#33443a',font=small)
        filename=f'{row.cadastre_code}.png'
        canvas.save(REVIEW/filename)
        diagnostics['sheets'].append({'cadastre_code':row.cadastre_code,'group':row.review_group,'stage':row.stage,
                                    'file':filename,'status':'not_reviewed','dates':sorted(rgb_paths)})
        cards.append(f'<article><h2>{html.escape(row.cadastre_code)}: {html.escape(row.review_group)}</h2><img loading="lazy" src="{filename}" alt="Dated RGB and NDVI"></article>')
    (REVIEW/'index.html').write_text('<!doctype html><meta charset="utf-8"><title>Owner EO verification</title><style>body{font:16px system-ui;margin:20px;background:#f3f6f4;color:#17251f}img{max-width:100%;height:auto}article{margin:32px 0}h2{font-size:18px}</style><h1>Owner verification: not approved</h1>'+''.join(cards),encoding='utf-8')
    (REVIEW/'sample.json').write_text(json.dumps(diagnostics,indent=2),encoding='utf-8')
    print(json.dumps(counts),flush=True)


if __name__ == '__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--prefetch',action='store_true')
    args=parser.parse_args()
    prefetch() if args.prefetch else build()
