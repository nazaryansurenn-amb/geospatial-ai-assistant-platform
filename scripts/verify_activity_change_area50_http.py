"""Check only the data replacement against the live sealed v6 baseline."""
from pathlib import Path
import hashlib
import json
import sys
import time
import geopandas as gpd
from verify_activity_change_http import get

ROOT = Path(__file__).resolve().parents[1]
SLUG = "activity_change_area50_20260907_v1"
REVIEW = ROOT / "server_data/review" / SLUG


def digest(value):
    return hashlib.sha256(value).hexdigest()


def main():
    port = int(sys.argv[1])
    preview = port == 8528
    phase = "preview" if preview else "activation"
    baseline = None if preview else json.loads((REVIEW / "http_preview.json").read_text())
    result = {"phase":phase, "port":port, "checked_at":time.strftime("%Y-%m-%dT%H:%M:%S%z"), "api":{}}
    endpoints = ["/health", "/api/land/delivery", "/api/land/activity-2026", "/api/land/history-2021-2025",
                 "/api/land/use-type", "/api/land/consolidation", "/api/land/degradation"]
    for path in endpoints:
        status, body = get(port, path)
        assert status == 200, path
        if preview:
            assert json.loads(body) == json.loads(get(8526, path)[1]), path
        else:
            assert digest(body) == baseline["api"][path], path
        result["api"][path] = digest(body)
    status, body = get(port, "/api/land/activity-change")
    assert status == 200 and json.loads(body) == json.loads((REVIEW / "summary.json").read_text())
    assert len(body) < 20000
    result["summary_bytes"] = len(body)
    lookup = json.loads((REVIEW / "parcel_lookup.json").read_text())
    register = gpd.read_parquet(ROOT / "data/analysis/activity_change" / SLUG / "register.parquet")
    samples = register.groupby(["previous_change_class", "change_class", "stage"], dropna=False).head(1)
    checked = {}
    for code in samples.cadastre_code:
        path = f"/api/land/parcel?code={code}"
        status, body = get(port, path)
        assert status == 200, code
        payload = json.loads(body)
        assert payload.pop("activityChange") == lookup[code], code
        assert "degradation" in payload, code
        if preview:
            original = json.loads(get(8526, path)[1])
            original.pop("activityChange")
            assert payload == original, code
        else:
            assert payload == baseline["parcel_regression"][code], code
        checked[code] = payload
    result["parcel_regression"] = checked
    for path, expected in [("/api/land/parcel?code=invalid",400), ("/api/land/parcel?code=99-999-9999-9999",404),
        ("/.env",404), (f"/server_data/review/{SLUG}/parcel_lookup.json",404),
        (f"/data/analysis/activity_change/{SLUG}/register.parquet",404), ("/wp_core/activity_change_area50.py",404)]:
        assert get(port,path)[0] == expected, path
    frontend = ROOT / "output" / f"frontend_{SLUG}"
    tiles = sorted((frontend / "data/activity_change" / SLUG).rglob("*.pbf"))
    for tile in tiles[::max(1,len(tiles)//12)]:
        status, body = get(port, "/" + tile.relative_to(frontend).as_posix())
        assert status == 200 and body == tile.read_bytes(), str(tile)
    html = get(port, "/")[1]
    assert html == (frontend / "index.html").read_bytes()
    result["html_sha256"] = digest(html)
    result["8525_html_sha256"] = digest(get(8525, "/")[1])
    if preview:
        assert get(8526, "/")[1] == (ROOT / "output/frontend_agent_review_20260906_v6/index.html").read_bytes()
    else:
        assert result["8525_html_sha256"] == baseline["8525_html_sha256"]
    result["passed"] = True
    (REVIEW / f"http_{phase}.json").write_text(json.dumps(result, indent=2),encoding="utf-8")
    print(json.dumps({"passed":True,"phase":phase,"unchanged_endpoints":len(endpoints),"parcels_checked":len(checked),"summary_bytes":result["summary_bytes"]}))


if __name__ == "__main__":
    main()
