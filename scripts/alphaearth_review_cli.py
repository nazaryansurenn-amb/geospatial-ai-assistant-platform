"""Reviewer command line for the land-type photo review (v2): prerendered images, 2-3 calls per unit.

  alphaearth_review_cli.py render QUEUE (UNIT ... | --all) [--workers N]   prerender JPEGs (orchestrator, no AI)
  alphaearth_review_cli.py claim  QUEUE REVIEWER N                          lease N units; prints image path + parcels
  alphaearth_review_cli.py photo  QUEUE REVIEWER 'LINES|JSON'               save photo readings (lean lines, see parse_compact);
                                                                            prints the signals for those parcels
  alphaearth_review_cli.py final  QUEUE REVIEWER 'LINES|JSON'               save final answers
  alphaearth_review_cli.py shot   QUEUE UNIT ZOOM                           one extra render at another zoom

A unit is a block (several numbered parcels in one image) or a single parcel. Only the local review server
(http://127.0.0.1:8547) is contacted. Images show yellow outlines; everything outside the parcels is dimmed.
"""
from __future__ import annotations
import json
import re
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

SERVER = "http://127.0.0.1:8547"
EDGE = Path(r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe")
WORK = Path(tempfile.gettempdir()) / "alphaearth_review_shots"


def call(path, body=None):
    request = urllib.request.Request(SERVER + path, data=None if body is None else json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json"} if body is not None else {},
                                     method="POST" if body is not None else "GET")
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            return json.loads(response.read())
    except urllib.error.HTTPError as exc:
        return {"error": json.loads(exc.read() or b"{}").get("error", str(exc))}


def unit_kind(queue, unit):
    return "block" if not "error" in call(f"/api/block?queue={queue}&block={unit}") else "item"


def image_path(queue, unit, zoom=None):
    return WORK / "render" / queue / f"{unit}{'_z' + str(zoom) if zoom else ''}.jpg"


def flagged(png):
    """True when the red 'TILES LOADING/FAILED' marker is visible (top-left corner)."""
    from PIL import Image
    corner = Image.open(png).convert("RGB").crop((0, 0, 260, 50))
    return sum(1 for r, g, b in corner.getdata() if r > 160 and g < 70 and b < 60) > 200


def render(queue, unit, zoom=None, profile="render"):
    from PIL import Image
    target = image_path(queue, unit, zoom)
    if target.exists():
        return target
    target.parent.mkdir(parents=True, exist_ok=True)
    kind = unit_kind(queue, unit)
    url = f"{SERVER}/review?queue={queue}&reviewer=claude&view={'block' if kind == 'block' else 'map'}&{kind}={unit}" + (f"&zoom={zoom}" if zoom else "")
    png = target.with_suffix(".png")
    for _ in range(3):
        subprocess.run([str(EDGE), "--headless=new", "--disable-gpu", "--hide-scrollbars", f"--user-data-dir={WORK / ('profile_' + profile)}",
                        "--window-size=768,768", "--virtual-time-budget=15000", f"--screenshot={png}", url],
                       capture_output=True, timeout=90, check=False)
        if png.exists() and not flagged(png):
            Image.open(png).convert("RGB").save(target, "JPEG", quality=85)
            png.unlink()
            return target
    raise RuntimeError(f"{unit}: tiles did not load after 3 attempts")


def facts(queue, unit, pending=None):
    """Parcels of a unit with their image numbers; with `pending`, only those still unanswered (crash recovery)."""
    data = call(f"/api/block?queue={queue}&block={unit}")
    items = data["items"] if "items" in data else [call(f"/api/item?queue={queue}&item={unit}")]
    return [{"n": k + 1, "id": it["id"], "mode": it["mode"], "area_ha": it["area_ha"], "width_m": it.get("mean_width_m"),
             "photo": f"{it['photo']['date']} {it['photo']['resolution_m']} m" + ("" if it["photo"]["current"] else " OLD")}
            for k, it in enumerate(items) if pending is None or it["id"] in pending]


def log_problem(kind, **fields):
    from alphaearth_photo_review import log_problem as log  # same log file the server writes
    log(kind, **fields)


def compact(signals):
    """Same summary for every reviewer: yearly peak/month/green months, the 2025 curve, cut dips, summer BSI/NDMI."""
    def cut_dips(curve):  # green, a drop of at least 0.1, green again: a cut and regrowth
        seq = [v for v in curve if v is not None]
        return sum(1 for a, b, c in zip(seq, seq[1:], seq[2:]) if a >= .4 and c >= .4 and b <= min(a, c) - .1)

    years = {}
    for year, curve in sorted(signals["ndvi"].items()):
        values = [(v, m + 1) for m, v in enumerate(curve) if v is not None]
        if values:
            peak, month = max(values)
            years[year] = [round(peak, 2), month, sum(1 for v, _ in values if v >= .4), cut_dips(curve)]
    c25 = [None if v is None else round(v, 2) for v in signals["ndvi"].get("2025", [None] * 12)]
    dips = cut_dips(c25)
    summer = lambda key: (lambda v: round(sum(v) / len(v), 2) if v else None)([x for x in (signals.get(key) or [None] * 12)[5:9] if x is not None])
    return {"ndvi_2025": c25, "year_peak_month_greenmonths": years, "cycles": signals["cycles"], "cut_dips_2025": dips,
            "bsi_summer_2025": summer("bsi_2025"), "ndmi_summer_2025": summer("ndmi_2025")}


PERENNIAL, ACTIVE = {"VIN", "ORC"}, {"ANN", "ALF", "GRH", "VIN", "ORC"}


def parse_compact(text, stage):
    """Lean answer lines -> {"ID": answer}. One parcel per line (or ';'):
         ID CODE[.share] [CODE.share ...] h|m [+a|-a] reason      e.g.  t002 FLD.6 BLT.4 m track-edge
         ID ? reason                                                e.g.  t009 ? vines or trees, 2021 photo
    A bare CODE is share 1. Flags are derived: perennial_structure from VIN/ORC, built from BLT, active_2025 (final
    only) from cultivated types unless +a / -a is given."""
    answers = {}
    # a semicolon separates parcels only when a parcel id follows, so reasons may contain semicolons
    for line in filter(None, (x.strip() for x in re.split(r"\n|;\s*(?=[a-z]\d{3}\s)", text))):
        parts = line.split()
        item_id, rest = parts[0], parts[1:]
        if rest and rest[0] == "?":
            answers[item_id] = {"defer": True, "reason": " ".join(rest[1:]) or "unsure"}
            continue
        shares, k = {}, 0
        while k < len(rest) and rest[k][:3].isupper() and rest[k][:3].isalpha():
            code, _, share = rest[k].partition(".")
            shares[code] = float("." + share) if share else 1.0
            k += 1
        if k >= len(rest) or rest[k] not in ("h", "m", "l"):
            raise SystemExit(f"NOT SAVED: {item_id}: expected h, m or l after the shares")
        confidence, k = {"h": "high", "m": "medium", "l": "low"}[rest[k]], k + 1  # l only in forced-choice queues
        active = None
        if k < len(rest) and rest[k] in ("+a", "-a"):
            active, k = rest[k] == "+a", k + 1
        flags = {"perennial_structure": bool(PERENNIAL & set(shares)), "built": "BLT" in shares}
        if stage == "final":
            flags["active_2025"] = active if active is not None else bool(ACTIVE & set(shares))
        answers[item_id] = {"shares": shares, "flags": flags, "confidence": confidence, "reason": " ".join(rest[k:]) or "-"}
    return answers


def compact_line(signals):
    """One short line: 2025 NDVI x100 per month, yearly peak x100 @ month / green months, cycles, dips, BSI/NDMI."""
    c = compact(signals)
    curve = " ".join("--" if v is None else f"{round(v * 100):02d}" for v in c["ndvi_2025"])
    cuts, green = signals.get("cuts") or {}, signals.get("green_days") or {}
    # v2_3: cN = cut-and-regrow events and gN = green days (NDVI >= 0.5) from the frequent series; older queues: dN monthly dips
    years = " ".join(f"{y[2:]}:{round(p * 100)}@{m}/{g}" + (f" c{cuts[y]} g{green.get(y, 0)}" if y in cuts else f"d{d}")
                     for y, (p, m, g, d) in c["year_peak_month_greenmonths"].items())
    cyc = c["cycles"].get("2025") if isinstance(c["cycles"], dict) else c["cycles"]
    return f"n25 {curve} | pk {years} | cyc25 {cyc} dip {c['cut_dips_2025']} bsi {c['bsi_summer_2025']} ndmi {c['ndmi_summer_2025']}"


def main():
    sys.stdout.reconfigure(encoding="utf-8")  # community names are Armenian; the Windows console default is cp1251
    action, args = (sys.argv[1], sys.argv[2:]) if len(sys.argv) > 1 else ("", [])
    if action == "render" and len(args) >= 2:
        queue, rest = args[0], args[1:]
        workers = int(rest[rest.index("--workers") + 1]) if "--workers" in rest else 6
        rest = [r for i, r in enumerate(rest) if r != "--workers" and (i == 0 or rest[i - 1] != "--workers")]
        if rest == ["--all"]:
            data = call(f"/api/queue?queue={queue}")
            rest = [b["id"] for b in data.get("blocks", [])] or [i["id"] for i in data["items"]]
        failed, total = {}, len(rest)
        for _ in range(4):  # slow first tile loads succeed on a later pass once Esri has served them once
            with ThreadPoolExecutor(workers) as pool:
                futures = {u: pool.submit(render, queue, u, None, f"render{k % workers}") for k, u in enumerate(rest)}
            failed = {u: str(f.exception()) for u, f in futures.items() if f.exception()}
            if not failed:
                break
            rest = list(failed)
        print(json.dumps({"rendered": total - len(failed), "failed": failed}))
    elif action == "claim" and len(args) == 3:
        queue, reviewer, n = args
        out = call("/api/claim", {"queue": queue, "reviewer": reviewer, "n": int(n)})
        if "error" in out:
            raise SystemExit(out["error"])
        for unit in out["units"]:  # one line per unit: image, then n:id mode area width photo (unanswered parcels only)
            parcels = " ".join(f"| {p['n']}:{p['id']} {'s' if p['mode'] == 'small' else 'l'} {p['area_ha']}ha {p['width_m']}m {p['photo'].replace(' m', 'm')}"
                               for p in facts(queue, unit, out.get("pending", {}).get(unit)))
            try:
                image = render(queue, unit, None, reviewer)
            except (RuntimeError, subprocess.SubprocessError) as exc:
                log_problem("render_failed", queue=queue, reviewer=reviewer, unit=unit, error=str(exc)[:300])
                print(f"{unit} IMAGE FAILED (logged; skip this unit, it returns to the queue)")
                continue
            print(f"{unit} {image} {parcels}")
        if not out["units"]:
            print("nothing left to claim")
    elif action in ("photo", "final") and len(args) == 3:
        queue, reviewer, payload = args
        try:
            answers = json.loads(payload) if payload.lstrip().startswith("{") else parse_compact(payload, action)
        except (SystemExit, ValueError) as exc:
            log_problem("bad_answer_format", queue=queue, reviewer=reviewer, stage=action, error=str(exc)[:300])
            raise SystemExit(str(exc) if str(exc).startswith("NOT SAVED") else f"NOT SAVED: {exc}")
        out = call("/api/batch", {"queue": queue, "reviewer": reviewer, "stage": action, "answers": answers})
        if "error" in out:
            raise SystemExit("NOT SAVED: " + out["error"])
        for item_id, r in out["results"].items():  # one line per parcel: type [attribute] [!contradictions] [| signals]
            line = f"{item_id} {r['decision']['type']}" + (f" {r['decision']['attribute']}" if r["decision"].get("attribute") else "")
            if r.get("contradictions"):
                line += " !" + "; ".join(r["contradictions"])
            if "signals" in r:
                line += " | " + compact_line(r["signals"])
            print(line)
    elif action == "shot" and len(args) == 3:
        for zoom in range(int(args[2]), 16, -1):  # older photo areas lack the deepest tiles: step out one level at a time
            try:
                print(json.dumps({"image": str(render(args[0], args[1], zoom, "extra")), "zoom": zoom}))
                break
            except RuntimeError:
                continue
        else:
            log_problem("shot_failed", queue=args[0], unit=args[1], zoom=int(args[2]))
            raise SystemExit(f"{args[1]}: no detail shot could be rendered (logged); answer from the main image or defer")
    else:
        raise SystemExit(__doc__)


if __name__ == "__main__":
    main()
