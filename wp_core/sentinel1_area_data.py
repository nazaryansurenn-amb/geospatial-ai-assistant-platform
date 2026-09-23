"""Full-area data preparation using cached scene metadata and public RTC assets.

No catalogue query or study geometry is sent to a service. New image reads use
HTTP byte ranges on the already known public raster assets. Sources stay local.
"""
from __future__ import annotations
import json
import math
import os
import shutil
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import contextmanager
from pathlib import Path
from urllib.request import urlopen

import geopandas as gpd
import numpy as np
import pandas as pd
import rasterio
from affine import Affine
from rasterio.features import geometry_mask
from rasterio.windows import from_bounds
from shapely.geometry import mapping

from wp_core import sentinel1_multiyear as multi
from wp_core import sentinel1_peer_comparison as peer
from wp_core import sentinel1_pilot as pilot

ROOT = Path(__file__).resolve().parents[1]
VERSION = "sentinel1_area_20260906_v1"
CONFIG = ROOT / "config" / (VERSION + ".json")
DATA = ROOT / "data/observations" / VERSION
OUT = ROOT / "data/analysis/rapid_water_loss" / VERSION


@contextmanager
def lock():
    import msvcrt
    DATA.mkdir(parents=True, exist_ok=True)
    with (DATA / "run.lock").open("a+b") as f:
        if f.tell() == 0:
            f.write(b"0")
            f.flush()
        f.seek(0)
        try:
            msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            raise RuntimeError("Another full-area worker owns the OS lock") from None
        try:
            yield
        finally:
            f.seek(0)
            msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)


def pinned():
    marker = peer.read(DATA / "prepared.json")
    for name, digest in marker["sources"].items():
        assert peer.sha(ROOT / name) == digest, name
    for item in marker["weather"]:
        assert peer.sha(ROOT / item["path"]) == item["sha256"], item["month"]
    return marker


def pixel_indices(geom, transform, width):
    if geom.is_empty:
        return np.array([], dtype=np.int64)
    x0,y0,x1,y1 = geom.bounds
    left = math.floor((x0-transform.c)/10)
    right = math.ceil((x1-transform.c)/10)
    top = math.floor((transform.f-y1)/10)
    bottom = math.ceil((transform.f-y0)/10)
    local = transform * Affine.translation(left, top)
    mask = geometry_mask([mapping(geom)], out_shape=(bottom-top,right-left), transform=local, invert=True, all_touched=False)
    rows,cols = np.nonzero(mask)
    return ((rows+top)*width+cols+left).astype(np.int64)


def prepare():
    if (DATA / "prepared.json").exists():
        return pinned()["summary"]
    cfg = peer.read(CONFIG)
    multi.pinned()
    peer.verify_prepared()
    scope = pd.read_parquet(pilot.SCOPE)
    selected = scope.loc[scope.included].copy()
    assert len(selected)==cfg["expected_eligible"] and not selected.household.astype(bool).any() and not selected.road_excluded.astype(bool).any()
    geo = gpd.read_file(pilot.GEOM, layer="parcels", columns=["cadastre_code", "area_official_m2"])
    geo = geo.merge(selected[["internal_parcel_id","cadastre_code","area_official_m2"]], on="cadastre_code", validate="one_to_one", suffixes=("_source",""))
    assert geo.area_official_m2.eq(geo.area_official_m2_source).all()
    geo = geo.drop(columns="area_official_m2_source").sort_values("internal_parcel_id").reset_index(drop=True)
    DATA.mkdir(parents=True, exist_ok=True)
    geo.to_parquet(DATA / "original_geometry.parquet", index=False)
    geo = geo.to_crs(32638)
    spatial = pd.read_parquet(pilot.SPATIAL)
    context = pd.read_parquet(pilot.READINESS)
    geo = geo.merge(spatial[["internal_parcel_id","minimum_width_m"]], on="internal_parcel_id", validate="one_to_one")
    geo = geo.merge(context[["internal_parcel_id","review_community_hy"]], on="internal_parcel_id", validate="one_to_one")
    inner = geo.geometry.buffer(-10)
    geo["interior_area_m2"] = inner.area
    geo["sample_group"] = np.where(geo.interior_area_m2.ge(9*440) & geo.minimum_width_m.ge(50), "clean_interior", "boundary_challenge")
    b=geo.total_bounds
    bounds=[math.floor(b[0]/10)*10,math.floor(b[1]/10)*10,math.ceil(b[2]/10)*10,math.ceil(b[3]/10)*10]
    width=int((bounds[2]-bounds[0])/10); height=int((bounds[3]-bounds[1])/10)
    transform=Affine(10,0,bounds[0],0,-10,bounds[3])
    geo.to_parquet(DATA / "parcels_utm.parquet", index=False)
    usable=geo.loc[geo.sample_group.eq("clean_interior")].copy().reset_index(drop=True)
    indices={}
    for name in ("full","inner"):
        pieces=[pixel_indices(g if name=="full" else g.buffer(-10),transform,width) for g in usable.geometry]
        indices[name]=np.concatenate(pieces)
        indices[name+"_offsets"]=np.r_[0,np.cumsum([len(x) for x in pieces])]
    indices["parcel_ids"]=usable.internal_parcel_id.to_numpy(dtype="U")
    np.savez_compressed(DATA / "pixel_indices.npz",**indices)
    months=[f"{y}_{m:02d}" for y in cfg["years"] for m in range(3,10)]
    weather=peer.weather_status({"weather_months":months})
    month_set={x["month"] for x in weather["completed"]}
    cached=peer.read(multi.DATA / "scenes.json")
    items=[]
    for item in cached["items"]:
        props=item["properties"]
        q=props["proj:bbox"]
        assert q[0]<=bounds[0] and q[1]<=bounds[1] and q[2]>=bounds[2] and q[3]>=bounds[3]
        assert props["proj:epsg"]==32638 and props["proj:transform"][0]==10 and props["proj:transform"][4]==-10
        for band in ("vv","vh"):
            href=item["assets"][band]["href"]
            assert href.startswith("https://sentinel1euwestrtc.blob.core.windows.net/sentinel1-grd-rtc/") and "?" not in href
        if props["datetime"][:7].replace("-","_") in month_set:
            items.append(item)
    peer.write(DATA / "scenes.json",{"items":items,"bounds":bounds,"width":width,"height":height,"transform":list(transform)[:6],"cached_inventory_count":len(cached["items"])})
    points=geo.geometry.centroid
    ll=points.to_crs(4326)
    grid=pd.read_parquet(peer.OUT / "weather_grid.parquet")
    distance=((ll.x.to_numpy()[:,None]-grid.longitude.to_numpy())*np.cos(np.deg2rad(ll.y.to_numpy()[:,None])))**2+(ll.y.to_numpy()[:,None]-grid.latitude.to_numpy())**2
    nearest=grid.iloc[distance.argmin(axis=1)].reset_index(drop=True)
    fields=geo.drop(columns="geometry").copy()
    fields["x_m"]=points.x.to_numpy(); fields["y_m"]=points.y.to_numpy()
    fields["latitude"]=nearest.latitude; fields["longitude"]=nearest.longitude
    fields["cell_id"]=[f"era5land_{a:.1f}_{b:.1f}" for a,b in zip(nearest.latitude,nearest.longitude)]
    fields.to_parquet(DATA / "fields.parquet",index=False)
    sources=[CONFIG,Path(__file__),ROOT/"run_sentinel1_area_data.py",pilot.SCOPE,pilot.GEOM,pilot.SPATIAL,pilot.READINESS,
             multi.DATA/"scenes.json",multi.DATA/"manifest.json",peer.CONFIG,ROOT/"wp_core/sentinel1_pilot.py",
             ROOT/"wp_core/sentinel1_peer_comparison.py",DATA/"original_geometry.parquet",DATA/"parcels_utm.parquet",
             DATA/"pixel_indices.npz",DATA/"scenes.json",DATA/"fields.parquet"]
    for year in cfg["years"]:
        sources.append(ROOT/f"data/analysis/observation_screening/transitions_area_20260906_v1/daily/{year}.parquet")
    summary={"eligible":len(geo),"spatially_supported":len(usable),"insufficient_spatial_support":len(geo)-len(usable),
             "acquisitions_for_available_weather":len(items),"cached_inventory":len(cached["items"]),"raw_raster_gb":width*height*8*len(items)/1e9}
    peer.write(DATA/"prepared.json",{"version":VERSION,"prepared_utc":peer.utc(),"sources":{p.relative_to(ROOT).as_posix():peer.sha(p) for p in sources},
                                     "weather":weather["completed"],"missing_weather":weather["missing_months"],"summary":summary})
    return summary


def image_checkpoint(item):
    path=DATA/"clips"/(item["id"]+".tif")
    marker=DATA/"checkpoints"/(item["id"]+".json")
    if not marker.exists():
        return None
    r=peer.read(marker)
    assert path.stat().st_size==r["bytes"] and peer.sha(path)==r["sha256"]
    return r


def download(item,scenes,token):
    if image_checkpoint(item):
        return "reused"
    for attempt in range(3):
        try:
            assert shutil.disk_usage(DATA).free > peer.read(CONFIG)["minimum_free_gb"]*1e9
            with rasterio.Env(GDAL_DISABLE_READDIR_ON_OPEN="EMPTY_DIR",CPL_VSIL_CURL_ALLOWED_EXTENSIONS=".tiff",GDAL_HTTP_MAX_RETRY=2,
                              GDAL_HTTP_RETRY_DELAY=1,GDAL_HTTP_TIMEOUT=90,GDAL_NUM_THREADS="1"):
                with rasterio.open(item["assets"]["vv"]["href"]+"?"+token) as vv,rasterio.open(item["assets"]["vh"]["href"]+"?"+token) as vh:
                    assert vv.crs==vh.crs and vv.transform==vh.transform and vv.crs.to_epsg()==32638
                    win=from_bounds(*scenes["bounds"],transform=vv.transform).round_offsets().round_lengths()
                    assert win.col_off>=0 and win.row_off>=0 and win.col_off+win.width<=vv.width and win.row_off+win.height<=vv.height
                    arrays=np.stack([vv.read(1,window=win),vh.read(1,window=win)])
                    assert arrays.shape==(2,scenes["height"],scenes["width"]) and (arrays>0).any()
                    dest=DATA/"clips"/(item["id"]+".tif"); dest.parent.mkdir(parents=True,exist_ok=True)
                    temp=dest.with_suffix(".partial.tif")
                    with rasterio.open(temp,"w",driver="GTiff",dtype="float32",count=2,height=arrays.shape[1],width=arrays.shape[2],
                                       crs=vv.crs,transform=vv.window_transform(win),nodata=-32768,compress="deflate",tiled=True,blockxsize=256,blockysize=256) as target:
                        target.write(arrays)
                        target.update_tags(source_item=item["id"],observation_datetime=item["properties"]["datetime"],units="linear_gamma0")
                    if dest.exists():
                        assert peer.sha(dest)==peer.sha(temp),"Existing unsealed image differs"
                        temp.unlink()
                    else:
                        os.replace(temp,dest)
            peer.write(DATA/"checkpoints"/(item["id"]+".json"),{"source_item":item["id"],"sha256":peer.sha(dest),"bytes":dest.stat().st_size,"completed_utc":peer.utc()})
            return "downloaded"
        except Exception:
            if attempt==2:
                raise RuntimeError("Full-area radar read failed after retries: "+item["id"]) from None
            time.sleep(attempt+1)


def collect():
    pinned()
    scenes=peer.read(DATA/"scenes.json")
    pending=[x for x in scenes["items"] if not image_checkpoint(x)]
    done=len(scenes["items"])-len(pending)
    if pending:
        with urlopen("https://planetarycomputer.microsoft.com/api/sas/v1/token/sentinel1euwestrtc/sentinel1-grd-rtc",timeout=30) as response:
            token=json.load(response)["token"]
        with ThreadPoolExecutor(max_workers=peer.read(CONFIG)["download_workers"]) as pool:
            futures=[pool.submit(download,item,scenes,token) for item in pending]
            for future in as_completed(futures):
                future.result(); done+=1
                progress={"phase":"radar_collection","completed":done,"total":len(scenes["items"]),"updated_utc":peer.utc()}
                peer.write(DATA/"progress.json",progress)
                print(json.dumps(progress),flush=True)
    if not (DATA/"collection_complete.json").exists():
        peer.write(DATA/"collection_complete.json",{"acquisitions":done,"bytes":sum(p.stat().st_size for p in (DATA/"clips").glob("*.tif")),"completed_utc":peer.utc()})
    return peer.read(DATA/"collection_complete.json")


def extract_item(item,indices):
    folder=DATA/"scene_tables"; folder.mkdir(parents=True,exist_ok=True)
    marker=folder/(item["id"]+".json")
    path=folder/(item["id"]+".parquet")
    if marker.exists():
        r=peer.read(marker); assert peer.sha(path)==r["sha256"]
        return r
    assert image_checkpoint(item)
    with rasterio.open(DATA/"clips"/(item["id"]+".tif")) as src:
        arrays=src.read().reshape(2,-1)
    props=item["properties"]; records=[]
    for i,pid in enumerate(indices["parcel_ids"]):
        row={"internal_parcel_id":str(pid),"source_item":item["id"],"datetime":props["datetime"],"platform":props["platform"].lower(),
             "relative_orbit":props["sat:relative_orbit"],"orbit_direction":props["sat:orbit_state"]}
        for name in ("full","inner"):
            ix=indices[name][indices[name+"_offsets"][i]:indices[name+"_offsets"][i+1]]
            for band,values in zip(("vv","vh"),arrays):
                stats=pilot.power_summary(values[ix],np.ones(len(ix),dtype=bool))
                row.update({f"{band}_{name}_{key}":value for key,value in stats.items()})
        row["radar_usable"]=bool(row["vv_inner_valid_fraction"]>=.9 and row["vh_inner_valid_fraction"]>=.9 and row["vv_inner_valid_pixel_count"]*100/440>=9)
        records.append(row)
    frame=pd.DataFrame(records); frame.to_parquet(path,index=False)
    r={"rows":len(frame),"sha256":peer.sha(path),"usable":int(frame.radar_usable.sum())}
    peer.write(marker,r)
    return r


def extract():
    pinned()
    assert (DATA/"collection_complete.json").exists()
    scenes=peer.read(DATA/"scenes.json")
    with np.load(DATA/"pixel_indices.npz",allow_pickle=False) as z:
        indices={k:z[k] for k in z.files}
    # Array reads and small parcel statistics share the same immutable index cache.
    with ThreadPoolExecutor(max_workers=4) as pool:
        futures=[pool.submit(extract_item,item,indices) for item in scenes["items"]]
        for done,future in enumerate(as_completed(futures),1):
            future.result()
            if done%10==0 or done==len(futures):
                value={"phase":"radar_extraction","completed":done,"total":len(futures),"updated_utc":peer.utc()}
                peer.write(DATA/"progress.json",value); print(json.dumps(value),flush=True)
    return {"scene_tables":len(scenes["items"]),"parcels_per_table":len(indices["parcel_ids"])}


def main(action):
    with lock():
        result={"prepare":prepare,"collect":collect,"extract":extract}[action]()
        print(json.dumps(result,ensure_ascii=False,indent=2),flush=True)
