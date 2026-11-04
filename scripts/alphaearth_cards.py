"""Group cards for naming the AlphaEarth groups.

  alphaearth_cards.py chips   (GPU/Earth Engine env) Sentinel-2 true-colour chips, May and August, 640 m squares.
                              Only a square around each example's centre point is sent; no codes or outlines.
  alphaearth_cards.py build   (product .venv) one self-contained HTML page; outlines are drawn locally.
"""
from __future__ import annotations
import argparse
import base64
import html
import io
import json
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CONFIG = json.loads((ROOT / "config/alphaearth_groups_20261030_v1.json").read_text())
YEAR = CONFIG["years_initial"][0]
PARCELS = ROOT / CONFIG["outputs"]["data"] / f"parcels_{YEAR}"
REVIEW = ROOT / CONFIG["outputs"]["review"]
CHIPS = REVIEW / "chips"
HALF = 320
MONTHS = {5: "May", 8: "August"}
PALETTE = ["#2f7d4f", "#d9a21b", "#9bb84a", "#a08c78", "#3d7fc1", "#7a5c99", "#d0603a"]


def chips():
    import ee
    ee.Initialize(project=CONFIG["earth_engine_project"])
    examples = json.loads((PARCELS / "examples.json").read_text(encoding="utf-8"))
    CHIPS.mkdir(parents=True, exist_ok=True)

    def composite(month):
        def clear(image):
            scl = image.select("SCL")
            return image.updateMask(scl.neq(3).And(scl.neq(8)).And(scl.neq(9)).And(scl.neq(10)))
        return (ee.ImageCollection("COPERNICUS/S2_SR_HARMONIZED")
                .filterDate(f"{YEAR}-{month:02d}-01", f"{YEAR}-{month + 1:02d}-01")
                .filter(ee.Filter.lt("CLOUDY_PIXEL_PERCENTAGE", 40)).map(clear).median().select(["B4", "B3", "B2"]))

    images = {m: composite(m) for m in MONTHS}
    fetched = 0
    for g, spots in examples.items():
        for i, spot in enumerate(spots):
            x, y = spot["centre_utm"]
            square = ee.Geometry.Rectangle([x - HALF, y - HALF, x + HALF, y + HALF], CONFIG["grid"]["crs"], False)
            for m, image in images.items():
                path = CHIPS / f"g{int(g)}_e{i}_m{m:02d}.png"
                if path.exists():
                    continue
                for attempt in range(4):
                    try:
                        url = image.getThumbURL({"region": square, "dimensions": 256, "format": "png", "min": 200,
                                                 "max": 2600, "gamma": 1.25, "crs": CONFIG["grid"]["crs"]})
                        with urllib.request.urlopen(url, timeout=60) as response:
                            path.write_bytes(response.read())
                        fetched += 1
                        break
                    except Exception as exc:
                        if attempt == 3:
                            raise
                        print(f"retry g{g} e{i} m{m}: {type(exc).__name__}", flush=True)
                        time.sleep(3 * (attempt + 1))
        print(f"group {g}: done", flush=True)
    print(f"chips fetched: {fetched}")


def chip(path, spot):
    from PIL import Image, ImageDraw
    image = Image.open(path).convert("RGB").resize((256, 256))
    x0, y1 = spot["centre_utm"][0] - HALF, spot["centre_utm"][1] + HALF
    outline = [((x - x0) / (2 * HALF) * 256, (y1 - y) / (2 * HALF) * 256) for x, y in spot["outline_utm"]]
    ImageDraw.Draw(image).line(outline + [outline[0]], fill=(255, 214, 0), width=2)
    buffer = io.BytesIO()
    image.save(buffer, "JPEG", quality=82)
    return "data:image/jpeg;base64," + base64.b64encode(buffer.getvalue()).decode()


def curve(groups, gid, key, low, high):
    w, h, left, right, top, bottom = 320, 150, 34, 8, 10, 24
    x = lambda i: left + i * (w - left - right) / 11
    y = lambda v: top + (high - v) / (high - low) * (h - top - bottom)
    parts = [f'<svg viewBox="0 0 {w} {h}" class="curve" role="img" aria-label="Monthly {key} for this group and others">']
    step = (high - low) / 4
    for n in range(5):
        v = low + n * step
        parts.append(f'<line x1="{left}" x2="{w - right}" y1="{y(v):.1f}" y2="{y(v):.1f}" class="grid"/>'
                     f'<text x="{left - 5}" y="{y(v) + 4:.1f}" class="ax" text-anchor="end">{v:.1f}</text>')
    for i, m in enumerate("JFMAMJJASOND"):
        parts.append(f'<text x="{x(i):.1f}" y="{h - 7}" class="ax" text-anchor="middle">{m}</text>')

    def line(values, cls, style=""):
        points = " ".join(f"{x(i):.1f},{y(v):.1f}" for i, v in enumerate(values) if v is not None)
        return f'<polyline points="{points}" class="{cls}" style="{style}"/>'
    for g in groups:
        if g["group"] != gid and g[key]:
            parts.append(line(g[key], "other"))
    me = next(g for g in groups if g["group"] == gid)
    parts.append(line(me[key], "me", f"stroke:{PALETTE[gid]}"))
    parts.append("</svg>")
    return "".join(parts)


def bars(counts, labels):
    total = sum(counts.values()) or 1
    rows = []
    for key, label in labels.items():
        n = counts.get(key, 0)
        rows.append(f'<div class="bar"><span>{html.escape(label)}</span><i style="width:{100 * n / total:.1f}%"></i>'
                    f'<b>{100 * n / total:.0f}%</b></div>')
    return "".join(rows)


CROP = {"annual": "Annual (today's map)", "perennial": "Perennial (today's map)", "undetermined": "Undetermined (today's map)"}
ACTIVITY = {"active": "Active in 2026", "partial": "Partly active in 2026", "no_current_activity": "No activity seen in 2026"}
CONFIDENCE = {"high": "High", "medium": "Medium", "low": "Low"}


def build():
    summary = json.loads((PARCELS / "summary.json").read_text(encoding="utf-8"))
    examples = json.loads((PARCELS / "examples.json").read_text(encoding="utf-8"))
    suggestions = json.loads((REVIEW / "suggestions.json").read_text(encoding="utf-8"))
    groups = summary["groups"]
    cards = []
    for g in sorted(groups, key=lambda item: -item["official_ha"]):
        gid = g["group"]
        s = suggestions[str(gid)]
        spots = []
        for i, spot in enumerate(examples[str(gid)]):
            lat, lon = spot["centre_lonlat"][1], spot["centre_lonlat"][0]
            pictures = "".join(f'<figure><img src="{chip(CHIPS / f"g{gid}_e{i}_m{m:02d}.png", spot)}" alt="{name} {YEAR}">'
                               f'<figcaption>{name}</figcaption></figure>' for m, name in MONTHS.items())
            spots.append(f'<div class="spot"><div class="pair">{pictures}</div><p>{html.escape(spot["community"])} · '
                         f'{spot["official_ha"]:.2f} ha · <a href="https://www.google.com/maps/@{lat},{lon},250m/data=!3m1!1e3" '
                         f'target="_blank" rel="noopener">high-resolution view</a></p></div>')
        question = ""
        if s.get("question"):
            options = "".join(f"<li>{html.escape(o)}</li>" for o in s["question"]["options"])
            question = f'<div class="ask"><b>Question for you:</b> {html.escape(s["question"]["text"])}<ol type="A">{options}</ol></div>'
        communities = ", ".join(f"{html.escape(k)} ({v})" for k, v in g["communities_top"].items())
        cards.append(f"""
<section class="card" id="g{gid}">
  <header><span class="sw" style="background:{PALETTE[gid]}"></span>
    <div><p class="gid">Group {gid}</p><h2>{html.escape(s["name_hy"])}</h2><p class="en">{html.escape(s["name_en"])}</p></div>
    <span class="conf c-{s["confidence"]}">{CONFIDENCE[s["confidence"]]} confidence</span></header>
  <p class="why">{html.escape(s["reason"])}</p>
  <div class="facts">
    <div><b>{g["parcels"]:,}</b><span>parcels</span></div>
    <div><b>{g["official_ha"]:,.0f}</b><span>official ha</span></div>
    <div><b>{g["median_parcel_ha"]:.2f}</b><span>median parcel ha</span></div>
    <div><b>{g["parcels_mixed"]:,}</b><span>mixed parcels</span></div>
  </div>
  <div class="cols">
    <div><h3>Greenness through {YEAR} (NDVI)</h3>{curve(groups, gid, "ndvi_monthly", 0, .8)}
         <h3>Moisture through {YEAR} (NDMI)</h3>{curve(groups, gid, "ndmi_monthly", -.2, .6)}</div>
    <div><h3>Same parcels in today's map</h3>{bars(g["compare"]["crop_type"], CROP)}
         <h3>Activity seen in 2026</h3>{bars(g["compare"]["activity_class"], ACTIVITY)}
         <h3>Most parcels in</h3><p class="small">{communities}</p></div>
  </div>
  {question}
  <h3>Example parcels (yellow outline), {YEAR}</h3>
  <div class="spots">{"".join(spots)}</div>
</section>""")
    total_ha = sum(g["official_ha"] for g in groups)
    legend = "".join(f'<a href="#g{g["group"]}"><span class="sw" style="background:{PALETTE[g["group"]]}"></span>'
                     f'{html.escape(suggestions[str(g["group"])]["name_en"])} · {g["official_ha"]:,.0f} ha</a>'
                     for g in sorted(groups, key=lambda item: -item["official_ha"]))
    page = f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>AlphaEarth Group Cards</title>
<style>
:root{{--bg:#f5f6f3;--card:#fff;--ink:#1d2a24;--muted:#5d6b64;--line:#dfe4df;--other:#c9d0cb;--accent:#176b56}}
@media (prefers-color-scheme:dark){{:root:not([data-theme="light"]){{--bg:#141a17;--card:#1d2420;--ink:#e6ece8;--muted:#a3b0a9;--line:#2e3833;--other:#46524b;--accent:#5cc2a0}}}}
:root[data-theme="dark"]{{--bg:#141a17;--card:#1d2420;--ink:#e6ece8;--muted:#a3b0a9;--line:#2e3833;--other:#46524b;--accent:#5cc2a0}}
*{{box-sizing:border-box}}body{{margin:0;background:var(--bg);color:var(--ink);font:16px/1.5 system-ui,"Segoe UI",sans-serif}}
main{{max-width:1080px;margin:0 auto;padding:24px 16px 64px}}h1{{font-size:28px;margin:0 0 4px}}
.lead{{color:var(--muted);max-width:760px}}.legend{{display:flex;flex-wrap:wrap;gap:8px;margin:16px 0 24px}}
.legend a{{display:flex;align-items:center;gap:6px;padding:6px 10px;border:1px solid var(--line);border-radius:999px;color:inherit;text-decoration:none;background:var(--card);font-size:14px}}
.sw{{display:inline-block;width:14px;height:14px;border-radius:3px;flex:none}}
.card{{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:20px;margin:0 0 20px}}
.card header{{display:flex;flex-wrap:wrap;gap:12px;align-items:flex-start}}.card header .sw{{width:22px;height:22px;margin-top:6px}}
.card header div{{flex:1;min-width:0}}h2{{overflow-wrap:anywhere}}.gid{{margin:0;color:var(--muted);font-size:13px}}h2{{margin:0;font-size:22px}}.en{{margin:0;color:var(--muted)}}
.conf{{font-size:13px;padding:3px 9px;border-radius:999px;white-space:nowrap}}.c-high{{background:#d5efe2;color:#14563f}}
.c-medium{{background:#f6ead0;color:#6b4b07}}.c-low{{background:#f6dcd6;color:#7a2b1c}}
.why{{margin:12px 0}}.facts{{display:grid;grid-template-columns:repeat(4,1fr);gap:8px;margin:12px 0}}
.facts div{{border:1px solid var(--line);border-radius:8px;padding:8px}}.facts b{{display:block;font-size:20px}}.facts span{{color:var(--muted);font-size:13px}}
.cols{{display:grid;grid-template-columns:1fr 1fr;gap:20px}}h3{{font-size:14px;margin:14px 0 6px;color:var(--muted);font-weight:600}}
.curve{{width:100%;height:auto}}.curve .grid{{stroke:var(--line)}}.curve .ax{{fill:var(--muted);font-size:10px}}
.curve .other{{fill:none;stroke:var(--other);stroke-width:1.2}}.curve .me{{fill:none;stroke-width:3}}
.bar{{display:grid;grid-template-columns:minmax(0,1.6fr) 1fr 40px;align-items:center;gap:8px;font-size:13px;margin:3px 0}}
.bar i{{display:block;height:10px;background:var(--accent);border-radius:3px}}.bar b{{text-align:right;font-weight:500}}
.small{{font-size:13px;color:var(--muted)}}.ask{{border-left:4px solid #d9a21b;padding:8px 12px;margin:14px 0;background:var(--bg)}}
.ask ol{{margin:6px 0 0}}.spots{{display:grid;grid-template-columns:repeat(auto-fill,minmax(260px,1fr));gap:12px}}
.spot{{border:1px solid var(--line);border-radius:8px;padding:8px}}.pair{{display:grid;grid-template-columns:1fr 1fr;gap:6px}}
figure{{margin:0}}img{{width:100%;height:auto;border-radius:4px;display:block}}figcaption{{font-size:12px;color:var(--muted)}}
.spot p{{margin:6px 0 0;font-size:13px}}a{{color:var(--accent)}}footer{{color:var(--muted);font-size:13px;margin-top:24px}}
@media (max-width:700px){{.cols{{grid-template-columns:1fr}}.facts{{grid-template-columns:repeat(2,1fr)}}}}
</style></head><body><main>
<h1>AlphaEarth groups, {YEAR}</h1>
<p class="lead">Built on Google DeepMind's AlphaEarth Foundations embeddings. The computer only grouped
{summary["parcels"]:,} eligible parcels ({total_ha:,.0f} official ha) into {summary["k"]} groups that look alike from space
over the whole year. The names below are <b>suggestions</b>; please confirm or correct each one. Groups can reflect
surroundings (houses, roads, wet ground) as well as crops. This is a review candidate, not an accepted classification,
and nothing on the map has changed.</p>
<p class="lead"><b>How to read a card:</b> the coloured line is this group's typical greenness or moisture month by month;
grey lines are the other groups. Bars show how today's map classifies the same parcels. Photos show two seasons;
the yellow line is the parcel boundary.</p>
<nav class="legend">{legend}</nav>
{"".join(cards)}
<footer><p>The AlphaEarth Foundations Satellite Embedding dataset is produced by Google and Google DeepMind.
Sentinel-2 images: Copernicus. Google Earth Engine is free for research and non-commercial use; operational
use may need a licence.</p><p>{summary["mixed_parcels"]:,} parcels are mixed (no group covers half of them) and
{summary["parcels_without_whole_pixel"]:,} are too small or narrow to contain a whole 10 m pixel; their group is less certain.</p></footer>
</main></body></html>"""
    REVIEW.mkdir(parents=True, exist_ok=True)
    (REVIEW / "group_cards.html").write_text(page, encoding="utf-8")
    print(f"written {REVIEW / 'group_cards.html'} ({len(page) / 1e6:.1f} MB)")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=["chips", "build"])
    args = parser.parse_args()
    chips() if args.action == "chips" else build()
