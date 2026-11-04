"""Season replay page (owner 2026-11-01): the 2026 season week by week, where the WUA may wish to look first,
compared with the same week of 2025. Local, read-only, loopback only, port 8553 (8551 is kept for the presentation
version). Community level only: no parcels, codes or record-level data are served.

  season_replay_view.py serve   -> http://127.0.0.1:8553

/api/week/<n> returns ONLY what was known at the end of ISO week n of 2026 (rows of weeks <= n), so the page cannot
show the future while it plays. Weekly notes are written by fixed code from those rows (no AI model call).
"""
from __future__ import annotations
import json
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data/analysis/season_replay_v1"
PAGE = Path(__file__).with_name("season_replay_view.html")
COMMUNITIES = ROOT / "data/source/wua_areas.geojson"
CFG = json.loads((ROOT / "config/season_replay_v1.json").read_text(encoding="utf-8"))
PORT = 8553
YEAR, PREV = CFG["years"]["replay"], CFG["years"]["compare"]
LEVEL_RANK = {"attention": 3, "watch": 2, "none": 1, "unassessed": 0}
FIELDS = ["community", "area_ha", "parcels", "score", "level", "scored_share", "guarded_share", "bare_share", "no_view_share",
          "dry_ha", "dry_parcels", "ndvi_median", "ndmi_median", "net_m3", "etc_m3", "et0_mm", "et0_normal_mm", "rain_mm",
          "tmax_c", "pairs", "ndvi_diff", "ndmi_diff"]


def clean(v):
    if isinstance(v, (float, np.floating)):
        return None if not np.isfinite(v) else round(float(v), 4)
    if isinstance(v, (np.integer,)):
        return int(v)
    return v


def rows(df):
    return [{k: clean(r[k]) for k in FIELDS if k in r} for r in df.to_dict("records")]


def pct(a, b):
    return None if (a is None or b in (None, 0)) else round(100 * (a - b) / b, 0)


def markers(now):
    """At most three 'look here first' communities: attention first, then watch, by score."""
    c = now[(now.community != "ALL") & now.level.isin(["attention", "watch"])].copy()
    c["rank"] = c.level.map(LEVEL_RANK)
    c["dry_share"] = c.dry_ha / c.area_ha                         # tie-break: share of land with possible dryness
    return c.sort_values(["rank", "score", "dry_share"], ascending=False).community.head(CFG["community"]["max_markers"]).tolist()


def notes(now, prev_week, same_2025, n, top):
    """Plain weekly note in Armenian and English from this week's rows only."""
    a = now[now.community == "ALL"].iloc[0]
    b = same_2025[same_2025.community == "ALL"].iloc[0] if len(same_2025) else None
    en, hy = [], []
    if a.et0_mm is not None and np.isfinite(a.et0_mm):
        d_norm = pct(a.et0_mm, a.et0_normal_mm)
        en.append(f"Weather: crop evaporation demand {a.et0_mm:.0f} mm this week ({'+' if d_norm >= 0 else ''}{d_norm:.0f}% vs the 1991-2020 normal), rain {a.rain_mm:.0f} mm, mean daily maximum {a.tmax_c:.0f} °C.")
        hy.append(f"Եղանակ. այս շաբաթ գոլորշացման պահանջը {a.et0_mm:.0f} մմ է (բազմամյա նորմից {'+' if d_norm >= 0 else ''}{d_norm:.0f}%), տեղումներ՝ {a.rain_mm:.0f} մմ, միջին առավելագույն ջերմաստիճանը՝ {a.tmax_c:.0f} °C։")
    if top and (now[now.community.isin(top)].level == "attention").any():
        en.append("Look here first this week: " + ", ".join(top) + ". The WUA may wish to give fields at a sensitive stage there an early turn.")
        hy.append("Այս շաբաթ առաջին հերթին ուշադրություն՝ " + ", ".join(top) + "։ ՋՕԸ-ն կարող է այնտեղ զգայուն փուլում գտնվող դաշտերին ավելի վաղ հերթ տալ։")
    elif top:
        en.append("No community reaches 'look here first'. Highest on 'watch' this week: " + ", ".join(top) + " (possible rising need; worth checking turn dates).")
        hy.append("Ոչ մի համայնք «առաջին հերթին ուշադրություն» մակարդակին չի հասնում։ «Հետևել» մակարդակում ամենաբարձրը՝ " + ", ".join(top) + " (հնարավոր աճող կարիք, արժե ստուգել ջրման հերթերը)։")
    else:
        en.append("No community shows a special sign of need from satellite and weather this week.")
        hy.append("Այս շաբաթ արբանյակային և եղանակային տվյալներով որևէ համայնքում հատուկ կարիքի նշան չկա։")
    if len(prev_week):
        up = []
        for _, r in now[now.community != "ALL"].iterrows():
            p = prev_week[prev_week.community == r.community]
            if len(p) and LEVEL_RANK[r.level] > LEVEL_RANK[p.iloc[0].level] and r.level in ("attention", "watch"):
                up.append(r.community)
        if up:
            en.append("Moved up since last week: " + ", ".join(up[:6]) + ".")
            hy.append("Նախորդ շաբաթվա համեմատ բարձրացել են՝ " + ", ".join(up[:6]) + "։")
    if b is not None:
        dn = pct(a.net_m3, b.net_m3)
        if dn is not None:
            en.append(f"Compared with the same week of 2025: estimated crop water need {'+' if dn >= 0 else ''}{dn:.0f}%.")
            hy.append(f"2025-ի նույն շաբաթվա համեմատ՝ մշակաբույսերի ջրի գնահատված պահանջը {'+' if dn >= 0 else ''}{dn:.0f}%։")
    cov = a.scored_share
    if cov is not None and cov < 0.5:
        en.append(f"Only {cov:.0%} of the farmland could be scored this week (clouds, harvests, bare fields).")
        hy.append(f"Այս շաբաթ գնահատվել է գյուղատնտեսական հողերի միայն {cov:.0%}-ը (ամպեր, բերքահավաք, բաց հող)։")
    return {"en": en, "hy": hy}


def load():
    cw = pd.read_parquet(DATA / "community_weeks.parquet")
    return cw


def week_payload(cw, n):
    cur = cw[(cw.year == YEAR) & (cw.week <= n)]                       # nothing after week n leaves the server
    now = cur[cur.week == n]
    if now.empty:
        return None
    last = cw[(cw.year == PREV) & (cw.week == n)]
    prev_week = cur[cur.week == n - 1]
    top = markers(now)
    hist = cw[(cw.week == n) & cw.year.between(*CFG["years"]["range"]) & (cw.community == "ALL")]
    series = {}
    for name, g in cur.groupby("community"):
        series[name] = {"2026": [[int(w), clean(s)] for w, s in zip(g.week, g.score)]}
    for name, g in cw[(cw.year == PREV) & (cw.week <= n)].groupby("community"):
        series.setdefault(name, {})["2025"] = [[int(w), clean(s)] for w, s in zip(g.week, g.score)]
    ser_net = cur[cur.community == "ALL"][["week", "net_m3"]].values.tolist()
    ser_net_prev = cw[(cw.year == PREV) & (cw.week <= n) & (cw.community == "ALL")][["week", "net_m3"]].values.tolist()
    return {"week": n, "week_start": now.week_start.iloc[0], "week_end": now.week_end.iloc[0],
            "rows": rows(now), "rows_2025": rows(last), "markers": top, "notes": notes(now, prev_week, last, n, top),
            "range_net_m3": [clean(hist.net_m3.min()), clean(hist.net_m3.max())] if len(hist) else None,
            "series": series, "net_series": {"2026": ser_net, "2025": ser_net_prev}}


def meta(cw):
    w = cw[(cw.year == YEAR) & (cw.community == "ALL")][["week", "week_start", "week_end"]]
    return {"year": YEAR, "compare": PREV, "weeks": w.to_dict("records"), "levels": CFG["levels"],
            "min_scored_share": CFG["community"]["min_scored_share"],
            "season_net_thousand_m3": {str(y): round(float(g.net_m3.sum()) / 1000) for y, g in
                                       cw[cw.community == "ALL"].groupby("year")}}


def serve():
    cw = load()
    page = PAGE.read_text(encoding="utf-8").encode()
    geo = json.loads(COMMUNITIES.read_text(encoding="utf-8"))
    have = set(cw.community)
    for f in geo["features"]:
        f["properties"] = {"name": f["properties"]["name_hy"], "in_study": f["properties"]["name_hy"] in have}
    geo_b = json.dumps(geo, ensure_ascii=False).encode()
    meta_b = json.dumps(meta(cw), ensure_ascii=False).encode()
    cache = {}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _send(self, code, body, ctype):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Cache-Control", "no-store")
            self.send_header("Referrer-Policy", "no-referrer")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if self.headers.get("Host") != f"127.0.0.1:{PORT}":
                return self._send(403, b'{"error":"forbidden"}', "application/json")
            path = urlparse(self.path).path
            if path == "/":
                return self._send(200, page, "text/html; charset=utf-8")
            if path == "/api/meta":
                return self._send(200, meta_b, "application/json; charset=utf-8")
            if path == "/data/communities.geojson":
                return self._send(200, geo_b, "application/json; charset=utf-8")
            if path.startswith("/api/week/") and path[10:].isdigit():
                n = int(path[10:])
                if n not in cache:
                    p = week_payload(cw, n)
                    cache[n] = json.dumps(p, ensure_ascii=False).encode() if p else None
                if cache[n] is None:
                    return self._send(404, b'{"error":"no such week"}', "application/json")
                return self._send(200, cache[n], "application/json; charset=utf-8")
            self._send(404, b'{"error":"not found"}', "application/json")

    print(f"season replay: http://127.0.0.1:{PORT}", flush=True)
    ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()


if __name__ == "__main__":
    if sys.argv[1:2] == ["serve"]:
        serve()
    else:
        sys.exit(__doc__)
