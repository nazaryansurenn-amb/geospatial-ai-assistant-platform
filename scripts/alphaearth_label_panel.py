"""Vineyard / orchard verification panel: training labels for the AlphaEarth perennial classifier.

  alphaearth_label_panel.py select   (product .venv) ~200 parcels from five strata, kept apart from the accuracy sample
  alphaearth_label_panel.py chips    (GPU/Earth Engine env) May/August 2025 Sentinel-2 squares only
  alphaearth_label_panel.py build    (product .venv) one-parcel-at-a-time panel
  alphaearth_label_panel.py serve    (product .venv) loopback server on 8546 that saves each answer to answers.json
These labels train the classifier; they are never used to measure accuracy (that is the separate blind sheet).
"""
from __future__ import annotations
import base64
import html
import io
import json
import sys
import time
import urllib.request
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

CONFIG = json.loads((ROOT / "config/alphaearth_groups_20261030_v1.json").read_text())
DATA = ROOT / CONFIG["outputs"]["data"]
OUT = ROOT / CONFIG["outputs"]["review"] / "labels"
CHIPS = OUT / "chips"
PORT = 8546
# Land types an answer can combine: the first chosen is the main use, the others are also present.
TYPES = {"vineyard": "Խաղողի այգի · Vineyard", "orchard": "Պտղատու այգի · Orchard",
         "annual": "Միամյա մշակաբույսեր · Annual crops", "alfalfa": "Առվույտ, բազմամյա խոտ · Alfalfa / perennial grass",
         "bare": "Մերկ հող, խոպան · Bare soil / fallow", "grass": "Խոտածածկ, արոտ, մոլախոտ · Grass / pasture / weeds",
         "built": "Շինություններ, ջերմոցներ, բակեր · Buildings / greenhouses / yards",
         "trees": "Ոչ պտղատու ծառաշարեր · Non-fruit tree rows / windbreak", "water": "Ջուր, խոնավ հող · Water / wet ground"}
UNSURE = "cannot_tell"
STRATA = {"group6_today_perennial": 40, "group0": 40, "stable_not_group0": 40, "group6_other": 40, "random": 40}
SEED = 20261101
MIN_HA = .1


def select():
    import geopandas as gpd
    import numpy as np
    import pandas as pd
    if (OUT / "panel_key.csv").exists():
        raise SystemExit("The panel sample already exists; it is never redrawn.")
    stability = pd.read_parquet(DATA / "stability/parcels.parquet")
    accuracy = set(pd.read_csv(ROOT / CONFIG["outputs"]["review"] / "accuracy/sample_key.csv").internal_parcel_id)
    pool = stability[~stability.internal_parcel_id.isin(accuracy) & stability.has_whole_pixel
                     & (stability.area_official_m2 >= MIN_HA * 1e4)].copy()
    stable = pool.stability_mean >= pool.stability_mean.quantile(.75)
    masks = {"group6_today_perennial": (pool.group == 6) & (pool.crop_type == "perennial"),
             "group0": pool.group == 0,
             "stable_not_group0": stable & (pool.group != 0),
             "group6_other": (pool.group == 6) & (pool.crop_type != "perennial"),
             "random": pd.Series(True, index=pool.index)}
    rng = np.random.default_rng(SEED)
    taken, picks = set(), []
    for name, n in STRATA.items():
        candidates = pool[masks[name] & ~pool.internal_parcel_id.isin(taken)]
        chosen = candidates.sample(n=min(n, len(candidates)), random_state=rng.integers(2**31))
        taken.update(chosen.internal_parcel_id)
        picks.append(chosen.assign(stratum=name))
    panel = pd.concat(picks).sample(frac=1, random_state=rng.integers(2**31)).reset_index(drop=True)
    panel.insert(0, "panel_id", np.arange(1, len(panel) + 1))
    basis = json.loads((ROOT / "data/observations/observations_2021_2025_v1/specification.json").read_text())["config"]["basis"]
    geometry = gpd.read_parquet(ROOT / basis).set_index("internal_parcel_id").loc[panel.internal_parcel_id].to_crs(CONFIG["grid"]["crs"])
    lonlat = geometry.geometry.centroid.to_crs(4326)
    places = []
    for i, shape in enumerate(geometry.geometry):
        x0, y0, x1, y1 = shape.bounds
        half = min(max(max(x1 - x0, y1 - y0) * 1.6, 120), 600) / 2
        part = shape if shape.geom_type == "Polygon" else max(shape.geoms, key=lambda s: s.area)
        places.append({"panel_id": int(panel.panel_id[i]), "centre_utm": [round((x0 + x1) / 2, 1), round((y0 + y1) / 2, 1)],
                       "half_m": round(half, 1), "outline_utm": [[round(x, 1), round(y, 1)] for x, y in part.exterior.coords],
                       "lonlat": [round(lonlat.iloc[i].x, 6), round(lonlat.iloc[i].y, 6)]})
    OUT.mkdir(parents=True)
    panel[["panel_id", "internal_parcel_id", "cadastre_code", "area_official_m2", "group", "crop_type", "stability_mean", "stratum"]].to_csv(
        OUT / "panel_key.csv", index=False)
    (OUT / "panel_places.json").write_text(json.dumps(places), encoding="utf-8")
    print(panel.stratum.value_counts().to_string())


def chips():
    import ee
    ee.Initialize(project=CONFIG["earth_engine_project"])
    places = json.loads((OUT / "panel_places.json").read_text())
    CHIPS.mkdir(exist_ok=True)

    def composite(month):
        def clear(image):
            scl = image.select("SCL")
            return image.updateMask(scl.neq(3).And(scl.neq(8)).And(scl.neq(9)).And(scl.neq(10)))
        return (ee.ImageCollection("COPERNICUS/S2_SR_HARMONIZED").filterDate(f"2025-{month:02d}-01", f"2025-{month + 1:02d}-01")
                .filter(ee.Filter.lt("CLOUDY_PIXEL_PERCENTAGE", 40)).map(clear).median().select(["B4", "B3", "B2"]))
    images = {5: composite(5), 8: composite(8)}
    for place in places:
        (x, y), half = place["centre_utm"], place["half_m"]
        square = ee.Geometry.Rectangle([x - half, y - half, x + half, y + half], CONFIG["grid"]["crs"], False)
        for m, image in images.items():
            path = CHIPS / f"p{place['panel_id']:03d}_m{m:02d}.png"
            if path.exists():
                continue
            for attempt in range(4):
                try:
                    url = image.getThumbURL({"region": square, "dimensions": 192, "format": "png", "min": 200, "max": 2600,
                                             "gamma": 1.25, "crs": CONFIG["grid"]["crs"]})
                    with urllib.request.urlopen(url, timeout=60) as response:
                        path.write_bytes(response.read())
                    break
                except Exception as exc:
                    if attempt == 3:
                        raise
                    print(f"retry {place['panel_id']} m{m}: {type(exc).__name__}", flush=True)
                    time.sleep(3 * (attempt + 1))
    print(f"chips: {len(list(CHIPS.glob('*.png')))}")


def outline(place, size):
    (x, y), half = place["centre_utm"], place["half_m"]
    return [((px - (x - half)) / (2 * half) * size, ((y + half) - py) / (2 * half) * size) for px, py in place["outline_utm"]]


def chip(path, place):
    from PIL import Image, ImageDraw
    image = Image.open(path).convert("RGB").resize((192, 192))
    points = outline(place, 192)
    ImageDraw.Draw(image).line(points + [points[0]], fill=(255, 214, 0), width=2)
    buffer = io.BytesIO()
    image.save(buffer, "JPEG", quality=80)
    return "data:image/jpeg;base64," + base64.b64encode(buffer.getvalue()).decode()


def build():
    items = []
    for place in json.loads((OUT / "panel_places.json").read_text()):
        pid, ((x, y), half) = place["panel_id"], (place["centre_utm"], place["half_m"])
        items.append({"id": pid, "lon": place["lonlat"][0], "lat": place["lonlat"][1],
                      "esri": ("https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/export?"
                               f"bbox={x - half:.1f},{y - half:.1f},{x + half:.1f},{y + half:.1f}&bboxSR=32638&imageSR=32638"
                               "&size=640,640&format=jpg&f=image"),
                      "outline": " ".join(f"{a:.1f},{b:.1f}" for a, b in outline(place, 100)),
                      "may": chip(CHIPS / f"p{pid:03d}_m05.png", place), "aug": chip(CHIPS / f"p{pid:03d}_m08.png", place),
                      "width_m": round(2 * half)})
    buttons = "".join(f'<button type="button" data-type="{key}" aria-pressed="false"><kbd>{i}</kbd>{html.escape(text)}<span class="tag"></span></button>'
                      for i, (key, text) in enumerate(TYPES.items(), 1))
    page = f"""<!doctype html><html lang="hy"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Vineyard Check Panel</title>
<style>
:root{{--bg:#f5f6f3;--card:#fff;--ink:#1d2a24;--muted:#5d6b64;--line:#dfe4df;--accent:#176b56;--pick:#d9f1e6}}
@media (prefers-color-scheme:dark){{:root:not([data-theme="light"]){{--bg:#141a17;--card:#1d2420;--ink:#e6ece8;--muted:#a3b0a9;--line:#2e3833;--accent:#5cc2a0;--pick:#1f3b30}}}}
:root[data-theme="dark"]{{--bg:#141a17;--card:#1d2420;--ink:#e6ece8;--muted:#a3b0a9;--line:#2e3833;--accent:#5cc2a0;--pick:#1f3b30}}
*{{box-sizing:border-box}}body{{margin:0;background:var(--bg);color:var(--ink);font:16px/1.45 system-ui,"Segoe UI",sans-serif}}
main{{max-width:1040px;margin:0 auto;padding:16px}}header{{display:flex;flex-wrap:wrap;gap:8px 16px;align-items:baseline;justify-content:space-between}}
h1{{font-size:22px;margin:0}}.muted{{color:var(--muted);font-size:14px}}
.stage{{display:grid;grid-template-columns:minmax(0,2fr) minmax(0,1fr);gap:12px;margin-top:12px}}
.big{{position:relative;background:var(--card);border:1px solid var(--line);border-radius:8px;overflow:hidden;aspect-ratio:1}}
.big img,.big svg{{position:absolute;inset:0;width:100%;height:100%}}.big polygon{{fill:none;stroke:#ffd600;stroke-width:.5}}
.side{{display:grid;gap:8px;align-content:start}}figure{{margin:0}}figure img{{width:100%;display:block;border-radius:6px;aspect-ratio:1}}
figcaption{{font-size:13px;color:var(--muted)}}.answers{{display:grid;grid-template-columns:repeat(3,1fr);gap:8px;margin-top:12px}}
.answers button{{font:inherit;font-size:14px;padding:10px 8px;border-radius:8px;border:1px solid var(--line);background:var(--card);color:var(--ink);cursor:pointer;min-height:56px;text-align:left;overflow-wrap:anywhere}}
.answers button.on{{background:var(--pick);border-color:var(--accent);font-weight:600}}.tag{{display:block;font-size:12px;color:var(--accent)}}
.choice{{margin:10px 0 0;font-weight:600}}.nav .primary{{background:var(--accent);color:#fff;border-color:var(--accent)}}kbd{{display:inline-block;margin-right:6px;padding:0 6px;border:1px solid var(--line);border-radius:4px;font-size:12px}}
.nav{{display:flex;gap:8px;align-items:center;margin-top:10px;flex-wrap:wrap}}.nav button{{font:inherit;padding:8px 12px;border-radius:6px;border:1px solid var(--line);background:var(--card);color:var(--ink);cursor:pointer}}
a{{color:var(--accent)}}#saved{{font-size:14px}}@media (max-width:720px){{.stage{{grid-template-columns:1fr}}.side{{grid-template-columns:1fr 1fr}}.answers{{grid-template-columns:1fr}}}}
</style></head><body><main>
<header><h1>What is inside this parcel?</h1><span class="muted" id="progress"></span></header>
<p class="muted">Look at the parcel inside the yellow outline. <b>Click the main land use first</b> (the one covering the most
area), then any others that are also present, then <b>Save</b> (Enter). One choice = a pure parcel; two or more = a mixed
parcel. Keys 1–9 choose, Backspace clears, 0 = can't tell. Vineyards show regular parallel rows in the large photo
(its date varies; the small 2025 images show the current season). Each answer is saved immediately.</p>
<div class="stage"><div class="big"><img id="esri" alt="High-resolution image of the parcel"><svg viewBox="0 0 100 100" preserveAspectRatio="none"><polygon id="shape"/></svg></div>
<div class="side"><figure><img id="may" alt="May 2025"><figcaption>May 2025</figcaption></figure>
<figure><img id="aug" alt="August 2025"><figcaption>August 2025</figcaption></figure></div></div>
<div class="answers">{buttons}</div>
<p id="choice" class="choice" role="status">Nothing chosen yet.</p>
<div class="nav"><button type="button" id="save" class="primary">Save <kbd>Enter</kbd></button>
<button type="button" id="clear">Clear <kbd>⌫</kbd></button><button type="button" id="unsure">Can't tell <kbd>0</kbd></button>
<button type="button" id="prev">← Back</button><button type="button" id="next">Skip →</button>
<button type="button" id="todo">Next unanswered</button><a id="google" target="_blank" rel="noopener">Open in Google satellite</a>
<span class="muted" id="meta"></span><span id="saved" role="status"></span></div>
<p class="muted">High-resolution images: Esri World Imagery. Sentinel-2: Copernicus.</p>
</main><script>
const ITEMS = {json.dumps(items)};
const NAMES = {json.dumps(TYPES, ensure_ascii=False)}, UNSURE = "{UNSURE}";
const ORDER = Object.keys(NAMES);
let answers = {{}}, i = 0, picked = [];
const $ = id => document.getElementById(id);
const short = key => NAMES[key].split(" · ")[1];
function describe(a) {{
  if (!a) return "";
  if (a.main === UNSURE) return "Can't tell";
  return a.also.length ? `Mixed: ${{short(a.main)}} (main) + ${{a.also.map(short).join(" + ")}}` : `Pure: ${{short(a.main)}}`;
}}
function paint() {{
  document.querySelectorAll(".answers button").forEach(b => {{
    const at = picked.indexOf(b.dataset.type);
    b.classList.toggle("on", at >= 0); b.setAttribute("aria-pressed", at >= 0);
    b.querySelector(".tag").textContent = at === 0 ? "main" : at > 0 ? "also present" : "";
  }});
  $("choice").textContent = picked.length ? describe({{main: picked[0], also: picked.slice(1)}}) + " — press Save" : "Nothing chosen yet.";
}}
function show() {{
  const it = ITEMS[i], saved = answers[it.id];
  $("esri").src = it.esri; $("shape").setAttribute("points", it.outline); $("may").src = it.may; $("aug").src = it.aug;
  $("google").href = `https://www.google.com/maps/@${{it.lat}},${{it.lon}},180m/data=!3m1!1e3`;
  $("meta").textContent = `Parcel ${{it.id}} · window ${{it.width_m}} m` + (saved ? ` · saved: ${{describe(saved)}}` : "");
  picked = saved && saved.main !== UNSURE ? [saved.main, ...saved.also] : [];
  paint();
  $("progress").textContent = `${{Object.keys(answers).length}} / ${{ITEMS.length}} answered · showing ${{i + 1}}`;
  if (ITEMS[i + 1]) new Image().src = ITEMS[i + 1].esri;
}}
function toggle(key) {{ picked = picked.includes(key) ? picked.filter(k => k !== key) : [...picked, key]; paint(); }}
async function save(main, also) {{
  const it = ITEMS[i];
  try {{
    const r = await fetch("/api/answer", {{method: "POST", headers: {{"Content-Type": "application/json"}}, body: JSON.stringify({{panel_id: it.id, main, also}})}});
    if (!r.ok) throw new Error(await r.text());
    answers[it.id] = {{main, also}}; $("saved").textContent = "Saved";
    if (i < ITEMS.length - 1) i++;
    show();
  }} catch (e) {{ $("saved").textContent = "Not saved: is the panel server running?"; }}
}}
document.querySelectorAll(".answers button").forEach(b => b.addEventListener("click", () => toggle(b.dataset.type)));
$("save").onclick = () => {{ if (picked.length) save(picked[0], picked.slice(1)); else $("choice").textContent = "Choose at least one land use, or Can't tell."; }};
$("clear").onclick = () => {{ picked = []; paint(); }};
$("unsure").onclick = () => save(UNSURE, []);
$("prev").onclick = () => {{ if (i > 0) i--; show(); }};
$("next").onclick = () => {{ if (i < ITEMS.length - 1) i++; show(); }};
$("todo").onclick = () => {{ const j = ITEMS.findIndex(it => !answers[it.id]); if (j >= 0) {{ i = j; show(); }} }};
document.addEventListener("keydown", e => {{
  if (e.target.tagName === "BUTTON" && e.key === "Enter") return;
  const n = Number(e.key);
  if (e.key >= "1" && e.key <= "9" && ORDER[n - 1]) toggle(ORDER[n - 1]);
  else if (e.key === "0") $("unsure").click();
  else if (e.key === "Enter") $("save").click();
  else if (e.key === "Backspace") {{ e.preventDefault(); $("clear").click(); }}
  else if (e.key === "ArrowLeft") $("prev").click(); else if (e.key === "ArrowRight") $("next").click();
}});
fetch("/api/answers").then(r => r.json()).then(saved => {{ answers = saved; const j = ITEMS.findIndex(it => !answers[it.id]); i = j >= 0 ? j : 0; show(); }})
  .catch(() => show());
</script></body></html>"""
    (OUT / "panel.html").write_text(page, encoding="utf-8")
    print(f"written {OUT / 'panel.html'} ({len(page) / 1e6:.1f} MB, {len(items)} parcels)")


def valid_answer(main, also):
    """main: one land type (or can't tell); also: other distinct types present, in the order chosen."""
    if not isinstance(also, list) or len(also) > len(TYPES) - 1 or len(set(also)) != len(also):
        return False
    if main == UNSURE:
        return also == []
    return main in TYPES and all(t in TYPES and t != main for t in also)


def answer_kind(main, also):
    return "cannot_tell" if main == UNSURE else ("mixed" if also else "pure")


def serve():
    page = (OUT / "panel.html").read_bytes()
    valid_ids = {p["panel_id"] for p in json.loads((OUT / "panel_places.json").read_text())}
    store = OUT / "answers.json"

    class Handler(BaseHTTPRequestHandler):
        def _reply(self, status, body, kind="application/json; charset=utf-8"):
            self.send_response(status)
            self.send_header("Content-Type", kind)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            self.path = self.path.split("?", 1)[0]
            if self.path in ("/", "/panel.html"):
                self._reply(200, page, "text/html; charset=utf-8")
            elif self.path == "/api/answers":
                saved = json.loads(store.read_text()) if store.exists() else {}
                self._reply(200, json.dumps({k: {"main": v["main"], "also": v["also"]} for k, v in saved.items()}).encode())
            else:
                self._reply(404, b"{}")

        def do_POST(self):
            if self.path != "/api/answer" or self.headers.get("Host") != f"127.0.0.1:{PORT}" or \
                    self.headers.get("Origin") not in (None, f"http://127.0.0.1:{PORT}"):
                self._reply(403, b"{}")
                return
            size = int(self.headers.get("Content-Length", "0"))
            body = json.loads(self.rfile.read(min(size, 1000)) or b"{}")
            main, also = body.get("main"), body.get("also")
            if body.get("panel_id") not in valid_ids or not valid_answer(main, also):
                self._reply(400, b'{"error":"invalid answer"}')
                return
            saved = json.loads(store.read_text()) if store.exists() else {}
            saved[str(body["panel_id"])] = {"main": main, "also": also, "kind": answer_kind(main, also),
                                            "at": datetime.now(timezone.utc).isoformat()}
            temporary = store.with_suffix(".tmp")
            temporary.write_text(json.dumps(saved, indent=1))
            temporary.replace(store)
            self._reply(200, b'{"ok":true}')

        def log_message(self, *args):
            pass

    print(f"Vineyard panel: http://127.0.0.1:{PORT}/", flush=True)
    ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()


if __name__ == "__main__":
    action = sys.argv[1] if len(sys.argv) > 1 else ""
    {"select": select, "chips": chips, "build": build, "serve": serve}.get(action, lambda: sys.exit(__doc__))()
