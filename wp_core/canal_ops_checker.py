"""Fixed-code Checker for every Irrigation Assistant output (canal_ops_sim_v1). Never a model.

check(text, results, profile) fills {{R<id>.<key>}} placeholders from tool results, then rejects:
  - any digit not coming from a placeholder or an allowed identifier (B7, B7.2, D2, dekad labels, section numbers);
  - banned wording (en / hy / ru) from config/canal_ops_sim_v1.json;
  - missing required labels for the profile;
  - restricted patterns (cadastral codes, coordinates, internal paths, archive names);
  - comparative claims ("more than" / "less than") between items that contradict the referenced values.
Plans are also re-checked with canal_allocation.invariants.

check_internal(text, results) is the plan v6 internal profile (staff page only, never public): network ids of the
tree (I-G04, I-G04-S1-G2-T1-G3, ...-D5, ...-E), parcel ids (f00001) and cadastral codes are accepted only when a tool
returned them (or the user typed them), and hectares are written with 2 decimals. check() and its profiles are
unchanged; the public profile still blocks codes, coordinates and parcel detail.
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

CFG = json.loads((Path(__file__).resolve().parents[1] / "config/canal_ops_sim_v1.json").read_text(encoding="utf-8"))
PLACEHOLDER = re.compile(r"\{\{(R\d+)\.([A-Za-z0-9_.\-]+)\}\}")
ALLOWED_IDS = re.compile(r"\b(?:(?:I{1,2}-)?B\d+(?:-G\d+|(?:\.\d+)+)?|D[123]|[IV]{1,3})\b")
RESTRICTED = {
    "cadastral_code": re.compile(r"\b\d{2}-\d{3}-\d{4}-\d{4}\b"),
    "coordinates": re.compile(r"\b[34]\d\.\d{4,}\b"),
    "internal_path": re.compile(r"[A-Za-z]:\\|/server_data/|\.parquet|\.sqlite"),
    "archive_name": re.compile("|".join(filter(None, [os.environ.get("ARCHIVE_NAMES", ""), r"SQL\s*archive|sqlcmd"])), re.I),
    "parcel_id": re.compile(r"parcel_[0-9a-f]{8,}"),
}
# plan v6 internal profile: tree ids (scheme of scripts/canal_ops_network_v2.py ID_RE), parcel ids, cadastral codes
TREE_ID = re.compile(r"\b(?:I|II)-[GP]\d{2}(?:-S1(?:-[GP]\d+(?:-T1(?:-G\d+)?)?|-D\d+|-E)?)?\b")
PARCEL_ITEM = re.compile(r"\bf\d{5}\b")
TRACED = (TREE_ID, PARCEL_ITEM, RESTRICTED["cadastral_code"])
INTERNAL_RESTRICTED = {k: v for k, v in RESTRICTED.items() if k != "cadastral_code"}
HA_KEY = re.compile(r"(?:^|_)ha(?:$|_)")
COMPARE = re.compile(r"\{\{(R\d+\.[\w.\-]+)\}\}[^{}]{0,40}?\b(more than|less than|higher than|lower than)\b"
                     r"[^{}]{0,40}?\{\{(R\d+\.[\w.\-]+)\}\}", re.I)
PROFILE_LABELS = {
    "plan": ["Illustrative sharing rule, not legal priority", "Simulation"],
    "sheet": ["Simulation - not a water order"],
    "answer": [],
    "public": ["Illustrative network"],
}


@dataclass
class Verdict:
    ok: bool
    text: str
    reasons: list = field(default_factory=list)


def _walk(cur, parts):
    for part in parts:
        if isinstance(cur, dict) and part in cur:
            cur = cur[part]
        else:
            return None
    return cur


def _lookup(results: dict, rid: str, key: str, loose: bool = False):
    """loose (internal profile): when the exact path is missing, accept the same key path one to three levels deeper
    in the same result (e.g. R2.ha for R2.gate.ha), but only when every such match holds the same value."""
    cur = results.get(rid)
    if key.startswith("data.") and isinstance(cur, dict) and "data" not in cur:
        key = key[5:]                                   # tools show results as {"rid", "data"}; the store keeps data
    parts = key.split(".")
    v = _walk(cur, parts)
    if v is not None or not loose:
        return v
    found = []

    def deeper(node, depth):
        if not isinstance(node, dict) or depth > 3:
            return
        for x in node.values():
            hit = _walk(x, parts)
            if hit is not None:
                found.append(hit)
            deeper(x, depth + 1)
    deeper(cur, 1)
    vals = {json.dumps(x, sort_keys=True, default=str) for x in found}
    return found[0] if len(vals) == 1 else None


def key_hint(results: dict, rid: str, key: str) -> str:
    """For a placeholder that missed: the real key paths of that result near the guessed one (fixed code, so the
    repair turn can cite an existing path)."""
    cur = results.get(rid)
    if not isinstance(cur, dict):
        return f" ({rid} is not a result of this question)"
    parts = [p for p in key.split(".") if p != "data"]
    paths = []

    def walk(node, path, depth):
        if len(paths) > 400 or depth > 4:
            return
        if isinstance(node, dict):
            for k, x in node.items():
                walk(x, path + [str(k)], depth + 1)
        elif not isinstance(node, list):
            paths.append(".".join(path))
    walk(cur, [], 0)
    near = [p for p in paths if parts and parts[-1] == p.split(".")[-1] and any(q in p.split(".") for q in parts[:-1])]
    near = near or [p for p in paths if any(q in p.split(".") for q in parts)]
    return f" (existing keys: {', '.join((near or paths)[:6])})"


def _fmt(v, key="", ha2=False):
    if ha2 and isinstance(v, (int, float)) and not isinstance(v, bool) and any(HA_KEY.search(k) for k in key.split(".")):
        return f"{v:,.2f}"                              # internal profile: hectares with 2 decimals
    if isinstance(v, float):
        return f"{v:,.1f}" if abs(v) < 100 else f"{v:,.0f}"
    if isinstance(v, int):
        return f"{v:,}"
    if isinstance(v, (list, tuple)) and all(isinstance(x, (str, int, float)) for x in v):
        return ", ".join(_fmt(x, key, ha2) for x in v)
    return str(v)


def check(text: str, results: dict, profile: str = "answer", lang: str = "en", echo: str = "") -> Verdict:
    """echo: text the user (or fixed code) gave the model, e.g. the question and the planning dekad; numbers that
    appear there may be repeated without a placeholder. Every other number must come from a tool result."""
    return _check(text, results, profile, echo, ALLOWED_IDS, RESTRICTED, False, [])


def returned_tokens(results: dict) -> set:
    """Every tree id, parcel id and cadastral code that appears anywhere in the tool results."""
    found = set()

    def walk(v):
        if isinstance(v, dict):
            for k, x in v.items():
                walk(k)
                walk(x)
        elif isinstance(v, (list, tuple, set)):
            for x in v:
                walk(x)
        elif isinstance(v, str):
            for rx in TRACED:
                found.update(m.group(0) for m in rx.finditer(v))
    walk(results)
    return found


NAME_WORD = re.compile(r"[A-Za-z0-9_.]*\d[A-Za-z0-9_.]*")
CLOCK = re.compile(r"(?<![\w:])\d{1,2}:\d{2}(?![\w:])")
SCENARIOS = tuple(CFG["supply"]["scenarios"])            # fixed scenario names (normal, short20, short40)
# internal profile: years, version names (v1.5, "version 1.5"), markdown list numbers and the terrain screening band
# of the shared map context (+-3 m) are not figures
NOT_FIGURES = re.compile(r"(?<![\w.])(?:20[12]\d|v\d+(?:\.\d+)?)(?![\w])|^[ \t]*\d{1,2}[.)](?=\s)"
                         r"|(?i:version|верси[яиюей]|տարբերակ)\s?\d+(?:\.\d+)?"
                         r"|(?:±|\+-|\+/-)\s?3\s?(?:m|м|մ)(?!\w)", re.M)
NOT_PRIORITY = re.compile(r"\bnot\s+(?:a\s+)?legal\s+priority\b")    # the required label, in any number or case


def returned_names(results: dict) -> set:
    """Names with digits inside tool-returned strings and keys: round names (D3A), scenario names (short40), simulation
    ids, version names. Words that are only digits are figures, not names, and are not included: those must be
    placeholders. Tree ids are taken out first, so a short form of an id (G18 for I-G08-S1-G18) is not a name."""
    found = set()

    def add(s):
        s = TREE_ID.sub(" ", s)
        found.update(w.strip(".") for w in NAME_WORD.findall(s) if re.search(r"[A-Za-z]", w))
        found.update(CLOCK.findall(s))                                  # clock times a tool gave (07:00)

    def walk(v):
        if isinstance(v, dict):
            for k, x in v.items():
                add(str(k))
                walk(x)
        elif isinstance(v, (list, tuple, set)):
            for x in v:
                walk(x)
        elif isinstance(v, str):
            add(v)
    walk(results)
    return {w for w in found if w}


def check_internal(text: str, results: dict, lang: str = "en", echo: str = "", profile: str = "answer") -> Verdict:
    """Plan v6 internal profile. Ids and codes must have been returned by a tool (or typed by the user)."""
    known = returned_tokens(results)
    bare = PLACEHOLDER.sub(" ", text)
    typed = {m.group(0) for rx in TRACED for m in rx.finditer(echo or "")}
    made_up = sorted({m.group(0) for rx in TRACED for m in rx.finditer(bare)} - known - typed)
    reasons = [f"id or code not returned by a tool: {x}" for x in made_up[:5]]
    ids = re.compile("|".join(f"(?:{rx.pattern})" for rx in (*TRACED, ALLOWED_IDS)))   # longest ids first
    names = returned_names(results) | set(SCENARIOS)
    names |= {f"{m.group(1)}%" for m in re.finditer(r"(?i)\bshort(\d{2})\b", text)}   # "20%" beside short20
    return _check(text, results, profile, echo, ids, INTERNAL_RESTRICTED, True, reasons,
                  names=names, not_figures=NOT_FIGURES)


def _check(text, results, profile, echo, ids_rx, restricted, ha2, reasons, names=(), not_figures=None) -> Verdict:
    reasons = list(reasons)
    # 1. comparative claims must agree with the referenced numbers
    for a, word, b in COMPARE.findall(text):
        va, vb = _lookup(results, *a.split(".", 1), loose=ha2), _lookup(results, *b.split(".", 1), loose=ha2)
        if not isinstance(va, (int, float)) or not isinstance(vb, (int, float)):
            reasons.append(f"comparison without numeric values: {a} / {b}")
        elif (word.lower() in ("more than", "higher than")) != (va > vb):
            reasons.append(f"comparison contradicts values: {a} {word} {b}")
    # 2. digits outside placeholders and allowed ids are untraced
    bare = PLACEHOLDER.sub(" ", text)
    for tok in sorted(set(re.findall(r"[\w.-]*\d[\w.-]*", echo or "")), key=len, reverse=True):
        bare = re.sub(r"(?<![\w.])" + re.escape(tok) + r"(?![\w])", " ", bare)    # numbers the user gave
    bare = ids_rx.sub(" ", bare)
    for tok in sorted(names, key=len, reverse=True):                               # names a tool returned
        bare = re.sub(r"(?<![\w.])" + re.escape(tok) + r"(?![\w])", " ", bare, flags=re.I)
    if not_figures is not None:
        bare = not_figures.sub(" ", bare)
    bare = re.sub(r"\b(?:m3|km2|m2|R\d+)\b|m³|km²|m²", " ", bare)                 # unit symbols and result ids
    if re.search(r"\d", bare):
        toks = list(dict.fromkeys(re.findall(r"[^\s,;:()]*\d[^\s,;:()]*", bare)))
        reasons.append("untraced number in text: " + ", ".join(toks[:5]))
    # 3. fill placeholders
    missing = []

    def sub(m):
        v = _lookup(results, m.group(1), m.group(2), loose=ha2)
        if v is None:
            missing.append(m.group(0) + key_hint(results, m.group(1), m.group(2)))
            return m.group(0)
        return _fmt(v, m.group(2), ha2)

    filled = PLACEHOLDER.sub(sub, text)
    if missing:
        reasons.append("placeholder without a result: " + ", ".join(missing[:3]))
    # 4. wording
    low = filled.lower()
    scan = low
    for labs in PROFILE_LABELS.values():                  # required labels may contain banned words ("not legal priority")
        for lab in labs:
            scan = scan.replace(lab.lower(), " ")
    for lab in CFG["labels"].values():
        scan = scan.replace(lab.lower(), " ")
    scan = NOT_PRIORITY.sub(" ", scan)
    for lg in ("en", "hy", "ru"):
        for w in CFG["wording_ban"][lg]:
            if re.search(r"(?<!\w)" + re.escape(w.lower()) + r"(?!\w)", scan):
                reasons.append(f"banned wording: {w}")
    # 5. labels
    for lab in PROFILE_LABELS.get(profile, []):
        if lab.lower() not in low:
            reasons.append(f"missing label: {lab}")
    # 6. restricted patterns
    for name, rx in restricted.items():
        if rx.search(filled):
            reasons.append(f"restricted content: {name}")
    return Verdict(not reasons, filled if not reasons else "", reasons)


def check_plan(plan, caps=None) -> list[str]:
    from wp_core.canal_allocation import invariants
    return invariants(plan, caps)
