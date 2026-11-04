"""Accuracy assessment for the AlphaEarth groups: stratified sample, blind review sheet, estimates.

  alphaearth_accuracy.py sample                 (product .venv) stratified random parcels + hidden key
  alphaearth_accuracy.py chips                  (GPU/Earth Engine env) May/August Sentinel-2 squares only
  alphaearth_accuracy.py sheet                  (product .venv) blind HTML review sheet with CSV export
  alphaearth_accuracy.py analyse ANSWERS.csv    (product .venv) area-weighted accuracy and area estimates
The sheet never shows the map group. Estimates follow Olofsson et al. (2014) with the ratio form for
parcels of unequal area (Stehman 2014). Only squares around centre points reach Earth Engine.
"""
from __future__ import annotations
import argparse
import base64
import csv
import html
import io
import json
import sys
import time
import urllib.request
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

CONFIG = json.loads((ROOT / "config/alphaearth_groups_20261030_v1.json").read_text())
YEAR = CONFIG["years_initial"][0]
PARCELS = ROOT / CONFIG["outputs"]["data"] / f"parcels_{YEAR}"
REVIEW = ROOT / CONFIG["outputs"]["review"]
OUT = REVIEW / "accuracy"
CHIPS = OUT / "chips"
OTHER, UNSURE = "other", "cannot_tell"


def labels():
    groups = json.loads((REVIEW / "group_labels.json").read_text(encoding="utf-8"))["groups"]
    if any(g["status"] != "owner_confirmed" for g in groups.values()):
        raise ValueError("All group names must be owner-confirmed before the accuracy check")
    return {int(k): v for k, v in groups.items()}


def sample():
    import geopandas as gpd
    import pandas as pd
    from wp_core.alphaearth import allocate_sample
    if (OUT / "sample_key.csv").exists():
        raise SystemExit("The sample already exists; it is never redrawn.")
    a = CONFIG["accuracy"]
    names = labels()
    table = pd.read_parquet(PARCELS / "parcels.parquet")
    k = len(names)
    stratum_area = table.groupby("group").area_official_m2.sum().reindex(range(k)).to_numpy()
    stratum_units = table.group.value_counts().reindex(range(k)).to_numpy()
    counts = allocate_sample(stratum_area, a["sample_size"], a["minimum_per_group"])
    rng = np.random.default_rng(a["seed"])
    picks = [table[table.group == g].sample(n=int(n), random_state=rng.integers(2**31)) for g, n in enumerate(counts)]
    chosen = pd.concat(picks).sample(frac=1, random_state=rng.integers(2**31)).reset_index(drop=True)
    chosen.insert(0, "sample_id", np.arange(1, len(chosen) + 1))
    basis = json.loads((ROOT / "data/observations/observations_2021_2025_v1/specification.json").read_text())["config"]["basis"]
    geometry = gpd.read_parquet(ROOT / basis).set_index("internal_parcel_id").loc[chosen.internal_parcel_id].to_crs(CONFIG["grid"]["crs"])
    w = a["window_m"]
    places = []
    for i, shape in enumerate(geometry.geometry):
        x0, y0, x1, y1 = shape.bounds
        half = min(max(max(x1 - x0, y1 - y0) * w["parcel_extent_multiple"], w["minimum"]), w["maximum"]) / 2
        part = shape if shape.geom_type == "Polygon" else max(shape.geoms, key=lambda s: s.area)
        cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
        places.append({"sample_id": int(chosen.sample_id[i]), "centre_utm": [round(cx, 1), round(cy, 1)], "half_m": round(half, 1),
                       "outline_utm": [[round(x, 1), round(y, 1)] for x, y in part.exterior.coords]})
    lonlat = geometry.geometry.centroid.to_crs(4326)
    OUT.mkdir(parents=True)
    key = chosen[["sample_id", "internal_parcel_id", "cadastre_code", "community", "area_official_m2", "group", "group_share"]]
    key.to_csv(OUT / "sample_key.csv", index=False)
    (OUT / "sample_places.json").write_text(json.dumps(places), encoding="utf-8")
    sheet = chosen[["sample_id", "cadastre_code", "community", "area_official_m2"]].copy()
    sheet["lon"], sheet["lat"] = lonlat.x.round(6).to_numpy(), lonlat.y.round(6).to_numpy()
    sheet.to_csv(OUT / "sheet_items.csv", index=False)
    design = {"version": CONFIG["version"], "year": YEAR, "design": a["design"], "seed": a["seed"],
              "strata": [{"group": g, "name_en": names[g]["name_en"], "mapped_official_m2": float(stratum_area[g]),
                          "parcels": int(stratum_units[g]), "sampled": int(counts[g])} for g in range(k)]}
    (OUT / "design.json").write_text(json.dumps(design, indent=2, ensure_ascii=False), encoding="utf-8")
    print(json.dumps(design, indent=1, ensure_ascii=False))


def chips():
    import ee
    ee.Initialize(project=CONFIG["earth_engine_project"])
    places = json.loads((OUT / "sample_places.json").read_text())
    CHIPS.mkdir(exist_ok=True)

    def composite(month):
        def clear(image):
            scl = image.select("SCL")
            return image.updateMask(scl.neq(3).And(scl.neq(8)).And(scl.neq(9)).And(scl.neq(10)))
        return (ee.ImageCollection("COPERNICUS/S2_SR_HARMONIZED").filterDate(f"{YEAR}-{month:02d}-01", f"{YEAR}-{month + 1:02d}-01")
                .filter(ee.Filter.lt("CLOUDY_PIXEL_PERCENTAGE", 40)).map(clear).median().select(["B4", "B3", "B2"]))
    images = {5: composite(5), 8: composite(8)}
    fetched = 0
    for n, place in enumerate(places, 1):
        (x, y), half = place["centre_utm"], place["half_m"]
        square = ee.Geometry.Rectangle([x - half, y - half, x + half, y + half], CONFIG["grid"]["crs"], False)
        for m, image in images.items():
            path = CHIPS / f"s{place['sample_id']:03d}_m{m:02d}.png"
            if path.exists():
                continue
            for attempt in range(4):
                try:
                    url = image.getThumbURL({"region": square, "dimensions": 192, "format": "png", "min": 200, "max": 2600,
                                             "gamma": 1.25, "crs": CONFIG["grid"]["crs"]})
                    with urllib.request.urlopen(url, timeout=60) as response:
                        path.write_bytes(response.read())
                    fetched += 1
                    break
                except Exception as exc:
                    if attempt == 3:
                        raise
                    print(f"retry {place['sample_id']} m{m}: {type(exc).__name__}", flush=True)
                    time.sleep(3 * (attempt + 1))
        if n % 50 == 0:
            print(f"  {n}/{len(places)} parcels", flush=True)
    print(f"chips fetched: {fetched}")


def outline_points(place, size):
    (x, y), half = place["centre_utm"], place["half_m"]
    return [((px - (x - half)) / (2 * half) * size, ((y + half) - py) / (2 * half) * size) for px, py in place["outline_utm"]]


def chip(path, place):
    from PIL import Image, ImageDraw
    image = Image.open(path).convert("RGB").resize((192, 192))
    points = outline_points(place, 192)
    ImageDraw.Draw(image).line(points + [points[0]], fill=(255, 214, 0), width=2)
    buffer = io.BytesIO()
    image.save(buffer, "JPEG", quality=78)
    return "data:image/jpeg;base64," + base64.b64encode(buffer.getvalue()).decode()


def sheet():
    names = labels()
    places = {p["sample_id"]: p for p in json.loads((OUT / "sample_places.json").read_text())}
    with open(OUT / "sheet_items.csv", encoding="utf-8") as stream:
        items = list(csv.DictReader(stream))
    options = "".join(f'<option value="{g}">{g + 1}. {html.escape(v["name_en"])} / {html.escape(v["name_hy"])}</option>'
                      for g, v in sorted(names.items()))
    options = ('<option value="">Choose…</option>' + options + f'<option value="{OTHER}">Other land (describe in the note)</option>'
               f'<option value="{UNSURE}">Cannot tell</option>')
    rows = []
    for item in items:
        sid = int(item["sample_id"])
        place = places[sid]
        (x, y), half = place["centre_utm"], place["half_m"]
        esri = ("https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/export?"
                f"bbox={x - half:.1f},{y - half:.1f},{x + half:.1f},{y + half:.1f}&bboxSR=32638&imageSR=32638"
                "&size=384,384&format=jpg&f=image")
        polygon = " ".join(f"{px:.1f},{py:.1f}" for px, py in outline_points(place, 100))
        lat, lon = item["lat"], item["lon"]
        rows.append(f"""
<article class="item" data-id="{sid}">
  <h2>Sample {sid}</h2>
  <p class="meta">{html.escape(item["community"])} · {float(item["area_official_m2"]) / 1e4:.2f} ha · {html.escape(item["cadastre_code"])}</p>
  <div class="imgs">
    <figure><div class="hr"><img loading="lazy" src="{esri}" alt="High-resolution image"><svg viewBox="0 0 100 100" preserveAspectRatio="none"><polygon points="{polygon}"/></svg></div><figcaption>High resolution (date varies)</figcaption></figure>
    <figure><img src="{chip(CHIPS / f's{sid:03d}_m05.png', place)}" alt="May {YEAR}"><figcaption>May {YEAR}</figcaption></figure>
    <figure><img src="{chip(CHIPS / f's{sid:03d}_m08.png', place)}" alt="August {YEAR}"><figcaption>August {YEAR}</figcaption></figure>
  </div>
  <p class="links"><a href="https://www.google.com/maps/@{lat},{lon},200m/data=!3m1!1e3" target="_blank" rel="noopener">Open in Google satellite</a>
     · window {2 * half:.0f} m</p>
  <label>What was this parcel in {YEAR}? <select data-field="reference">{options}</select></label>
  <label>Note (optional) <input data-field="note" maxlength="200" placeholder="e.g. vineyard, new houses"></label>
</article>""")
    legend = "".join(f"<li><b>{g + 1}. {html.escape(v['name_en'])}</b> / {html.escape(v['name_hy'])}</li>" for g, v in sorted(names.items()))
    page = f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Accuracy Check Sheet</title>
<style>
:root{{--bg:#f5f6f3;--card:#fff;--ink:#1d2a24;--muted:#5d6b64;--line:#dfe4df;--accent:#176b56}}
@media (prefers-color-scheme:dark){{:root:not([data-theme="light"]){{--bg:#141a17;--card:#1d2420;--ink:#e6ece8;--muted:#a3b0a9;--line:#2e3833;--accent:#5cc2a0}}}}
:root[data-theme="dark"]{{--bg:#141a17;--card:#1d2420;--ink:#e6ece8;--muted:#a3b0a9;--line:#2e3833;--accent:#5cc2a0}}
*{{box-sizing:border-box}}body{{margin:0;background:var(--bg);color:var(--ink);font:16px/1.5 system-ui,"Segoe UI",sans-serif}}
main{{max-width:980px;margin:0 auto;padding:24px 16px 96px}}h1{{margin:0 0 6px;font-size:26px}}.lead{{color:var(--muted)}}
.bar{{position:sticky;top:0;z-index:2;background:var(--bg);border-bottom:1px solid var(--line);padding:10px 0;display:flex;gap:12px;align-items:center;flex-wrap:wrap}}
button{{font:inherit;padding:8px 14px;border-radius:6px;border:0;background:var(--accent);color:#fff;cursor:pointer}}
.item{{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:16px;margin:16px 0}}
.item.done{{border-left:5px solid var(--accent)}}h2{{margin:0;font-size:18px}}.meta,.links{{margin:2px 0 10px;color:var(--muted);font-size:14px;overflow-wrap:anywhere}}
.imgs{{display:grid;grid-template-columns:repeat(3,1fr);gap:8px}}figure{{margin:0}}img{{width:100%;height:auto;display:block;border-radius:4px;aspect-ratio:1}}
.hr{{position:relative}}.hr svg{{position:absolute;inset:0;width:100%;height:100%}}.hr polygon{{fill:none;stroke:#ffd600;stroke-width:.6}}
figcaption{{font-size:12px;color:var(--muted)}}label{{display:block;margin:10px 0 0;font-size:14px}}
select,input{{display:block;width:100%;margin-top:4px;font:inherit;padding:8px;border-radius:6px;border:1px solid var(--line);background:var(--card);color:var(--ink)}}
a{{color:var(--accent)}}@media (max-width:620px){{.imgs{{grid-template-columns:1fr 1fr}}.imgs figure:first-child{{grid-column:1/-1}}}}
</style></head><body><main>
<h1>Accuracy check: AlphaEarth groups, {YEAR}</h1>
<p class="lead">{len(items)} parcels were drawn at random from every group. For each one, choose what the parcel really
was in {YEAR}, using the three images, the Google link and your knowledge of the place. The computer's answer is
deliberately hidden. Choose <i>Cannot tell</i> rather than guessing. Your answers are kept in this browser while you
work; when finished, press <b>Download answers</b> and send the file back.</p>
<ol class="lead">{legend}</ol>
<div class="bar"><span id="progress">0 / {len(items)} answered</span><button id="export">Download answers (CSV)</button></div>
{"".join(rows)}
<p class="lead">High-resolution images: Esri World Imagery. Sentinel-2: Copernicus. The AlphaEarth Foundations Satellite
Embedding dataset is produced by Google and Google DeepMind.</p>
</main>
<script>
const KEY = "alphaearth_accuracy_{CONFIG['version']}";
let saved = {{}};
try {{ saved = JSON.parse(localStorage.getItem(KEY) || "{{}}"); }} catch (e) {{ saved = {{}}; }}
const items = [...document.querySelectorAll(".item")];
function refresh() {{
  let done = 0;
  items.forEach(item => {{
    const ok = !!item.querySelector("select").value;
    item.classList.toggle("done", ok); if (ok) done++;
  }});
  document.getElementById("progress").textContent = done + " / " + items.length + " answered";
}}
items.forEach(item => {{
  const id = item.dataset.id, entry = saved[id] || {{}};
  item.querySelectorAll("[data-field]").forEach(field => {{
    if (entry[field.dataset.field]) field.value = entry[field.dataset.field];
    field.addEventListener("change", () => {{
      saved[id] = saved[id] || {{}}; saved[id][field.dataset.field] = field.value;
      try {{ localStorage.setItem(KEY, JSON.stringify(saved)); }} catch (e) {{}}
      refresh();
    }});
  }});
}});
refresh();
document.getElementById("export").addEventListener("click", () => {{
  const quote = v => '"' + String(v || "").replace(/"/g, '""') + '"';
  const lines = ["sample_id,reference,note"].concat(items.map(item => [item.dataset.id,
    item.querySelector("select").value, quote(item.querySelector("input").value)].join(",")));
  const link = document.createElement("a");
  link.href = URL.createObjectURL(new Blob([lines.join("\\n")], {{type: "text/csv"}}));
  link.download = "alphaearth_accuracy_answers.csv"; link.click();
}});
</script></body></html>"""
    (OUT / "review_sheet.html").write_text(page, encoding="utf-8")
    print(f"written {OUT / 'review_sheet.html'} ({len(page) / 1e6:.1f} MB)")


def analyse(answers_path):
    import pandas as pd
    from wp_core.alphaearth import area_accuracy
    names = labels()
    k = len(names)
    design = json.loads((OUT / "design.json").read_text(encoding="utf-8"))
    key = pd.read_csv(OUT / "sample_key.csv", dtype={"cadastre_code": str})
    answers = pd.read_csv(answers_path, dtype={"reference": str, "note": str})
    merged = key.merge(answers[["sample_id", "reference", "note"]], on="sample_id", how="left", validate="one_to_one")
    missing = merged.reference.isna() | (merged.reference == "")
    if missing.any():
        raise SystemExit(f"{int(missing.sum())} samples have no answer yet.")
    unsure = merged.reference == UNSURE
    assessed = merged[~unsure]
    reference = assessed.reference.map(lambda v: k if v == OTHER else int(v)).to_numpy()
    classes = list(range(k + 1))
    area_m2 = np.array([s["mapped_official_m2"] for s in design["strata"]])
    units = np.array([s["parcels"] for s in design["strata"]])
    r = area_accuracy(assessed.group.to_numpy(), reference, assessed.area_official_m2.to_numpy(), area_m2, units, classes)
    total_ha = area_m2.sum() / 1e4
    label = {**{g: names[g]["name_en"] for g in range(k)}, k: "Other land"}
    result = {"version": CONFIG["version"], "year": YEAR, "answers_file": Path(answers_path).name,
              "assessed": int(len(assessed)), "cannot_tell": int(unsure.sum()),
              "overall_accuracy": round(r["overall"], 4), "overall_ci95": round(1.96 * r["overall_se"], 4),
              "classes": [{"class": c, "name": label[c],
                           "mapped_ha": round(float(area_m2[c] / 1e4), 2) if c < k else 0.0,
                           "estimated_ha": round(float(r["proportion"][c] * total_ha), 2),
                           "estimated_ha_ci95": round(float(1.96 * r["proportion_se"][c] * total_ha), 2),
                           "users_accuracy": round(float(r["users"][c]), 4) if c < k else None,
                           "users_ci95": round(float(1.96 * r["users_se"][c]), 4) if c < k else None,
                           "producers_accuracy": None if c >= k or np.isnan(r["producers"][c]) else round(float(r["producers"][c]), 4)}
                          for c in classes],
              "area_proportion_matrix": np.round(r["matrix"], 5).tolist(),
              "caveats": ["Reference labels come from image interpretation and local knowledge, not field visits.",
                          "'Cannot tell' samples are excluded, which can bias estimates if they differ from the rest.",
                          "Producer's accuracy is a point estimate without a confidence interval."]}
    (OUT / "accuracy_results.json").write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")
    print(json.dumps(result, indent=1, ensure_ascii=False))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=["sample", "chips", "sheet", "analyse"])
    parser.add_argument("answers", nargs="?")
    args = parser.parse_args()
    if args.action == "analyse" and not args.answers:
        raise SystemExit("Give the answers CSV exported from the review sheet.")
    {"sample": sample, "chips": chips, "sheet": sheet}.get(args.action, lambda: analyse(args.answers))()
