"""Data-mistake auditor v1, fixed-code part (plan v5 section 3 'Data-fix loop'; rules in config/audit_detectors_v1.json).

  audit_review_v1.py detect                 -> runs the 16 checks on land types v1.4 + the 2021-2026 daily series;
                                               writes data/analysis/audit_v1/runs/<run_id>/ (a new folder per input set)
  audit_review_v1.py propose [--rule=ID]... -> appends new questions from the latest run to proposals.jsonl
                                               (append-only, hash-chained; rejected fingerprints are never re-proposed;
                                               --rule limits it to some checks, the rest can follow later)
  audit_review_v1.py status | verify        -> decisions and confirmed share per check | ledger chain check
  audit_review_v1.py serve                  -> owner review page on http://127.0.0.1:8555 (loopback, Host check):
                                               one item at a time, Accept / Reject / Comment, no bulk accept
  audit_review_v1.py expert-cards | expert-record -> open questions as cards for the AI irrigation and remote-sensing
                                               experts | records a decision only where both agree (else a comment);
                                               marked as AI expert decisions, never owner labels
  audit_review_v1.py apply --i-am-the-owner -> writes releases/landtypes_v1_5 (owner only, typed confirmation on a
                                               terminal; refuses if it exists; v1.4 is only read)

Hits and page payloads carry opaque handles, never cadastral codes or internal parcel ids. The handle -> parcel map
(handles.parquet) stays on this PC and is never served.
"""
from __future__ import annotations

import hmac
import json
import re
import secrets
import shutil
import sys
import threading
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from wp_core import audit_detectors as ad  # noqa: E402

CFG = ad.load_config()
OUT = ROOT / CFG["outputs"]["folder"]
LEDGER = ROOT / CFG["outputs"]["ledger"]
PORT = 8555
PAGE = Path(__file__).with_name("audit_review_v1.html")
CODE_PATTERN = re.compile(r"\b\d{2}-\d{3}-\d{4}-\d{4}\b|parcel_[0-9a-f]{8}")
CONFIRM_PHRASE = "APPLY v1.5"


def input_files(cfg=CFG, root=ROOT) -> dict:
    i, files = cfg["inputs"], {}
    for key, rel in i.items():
        if key == "photo_queue":
            continue
        if "{year}" in rel:
            for y in cfg["years"]:
                if key == "daily_2021_2025" and y == 2026:
                    continue
                files[rel.format(year=y)] = root / rel.format(year=y)
        else:
            files[rel] = root / rel
    return files


def detect(cfg=CFG, root=ROOT, out=OUT):
    shas = {rel: ad.sha256_file(p) for rel, p in input_files(cfg, root).items()}
    code = {rel: ad.sha256_file(root / rel) for rel in ("wp_core/audit_detectors.py", "config/audit_detectors_v1.json")}
    run_id = "r" + ad.sha256_bytes(ad.canonical({"inputs": shas, "code": code}))[:12]
    run = out / "runs" / run_id
    if (run / "complete.json").exists():
        print(f"run {run_id} exists for these inputs and code; reusing it")
        _print_summary(json.loads((run / "summary.json").read_text(encoding="utf-8")))
        (out / "latest_run.json").write_text(json.dumps({"run_id": run_id}), encoding="utf-8")
        return run_id
    ctx = ad.load_context(cfg, root)
    hits, summary = ad.run_detectors(cfg, ctx)
    packets = [ad.evidence_packet(cfg, ctx, h) for h in hits if h.get("_pid") is not None]
    tmp = out / "runs" / f".{run_id}.tmp"
    shutil.rmtree(tmp, ignore_errors=True)
    tmp.mkdir(parents=True)
    with open(tmp / "hits.jsonl", "w", encoding="utf-8", newline="\n") as f:
        for h in hits:
            f.write(ad.canonical(ad.public_hit(h)).decode("utf-8") + "\n")
    with open(tmp / "evidence.jsonl", "w", encoding="utf-8", newline="\n") as f:
        for p in packets:
            f.write(ad.canonical(p).decode("utf-8") + "\n")
    pids = sorted({h["_pid"] for h in hits if h.get("_pid") is not None})
    item = ctx.parcels.item_id
    pd.DataFrame({"handle": [ad.handle_of(p, cfg["handle_salt"]) for p in pids], "internal_parcel_id": pids,
                  "item_id": [item[p] for p in pids]}).to_parquet(tmp / "handles.parquet", index=False)
    summary |= {"run_id": run_id, "created": datetime.now(timezone.utc).isoformat()}
    (tmp / "summary.json").write_text(json.dumps(summary, indent=1, ensure_ascii=False, default=ad._plain), encoding="utf-8")
    for name in ("hits.jsonl", "evidence.jsonl", "summary.json"):
        if CODE_PATTERN.search((tmp / name).read_text(encoding="utf-8")):
            raise SystemExit(f"{name} contains a cadastral code or internal id; refusing to write the run")
    (tmp / "complete.json").write_text(json.dumps({"run_id": run_id, "inputs": shas, "code": code,
                                                   "note": "handles.parquet is local only: never served or sent"},
                                                  indent=1), encoding="utf-8")
    tmp.rename(run)
    (out / "latest_run.json").write_text(json.dumps({"run_id": run_id}), encoding="utf-8")
    _print_summary(summary)
    return run_id


def _print_summary(summary):
    print(f"{'#':>2} {'check':<34} {'hits':>6} {'parcels':>7} {'ha':>8} {'wide':>5} {'questions':>9}  extra")
    for rid, c in summary["checks"].items():
        extra = {k: v for k, v in c.items() if k not in ("no", "title", "report_only", "hits", "parcels", "ha",
                                                         "wide_parcels", "proposable", "proposable_ha")}
        q = "report" if c["report_only"] else c["proposable"]
        print(f"{c['no']:>2} {c['title']:<34} {c['hits']:>6} {c['parcels']:>7} {c['ha']:>8} {c['wide_parcels']:>5} "
              f"{q:>9}  {json.dumps(extra, ensure_ascii=False)[:160]}")
    print(f"parcels {summary['parcels']} (reconcile to {CFG['expected_parcels']}: {summary['parcels_reconcile']})")


def _latest_run(out=OUT) -> Path:
    f = out / "latest_run.json"
    if not f.exists():
        raise SystemExit("no detector run yet: run `detect` first")
    return out / "runs" / json.loads(f.read_text(encoding="utf-8"))["run_id"]


def _jsonl(path):
    return [json.loads(x) for x in Path(path).read_text(encoding="utf-8").splitlines() if x.strip()]


def propose_cmd(cfg=CFG, root=ROOT, out=OUT, ledger_path=LEDGER, rules=None):
    run = _latest_run(out)
    meta = json.loads((run / "complete.json").read_text(encoding="utf-8"))
    base_rel = cfg["inputs"]["landtypes_v1_4"]
    inputs_sha = meta["inputs"][base_rel]
    if ad.sha256_file(root / base_rel) != inputs_sha:
        raise SystemExit("land types v1.4 changed since this run; run `detect` again")
    hits = _jsonl(run / "hits.jsonl")
    if rules:                                       # owner may open the questions one check at a time
        unknown = set(rules) - set(cfg["checks"])
        if unknown:
            raise SystemExit(f"unknown rule ids: {sorted(unknown)}")
        hits = [h for h in hits if h["rule_id"] in rules]
    packets = {(p["rule_id"], p["handle"]): p for p in _jsonl(run / "evidence.jsonl")}
    counts = ad.propose(cfg, ad.Ledger(ledger_path), hits, packets, meta["run_id"], inputs_sha)
    print(json.dumps(counts))
    return counts


def status_rows(cfg, events):
    rows = {}
    for s in ad.ledger_state(events).values():
        r = rows.setdefault(s["proposal"]["rule_id"], {"proposed": 0, "pending": 0, "accepted": 0, "rejected": 0})
        r["proposed"] += 1
        dec = s["decision"]["decision"] if s["decision"] else None
        r[{"accept": "accepted", "reject": "rejected", None: "pending"}[dec]] += 1
    for r in rows.values():
        n = r["accepted"] + r["rejected"]
        r["confirmed_share"] = round(r["accepted"] / n, 3) if n else None
    return rows


# ---------------------------------------------------------------- review page
def page_payload(cfg, events, runs_dir, locate, secret: bytes, token: str) -> bytes:
    """Items for the owner page: one per proposal, with its evidence packet as recorded at proposal time. `locate`
    maps a handle to {'lat', 'lon', 'rings', 'photo'} for the photo; it never returns codes or ids."""
    cache, items = {}, []
    for pid, s in ad.ledger_state(events).items():
        p = s["proposal"]
        if p["run_id"] not in cache:
            cache[p["run_id"]] = {(e["rule_id"], e["handle"]): e for e in _jsonl(Path(runs_dir) / p["run_id"] / "evidence.jsonl")}
        ev = cache[p["run_id"]][(p["rule_id"], p["handle"])]
        if ev["evidence_sha"] != p["evidence_sha"]:
            raise ValueError(f"evidence for {pid} changed since it was proposed")
        dec = s["decision"]
        items.append({"proposal_id": pid, "sig": ad.item_sig(secret, pid, p["evidence_sha"]), "rule_id": p["rule_id"],
                      "check_no": cfg["checks"][p["rule_id"]]["no"], "title": cfg["checks"][p["rule_id"]]["title"],
                      "question": p["question"], "from": p["from"], "suggested": p["suggested"], "handle": p["handle"],
                      "ha": p["ha"], "width_class": p["width_class"], "community": ev["community"], "type": ev["type"],
                      "photo": ev["photo"], "curve": ev["curve_monthly_median"], "years": ev["years"],
                      "evidence": ev["hit_evidence"],
                      "decision": None if not dec else {"decision": dec["decision"], "value": dec.get("value"),
                                                        "comment": dec.get("comment", ""), "at": dec["at"]},
                      "comments": [{"comment": c["comment"], "at": c["at"]} for c in s["comments"]]}
                     | locate(p["handle"]))
    items.sort(key=lambda x: (x["check_no"], -(x["ha"] or 0), x["proposal_id"]))
    body = json.dumps({"items": items, "types": cfg["allowed_types"], "type_names": {k: v for k, v in ad.TYPE_NAMES.items() if k},
                       "token": token}, ensure_ascii=False, default=ad._plain).encode("utf-8")
    if CODE_PATTERN.search(body.decode("utf-8")):
        raise ValueError("page payload contains a cadastral code or internal id")
    return body


def real_locator(cfg=CFG, root=ROOT, out=OUT):
    """handle -> photo position for the owner's page (loopback only): parcel outline, centre and photo metadata."""
    import geopandas as gpd
    maps = [pd.read_parquet(r / "handles.parquet") for r in (out / "runs").iterdir()
            if (r / "complete.json").exists()]
    hmap = pd.concat(maps).drop_duplicates("handle").set_index("handle")
    lt = pd.read_parquet(root / cfg["inputs"]["landtypes_v1_4"], columns=["internal_parcel_id", "lon", "lat"]) \
        .set_index("internal_parcel_id")
    geom = gpd.read_parquet(root / cfg["inputs"]["geometry"], columns=["internal_parcel_id", "geometry"])
    geom = geom[geom.internal_parcel_id.isin(hmap.internal_parcel_id)].set_index("internal_parcel_id").to_crs(4326)
    queue = json.loads((root / cfg["inputs"]["photo_queue"]).read_text(encoding="utf-8"))
    photo = {i["id"]: i.get("photo", {}) for i in queue["items"]}

    def rings(g):
        polys = getattr(g, "geoms", [g])
        return [[[round(x, 6), round(y, 6)] for x, y in poly.exterior.coords] for poly in polys]

    def locate(handle):
        pid = hmap.at[handle, "internal_parcel_id"]
        ph = photo.get(hmap.at[handle, "item_id"], {})
        return {"lat": float(lt.at[pid, "lat"]), "lon": float(lt.at[pid, "lon"]),
                "rings": rings(geom.geometry[pid]) if pid in geom.index else [],
                "photo_meta": {"resolution_m": ph.get("resolution_m"), "max_zoom": ph.get("max_zoom") or 19,
                               "provider": ph.get("provider")}}
    return locate


def make_handler(cfg, ledger: ad.Ledger, payload_fn, secret: bytes, token: str, port=PORT):
    lock = threading.Lock()
    origin_ok = (None, f"http://127.0.0.1:{port}")

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _send(self, code, body, ctype="application/json"):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Cache-Control", "no-store")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("X-Robots-Tag", "noindex, nofollow")
            self.end_headers()
            self.wfile.write(body)

        def _host_ok(self):
            return self.headers.get("Host") == f"127.0.0.1:{port}"

        def do_GET(self):
            if not self._host_ok():
                return self._send(403, b'{"error":"forbidden"}')
            path = self.path.split("?")[0]
            if path == "/":
                self._send(200, PAGE.read_bytes(), "text/html; charset=utf-8")
            elif path == "/api/items":
                with lock:
                    body = payload_fn(ledger.read())
                self._send(200, body)
            else:
                self._send(404, b'{"error":"not found"}')

        def do_POST(self):
            if not self._host_ok() or self.path != "/api/decide" or self.headers.get("Origin") not in origin_ok \
                    or not hmac.compare_digest(self.headers.get("X-Audit-Token", ""), token):
                return self._send(403, b'{"error":"forbidden"}')
            try:
                body = json.loads(self.rfile.read(min(int(self.headers.get("Content-Length", "0")), 20000)) or b"{}")
                if not isinstance(body, dict) or not isinstance(body.get("proposal_id"), str):
                    raise ValueError("one proposal at a time")
                with lock:
                    st = ad.ledger_state(ledger.read()).get(body["proposal_id"])
                    if st is None:
                        raise ValueError("unknown proposal")
                    want = ad.item_sig(secret, body["proposal_id"], st["proposal"]["evidence_sha"])
                    if not hmac.compare_digest(str(body.get("sig", "")), want):
                        raise PermissionError("stale or forged item signature; reload the page")
                    ev = ad.decide(cfg, ledger, body["proposal_id"], body.get("decision"), body.get("value"),
                                   body.get("comment", ""), via=f"review page 127.0.0.1:{port}")
                self._send(200, json.dumps({"ok": True, "event": ev["event"], "seq": ev["seq"], "at": ev["at"]}).encode())
            except PermissionError as exc:
                self._send(403, json.dumps({"error": str(exc)}).encode())
            except (ValueError, KeyError, ad.LedgerError) as exc:
                self._send(400, json.dumps({"error": str(exc)}).encode())
    return Handler


def serve(cfg=CFG):
    ledger = ad.Ledger(LEDGER)
    events = ledger.read()                          # verifies the chain before anything is shown
    if not events:
        raise SystemExit("no proposals yet: run `detect` and `propose` first")
    secret, token = secrets.token_bytes(32), secrets.token_urlsafe(24)
    locate = real_locator(cfg)
    payload_fn = lambda ev: page_payload(cfg, ev, OUT / "runs", locate, secret, token)  # noqa: E731
    payload_fn(events)                              # refuse to start if a payload would leak a code
    print(f"audit review: {len(ad.ledger_state(events))} proposals on http://127.0.0.1:{PORT}")
    ThreadingHTTPServer(("127.0.0.1", PORT), make_handler(cfg, ledger, payload_fn, secret, token)).serve_forever()


# ---------------------------------------------------------------- AI expert panel (owner instruction 2026-11-01)
EXPERT_DIR = OUT / "expert_review"
EXPERT_ROLES = ("irrigation", "remote_sensing")
EXPERT_VIA = "AI expert panel (irrigation + remote sensing, both agree; owner instruction 2026-11-01)"


def expert_card(cfg, p, ev) -> dict:
    """Compact text evidence for an expert subagent: no handle map, codes, coordinates, community or photo."""
    r2 = lambda v: None if v is None else round(v, 2)                 # noqa: E731
    curve = ev["curve_monthly_median"]
    return {"proposal_id": p["proposal_id"], "check": cfg["checks"][p["rule_id"]]["title"],
            "check_rule": cfg["checks"][p["rule_id"]]["rule"], "question": p["question"],
            "current_type": p["from"], "current_type_source": ev["type"]["source"], "suggested": p["suggested"],
            "ha": p["ha"], "width_class": p["width_class"], "photo_date": ev["photo"]["date"],
            "years": ev["years"], "hit_evidence": ev["hit_evidence"],
            "ndvi_monthly": {y: [r2(v) for v in c["ndvi"]] for y, c in curve.items()},
            "ndmi_monthly_2025_2026": {y: [r2(v) for v in curve[y]["ndmi"]] for y in ("2025", "2026")},
            "bsi_monthly_2025_2026": {y: [r2(v) for v in curve[y]["bsi"]] for y in ("2025", "2026")}}


def expert_cards(cfg=CFG, out=OUT, ledger_path=LEDGER, batches=4):
    """Open proposals -> expert_review/batch_N.jsonl (the experts' only input)."""
    state = ad.ledger_state(ad.Ledger(ledger_path).read())
    open_ = sorted((s["proposal"] for s in state.values() if s["decision"] is None),
                   key=lambda p: (cfg["checks"][p["rule_id"]]["no"], p["proposal_id"]))
    cache = {}
    cards = []
    for p in open_:
        if p["run_id"] not in cache:
            cache[p["run_id"]] = {(e["rule_id"], e["handle"]): e for e in _jsonl(out / "runs" / p["run_id"] / "evidence.jsonl")}
        cards.append(expert_card(cfg, p, cache[p["run_id"]][(p["rule_id"], p["handle"])]))
    EXPERT_DIR.mkdir(parents=True, exist_ok=True)
    size = -(-len(cards) // batches)
    for b in range(batches):
        part = cards[b * size:(b + 1) * size]
        body = "\n".join(ad.canonical(c).decode("utf-8") for c in part) + "\n"
        if CODE_PATTERN.search(body):
            raise SystemExit("an expert card contains a cadastral code or internal id")
        (EXPERT_DIR / f"batch_{b + 1}.jsonl").write_text(body, encoding="utf-8")
    print(f"{len(cards)} open questions -> {batches} batches in {EXPERT_DIR}")
    return cards


def combine_expert_answers(cfg, answers: dict) -> dict:
    """answers: role -> {proposal_id: {'decision','value','reason'}} -> proposal_id -> outcome. A decision is taken
    only when both experts give the same accept type or both reject; anything else stays open for the owner."""
    out = {}
    ids = set().union(*(a.keys() for a in answers.values()))
    for pid in sorted(ids):
        got = [answers[r].get(pid) for r in EXPERT_ROLES]
        reasons = "; ".join(f"{r}: {g['decision']}{' ' + str(g.get('value')) if g and g.get('value') else ''} - "
                            f"{str(g.get('reason', ''))[:180]}" if g else f"{r}: no answer"
                            for r, g in zip(EXPERT_ROLES, got))
        if all(g and g["decision"] == "reject" for g in got):
            out[pid] = {"decision": "reject", "value": None, "comment": reasons}
        elif all(g and g["decision"] == "accept" and g.get("value") in cfg["allowed_types"] for g in got) \
                and len({g["value"] for g in got}) == 1:
            out[pid] = {"decision": "accept", "value": got[0]["value"], "comment": reasons}
        else:
            out[pid] = {"decision": "comment", "value": None, "comment": "AI expert panel did not agree: " + reasons}
    return out


def expert_record(cfg=CFG, ledger_path=LEDGER):
    answers = {}
    for role in EXPERT_ROLES:
        answers[role] = {}
        for f in sorted(EXPERT_DIR.glob(f"answers_{role}_*.jsonl")):
            for a in _jsonl(f):
                answers[role][a["proposal_id"]] = a
    led = ad.Ledger(ledger_path)
    state = ad.ledger_state(led.read())
    counts = {"accept": 0, "reject": 0, "comment": 0, "skipped (already decided)": 0}
    for pid, o in combine_expert_answers(cfg, answers).items():
        if pid not in state or state[pid]["decision"] is not None:
            counts["skipped (already decided)"] += 1
            continue
        if o["decision"] == "accept" and o["value"] == state[pid]["proposal"]["from"]:
            o = {"decision": "comment", "value": None, "comment": "AI expert panel kept the current type: " + o["comment"]}
        ad.decide(cfg, led, pid, o["decision"], o["value"], o["comment"], via=EXPERT_VIA)
        counts[o["decision"]] += 1
    print(json.dumps(counts))
    return counts


# ---------------------------------------------------------------- apply (owner only)
def _interactive() -> bool:
    """A real terminal with a person at it. On Windows the NUL device claims isatty(), so ask the console API."""
    if not sys.stdin or not sys.stdin.isatty():
        return False
    if sys.platform != "win32":
        return True
    import ctypes
    import msvcrt
    mode = ctypes.c_uint32()
    return bool(ctypes.windll.kernel32.GetConsoleMode(msvcrt.get_osfhandle(sys.stdin.fileno()), ctypes.byref(mode)))


def apply_cmd(cfg=CFG):
    if "--i-am-the-owner" not in sys.argv:
        raise SystemExit("apply is for the owner only: run it yourself with --i-am-the-owner")
    if not _interactive():
        raise SystemExit("apply needs the owner at a terminal (typed confirmation); refusing non-interactive input")
    base = ROOT / cfg["outputs"]["release_base"]
    target = ROOT / cfg["outputs"]["release_target"]
    if target.exists():
        raise SystemExit(f"{target} exists; versions are never overwritten")
    rows = status_rows(cfg, ad.Ledger(LEDGER).read())
    print(json.dumps(rows, indent=1))
    if input(f"Type '{CONFIRM_PHRASE}' to write {target.name} from the accepted proposals: ").strip() != CONFIRM_PHRASE:
        raise SystemExit("not confirmed; nothing written")
    summary = ad.build_release(cfg, base, target, LEDGER, frozen_files=[ad.CONFIG_PATH])
    print(json.dumps({k: summary[k] for k in ("applied", "applied_by_rule", "skipped", "reconcile")}, indent=1,
                     default=ad._plain))


def main(argv):
    cmd = argv[1] if len(argv) > 1 else ""
    if cmd == "detect":
        detect()
    elif cmd == "propose":
        propose_cmd(rules=[a.split("=", 1)[1] for a in argv[2:] if a.startswith("--rule=")] or None)
    elif cmd == "status":
        print(json.dumps(status_rows(CFG, ad.Ledger(LEDGER).read()), indent=1))
    elif cmd == "verify":
        print(f"ledger ok: {len(ad.Ledger(LEDGER).read())} events")
    elif cmd == "serve":
        serve()
    elif cmd == "apply":
        apply_cmd()
    elif cmd == "expert-cards":
        expert_cards()
    elif cmd == "expert-record":
        expert_record()
    else:
        sys.exit(__doc__)


if __name__ == "__main__":
    main(sys.argv)
