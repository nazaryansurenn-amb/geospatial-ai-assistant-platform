"""Owner labelling of every parcel still UNCLASSIFIED in land types v1.2 (owner request 2026-11-01: "prepare a
classification unit for me, all unclassified positions, NDVI chart, photo, hotkeys, I'll do it in one day").

  alphaearth_owner_label.py serve [--remote] -> http://127.0.0.1:8549 (loopback); --remote also opens a second door on
                                               127.0.0.1:8550 for a temporary tunnel: secret path, 24-hour expiry

Reads the frozen v1.2 release (never writes to it) and the production queue (outlines, photo dates, Sentinel-2 monthly
NDVI). Answers go to answers/owner_unclassified_v12__owner.json, one entry per parcel, saved at once; they are owner
labels and win over every rule in the next version (v1.3). Largest parcels come first so the most hectares are done
early. The AI's best guess is hidden unless the owner presses G; whether it was seen is stored with the answer.
"""
from __future__ import annotations
import hmac
import json
import secrets
import sys
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
from alphaearth_landtypes_v11 import OUT  # noqa: E402

PORT = 8549
RELEASE = OUT / "releases/landtypes_v1_2"
ANSWERS = OUT / "answers/owner_unclassified_v12__owner.json"
PAGE = Path(__file__).with_name("alphaearth_owner_label.html")
REMOTE_PORT = 8550                                  # behind a temporary tunnel only; see make_handler
REMOTE_INFO = OUT / "answers/owner_label_remote_link.json"
TYPES = ["FIELD_CROP", "NO_CROP", "VINEYARD", "ORCHARD", "NON_FARM", "GREENHOUSE", "CROPLAND_UNCERTAIN", "PERENNIAL",
         "cannot_tell"]


def items():
    d = pd.read_parquet(RELEASE / "parcels.parquet")
    d = d[d.type6 == "UNCLASSIFIED"].sort_values("area_ha", ascending=False)
    q = {i["id"]: i for i in json.loads((OUT / "queues/prod_full_v24.json").read_text(encoding="utf-8"))["items"]}
    out = []
    for r in d.itertuples():
        it = q.get(r.item_id)
        if not it:
            continue
        h = it["hints"]
        out.append({"id": r.item_id, "area_ha": it["area_ha"], "width_m": it.get("mean_width_m"), "community": it["community"],
                    "lat": it["lat"], "lon": it["lon"], "outline": it["outline"], "parts": it.get("parts"), "photo": it["photo"],
                    "ndvi": h.get("ndvi", {}), "cuts": h.get("cuts", {}), "green_days": h.get("green_days", {}),
                    "guess": r.hint if isinstance(r.hint, str) else None})
    return out


def record(body, valid_ids):
    """Validated answer record for one parcel (owner label)."""
    if body.get("id") not in valid_ids:
        raise ValueError("unknown parcel")
    if body.get("type") not in TYPES:
        raise ValueError("choose a type")
    return {"type": body["type"], "note": str(body.get("note", ""))[:300], "guess_seen": bool(body.get("guess_seen")),
            "at": datetime.now(timezone.utc).isoformat()}


PAIRS_CACHE = OUT / "answers/owner_nci_pairs_items.json"


def pair_items():
    """Owner request 2026-11-01: all side-by-side 'not classifiable' sliver pairs (212, the strict >= 70% rule from
    scripts/nci_pairs_pilot.py) for the owner to label; one answer per pair applies to both slivers, which stay separate
    parcels. Built once and cached (items keep both outlines as parts)."""
    if PAIRS_CACHE.exists():
        return json.loads(PAIRS_CACHE.read_text(encoding="utf-8"))
    import nci_pairs_pilot as npp
    key = json.loads((OUT / "queues/nci_pairs_pilot.key.json").read_text(encoding="utf-8"))
    pairs = key["all_pairs"]
    ids = sorted({p for a, b, _ in pairs for p in (a, b)})
    its, k = npp.build_items_without_nci_rule(ids, "q")
    by_pid = {kk["internal_parcel_id"]: it for kk, it in zip(k, its)}
    out = []
    # owner: neighbours together - a nearest-neighbour route through the pairs, so each next pair is the closest one
    left = list(pairs)
    pos = lambda pr: (by_pid[pr[0]]["lon"], by_pid[pr[0]]["lat"])
    route = [min(left, key=lambda pr: (pos(pr)[1], pos(pr)[0]))]
    left.remove(route[0])
    while left:
        x, y = pos(route[-1])
        nxt = min(left, key=lambda pr: (pos(pr)[0] - x) ** 2 + ((pos(pr)[1] - y) * 1.3) ** 2)
        route.append(nxt)
        left.remove(nxt)
    for n, (a, b, shared) in enumerate(route, 1):
        A, B = by_pid[a], by_pid[b]
        out.append({"id": f"P{n:03d}", "slivers": [a, b], "area_ha": round(A["area_ha"] + B["area_ha"], 3),
                    "width_m": round((A["mean_width_m"] + B["mean_width_m"]), 1), "community": A["community"],
                    "lat": A["lat"], "lon": A["lon"], "outline": A["outline"], "parts": (A.get("parts") or [A["outline"]]) + (B.get("parts") or [B["outline"]]),
                    "photo": A["photo"], "ndvi": {}, "cuts": {}, "green_days": {}, "guess": None})
    PAIRS_CACHE.write_text(json.dumps(out, ensure_ascii=False), encoding="utf-8")
    return out


def serve():
    data = pair_items() if "--pairs" in sys.argv else items()
    ids = {i["id"] for i in data}
    payload = json.dumps({"items": data, "types": TYPES}, ensure_ascii=False).encode()
    lock = threading.Lock()

    def load():
        return json.loads(ANSWERS.read_text(encoding="utf-8")) if ANSWERS.exists() else {}

    def make_handler(remote):
        """Local door: Host 127.0.0.1:8549 only. Remote door (owner-approved internet link, 2026-11-01): every path must
        start with /t/<secret>/, the link stops after 24 hours, POSTs must come from the same origin; nothing else."""
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, code, body, ctype="application/json", extra=()):
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Cache-Control", "no-store")
                self.send_header("Referrer-Policy", "no-referrer")     # the secret must never reach Esri or Google
                self.send_header("X-Robots-Tag", "noindex, nofollow")
                for k, v in extra:
                    self.send_header(k, v)
                self.end_headers()
                self.wfile.write(body)

            def _path(self):
                """The request path inside the door, or None when the door refuses it."""
                path = self.path.split("?")[0]
                if not remote:
                    return path if self.headers.get("Host") == f"127.0.0.1:{PORT}" else None
                if time.time() > remote["expires"]:
                    return None
                prefix = f"/t/{remote['token']}"
                if not hmac.compare_digest(path[:len(prefix)], prefix):
                    return None
                return path[len(prefix):] or "redirect"

            def do_GET(self):
                path = self._path()
                if path is None:
                    return self._send(403, b'{"error":"forbidden"}')
                if path == "redirect":
                    return self._send(302, b"", extra=[("Location", f"/t/{remote['token']}/")])
                if path == "/":
                    self._send(200, PAGE.read_bytes(), "text/html; charset=utf-8")     # fresh: page fixes need no restart
                elif path == "/api/items":
                    self._send(200, payload)
                elif path == "/api/answers":
                    self._send(200, json.dumps(load()).encode())
                else:
                    self._send(404, b'{"error":"not found"}')

            def do_POST(self):
                path, origin = self._path(), self.headers.get("Origin")
                same = (None, f"http://127.0.0.1:{PORT}") if not remote else (None, f"https://{self.headers.get('Host')}")
                if path != "/api/answer" or origin not in same:
                    return self._send(403, b'{"error":"forbidden"}')
                try:
                    body = json.loads(self.rfile.read(min(int(self.headers.get("Content-Length", "0")), 20000)) or b"{}")
                    rec = record(body, ids)
                    rec["via"] = "internet link" if remote else "local"
                    with lock:
                        saved = load()
                        saved[body["id"]] = rec
                        tmp = ANSWERS.with_suffix(".tmp")
                        tmp.write_text(json.dumps(saved, indent=1, ensure_ascii=False), encoding="utf-8")
                        tmp.replace(ANSWERS)
                    self._send(200, json.dumps({"ok": True, "done": len(saved)}).encode())
                except (ValueError, KeyError) as exc:
                    self._send(400, json.dumps({"error": str(exc)}).encode())
        return Handler

    if "--remote" in sys.argv:
        old = json.loads(REMOTE_INFO.read_text(encoding="utf-8")) if REMOTE_INFO.exists() else {}
        if old.get("expires_utc") and datetime.fromisoformat(old["expires_utc"]).timestamp() > time.time():
            # a restart keeps the owner's link and its original expiry (never extends it)
            remote = {"token": old["path"].strip("/").split("/")[1], "expires": datetime.fromisoformat(old["expires_utc"]).timestamp()}
        else:
            remote = {"token": secrets.token_urlsafe(24), "expires": time.time() + 24 * 3600}
        REMOTE_INFO.write_text(json.dumps({"path": f"/t/{remote['token']}/", "port": REMOTE_PORT,
                                           "expires_utc": datetime.fromtimestamp(remote["expires"], timezone.utc).isoformat()}),
                               encoding="utf-8")
        threading.Thread(target=ThreadingHTTPServer(("127.0.0.1", REMOTE_PORT), make_handler(remote)).serve_forever,
                         daemon=True).start()
        print(f"remote door on 127.0.0.1:{REMOTE_PORT} (secret path in {REMOTE_INFO.name}), expires in 24 h")
    print(f"owner labelling: {len(data)} unclassified parcels on http://127.0.0.1:{PORT}")
    ThreadingHTTPServer(("127.0.0.1", PORT), make_handler(None)).serve_forever()


if __name__ == "__main__":
    if "--pairs" in sys.argv:                       # the sliver-pair labelling runs as its own page on 8552
        PORT, ANSWERS = 8552, OUT / "answers/owner_nci_pairs__owner.json"
        sys.argv = [a for a in sys.argv if a != "--remote"]
    {"serve": serve}.get(sys.argv[1] if len(sys.argv) > 1 else "", lambda: sys.exit(__doc__))()
