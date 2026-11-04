"""Irrigation Assistant page (canal_ops_sim_v1): schematic network, dekad plans, chat panel, sheets.

  canal_ops_view.py serve     -> http://127.0.0.1:8554   (loopback only; Host and Origin checked)

  canal_ops_view.py serve --port 8564  -> a preview copy on another port (fixture mode unless --live)

  canal_ops_view.py serve --demo --port 8566  -> the demonstration front-end (canal_ops_demo.html): the water plan
      only, same engine and data, readable names instead of network ids; never live AI

Real network: the plan v6 tree (wp_core/canal_ops_tools_v6.py); Run dekad shares water down the tree and the map
colours every canal and gate by share of need met. The toy network (tests) keeps the v1 branch tools.
The map (plan v6 part 2) draws the layers of data/analysis/canal_ops_sim_v1/map/layers.json, read once under the read
guard and served gzip; the internal parcel id is dropped before anything reaches the page.
Every answer passes the fixed-code Checker before it is sent. Approvals only through a one-time token issued when the
Plan tab renders the "Select simulation plan" card; a "yes" typed in chat is never an approval.
"""
from __future__ import annotations

import gzip
import html
import io
import re
import json
import sys
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import irrigation_assistant as ia  # noqa: E402
from wp_core import canal_allocation as ca  # noqa: E402
from wp_core import canal_ops_tools as T  # noqa: E402
from wp_core import canal_ops_tools_v6 as V  # noqa: E402
from wp_core import canal_ops_world as CW  # noqa: E402
from wp_core import canal_rotation as RT  # noqa: E402
from wp_core import canal_tree_allocation as TA  # noqa: E402
from wp_core import read_guard  # noqa: E402
from wp_core import canal_planner_supply as SU  # noqa: E402
import canal_ops_planner_page as PP  # noqa: E402

PORT = 8554
PAGE = Path(__file__).with_name("canal_ops_view.html")
DEMO_PAGE = Path(__file__).with_name("canal_ops_demo.html")   # demonstration front-end (owner 2026-11-03)
STRINGS = Path(__file__).with_name("canal_ops_strings.json")
LEDGER = ROOT / "data/analysis/canal_ops_sim_v1/ledger_toy.sqlite"
LEDGER_REAL = ROOT / "data/analysis/canal_ops_sim_v1/ledger_real.sqlite"
MAP_DIR = ROOT / TA.NETWORK["map_dir"]                        # map of the network in use
MAP_FILE = re.compile(r"[a-z0-9_]+\.(geojson|geojson\.gz)")
PAGE_HIDDEN = {"id"}         # the internal parcel id never reaches the page (part 2; codes for agents come in part 3)
SIM_LABEL = ca.CFG["labels"]["simulation"]


class MapLayers:
    """Plan v6 section 1 layers for the page: layers.json plus every file it names, read once under the read guard,
    stripped of PAGE_HIDDEN properties and popup fields, and kept gzip for serving."""

    def __init__(self, folder=MAP_DIR):
        self.files, self.read = {}, []
        if not folder or not (Path(folder) / "layers.json").exists():
            return
        folder = Path(folder)
        with read_guard.ReadGuard() as g:
            man = json.loads((folder / "layers.json").read_text(encoding="utf-8"))
            for lay in man["layers"]:
                if "popup" in lay:
                    lay["popup"] = [f for f in lay["popup"] if f not in PAGE_HIDDEN]
                name = lay["file"]
                if name in self.files:
                    continue
                if not MAP_FILE.fullmatch(name):
                    raise ValueError(f"unexpected map layer file name: {name}")
                raw = (folder / name).read_bytes()
                data = json.loads(gzip.decompress(raw) if name.endswith(".gz") else raw)
                for f in data["features"]:
                    for k in PAGE_HIDDEN & f["properties"].keys():
                        del f["properties"][k]
                self.files[name] = self._gz(data)
        self.files["layers.json"] = self._gz(man)
        self.read = g.files

    @staticmethod
    def _gz(obj):
        return gzip.compress(json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode(), 6, mtime=0)

    def __bool__(self):
        return bool(self.files)


class App:
    def __init__(self, ledger_path=None, as_of="2025-07-D3", network="auto", mode="fixture", map_dir="auto"):
        self.mode = mode
        self.kind = "real" if (network == "real" or (network == "auto" and CW.available())) else "toy"
        self.map = MapLayers(MAP_DIR if map_dir == "auto" and self.kind == "real" else (None if map_dir == "auto" else map_dir))
        ledger_path = ledger_path or (LEDGER_REAL if self.kind == "real" else LEDGER)
        Path(ledger_path).parent.mkdir(parents=True, exist_ok=True)
        self.world = (V.v6_world(ledger_path, as_of) if self.kind == "real" else ia.toy_world(ledger_path, as_of=as_of))
        self.runs = {}
        self.planner = PP.Planner(self.world.ledger) if self.kind == "real" else None   # water delivery planner (B1, B5)

    def state(self):
        w = self.world
        if isinstance(w, V.World6):
            return {"mode": self.mode, "network_kind": self.kind, "as_of": w.as_of, "capacity_basis": None,
                    "periods": sorted(w.demand), "scenarios": list(ca.CFG["supply"]["scenarios"]), "labels": ca.CFG["labels"],
                    "network": {"kind": "tree"}, "map": bool(self.map), "order": [], "gates": [],
                    "planner": {"from": str(PP.PLAN_DATES[0]), "to": str(PP.PLAN_DATES[1])} if self.planner else None}
        net = T.get_network(w, agent="main")["data"]
        b = w.branches.assign(pos=w.branches.chainage_m + w.branches.upstream_m)
        return {"mode": self.mode, "network_kind": self.kind, "as_of": w.as_of,
                "capacity_basis": CW.capacity_basis() if self.kind == "real" else None, "periods": sorted(w.demand), "scenarios": list(ca.CFG["supply"]["scenarios"]),
                "labels": ca.CFG["labels"], "network": net, "map": bool(self.map),
                "order": b.sort_values(["stage", "chainage_m"]).branch.tolist(),
                "gates": w.gates[["gate", "branch", "dist_km", "ha"]].to_dict("records")}

    def run(self, period, scenario):
        w = self.world
        if self.kind == "real":
            if period not in w.demand:
                raise KeyError(period)
            w.as_of = period             # replay: the selected dekad is "now"; tools refuse anything later
        out = ia.run_dekad(w, period, scenario)
        if isinstance(w, V.World6):
            return self._run_tree(out, period, scenario)
        sim = w.sims[out["sim_run_id"]]
        plan, days = sim["plan"], w.days[period]
        turns = ca.build_turns(plan, w.branches, days)
        rules = next(v for v in reversed(list(w.results.values())) if "rules" in v)["rules"]
        rows = T._branch_rows(plan)
        hours = turns.groupby("branch").hours.sum().round(1).to_dict() if len(turns) else {}
        table = [{"branch": b, **r, "hours": hours.get(b, 0.0),
                  "pct_A": rules["A"]["branches"].get(b, {}).get("pct"), "pct_B": rules["B"]["branches"].get(b, {}).get("pct")}
                 for b, r in rows.items()]
        gate_pct = plan.gate_totals().assign(pct=lambda g: (100 * g.alloc / g.need).round(1)).pct.to_dict()
        self.runs[out["proposal"]] = {"period": period, "scenario": scenario, "turns": turns, "table": table,
                                      "sim_run_id": out["sim_run_id"]}
        return {**{k: out[k] for k in ("period", "scenario", "proposal", "beats_rules", "objections", "what_i_did",
                                         "answer", "metrics")},
                "table": table, "gate_pct": gate_pct, "rules": {k: v.get("metrics") for k, v in rules.items()},
                "turns": turns.to_dict("records"), "floors_met": plan.floors_met, "notes": plan.notes}

    def _run_tree(self, out, period, scenario):
        """Run dekad on the tree: the Plan table per main-canal offtake, the turn chart per secondary canal, and the
        share of need met at every node for the map."""
        w = self.world
        sim = w.sims[out["sim_run_id"]]
        plan, nt = sim["plan"], sim["nodes"]
        rules = w.sims[f"rules:{period}:{scenario}"]["nodes"]
        rounds = [r for r in V.season_rounds(w, period, scenario, out["sim_run_id"]) if r.period == period]
        t0 = RT.dekad_start(period)

        def h(t):
            return round((t - t0).total_seconds() / 3600, 2)

        def clock(t):
            return t.strftime("%d %b %H:%M") if t else ""

        sched = [t for r in rounds for t in r.turns]
        main_turns = [{"gate": t["unit"], "branch": t["parent"], "start_h": h(t["open"]), "end_h": h(t["close"]),
                       "hours": round(h(t["close"]) - h(t["open"]), 2), "fits_dekad": True, "open": clock(t["open"]),
                       "close": clock(t["close"]), "lps": t["lps"], "m3": t["m3"], "mode": t["mode"], "next": t.get("next"),
                       "next_open": clock(t.get("next_open")), "flags": t["flags"], "night": t["night_h"] > 0}
                      for t in sorted((t for t in sched if t["level"] == "main"), key=lambda t: (t["open"], t["unit"]))]
        sheet = [{"canal": t["parent"], "gate": t["unit"], "open": clock(t["open"]), "close": clock(t["close"]),
                  "lps": t["lps"], "m3": t["m3"], "night_h": t["night_h"], "next": t.get("next") or "",
                  "next_open": clock(t.get("next_open")), "note": ", ".join(t["flags"])}
                 for t in sorted((t for t in sched if t["level"] in ("secondary", "tertiary")),
                                 key=lambda t: (t["parent"], t["open"], t["unit"]))]
        hours = {}
        for t in main_turns:
            hours[t["gate"]] = round(hours.get(t["gate"], 0.0) + t["hours"], 1)

        def pct(t, n):
            v = t.at[n, "pct"]
            return None if pd.isna(v) else round(float(v), 1)

        table = [{"branch": n, "need_m3": round(float(nt.at[n, "need_m3"])), "alloc_m3": round(float(nt.at[n, "in_m3"])),
                  "pct": pct(nt, n), "hours": hours.get(n, 0.0), "pct_A": pct(rules["A"], n), "pct_B": pct(rules["B"], n)}
                 for n in w.maps.offtakes]
        node_pct = {n: round(float(v), 1) for n, v in nt.pct.items() if pd.notna(v)}
        self.runs[out["proposal"]] = {"period": period, "scenario": scenario, "turns": pd.DataFrame(main_turns),
                                      "table": table, "sim_run_id": out["sim_run_id"], "sheet": sheet}
        rules_m = next(v for v in reversed(list(w.results.values())) if "rules" in v)["rules"]
        return {**{k: out[k] for k in ("period", "scenario", "proposal", "beats_rules", "objections", "what_i_did",
                                         "answer", "metrics")},
                "table": table, "gate_pct": {}, "node_pct": node_pct, "balance": TA.balance(plan, nt),
                "rounds": [{"round": f"{r.period}{r.label}", "start": clock(r.start),
                            "stage_II_window_days": round(r.stage2_window_slots * RT.SLOT_H / 24, 1),
                            "lead_order": r.lead_order, "missed": len(r.flags["missed"])} for r in rounds],
                "rules": {k: v.get("metrics") for k, v in rules_m.items()},
                "turns": main_turns, "floors_met": plan.floors_met, "notes": plan.notes}

    def card(self, proposal):
        """Render data for the 'Select simulation plan' card, with a fresh one-time token."""
        if proposal not in self.runs:
            raise KeyError(proposal)
        return {"proposal": proposal, "token": self.world.ledger.issue_token(proposal),
                "title": "Select simulation plan", "note": "A simulation for review only; it authorises no delivery."}

    def decide(self, proposal, decision, token, reason=""):
        r = self.runs[proposal]
        cur = T.input_hash_of(self.world, r["period"])
        ev = self.world.ledger.decide(proposal, decision, token, cur, by="owner", reason=reason)
        r["decision"] = decision
        return {"ok": True, "event": ev["id"]}

    def sheet_html(self, proposal, lang="hy"):
        r = self.runs[proposal]
        approved = r.get("decision") == "approve"
        wm = {"hy": "Մոդելավորում — ոչ ջրի հրահանգ, ոչ ՋՕԸ բաշխման որոշում",
              "en": SIM_LABEL}[lang if lang in ("hy", "en") else "en"]
        if "sheet" in r:
            return self._sheet_tree(r, lang, wm, approved)
        head = {"hy": ["Փական", "Ճյուղ", "Հերթ", "Սկիզբ, ժ", "Ավարտ, ժ", "Ժամեր", "Գիշեր", "Ստուգում"],
                "en": ["Gate", "Branch", "Lane", "Start, h", "End, h", "Hours", "Night", "Check"]}[lang if lang in ("hy", "en") else "en"]
        rows = "".join(f"<tr><td>{html.escape(t['gate'])}</td><td>{html.escape(t['branch'])}</td><td>{t['lane'] + 1}</td>"
                       f"<td>{t['start_h']}</td><td>{t['end_h']}</td><td>{t['hours']}</td><td>{'✓' if t['night'] else ''}</td>"
                       f"<td>☐</td></tr>" for t in r["turns"].to_dict("records"))
        status = ("" if approved else {"hy": " · ՆԱԽԱԳԻԾ", "en": " · DRAFT"}.get(lang, " · DRAFT"))
        return (f"<!doctype html><meta charset=utf-8><title>{html.escape(r['period'])}</title><style>"
                "body{font:14px system-ui;margin:24px}table{border-collapse:collapse;width:100%}td,th{border:1px solid #999;"
                "padding:4px 6px}th{background:#eee}.wm{position:fixed;top:40%;left:5%;font-size:40px;color:rgba(200,0,0,.13);"
                "transform:rotate(-20deg);pointer-events:none}.ft{margin-top:12px;font-size:12px;color:#a00}</style>"
                f"<div class=wm>{wm}</div><h2>{html.escape(r['period'])} · {html.escape(r['scenario'])}{status}</h2>"
                f"<p>{wm}. {html.escape(ca.CFG['labels']['network'])}.</p><table><tr>{''.join(f'<th>{h}</th>' for h in head)}</tr>"
                f"{rows}</table><div class=ft>{wm} · run {html.escape(r['sim_run_id'])}</div>")

    def _sheet_tree(self, r, lang, wm, approved):
        """Ditch-rider sheet on the tree: one block per canal, every turn with clock times, l/s, m3 and who is next."""
        lg = lang if lang in ("hy", "en") else "en"
        head = {"hy": ["Փական / ջրթող", "Բաց", "Փակ", "լ/վ", "մ³", "Գիշեր, ժ", "Հաջորդը", "Ժամը", "Նշում", "Ստուգում"],
                "en": ["Gate / outlet", "Open", "Close", "l/s", "m³", "Night h", "Next here", "At", "Note", "Check"]}[lg]
        blocks, cur = [], None
        for t in r["sheet"]:
            if t["canal"] != cur:
                cur = t["canal"]
                blocks.append(f"<tr><th colspan=10 style='text-align:left;background:#dde'>{html.escape(cur)}</th></tr>")
            blocks.append(f"<tr><td>{html.escape(t['gate'])}</td><td>{html.escape(t['open'])}</td><td>{html.escape(t['close'])}</td>"
                          f"<td>{t['lps']:g}</td><td>{t['m3']}</td><td>{t['night_h']:g}</td><td>{html.escape(t['next'])}</td>"
                          f"<td>{html.escape(t['next_open'])}</td><td>{html.escape(t['note'])}</td><td>☐</td></tr>")
        status = ("" if approved else {"hy": " · ՆԱԽԱԳԻԾ", "en": " · DRAFT"}[lg])
        return (f"<!doctype html><meta charset=utf-8><title>{html.escape(r['period'])}</title><style>"
                "body{font:13px system-ui;margin:24px}table{border-collapse:collapse;width:100%}td,th{border:1px solid #999;"
                "padding:3px 5px}th{background:#eee}.wm{position:fixed;top:40%;left:5%;font-size:40px;color:rgba(200,0,0,.13);"
                "transform:rotate(-20deg);pointer-events:none}.ft{margin-top:12px;font-size:12px;color:#a00}</style>"
                f"<div class=wm>{wm}</div><h2>{html.escape(r['period'])} · {html.escape(r['scenario'])}{status}</h2>"
                f"<p>{wm}. {html.escape(RT.LABEL)}.</p><table><tr>{''.join(f'<th>{h}</th>' for h in head)}</tr>"
                f"{''.join(blocks)}</table><div class=ft>{wm} · run {html.escape(r['sim_run_id'])}</div>")

    def excel(self, proposal):
        r = self.runs[proposal]
        if r.get("decision") != "approve":
            raise PermissionError("Excel export is available after the plan is selected (Approval B)")
        rows = [["Simulation - not a water order, not a WUA distribution decision"],
                ["Branch", "Need m3", "Allocated m3", "% of need", "Hours", "Rule A %", "Rule B %"]]
        rows += [[t["branch"], t["need_m3"], t["alloc_m3"], t["pct"], t["hours"], t["pct_A"], t["pct_B"]] for t in r["table"]]
        return xlsx(rows)


def xlsx(rows):
    """Minimal one-sheet .xlsx (inline strings, numbers) without extra libraries."""
    def cell(v, ref):
        if isinstance(v, (int, float)) and v is not None:
            return f'<c r="{ref}"><v>{v}</v></c>'
        return f'<c r="{ref}" t="inlineStr"><is><t>{html.escape("" if v is None else str(v))}</t></is></c>'
    cols = "ABCDEFGHIJKLMNOP"
    body = "".join(f'<row r="{i + 1}">' + "".join(cell(v, f"{cols[j]}{i + 1}") for j, v in enumerate(r)) + "</row>"
                   for i, r in enumerate(rows))
    files = {
        "[Content_Types].xml": '<?xml version="1.0" encoding="UTF-8"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/><Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/><Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/></Types>',
        "_rels/.rels": '<?xml version="1.0" encoding="UTF-8"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/></Relationships>',
        "xl/workbook.xml": '<?xml version="1.0" encoding="UTF-8"?><workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets><sheet name="Plan" sheetId="1" r:id="rId1"/></sheets></workbook>',
        "xl/_rels/workbook.xml.rels": '<?xml version="1.0" encoding="UTF-8"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/></Relationships>',
        "xl/worksheets/sheet1.xml": f'<?xml version="1.0" encoding="UTF-8"?><worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData>{body}</sheetData></worksheet>',
    }
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for n, c in files.items():
            z.writestr(n, c)
    return buf.getvalue()


def make_handler(app, port=PORT):
    origin = f"http://127.0.0.1:{port}"

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _send(self, code, body, ctype="application/json; charset=utf-8", extra=None):
            if isinstance(body, (dict, list)):
                body = json.dumps(body, ensure_ascii=False, default=str).encode()
            elif isinstance(body, str):
                body = body.encode()
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Cache-Control", "no-store")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("X-Content-Type-Options", "nosniff")
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)

        def _host_ok(self):
            return self.headers.get("Host") == f"127.0.0.1:{port}"

        def do_GET(self):
            if not self._host_ok():
                return self._send(403, {"error": "forbidden"})
            u = urlparse(self.path)
            q = {k: v[0] for k, v in parse_qs(u.query).items()}
            try:
                if u.path == "/":
                    return self._send(200, (DEMO_PAGE if getattr(app, "demo", False) else PAGE).read_bytes(),
                                      "text/html; charset=utf-8")
                if u.path == "/api/strings":
                    return self._send(200, STRINGS.read_bytes())
                if u.path.startswith("/map/") and u.path[5:] in app.map.files:
                    body, extra = app.map.files[u.path[5:]], {"Content-Encoding": "gzip"}
                    if "gzip" not in self.headers.get("Accept-Encoding", ""):
                        body, extra = gzip.decompress(body), {}
                    return self._send(200, body, "application/geo+json" if u.path.endswith((".geojson", ".gz"))
                                      else "application/json; charset=utf-8", extra)
                if u.path == "/api/state":
                    return self._send(200, app.state())
                if u.path == "/api/sheet":
                    return self._send(200, app.sheet_html(q["proposal"], q.get("lang", "hy")), "text/html; charset=utf-8")
                if u.path == "/api/planner/report" and app.planner:
                    return self._send(200, app.planner.report_html(q["plan"], q.get("lang", "hy"),
                                                                   demo=getattr(app, "demo", False) or q.get("demo") == "1"),
                                      "text/html; charset=utf-8")
                if u.path == "/api/planner/communities" and app.planner:
                    return self._send(200, PP.community_layer())
                if u.path == "/api/planner/crops" and app.planner:
                    body, extra = PP.crop_layer_gz(), {"Content-Encoding": "gzip"}
                    if "gzip" not in self.headers.get("Accept-Encoding", ""):
                        body, extra = gzip.decompress(body), {}
                    return self._send(200, body, "application/geo+json", extra)
                if u.path == "/api/planner/states" and app.planner:
                    return self._send(200, app.planner.parcel_states(q["plan"]))
                if u.path == "/api/planner/parcels" and app.planner:
                    body, extra = app.planner.parcels_geojson(q["plan"]), {"Content-Encoding": "gzip"}
                    if "gzip" not in self.headers.get("Accept-Encoding", ""):
                        body, extra = gzip.decompress(body), {}
                    return self._send(200, body, "application/geo+json", extra)
                if u.path == "/api/planner/excel" and app.planner:
                    return self._send(200, xlsx(app.planner.excel_rows(q["plan"])),
                                      "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                                      {"Content-Disposition": f'attachment; filename="water_plan_{q["plan"]}_simulation.xlsx"'})
                if u.path == "/api/excel":
                    return self._send(200, app.excel(q["proposal"]),
                                      "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                                      {"Content-Disposition": f'attachment; filename="plan_{q["proposal"]}_simulation.xlsx"'})
            except PermissionError as e:
                return self._send(403, {"error": str(e)})
            except (KeyError, ValueError):
                return self._send(404, {"error": "not found"})
            return self._send(404, {"error": "not found"})

        def do_POST(self):
            if not self._host_ok() or self.headers.get("Origin") not in (origin, None) or \
                    self.headers.get("Content-Type", "").split(";")[0] != "application/json":
                return self._send(403, {"error": "forbidden"})
            n = int(self.headers.get("Content-Length") or 0)
            if n > 20000:
                return self._send(413, {"error": "too large"})
            try:
                body = json.loads(self.rfile.read(n) or b"{}")
                p = urlparse(self.path).path
                if p == "/api/run":
                    return self._send(200, app.run(body["period"], body.get("scenario", "normal")))
                if p == "/api/chat":
                    return self._send(200, ia.answer(app.world, str(body.get("text", ""))[:1000], body.get("lang", "en"),
                                                     mode=app.mode))
                if p.startswith("/api/planner/") and app.planner:
                    if p == "/api/planner/parse":
                        return self._send(200, app.planner.parse(body["text"], body.get("date"), body.get("lang", "en"),
                                                                     int(body.get("year", 2025))))
                    if p == "/api/planner/confirm":
                        return self._send(200, app.planner.confirm(body["supply_id"], body["token"]))
                    if p == "/api/planner/run":
                        return self._send(200, app.planner.run(body["supply_id"], body.get("losses", "assumed")))
                if p == "/api/card":
                    return self._send(200, app.card(body["proposal"]))
                if p == "/api/decide":
                    return self._send(200, app.decide(body["proposal"], body["decision"], body["token"], body.get("reason", "")))
            except (T.ToolRefused, SU.SupplyRefused) as e:
                return self._send(400, {"error": str(e)})
            except PermissionError as e:
                return self._send(403, {"error": str(e)})
            except (KeyError, ValueError, TypeError):
                return self._send(400, {"error": "bad request"})
            return self._send(404, {"error": "not found"})

    return H


def serve(live=False, port=PORT, demo=False):
    if live:                                   # the owner's switch: starting with --live turns live AI on
        import os
        os.environ["LIVE_ENABLED"] = "1"
        ia.guard_live("live")                  # refuses unless the payload approval matches and the SDK is installed
    if demo and live:
        raise SystemExit("the demonstration page runs without live AI")
    app = App(mode="live" if live else "fixture")
    app.demo = demo                            # demonstration front-end: the water plan only, readable names
    if demo and app.planner:                   # build the demonstration days' parcel snapshots before the first request
        import threading
        from datetime import date as _d
        threading.Thread(target=lambda: [app.planner._snapshot(_d(2025, m, 1), 9) for m in (7, 8)], daemon=True).start()
    print(f"Irrigation Assistant ({app.mode}, {app.kind} network, map {'on' if app.map else 'off'}): "
          f"http://127.0.0.1:{port}", flush=True)
    ThreadingHTTPServer(("127.0.0.1", port), make_handler(app, port)).serve_forever()


if __name__ == "__main__":
    if sys.argv[1:2] == ["serve"]:
        args = sys.argv[2:]
        serve(live="--live" in args, port=int(args[args.index("--port") + 1]) if "--port" in args else PORT,
              demo="--demo" in args)
    else:
        sys.exit(__doc__)
