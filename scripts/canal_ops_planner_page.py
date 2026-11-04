"""Water delivery planner on the Irrigation Assistant page (owner approval 2026-11-02, B1 and B5; "Page first").

The owner types a supply; fixed code reads it (wp_core/canal_planner_supply.py) and shows a card; the owner confirms it
with a one-time token; then the engine (wp_core/canal_planner_engine.py) makes the operational plan. The page gets the
plan as plain data: the water account, the summary, every due outlet with its status and times, the canals that carry
water and when, and every main-canal gate change. The report is a printable page in hy / en / ru with the gatekeeper
cards; Excel lists every due outlet. No model is called; every number comes from fixed code. Simulation on the
synthetic network: not a water order, people open gates.
"""
from __future__ import annotations

import gzip
import html
import json
import math
import sys
from datetime import date, datetime
from functools import lru_cache
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from wp_core import canal_planner_engine as E  # noqa: E402
from wp_core import canal_planner_state as ST  # noqa: E402
from wp_core import canal_planner_supply as SU  # noqa: E402
from wp_core import read_guard  # noqa: E402

PLAN_DATES = (date(2024, 3, 25), date(2026, 10, 31))           # days with satellite views and the forecast archive
KEEP = 6                                                        # plans kept in memory

TXT = {
    "en": {"title": "Water delivery plan", "sim": "Simulation on a synthetic canal network along the real main canal - not a water order. People open the gates.",
           "net": "Synthetic network on the real main canal; Lower Hrazdan observed zone, stages I and II.",
           "supply": "Supply", "planning": "Planning date", "account": "Water account (m³, simulation estimates)",
           "released_head_m3": "Released at the head", "main_canal_seepage_m3": "Main canal seepage",
           "fill_and_drain_m3": "Filling dry canals", "refill_m3": "Refilling after a close",
           "secondary_tertiary_seepage_m3": "Secondary and tertiary seepage", "turn_end_drain_m3": "Drained at turn ends",
           "holding_flow_m3": "Holding flow in short gaps", "delivered_field_inlets_m3": "Delivered at field inlets",
           "escape_m3": "Spill at canal tails", "summary": "Summary", "due_ha": "Land due in the window (ha)",
           "ha_with_a_turn": "Due land getting a turn (ha)", "need_met_pct": "Due need met (%)",
           "outlets": "Outlets served / partly / not reached", "last_arrival_h": "Last water arrives after (h)",
           "not_reached": "Not reached (most urgent first)", "outlet": "Outlet", "community": "Community",
           "days": "Days to stress", "reason": "Reason", "main_sheet": "Main canal: gate changes",
           "head_time": "Order at the head", "here_time": "Change at the gate", "lps": "l/s",
           "cards": "Ditch-rider sheets", "open": "Water arrives (approx.)", "close": "Water stops (approx.)",
           "m3": "m³ at the field inlet", "next": "Next",
           "check": "Check on site: did water reach the tail? Start and end time. Times are approximate: flow builds up and falls off gradually; m³ is the water at the field inlet after canal losses.", "pump": "pump station",
           "none": "none", "escape_pct": "Tail escape (% of released)", "flag": "Warning"},
    "hy": {"title": "Ջրի մատակարարման պլան", "sim": "Մոդելավորում, ոչ ջրի հրաման։ Ջրթողները բացում են մարդիկ։",
           "net": "Սինթետիկ ցանց իրական մայր ջրանցքի վրա․ Ստորին Հրազդանի դիտարկվող գոտի, I և II հերթեր։",
           "supply": "Մատակարարում", "planning": "Պլանավորման օր", "account": "Ջրի հաշվեկշիռ (մ³, մոդելավորման գնահատական)",
           "released_head_m3": "Բաց թողնված գլխամասից", "main_canal_seepage_m3": "Կորուստ մայր ջրանցքում",
           "fill_and_drain_m3": "Չոր ջրանցքների լցում", "refill_m3": "Վերալցում փակվելուց հետո",
           "secondary_tertiary_seepage_m3": "Կորուստ երկրորդային և երրորդային ջրանցքներում",
           "turn_end_drain_m3": "Դատարկում ջրման հերթի վերջում", "holding_flow_m3": "Պահող հոսք կարճ ընդմիջումներում",
           "delivered_field_inlets_m3": "Հասցված դաշտերի մուտքին", "escape_m3": "Պոչային ջրթափ",
           "summary": "Ամփոփում", "due_ha": "Ժամկետում ջրման կարիք ունեցող հող (հա)",
           "ha_with_a_turn": "Ջրման հերթ ստացող հող (հա)", "need_met_pct": "Բավարարված կարիք (%)",
           "outlets": "Ջրթողներ՝ սպասարկված / մասամբ / չհասած", "last_arrival_h": "Վերջին ջուրը հասնում է (ժ) հետո",
           "not_reached": "Չհասած (ամենահրատապը առաջինը)", "outlet": "Ջրթող", "community": "Համայնք",
           "days": "Օր մինչև սթրես", "reason": "Պատճառ", "main_sheet": "Մայր ջրանցք՝ ջրթողների փոփոխություններ",
           "head_time": "Հրաման գլխամասում", "here_time": "Փոփոխություն ջրթողում", "lps": "լ/վ",
           "cards": "Ջրբաշխի մատյան", "open": "Ջուրը հասնում է (մոտ.)", "close": "Ջուրը դադարում է (մոտ.)",
           "m3": "մ³ դաշտի մուտքում", "next": "Հաջորդը",
           "check": "Ստուգել տեղում՝ ջուրը հասա՞վ պոչին։ Սկզբի և ավարտի ժամը։ Ժամերը մոտավոր են՝ հոսքն աստիճանաբար է աճում և նվազում, մ³-ը դաշտի մուտքին հասնող ջուրն է ջրանցքի կորուստներից հետո։", "pump": "պոմպակայան",
           "none": "չկա", "escape_pct": "Պոչային արտահոսք (% բաց թողնվածից)", "flag": "Զգուշացում"},
    "ru": {"title": "План подачи воды", "sim": "Моделирование, не распоряжение о подаче воды. Затворы открывают люди.",
           "net": "Синтетическая сеть на реальном магистральном канале; зона наблюдения Нижний Раздан, I и II очереди.",
           "supply": "Подача", "planning": "Дата планирования", "account": "Водный баланс (м³, оценки моделирования)",
           "released_head_m3": "Подано в голову", "main_canal_seepage_m3": "Потери в магистральном канале",
           "fill_and_drain_m3": "Заполнение сухих каналов", "refill_m3": "Повторное заполнение после закрытия",
           "secondary_tertiary_seepage_m3": "Потери во вторичных и третичных каналах",
           "turn_end_drain_m3": "Сброс в конце полива", "holding_flow_m3": "Поддерживающий расход в коротких паузах",
           "delivered_field_inlets_m3": "Доставлено к полям", "escape_m3": "Концевой сброс",
           "summary": "Итог", "due_ha": "Земля с потребностью в окне (га)", "ha_with_a_turn": "Земля, получающая полив (га)",
           "need_met_pct": "Покрыто потребности (%)", "outlets": "Водовыпуски: полностью / частично / не достигнуты",
           "last_arrival_h": "Последняя вода приходит через (ч)", "not_reached": "Не достигнуты (сначала самые срочные)",
           "outlet": "Водовыпуск", "community": "Община", "days": "Дней до стресса", "reason": "Причина",
           "main_sheet": "Магистральный канал: переключения затворов", "head_time": "Команда в голове",
           "here_time": "Переключение на затворе", "lps": "л/с", "cards": "Листы водораспределителя",
           "open": "Вода приходит (прибл.)", "close": "Вода уходит (прибл.)", "m3": "м³ у поля", "next": "Следующий",
           "check": "Проверить на месте: дошла ли вода до хвоста? Время начала и конца. Время приблизительное: расход нарастает и спадает постепенно; м³ - вода у поля после потерь в каналах.", "pump": "насосная станция",
           "none": "нет", "escape_pct": "Концевой сброс (% поданного)", "flag": "Предупреждение"},
}


# demonstration wording (expert panel 2026-11-03): calmer simulation line, readable names, whole numbers
DEMO_TXT = {
    "en": {"title": "Water delivery plan · simulation study",
           "sim": "Simulation on an irrigation network along the real main canal. People open the gates.",
           "branch": "Branch", "outlet": "Outlet", "stage": "Stage", "cards": "Sheets for the ditch riders",
           "escape_m3": "Spill at canal tails", "not_reached": "Land needing water that gets no turn, by stage",
           "nr_note": "Each parcel takes the colour of its outlet in this plan; it is not a statement about one field.",
           "due_ha": "Land needing water (ha)", "ha_with_a_turn": "Land given a turn (ha)",
           "need_met_pct": "Share of the need covered (%)", "outlets": "Outlets: full turn / part turn / no turn"},
    "hy": {"title": "Ջրաբաշխման պլան · մոդելավորում",
           "sim": "Մոդելավորում՝ իրական մայր ջրանցքի երկայնքով ոռոգման ցանցի վրա։ Ջրթողները բացում են մարդիկ։",
           "branch": "Ճյուղ", "outlet": "Ջրթող", "stage": "Հերթ", "cards": "Ջրբաշխի մատյան",
           "escape_m3": "Պոչային ջրթափ", "not_reached": "Ջրապահանջ ունեցող, բայց ջրման հերթ չստացող հող՝ ըստ հերթի",
           "nr_note": "Յուրաքանչյուր հողամաս ստանում է իր ջրթողի գույնը այս պլանում․ դա առանձին դաշտի մասին պնդում չէ։",
           "due_ha": "Ջրապահանջ ունեցող հող (հա)", "ha_with_a_turn": "Ջրման հերթ ստացող հող (հա)",
           "need_met_pct": "Ջրապահանջարկի բավարարված մասը (%)", "outlets": "Ջրթողներ՝ լրիվ / մասնակի / առանց ջրման հերթի"},
}


# the engine's fixed texts (wp_core/canal_planner_engine.py) in the report's language
ENGINE_TXT = {
    "en": {E.ROUTE_FLAG: "in some 12-hour blocks the supply is more than the canals can route: reduce the flow at the canal intake"},
    "hy": {E.ROUTE_FLAG: "որոշ 12-ժամյա միջակայքերում մատակարարումը գերազանցում է այն, ինչ ջրանցքները կարող են բաշխել․ նվազեցրեք հոսքը ջրընդունիչում",
           "canal level: the offtake cannot draw at this flow": "ջրանցքի մակարդակ՝ ջրթողը այս հոսքով ջուր վերցնել չի կարող",
           "full turn does not fit: supply under 60 % of due demand, fewer outlets get full turns":
               "լրիվ ջրման հերթը չի տեղավորվում․ մատակարարումը կարիքի 60 %-ից պակաս է, ավելի քիչ ջրթողներ են ստանում լրիվ պահ",
           "turn not finished in the window": "ջրման հերթը ժամկետում չի ավարտվում",
           "lower in the order of service; outlet does not take night turns":
               "սպասարկման հերթականությունում ավելի ցածր է․ ջրթողը գիշերային պահ չի ընդունում",
           "lower in the order of service; the window's water went to outlets ahead of it":
               "սպասարկման հերթականությունում ավելի ցածր է․ ժամկետի ջուրը գնաց նախորդ ջրթողներին"},
    "ru": {E.ROUTE_FLAG: "в некоторых 12-часовых интервалах подача больше, чем каналы могут распределить: уменьшите расход на водозаборе",
           "canal level: the offtake cannot draw at this flow": "уровень в канале: водовыпуск не может забирать при этом расходе",
           "full turn does not fit: supply under 60 % of due demand, fewer outlets get full turns":
               "полный полив не помещается: подача меньше 60 % потребности, меньше водовыпусков получают полный полив",
           "turn not finished in the window": "полив не заканчивается в окне",
           "lower in the order of service; outlet does not take night turns":
               "ниже в очереди обслуживания; водовыпуск не принимает ночной полив",
           "lower in the order of service; the window's water went to outlets ahead of it":
               "ниже в очереди обслуживания; вода окна ушла водовыпускам впереди"},
}


@lru_cache(maxsize=1)
def parcel_geometry() -> dict:
    """Parcel outlines of the map in use, keyed by the network's parcel id (stays on the server)."""
    path = ROOT / E.ACTIVE["map_dir"] / "parcels.geojson.gz"
    with read_guard.ReadGuard():
        d = json.loads(gzip.decompress(path.read_bytes()))
    return {f["properties"]["id"]: f["geometry"] for f in d["features"]}


CROP_GROUP = {"ORCHARD": "P", "VINEYARD": "P", "PERENNIAL": "P", "FIELD_CROP": "F", "CROPLAND_UNCERTAIN": "F",
              "UNCLASSIFIED": "F"}


@lru_cache(maxsize=1)
def crop_layer() -> tuple[bytes, dict]:
    """Farm parcels by crop group for the demonstration map (owner 2026-11-03): c = P perennial plantings, F field
    crops, from the AI-interpreted land types of the map in use. Each outline carries only its position in this layer
    (feature id 0, 1, 2 ...) so a plan can recolour it with a small status list; no parcel id or code leaves the
    server. Returns (gzip GeoJSON, {network parcel id: position})."""
    path = ROOT / E.ACTIVE["map_dir"] / "parcels.geojson.gz"
    with read_guard.ReadGuard():
        d = json.loads(gzip.decompress(path.read_bytes()))
    feats, pos = [], {}
    for f in d["features"]:
        c = CROP_GROUP.get(f["properties"].get("t"))
        if c:
            pos[f["properties"]["id"]] = len(feats)
            feats.append({"type": "Feature", "id": len(feats), "geometry": f["geometry"], "properties": {"c": c}})
    gz = gzip.compress(json.dumps({"type": "FeatureCollection", "features": feats}, separators=(",", ":")).encode(),
                       6, mtime=0)
    return gz, pos


def crop_layer_gz() -> bytes:
    return crop_layer()[0]


@lru_cache(maxsize=1)
def community_layer() -> dict:
    """Community boundaries and names for the demonstration map (owner 2026-11-03: "add community boundaries and
    names"): the outlines of the map in use with the Armenian and English names, plus one label point per community
    (the point deepest inside its outline). Nothing else from the layer is sent."""
    from shapely.geometry import mapping, shape
    from shapely.ops import polylabel
    from wp_core.agent_queries_v7 import COMMUNITY_NAMES
    en = {hy: e for hy, e, _ru in COMMUNITY_NAMES}
    path = ROOT / E.ACTIVE["map_dir"] / "communities.geojson"
    with read_guard.ReadGuard():
        d = json.loads(path.read_text(encoding="utf-8"))
    areas, labels = [], []
    for f in d["features"]:
        hy = f["properties"]["name"]
        names = {"hy": hy, "en": en.get(hy, hy)}
        g = shape(f["geometry"])
        big = max(g.geoms, key=lambda x: x.area) if g.geom_type == "MultiPolygon" else g
        pt = polylabel(big, tolerance=1e-4)
        areas.append({"type": "Feature", "geometry": f["geometry"], "properties": names})
        labels.append({"type": "Feature", "geometry": mapping(pt), "properties": names})
    return {"areas": {"type": "FeatureCollection", "features": areas},
            "labels": {"type": "FeatureCollection", "features": labels}}


@lru_cache(maxsize=1)
def display_names():
    """Readable names for the demonstration page: main-canal offtakes numbered from the head (Branch 1, 2, ...),
    their secondary canals with the same number, and outlets numbered along each branch from its intake (3.5)."""
    n = E.network()[0]
    offs = n.index[n.kind.isin(["main_gate", "main_pump"])]
    order = sorted(offs, key=lambda k: E._dist_from_head(n, k))
    off_no = {k: i + 1 for i, k in enumerate(order)}
    branch_no = {f"{k}-S1": i for k, i in off_no.items()}
    out_no, pos = {}, {}
    for k in n.index[n.kind.isin(["tertiary_gate", "direct_outlet"])]:
        x, along = k, float(n.at[k, "path_m"] or 0)
        if n.at[k, "kind"] == "tertiary_gate":
            gate = n.at[n.at[k, "parent"], "parent"]                         # tertiary canal -> secondary gate
            along += float(n.at[gate, "path_m"] or 0)
            x = gate
        pos.setdefault(n.at[x, "parent"], []).append((along, k))
    for sec, items in pos.items():
        for i, (_, k) in enumerate(sorted(items)):
            out_no[k] = f"{branch_no.get(sec, '?')}.{i + 1}"
    return off_no, branch_no, out_no


def tr(text, lang):
    return ENGINE_TXT.get(lang, {}).get(text, text) if text else text


def SupplyRefused_date():
    return SU.SupplyRefused("no start date found (e.g. from 1 July, հուլիսի 1-ից)")


def _iso(t):
    return None if t is None or pd.isna(t) else pd.Timestamp(t).strftime("%Y-%m-%d %H:%M")


def _clock(t):
    return "" if t is None or pd.isna(t) else pd.Timestamp(t).strftime("%d.%m %H:%M")


class Planner:
    """Planner state for one page: drafts and confirmed supplies live in the ledger; plans in memory."""

    def __init__(self, ledger):
        self.ledger = ledger
        self.drafts, self.plans, self.snaps = {}, {}, {}

    # ---------------------------------------------------------- supply card
    def parse(self, text: str, day: str | None, lang: str = "en", year: int = 2025) -> dict:
        """day None: the date is read from the sentence ("հուլիսի 1-ից", "from 1 July"), in the given season year."""
        if day:
            d = date.fromisoformat(str(day))
        else:
            d = SU.read_date(str(text)[:300], int(year))
            if d is None:
                raise SupplyRefused_date()
        if not PLAN_DATES[0] <= d <= PLAN_DATES[1]:
            raise SU.SupplyRefused(f"planning date must be between {PLAN_DATES[0]} and {PLAN_DATES[1]}")
        s = SU.parse_supply(str(text)[:300], SU.planning_moment(d))
        if s["horizon"] != "operational":
            raise SU.SupplyRefused("windows of up to 10 days for now; month and season plans come next")
        self.drafts[s["supply_id"]] = s
        tok = SU.propose(self.ledger, s)["token"]
        return {**SU.card(s, lang), "token": tok, "start": s["start"], "end": s["end"], "head_m3": s["head_m3"],
                "hours": s["hours"], "m3s": [x["m3s"] for x in s["segments"]], "planning": s["planning"][:10]}

    def confirm(self, supply_id: str, token: str) -> dict:
        SU.confirm(self.ledger, supply_id, token)
        return {"ok": True, "supply_id": supply_id}

    # ---------------------------------------------------------- plan
    def _snapshot(self, d: date, days: int):
        key = (d, days)
        if key not in self.snaps:
            while len(self.snaps) >= 3:                                  # a few snapshots kept (about 13 s each to build)
                self.snaps.pop(next(iter(self.snaps)))
            self.snaps[key] = ST.snapshot(d, "normal", days)
        return self.snaps[key]

    def run(self, supply_id: str, losses: str = "assumed") -> dict:
        s = SU.confirmed(self.ledger, supply_id)                         # only a confirmed supply can be planned
        if losses not in ("low", "assumed", "high"):
            raise ValueError("losses")
        d = datetime.fromisoformat(s["planning"]).date()
        snap = self._snapshot(d, int(math.ceil(s["hours"] / 24)) + 7)
        p = E.plan(s, losses=losses, snap=snap)
        stress = E.operating_stress_tests(p) if hasattr(E, "operating_stress_tests") else []
        self.plans[p.plan_id] = {"plan": p, "stress": stress}
        while len(self.plans) > KEEP:
            self.plans.pop(next(iter(self.plans)))
        return self.page_data(p.plan_id)

    def _community(self, p) -> dict:
        par = p.outlets.attrs.get("parcels")
        if par is None or "community" not in par:
            return {}
        return par.groupby("outlet").community.agg(lambda x: x.mode().iat[0] if len(x.mode()) else "").to_dict()

    def page_data(self, plan_id: str) -> dict:
        p = self.plans[plan_id]["plan"]
        o, com = p.outlets, self._community(p)
        took = o.delivered_m3 > 0
        outlets = [{"n": k, "s": r.status, "o": _iso(r.open), "c": _iso(r.close), "w": _iso(r.water_from),
                    "l": None if pd.isna(r.lps) else int(r.lps), "d": round(float(r.delivered_m3)),
                    "need": round(float(r.need_m3)), "u": None if pd.isna(r.urgency_days) else float(r.urgency_days),
                    "k": int(r.klass), "com": com.get(k, ""), "r": r.reason or "", "ha": round(float(r.due_ha), 2),
                    "st": r.stage}
                   for k, r in o.iterrows()]
        canals = {}
        for col in ("secondary", "unit"):
            for c, g in o[took].groupby(col):
                if c in canals or c in g.index:
                    continue
                canals[c] = {"from": _iso(g.water_from.min()), "to": _iso(g.close.max())}
        for k, r in p.offtakes.iterrows():
            canals[k] = {"from": _iso(r.open), "to": _iso(r.close)}
        offt = [{"n": k, "open": _iso(r.open), "close": _iso(r.close), "peak_lps": int(r.peak_lps), "pump": bool(r.pump),
                 "section": r.section, "changes": r.changes} for k, r in p.offtakes.sort_values("open").iterrows()]
        sm = p.summary
        keep = ("due_ha", "ha_with_a_turn", "ha_with_a_turn_by_group", "due_ha_by_group", "need_met_pct",
                "outlets_due", "outlets_served", "outlets_partly", "outlets_not_reached", "not_reached_ha",
                "last_arrival_h", "escape_pct", "routing_flag", "routing_error", "escape_waste_warning",
                "check_gates_assumed", "delivered_share_of_released_pct", "label")
        ends = [x for x in [p.supply["end"], *(c["to"] for c in canals.values())] if x]
        return {"plan_id": p.plan_id, "planning": p.planning[:10], "losses": p.losses,
                "supply": {"start": p.supply["start"], "end": p.supply["end"], "hours": p.supply["hours"],
                           "head_m3": p.supply["head_m3"], "segments": p.supply["segments"]},
                "window": {"start": p.supply["start"], "last": max(ends)},
                "account": p.account, "summary": {k: sm.get(k) for k in keep}, "outlets": outlets,
                "canals": canals, "offtakes": offt,
                "stress": [{k: v for k, v in x.items() if not isinstance(v, (list, dict))} for x in self.plans[plan_id]["stress"]],
                "crops": self.crops(p)}

    # ---------------------------------------------------------- parcels that get water (owner 2026-11-03: blue parcels)
    def parcels_geojson(self, plan_id: str) -> bytes:
        """Every parcel that gets water on the planning date (active or expected land with an outlet) as a gzip GeoJSON
        layer. c = crop group (P perennial plantings, F field crops; from the AI-interpreted land types, owner
        2026-11-03); s = w (gets water, full turn), p (partly), n (needs water, no turn), x (no need in this window);
        a / b = hours after the window start when its outlet's turn opens / closes. No parcel id or code."""
        rec = self.plans[plan_id]
        if "parcels_gz" in rec:
            return rec["parcels_gz"]
        p = rec["plan"]
        o, par = p.outlets, p.outlets.attrs["parcels"]
        t0 = pd.Timestamp(p.supply["start"])
        hrs = lambda t: None if pd.isna(t) else round((pd.Timestamp(t) - t0).total_seconds() / 3600, 2)
        st = {k: ("n", None, None) if r.delivered_m3 <= 0 else
              ("w" if r.status == "served" else "p", hrs(r.open), hrs(r.close)) for k, r in o.iterrows()}
        geom = parcel_geometry()
        feats = []
        for item, out, due, grp in zip(par.item_id, par.outlet, par.due, par.group):
            g = geom.get(item)
            if g is None:
                continue
            s, a, b = st[out] if due and out in st else ("x", None, None)
            feats.append({"type": "Feature", "geometry": g,
                          "properties": {"s": s, "a": a, "b": b, "c": "P" if grp == "perennial" else "F"}})
        rec["parcels_gz"] = gzip.compress(json.dumps({"type": "FeatureCollection", "features": feats},
                                                     separators=(",", ":")).encode(), 6, mtime=0)
        return rec["parcels_gz"]

    def parcel_states(self, plan_id: str) -> dict:
        """The plan's status of every crop-layer parcel that gets water on the planning date, as parallel lists:
        k = position in the crop layer, s = w / p / n / x (as parcels_geojson), a / b = hours of its outlet's turn."""
        rec = self.plans[plan_id]
        if "states" in rec:
            return rec["states"]
        p = rec["plan"]
        o, par = p.outlets, p.outlets.attrs["parcels"]
        t0 = pd.Timestamp(p.supply["start"])
        hrs = lambda x: None if pd.isna(x) else round((pd.Timestamp(x) - t0).total_seconds() / 3600, 2)
        st = {k: ("n", None, None) if r.delivered_m3 <= 0 else
              ("w" if r.status == "served" else "p", hrs(r.open), hrs(r.close)) for k, r in o.iterrows()}
        pos = crop_layer()[1]
        out = {"k": [], "s": [], "a": [], "b": []}
        for item, outlet, due in zip(par.item_id, par.outlet, par.due):
            k = pos.get(item)
            if k is None:
                continue
            s, a, b = st[outlet] if due and outlet in st else ("x", None, None)
            for key, v in (("k", k), ("s", s), ("a", a), ("b", b)):
                out[key].append(v)
        rec["states"] = out
        return out

    @staticmethod
    def crops(p) -> dict:
        """Hectares per crop group: land that gets water on the planning date, land that needs water in this window,
        and land that gets a turn (its outlet takes water) in this plan."""
        o, par = p.outlets, p.outlets.attrs["parcels"]
        took = set(o.index[o.delivered_m3 > 0])
        out = {}
        for key, m in (("perennial", par.group == "perennial"), ("field", par.group != "perennial")):
            x = par[m]
            out[key] = {"active_ha": round(float(x.area_ha.sum()), 2),
                        "due_ha": round(float(x.area_ha[x.due].sum()), 2),
                        "turn_ha": round(float(x.area_ha[x.due & x.outlet.isin(took)].sum()), 2)}
        return out

    # ---------------------------------------------------------- report and Excel
    def report_html(self, plan_id: str, lang: str = "hy", demo: bool = False) -> str:
        """The printable report. demo=True (demonstration page): readable names (Branch 3 · Outlet 3.5) instead of
        network ids, no community names, no plan id, not-reached area by stage, whole hectares."""
        p = self.plans[plan_id]["plan"]
        t = {**TXT.get(lang, TXT["en"]), **(DEMO_TXT.get(lang, DEMO_TXT["en"]) if demo else {})}
        t.setdefault("branch", DEMO_TXT.get(lang, DEMO_TXT["en"])["branch"])
        e = html.escape
        o, com = p.outlets, self._community(p)
        off_no, branch_no, out_no = display_names()
        nm_off = (lambda k: f"{t['branch']} {off_no.get(k, '?')}") if demo else (lambda k: k)
        nm_sec = (lambda k: f"{t['branch']} {branch_no.get(k, '?')}") if demo else (lambda k: k)
        nm_out = (lambda k: f"{t['outlet']} {out_no.get(k, '?')}") if demo else (lambda k: k)
        ha = (lambda v: f"{v:,.0f}") if demo else (lambda v: f"{v:,.2f}")
        acc = "".join(f"<tr><td>{e(t.get(k, k))}</td><td class=n>{v:,}</td></tr>" for k, v in p.account.items()
                      if k != "closes")
        sm = p.summary
        lines = [("due_ha", ha(sm["due_ha"])), ("ha_with_a_turn", ha(sm["ha_with_a_turn"])),
                 ("need_met_pct", f"{sm['need_met_pct']:.0f}" if demo else sm["need_met_pct"]),
                 ("outlets", f"{sm['outlets_served']} / {sm['outlets_partly']} / {sm['outlets_not_reached']}"),
                 ("last_arrival_h", sm.get("last_arrival_h"))]
        if not demo:
            lines.append(("escape_pct", sm.get("escape_pct")))
        summ = "".join(f"<tr><td>{e(t[k])}</td><td class=n>{v}</td></tr>" for k, v in lines)
        flag = sm.get("routing_error") or sm.get("routing_flag")
        nr = o[o.delivered_m3 <= 0]
        if demo:
            by = nr.groupby("stage").due_ha.sum()
            nr_table = (f"<table><tr><th>{e(t['stage'])}</th><th>ha</th></tr>"
                        + "".join(f"<tr><td>{e(s)}</td><td class=n>{v:,.0f}</td></tr>" for s, v in by.items())
                        + f"</table><p class=small>{e(t['nr_note'])}</p>")
        else:
            nr = nr.sort_values(["urgency_days", "klass"]).head(60)
            rows = "".join(f"<tr><td>{e(k)}</td><td>{e(com.get(k, ''))}</td><td class=n>{r.due_ha:.2f}</td>"
                           f"<td class=n>{'' if pd.isna(r.urgency_days) else int(r.urgency_days)}</td>"
                           f"<td>{e(tr(r.reason, lang) or '')}</td></tr>" for k, r in nr.iterrows())
            rows = rows or f"<tr><td colspan=5>{e(t['none'])}</td></tr>"
            nr_table = (f"<table><tr><th>{e(t['outlet'])}</th><th>{e(t['community'])}</th><th>ha</th>"
                        f"<th>{e(t['days'])}</th><th>{e(t['reason'])}</th></tr>{rows}</table>")
        main = "".join(f"<tr><td>{e(nm_off(k))}{' · ' + e(t['pump']) if r.pump and not demo else ''}</td>"
                       f"<td>{e(c['head'])}</td><td>{e(c['here'])}</td><td class=n>{c['lps']}</td></tr>"
                       for k, r in p.offtakes.sort_values("open").iterrows() for c in r.changes)
        cards = []
        served = o[o.delivered_m3 > 0].copy()
        for sec, g in served.sort_values("open").groupby("secondary", sort=False):
            g = g.sort_values("open")
            nxt = [nm_out(x) for x in g.index[1:]] + [""]
            rows = "".join(f"<tr><td>{e(nm_out(k))}</td><td>{e(_clock(r.open))}</td><td>{e(_clock(r.close))}</td>"
                           f"<td class=n>{'' if pd.isna(r.lps) else int(r.lps)}</td><td class=n>{round(r.delivered_m3):,}</td>"
                           f"<td>{e(n_)}</td><td>☐</td></tr>" for (k, r), n_ in zip(g.iterrows(), nxt))
            head = e(nm_sec(sec)) if demo else f"{e(sec)} · {e(com.get(g.index[0], ''))}"
            cards.append(f"<div class=card><h3>{head}</h3>"
                         f"<p class=wm2>{e(t['sim'])}</p><table><tr><th>{e(t['outlet'])}</th><th>{e(t['open'])}</th>"
                         f"<th>{e(t['close'])}</th><th>{e(t['lps'])}</th><th>{e(t['m3'])}</th><th>{e(t['next'])}</th>"
                         f"<th>✓</th></tr>{rows}</table><p class=small>{e(t['check'])}</p></div>")
        s = p.supply
        flow = " / ".join(f"{x['m3s']:g}" for x in s["segments"])
        ident = "" if demo else f" · {e(p.plan_id)}"
        first_col = t["branch"] if demo else t["outlet"]
        return (f"<!doctype html><html lang={lang}><meta charset=utf-8><meta name=viewport content='width=device-width,"
                f"initial-scale=1'><title>{e(t['title'])}</title>"
                "<link rel=stylesheet href='https://fonts.googleapis.com/css2?family=Noto+Sans+Armenian:wght@400;600;700"
                "&family=Noto+Sans:wght@400;600;700&display=swap'><style>"
                "body{font:14px/1.45 'Noto Sans','Noto Sans Armenian',system-ui,sans-serif;margin:24px;color:#1F2D38;"
                "max-width:1000px}h1{color:#0F1B24;margin:0 0 6px}h2{color:#0F1B24;font-size:18px;margin:20px 0 6px}"
                "table{border-collapse:collapse;width:100%;margin:6px 0 14px}td,th{border:1px solid #D5DCE1;padding:5px 7px;"
                "text-align:left;vertical-align:top}th{background:#F5F7F8}.n{text-align:right;font-feature-settings:'tnum'}"
                ".sim{color:#5B6B76;font-size:13px}.card{border:1.5px solid #0F1B24;border-radius:10px;padding:10px 12px;"
                "margin:14px 0;page-break-inside:avoid}.card h3{margin:0 0 4px;font-size:16px}.wm2{color:#5B6B76;"
                "margin:0 0 6px;font-size:12px}.small{font-size:12px;color:#5B6B76}.flag{background:#F5F7F8;border:1px solid"
                " #E3E8EC;padding:6px 10px;border-radius:6px;font-size:13px}@media print{.card{break-inside:avoid}}</style>"
                f"<h1>{e(t['title'])}</h1><p class=sim>{e(t['sim'])}</p><p>{e(t['net'])}</p>"
                f"<p><b>{e(t['supply'])}:</b> {e(flow)} m³/s · {e(s['start'])} → {e(s['end'])} · {s['head_m3']:,} m³ · "
                f"<b>{e(t['planning'])}:</b> {e(p.planning[:10])}{ident}</p>"
                + (f"<p class=flag><b>{e(t['flag'])}:</b> {e(tr(flag, lang))}</p>" if flag else "")
                + f"<h2>{e(t['summary'])}</h2><table>{summ}</table>"
                f"<h2>{e(t['account'])}</h2><table>{acc}</table>"
                f"<h2>{e(t['not_reached'])}</h2>{nr_table}"
                f"<h2>{e(t['main_sheet'])}</h2><table><tr><th>{e(first_col)}</th>"
                f"<th>{e(t['head_time'])}</th><th>{e(t['here_time'])}</th><th>{e(t['lps'])}</th></tr>{main}</table>"
                f"<h2 id=cards>{e(t['cards'])}</h2>{''.join(cards)}<p class=sim>{e(t['sim'])}</p>")

    def excel_rows(self, plan_id: str) -> list:
        p = self.plans[plan_id]["plan"]
        com = self._community(p)
        rows = [[TXT["en"]["sim"]], ["Outlet", "Community", "Status", "Due ha", "Need m3", "Delivered m3", "Opens",
                                     "Closes", "l/s", "Days to stress", "Reason"]]
        for k, r in p.outlets.iterrows():
            rows.append([k, com.get(k, ""), r.status, round(float(r.due_ha), 2), round(float(r.need_m3)),
                         round(float(r.delivered_m3)), _iso(r.open) or "", _iso(r.close) or "",
                         None if pd.isna(r.lps) else int(r.lps),
                         None if pd.isna(r.urgency_days) else float(r.urgency_days), r.reason or ""])
        return rows
