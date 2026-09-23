"""Retrieve public RTC catalogue metadata for the authorized full-area screening."""
import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
REVIEW = ROOT / "server_data/review/sentinel1_area_20260906_v1"


def query(year):
    dest = REVIEW / "catalogue" / f"{year}.json"
    if dest.exists():
        result = json.loads(dest.read_text(encoding="utf-8"))
    else:
        scope = json.loads((REVIEW / "scope_preflight.json").read_text())
        payload = {"collections": ["sentinel-1-rtc"], "bbox": scope["bbox_wgs84"],
                   "datetime": f"{year}-04-01T00:00:00Z/{year}-09-30T23:59:59Z", "limit": 1000}
        req = Request("https://planetarycomputer.microsoft.com/api/stac/v1/search",
                      data=json.dumps(payload).encode(), headers={"Content-Type": "application/json"})
        with urlopen(req, timeout=90) as response:
            result = json.load(response)
        assert not any(x["rel"] == "next" for x in result.get("links", [])), "Catalogue pagination needs explicit continuation"
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.with_suffix(".query.json").write_text(json.dumps(payload, indent=2), encoding="utf-8")
        dest.write_text(json.dumps(result), encoding="utf-8")
    selected = [x for x in result["features"] if x["properties"]["sat:relative_orbit"] in (72, 152)]
    grouped = {(x["properties"]["platform"], x["properties"]["sat:relative_orbit"], x["properties"]["datetime"][:10]) for x in selected}
    return {"year": year, "items": len(selected), "orbit_platform_dates": len(grouped)}


if __name__ == "__main__":
    with ThreadPoolExecutor(max_workers=5) as pool:
        for result in pool.map(query, range(2021, 2026)):
            print(json.dumps(result), flush=True)
