"""Water delivery planner, step 2: the supply plan and the planning date (owner approval 2026-11-02,
docs/OWNER_APPROVALS_2026_11_02_EN.md part B; config/canal_planner_v1.json). Fixed code, never a model.

The owner types a supply in Armenian, English or Russian ("5 m3/s for 48 hours", "5 խորանարդ մետր վայրկյան 48 ժամ",
"6 м³/с на неделю", "4 m3/s for the whole season"). This module reads it, caps it at the main canal's 12 m3/s (the
excess is spill), starts it at the next flow-change time (07:00 / 19:00) at or after the planning moment, and shows a
card. Only after the owner confirms the card with a one-time token is it a supply the planner may use. The model never
types a flow: the agents receive the supply_id only.

The planning date is "today" for the simulation: nothing after it is used (no hindsight). The horizon follows from the
length of the window: operational (up to 10 days, per outlet and 12-h slot), tactical (up to 42 days, per dekad) or
season (per secondary and dekad, as a range).
"""
from __future__ import annotations

import hashlib
import json
import re
from datetime import date, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CFG = json.loads((ROOT / "config/canal_planner_v1.json").read_text(encoding="utf-8"))
SUP = CFG["supply"]
CAP = float(SUP["main_canal_cap_m3s"])
CHANGE_H = sorted(int(t[:2]) for t in SUP["change_times"])

NUM = r"(\d+(?:[.,]\d+)?)"
CUBIC = re.compile(r"m3|m³|м3|м³|մ³|խմ|խ\.\s?մ|խորանարդ|կուբ|cub|куб", re.I)
LITRE = re.compile(r"l/s|л/с|լ/վ|liter|litre|литр|լիտր", re.I)
PER_SECOND = re.compile(r"/\s*(?:s|sec|с|сек|վ)|per\s*s|sec|վայրկ|վրկ|секунд|в\s*с\b|/s\b", re.I)
# month names for dates written in words ("հուլիսի 1-ից", "from 1 July", "с 1 июля"): genitive and nominative stems
MONTHS = {1: ("հունվար", "january", "jan", "январ"), 2: ("փետրվար", "february", "feb", "феврал"),
          3: ("մարտ", "march", "mar", "март"), 4: ("ապրիլ", "april", "apr", "апрел"), 5: ("մայիս", "may", "ма[йя]"),
          6: ("հունիս", "june", "jun", "июн"), 7: ("հուլիս", "july", "jul", "июл"), 8: ("օգոստոս", "august", "aug", "август"),
          9: ("սեպտեմբեր", "september", "sep", "сентябр"), 10: ("հոկտեմբեր", "october", "oct", "октябр"),
          11: ("նոյեմբեր", "november", "nov", "ноябр"), 12: ("դեկտեմբեր", "december", "dec", "декабр")}
MONTH_RX = "|".join(f"(?P<m{k}>{'|'.join(v)})" for k, v in MONTHS.items())
DAY_MONTH = [re.compile(rf"(?<!\d)(?P<d>[0-3]?\d)\s*(?:-?ին\s*)?(?:{MONTH_RX})\w*", re.I),
             re.compile(rf"(?:{MONTH_RX})\w*\s*(?P<d>[0-3]?\d)(?!\d)", re.I)]
YEAR = re.compile(r"(?<!\d)(20[2-3]\d)(?!\d)")
DURATION = [(re.compile(NUM + r"\s*(?:h\b|hours?|hrs?\b|ժամ|час|ч\b)", re.I), 1),
            (re.compile(NUM + r"\s*(?:days?\b|օր|дн|сут)", re.I), 24),
            (re.compile(NUM + r"\s*(?:weeks?\b|շաբաթ|недел)", re.I), 24 * 7)]
MONTH = re.compile(r"month|ամիս|ամս|месяц", re.I)
SEASON = re.compile(r"season|սեզոն|сезон", re.I)
START = re.compile(r"(20\d\d-\d\d-\d\d)(?:[ T](\d{1,2}):(\d\d))?")

TEXT = {
    "en": {"card": "Supply at the head: {m3s} m3/s for {hours} h, from {start} to {end}; about {head} m3 released at the "
                   "head. Plan: {horizon}. Confirm this supply?",
           "capped": "Above the main canal's {cap} m3/s; the excess {spill} m3/s is shown as spill.",
           "snapped": "Flows change only at 07:00 or 19:00, so the start is moved to {start}.",
           "season_start": "The season starts on {start}.",
           "rounded": "Canal flows run in whole 12-hour slots, so {asked} h becomes {hours} h.",
           "operational": "per outlet and 12-hour slot", "tactical": "per outlet and dekad",
           "season": "per canal and dekad, optimistic and pessimistic"},
    "hy": {"card": "Մատակարարում գլխամասում՝ {m3s} մ³/վ, {hours} ժամ, {start}-ից մինչև {end}; գլխամասից բաց թողնված "
                   "մոտ {head} մ³։ Պլան՝ {horizon}։ Հաստատե՞լ այս մատակարարումը։",
           "capped": "Մայր ջրանցքի {cap} մ³/վ-ից ավելի է. ավելցուկը՝ {spill} մ³/վ, ցույց է տրվում որպես արտահոսք։",
           "snapped": "Հոսքը փոխվում է միայն 07:00-ին կամ 19:00-ին, ուստի սկիզբը տեղափոխվում է {start}։",
           "season_start": "Սեզոնը սկսվում է {start}-ին։",
           "rounded": "Ջրանցքի հոսքերը աշխատում են ամբողջ 12-ժամյա միջակայքերով, ուստի {asked} ժամը դառնում է {hours} ժամ։",
           "operational": "ըստ ջրթողի և 12-ժամյա միջակայքի", "tactical": "ըստ ջրթողի և դեկադի",
           "season": "ըստ ջրանցքի և դեկադի՝ լավատեսական և վատատեսական"},
    "ru": {"card": "Подача в голове: {m3s} м³/с на {hours} ч, с {start} до {end}; около {head} м³ из головы. "
                   "План: {horizon}. Подтвердить эту подачу?",
           "capped": "Больше {cap} м³/с магистрального канала; излишек {spill} м³/с показан как сброс.",
           "snapped": "Расход меняется только в 07:00 или 19:00, поэтому начало перенесено на {start}.",
           "season_start": "Сезон начинается {start}.",
           "rounded": "Расход в каналах идёт целыми 12-часовыми интервалами, поэтому {asked} ч становится {hours} ч.",
           "operational": "по водовыпускам и 12-часовым интервалам", "tactical": "по водовыпускам и декадам",
           "season": "по каналам и декадам, оптимистичный и пессимистичный"},
}


class SupplyRefused(ValueError):
    pass


def _num(s: str) -> float:
    return float(s.replace(",", "."))


def next_change(t: datetime) -> datetime:
    """The first flow-change time (07:00 or 19:00) at or after t."""
    for d in range(2):
        day = (t + timedelta(days=d)).replace(minute=0, second=0, microsecond=0)
        for h in CHANGE_H:
            c = day.replace(hour=h)
            if c >= t:
                return c
    raise AssertionError("unreachable")


def season_bounds(year: int) -> tuple[datetime, datetime]:
    m0, d0 = map(int, SUP["season_start"].split("-"))
    m1, d1 = map(int, SUP["season_end"].split("-"))
    return datetime(year, m0, d0, CHANGE_H[0]), datetime(year, m1, d1, CHANGE_H[0]) + timedelta(days=1)


def horizon_of(hours: float) -> str:
    days = hours / 24
    if days <= CFG["horizons"]["operational_max_days"]:
        return "operational"
    return "tactical" if days <= CFG["horizons"]["tactical_max_days"] else "season"


def read_flow(text: str) -> float | None:
    """The head flow in m3/s: a number followed (within a few words) by a cubic or litre unit per second."""
    text = re.sub(r"(?<=[mмմ])3", "³", text or "", flags=re.I)       # m3 / մ3 -> m³ / մ³: no digit in the unit
    for m in re.finditer(NUM, text):
        tail = text[m.end():m.end() + 40]
        if re.match(r"\s*[-:]?\d", tail):                  # part of a date or a time, not a flow
            continue
        tail = re.split(r"\d", tail)[0]                    # the unit comes before the next number
        if LITRE.search(tail[:12]):
            return _num(m.group(1)) / 1000
        if CUBIC.search(tail[:20]) and PER_SECOND.search(tail):
            return _num(m.group(1))
    return None


def read_hours(text: str, start: datetime) -> tuple[float | None, str | None]:
    """The window length in hours, or (None, None) when the text gives none."""
    for rx, mult in DURATION:
        m = rx.search(text or "")
        if m:
            return _num(m.group(1)) * mult, None
    if SEASON.search(text or ""):
        return None, "season"
    if MONTH.search(text or ""):
        return SUP["month_days"] * 24.0, None
    return None, None


def read_date(text: str, default_year: int) -> date | None:
    """A start date written in words: "հուլիսի 1-ից", "from 1 July", "July 1", "с 1 июля" (year as typed, else the
    default year). An ISO date (2025-07-01) is read by parse_supply itself."""
    for rx in DAY_MONTH:
        m = rx.search(text or "")
        if m:
            month = next(int(k[1:]) for k, v in m.groupdict().items() if k.startswith("m") and v)
            y = YEAR.search(text or "")
            try:
                return date(int(y.group(1)) if y else default_year, month, int(m.group("d")))
            except ValueError:
                return None
    return None


def parse_supply(text: str, planning: datetime) -> dict:
    """Read a typed supply. Raises SupplyRefused when the flow or the window is missing or impossible."""
    q = read_flow(text)
    if q is None or q <= 0:
        raise SupplyRefused("no head flow found (e.g. 5 m3/s or 5000 l/s)")
    m = START.search(text or "")
    asked = planning
    if m:
        asked = datetime.fromisoformat(m.group(1)).replace(hour=int(m.group(2) or 0), minute=int(m.group(3) or 0))
        if asked < planning:
            raise SupplyRefused("the start is before the planning date (no hindsight)")
    hours, kind = read_hours(text, asked)
    notes = []
    start = next_change(asked)
    if kind == "season":
        s0, s1 = season_bounds(start.year)
        if start < s0:
            start = s0
            notes.append(("season_start", {"start": f"{s0:%Y-%m-%d}"}))
        if start >= s1:
            raise SupplyRefused("the season is over for this year")
        hours = (s1 - start).total_seconds() / 3600
    if not hours or hours <= 0:
        raise SupplyRefused("no window found (e.g. 48 hours, 7 days, next month, the whole season)")
    if hours % 12:
        notes.append(("rounded", {"asked": f"{hours:g}", "hours": f"{round_slots(hours):g}"}))
        hours = round_slots(hours)
    if start != asked and m:
        notes.append(("snapped", {"start": f"{start:%Y-%m-%d %H:%M}"}))
    return make_supply([{"start": start, "hours": hours, "m3s": q}], planning, notes)


def make_supply(segments: list[dict], planning: datetime, notes=None) -> dict:
    """A supply record from segments [{start, hours, m3s}]: capped at the main canal, contiguous, after the planning
    moment. Its id is the hash of what the planner will use."""
    if not segments or len(segments) > SUP["max_segments"]:
        raise SupplyRefused("between 1 and %d flow segments" % SUP["max_segments"])
    notes = list(notes or [])
    segs, t = [], None
    for s in sorted(segments, key=lambda s: s["start"]):
        start = s["start"] if isinstance(s["start"], datetime) else datetime.fromisoformat(str(s["start"]))
        if start < planning:
            raise SupplyRefused("a segment starts before the planning date (no hindsight)")
        if start.hour not in CHANGE_H or start.minute:
            raise SupplyRefused("flows change only at 07:00 or 19:00")
        if t is not None and start != t:
            raise SupplyRefused("segments must follow each other without gaps")
        req = round(float(s["m3s"]), 3)
        hours = float(s["hours"])
        if req < 0 or hours <= 0 or hours % 12:
            raise SupplyRefused("each segment needs a flow >= 0 and whole 12-hour slots")
        segs.append({"start": f"{start:%Y-%m-%d %H:%M}", "hours": hours, "m3s_requested": req, "m3s": min(req, CAP)})
        t = start + timedelta(hours=hours)
    spill = max(s["m3s_requested"] - s["m3s"] for s in segs)
    if spill > 0:
        notes.append(("capped", {"cap": f"{CAP:g}", "spill": f"{spill:g}"}))
    hours = sum(s["hours"] for s in segs)
    body = {"version": CFG["version"], "planning": f"{planning:%Y-%m-%d %H:%M}", "segments": segs,
            "start": segs[0]["start"], "end": f"{t:%Y-%m-%d %H:%M}", "hours": hours,
            "head_m3": round(sum(s["m3s"] * s["hours"] * 3600 for s in segs)),
            "spill_m3": round(sum((s["m3s_requested"] - s["m3s"]) * s["hours"] * 3600 for s in segs)),
            "horizon": horizon_of(hours), "label": CFG["label"]}
    body["supply_id"] = "F" + hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()[:10]
    body["notes"] = notes
    return body


def round_slots(hours: float) -> float:
    """A window rounded up to whole 12-hour slots (a typed 4 hours becomes one 12-hour slot)."""
    return max(12.0, -(-hours // 12) * 12)


def card(supply: dict, lang: str = "en") -> dict:
    """What the owner sees before confirming: one sentence plus notes, in the owner's language."""
    t = TEXT.get(lang, TEXT["en"])
    segs = supply["segments"]
    flow = segs[0]["m3s"] if len(segs) == 1 else " / ".join(f"{s['m3s']:g}" for s in segs)
    text = t["card"].format(m3s=f"{flow:g}" if isinstance(flow, float) else flow, hours=f"{supply['hours']:g}",
                            start=supply["start"], end=supply["end"], head=f"{supply['head_m3']:,}",
                            horizon=t[supply["horizon"]])
    return {"supply_id": supply["supply_id"], "text": text, "notes": [t[k].format(**v) for k, v in supply["notes"]],
            "label": supply["label"]}


def propose(ledger, supply: dict) -> dict:
    """Record the draft supply and issue a one-time token for the owner's card (loopback page only)."""
    ledger.append("supply_draft", {k: v for k, v in supply.items() if k != "notes"})
    return {"supply_id": supply["supply_id"], "token": ledger.issue_token(supply["supply_id"], SUP["token_ttl_s"])}


def confirm(ledger, supply_id: str, token: str) -> dict:
    """The owner's confirmation. Only a confirmed supply can be planned with."""
    draft = next((r for r in reversed(ledger.events("supply_draft")) if r["supply_id"] == supply_id), None)
    if draft is None:
        raise KeyError(supply_id)
    ledger.use_token(supply_id, token)
    return ledger.append("supply", {k: v for k, v in draft.items() if k not in ("id", "ts", "kind", "hash")})


def confirmed(ledger, supply_id: str) -> dict:
    """A confirmed supply by id; agents can name a supply only by this id."""
    rec = next((r for r in ledger.events("supply") if r["supply_id"] == supply_id), None)
    if rec is None:
        raise SupplyRefused("unknown or unconfirmed supply; the owner confirms a supply on the page")
    return rec


def planning_moment(day: str | date) -> datetime:
    """The planning date as a moment: the first change time (07:00) of that day."""
    d = day if isinstance(day, date) else date.fromisoformat(str(day))
    return datetime(d.year, d.month, d.day, CHANGE_H[0])


def dekad_of(t: datetime | date) -> str:
    """The dekad label (2025-07-D1) a moment falls in."""
    return f"{t.year:04d}-{t.month:02d}-D{min(3, (t.day - 1) // 10 + 1)}"
