"""Finite extraction pipeline overlapping completed downloads; separate OS lock.

Only finalized, hash-checked image checkpoints enter workers. The original data
collector never writes scene tables. This process never writes image checkpoints.
"""
import json
import os
import time
from concurrent.futures import ProcessPoolExecutor, wait, FIRST_COMPLETED
from pathlib import Path

import numpy as np
from wp_core import sentinel1_area_data as data

INDICES = None


def initialize():
    global INDICES
    with np.load(data.DATA / "pixel_indices.npz",allow_pickle=False) as z:
        INDICES={k:z[k] for k in z.files}


def extract(item):
    return data.extract_item(item,INDICES)


def main():
    import msvcrt
    with (data.DATA / "extraction.lock").open("a+b") as f:
        if f.tell()==0: f.write(b"0"); f.flush()
        f.seek(0)
        msvcrt.locking(f.fileno(),msvcrt.LK_NBLCK,1)
        try:
            data.pinned()
            signature={"script_sha256":data.peer.sha(Path(__file__)),"data_preparation_sha256":data.peer.sha(data.DATA/"prepared.json")}
            marker=data.DATA/"stream_prepared.json"
            if marker.exists(): assert data.peer.read(marker)==signature
            else: data.peer.write(marker,signature)
            items=data.peer.read(data.DATA/"scenes.json")["items"]
            pending={x["id"]:x for x in items}; active={}; complete=set()
            with ProcessPoolExecutor(max_workers=6,initializer=initialize) as pool:
                while pending or active:
                    for key,item in list(pending.items()):
                        if len(active)>=12: break
                        if (data.DATA/"checkpoints"/(key+".json")).exists():
                            future=pool.submit(extract,item); active[future]=key; del pending[key]
                    if not active:
                        time.sleep(2)
                        continue
                    done,_=wait(active,timeout=2,return_when=FIRST_COMPLETED)
                    for future in done:
                        future.result(); complete.add(active.pop(future))
                        if len(complete)%10==0 or len(complete)==len(items):
                            progress={"phase":"full_area_extraction","completed":len(complete),"total":len(items),"updated_utc":data.peer.utc()}
                            data.peer.write(data.DATA/"extraction_progress.json",progress)
                            print(json.dumps(progress),flush=True)
            result={"scene_tables":len(complete),"completed_utc":data.peer.utc(),**signature}
            data.peer.write(data.DATA/"extraction_complete.json",result)
            print(json.dumps(result),flush=True)
        finally:
            f.seek(0); msvcrt.locking(f.fileno(),msvcrt.LK_UNLCK,1)


if __name__=="__main__":
    main()
