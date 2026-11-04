"""Chat router with escalation (owner, 2026-11-02, change log "chat router with escalation"). Fixed code, never a model.

One chat agent, the router (the main agent), talks to the owner and is the only agent that delegates. The ladder:
  1. answer directly with its own tools when they are enough (which gate serves a parcel, hectares by type in a
     community, the network, the sharing rules);
  2. otherwise ask the one best agent (plans: planner; crops and stress: agronomy; data doubts: data auditor; trends:
     season monitor; summaries: report writer; pre-season briefs: season prep);
  3. every agent replies in one fixed shape through submit_reply: answered / partly (missing X, suggest agent Y) /
     outside my role; the router may then ask the suggested agent and combine the answers;
  4. if nobody can answer, the router says so plainly and names what is missing; it never guesses.

Limits enforced here (the live adapter's hooks call RouterGuard; the fixture router uses the same guard):
  - at most MAX_AGENTS agents per question; no agent asked twice for the same question;
  - only the router delegates (agents never call agents); public_qa and agents outside ROUTABLE are never called;
  - a plan always ends with the reviewer: the planner is refused when no slot is left for the reviewer, and the
    answer is withheld if the reviewer did not come after the planner;
  - each agent keeps its own tool list (wp_core/canal_ops_policy_v6.py), also when the router calls it.
"""
from __future__ import annotations

import re

MAX_AGENTS = 3
ROUTABLE = ("planner", "reviewer", "agronomy", "auditor", "season_monitor", "report_writer", "season_prep")
# live chat after the agentic AI review (owner "approve", 2026-11-04): only plans go to agents; the other specialists'
# jobs are instruction blocks of the main agent (FOLDED_ROLES), so a question is one agent loop
LIVE_ROUTABLE = ("planner", "reviewer")
NEVER = ("public_qa",)
STATUSES = ("answered", "partly", "outside_role")
NAMES = {"planner": "the planner", "reviewer": "the reviewer", "agronomy": "agronomy", "auditor": "the data auditor",
         "season_monitor": "the season monitor", "report_writer": "the report writer", "season_prep": "season prep"}
ANSWER_MAX = 1500
MISSING_MAX = 200
# fixed texts the owner sees, in the chat languages (owner 2026-11-02: deviation 4 accepted with hy and ru versions);
# agent names as the object of "asked" (hy dative, ru accusative)
ASKED_NAMES = {
    "hy": {"planner": "պլանավորողին", "reviewer": "վերանայողին", "agronomy": "ագրոնոմին",
           "auditor": "տվյալների աուդիտորին", "season_monitor": "սեզոնի դիտորդին",
           "report_writer": "հաշվետվություն գրողին", "season_prep": "սեզոնի նախապատրաստման գործակալին"},
    "ru": {"planner": "планировщика", "reviewer": "рецензента", "agronomy": "агронома", "auditor": "аудитора данных",
           "season_monitor": "монитора сезона", "report_writer": "составителя отчётов",
           "season_prep": "агента подготовки сезона"},
}
TEXTS = {
    "en": {"own_tools": "Answered with my own tools.", "asked": "Asked ", "then": ", then ",
           "checked": "; the reviewer checked", "end": ".",
           "not_reviewed": "A plan was not reviewed, so it is not shown.",
           "failed_checks": "The answer did not pass the checks, so it is not shown.",
           "repaired": "Rewrote the answer once after the checks.",
           "cannot": "I cannot answer this with the data and tools here. Asked: {who}. Missing: {miss}. "
                     "Simulation: synthetic network on the real main canal.",
           "no_agent": "no agent", "no_tool": "the data or a tool for this question"},
    "hy": {"own_tools": "Պատասխանեցի իմ գործիքներով։", "asked": "Հարցրի ", "then": ", հետո ",
           "checked": ", վերանայողը ստուգեց", "end": "։",
           "not_reviewed": "Պլանը չի վերանայվել, ուստի այն ցույց չի տրվում։",
           "failed_checks": "Պատասխանը չի անցել ստուգումները, ուստի այն ցույց չի տրվում։",
           "repaired": "Ստուգումներից հետո պատասխանը մեկ անգամ վերաշարադրեցի։",
           "cannot": "Այստեղ եղած տվյալներով և գործիքներով չեմ կարող պատասխանել այս հարցին։ Հարցրի՝ {who}։ "
                     "Բացակայում է՝ {miss}։ Մոդելավորում՝ սինթետիկ ցանց իրական մայր ջրանցքի վրա։",
           "no_agent": "ոչ մի գործակալի", "no_tool": "այս հարցի համար տվյալներ կամ գործիք"},
    "ru": {"own_tools": "Ответил своими инструментами.", "asked": "Спросил ", "then": ", затем ",
           "checked": "; рецензент проверил", "end": ".",
           "not_reviewed": "План не прошёл рецензента, поэтому он не показан.",
           "failed_checks": "Ответ не прошёл проверки, поэтому он не показан.",
           "repaired": "Переписал ответ один раз после проверок.",
           "cannot": "Я не могу ответить на этот вопрос с имеющимися данными и инструментами. Спросил: {who}. "
                     "Не хватает: {miss}. Моделирование: синтетическая сеть на реальном магистральном канале.",
           "no_agent": "ни одного агента", "no_tool": "данных или инструмента для этого вопроса"},
}
# the "What I did" step labels (English labels as keys; unknown labels stay as they are)
STEP_TEXTS = {
    "hy": {"Read the network": "Կարդացի ցանցը", "Checked supply and need": "Ստուգեցի մատակարարումը և կարիքը",
           "Compared sharing rules": "Համեմատեցի բաշխման կանոնները", "Simulated a plan": "Մոդելավորեցի պլան",
           "Checked tail-end branches": "Ստուգեցի պոչային ճյուղերը", "Read past dekads": "Կարդացի նախորդ դեկադները",
           "Read your standing rules": "Կարդացի ձեր մշտական կանոնները",
           "Saved a plan for your review (not approved)": "Պահպանեցի պլանը ձեր դիտարկման համար (հաստատված չէ)",
           "Read the canal tree": "Կարդացի ջրանցքների ծառը", "Read a gate": "Կարդացի ջրթողը",
           "Read a parcel": "Կարդացի հողակտորը", "Read a community": "Կարդացի համայնքը",
           "Searched the map": "Որոնեցի քարտեզում", "The planner chose its plans": "Պլանավորողն ընտրեց իր պլանները",
           "The reviewer gave its objections": "Վերանայողը ներկայացրեց իր առարկությունները",
           "Read the round schedule": "Կարդացի հերթերի ժամանակացույցը",
           "An agent replied to the router": "Մասնագետ գործակալը պատասխանեց",
           "Asked a specialist agent": "Հարցրի մասնագետ գործակալին", "Used a tool": "Օգտագործեցի գործիք",
           "Compared sharing rules this dekad": "Համեմատեցի բաշխման կանոնները այս դեկադում"},
    "ru": {"Read the network": "Прочитал сеть", "Checked supply and need": "Проверил подачу и потребность",
           "Compared sharing rules": "Сравнил правила распределения", "Simulated a plan": "Смоделировал план",
           "Checked tail-end branches": "Проверил хвостовые ветви", "Read past dekads": "Прочитал прошлые декады",
           "Read your standing rules": "Прочитал ваши постоянные правила",
           "Saved a plan for your review (not approved)": "Сохранил план для вашего рассмотрения (не утверждён)",
           "Read the canal tree": "Прочитал дерево каналов", "Read a gate": "Прочитал затвор",
           "Read a parcel": "Прочитал участок", "Read a community": "Прочитал общину",
           "Searched the map": "Искал на карте", "The planner chose its plans": "Планировщик выбрал свои планы",
           "The reviewer gave its objections": "Рецензент дал свои замечания",
           "Read the round schedule": "Прочитал график очерёдности",
           "An agent replied to the router": "Агент-специалист ответил",
           "Asked a specialist agent": "Спросил агента-специалиста", "Used a tool": "Использовал инструмент",
           "Compared sharing rules this dekad": "Сравнил правила распределения в этой декаде"},
}


def text(key: str, lang: str = "en") -> str:
    """A fixed text in the owner's language (English when the language is not covered)."""
    return TEXTS.get(lang, TEXTS["en"])[key]


def steps_in(steps, lang: str = "en") -> list:
    """The "What I did" lines in the owner's language."""
    tr = STEP_TEXTS.get(lang, {})
    return [tr.get(s, s) for s in steps]


def _asked(agent: str, lang: str) -> str:
    return ASKED_NAMES[lang][agent] if lang in ASKED_NAMES else NAMES[agent]

FOLDED_ROLES = """You also do the specialists' jobs yourself, with your own tools (no other agent is asked for them):
- crops, crop stage, perennials and sensitive areas: by gate and community (get_gate, get_community);
- doubts about a parcel's label: compare it with the parcels on the same canal (get_gate); offer fixes only as
  questions for the owner;
- trends over weeks: keep current irrigation stress, change and the forecast apart (get_history);
- summaries, sheets and pre-season community briefs by land type: from tool results only."""
LIVE_ROUTING_GUIDE = """Routing: answer every question yourself with your tools. Ask agents only for a plan for a day,
week or dekad: first the planner, then always the reviewer; each ends with submit_reply, and you combine and narrate.
Never ask any other agent. Start a plan answer with the path: "Asked the planner; the reviewer checked."
""" + FOLDED_ROLES
ROUTING_GUIDE = """Routing (you are the router; only you ask other agents):
1. Answer yourself when your tools are enough: which gate or canal serves a parcel (get_parcel), hectares by land type
   in a community (get_community), a gate or canal (get_gate, get_tree), the network, the sharing rules, the schedule,
   and which outlets or canals are worst off or most at risk this dekad (compare_rules lists them worst first).
   Each agent you ask adds about a minute, so ask one only when your own tools cannot answer.
2. Otherwise ask the one best agent: a plan for a dekad -> planner (then always the reviewer); crops, crop stage,
   stress, perennials -> agronomy; doubts about the data or a parcel's label -> auditor; trends over weeks ->
   season_monitor; a summary or sheet -> report_writer; a pre-season community brief -> season_prep.
3. Each agent ends with submit_reply: answered, partly (what is missing, which agent could help) or outside_role.
   If it says partly or outside_role, you may ask the agent it suggests and combine the answers.
4. At most 3 agents per question, each once. If nobody can answer, say so plainly and name what is missing; never guess.
Start your answer with the path, e.g. "Asked agronomy, then the planner; the reviewer checked." when you asked anyone."""


class RouteRefused(ValueError):
    pass


def parse_reply(obj) -> dict:
    """The fixed reply shape. Raises RouteRefused when the shape is wrong."""
    if not isinstance(obj, dict):
        raise RouteRefused("reply must be an object")
    status = obj.get("status")
    if status not in STATUSES:
        raise RouteRefused(f"status must be one of {', '.join(STATUSES)}")
    answer = str(obj.get("answer") or "").strip()
    missing = str(obj.get("missing") or "").strip()
    suggest = obj.get("suggest") or None
    if len(answer) > ANSWER_MAX or len(missing) > MISSING_MAX:
        raise RouteRefused("answer or missing too long")
    if status == "answered" and not answer:
        raise RouteRefused("an answered reply needs its answer")
    if status == "partly" and not (answer and missing):
        raise RouteRefused("a partly reply needs what was answered and what is missing")
    if status == "outside_role" and answer:
        raise RouteRefused("an outside_role reply carries no answer")
    if suggest is not None and suggest not in ROUTABLE:
        raise RouteRefused(f"suggest must be one of {', '.join(ROUTABLE)}")
    return {"status": status, "answer": answer, "missing": missing, "suggest": suggest}


class RouterGuard:
    """Per-question state: who was asked, in which order, and what each replied."""

    def __init__(self, routable=ROUTABLE):
        self.routable = tuple(routable)
        self.asked: list[str] = []
        self.replies: dict[str, dict] = {}

    def delegate(self, caller, agent) -> tuple[bool, str]:
        """May the caller ask this agent now? Records the call when allowed."""
        if caller not in (None, "main"):
            return False, "agents never call agents; only the router delegates"
        if agent in NEVER or agent not in self.routable:
            return False, f"{agent} cannot be asked in chat"
        if agent in self.asked:
            return False, f"{agent} was already asked for this question"
        if len(self.asked) >= MAX_AGENTS:
            return False, f"at most {MAX_AGENTS} agents per question"
        if agent == "planner" and ("reviewer" in self.asked or len(self.asked) >= MAX_AGENTS - 1):
            return False, "a plan needs a free slot for the reviewer after it"     # audit C1
        self.asked.append(agent)
        return True, ""

    def reply(self, agent, obj) -> dict:
        """Record an agent's structured reply (only agents the router asked may reply, once)."""
        if agent not in self.asked:
            raise RouteRefused("only an agent the router asked can reply")
        if agent in self.replies:
            raise RouteRefused("one reply per agent")
        r = parse_reply(obj)
        self.replies[agent] = r
        return r

    def plan_reviewed(self) -> bool:
        """No plan without the reviewer after the planner."""
        if "planner" not in self.asked:
            return True
        return "reviewer" in self.asked and self.asked.index("reviewer") > self.asked.index("planner")

    def nobody_answered(self) -> bool:
        """Agents were asked and none gave any part of an answer."""
        return bool(self.asked) and len(self.replies) == len(self.asked) and \
            all(r["status"] == "outside_role" for r in self.replies.values())

    def missing(self) -> list[str]:
        return [r["missing"] for r in self.replies.values() if r["missing"]]

    def path(self, lang: str = "en") -> str:
        """'What I did' line, e.g. 'Asked agronomy, then the planner; the reviewer checked.'"""
        t = TEXTS.get(lang, TEXTS["en"])
        if not self.asked:
            return t["own_tools"]
        names = [a for a in self.asked if a != "reviewer"]
        out = t["asked"] + t["then"].join(_asked(a, lang) for a in names) if names else ""
        if "reviewer" in self.asked:
            out = (out + t["checked"]) if names else t["asked"] + _asked("reviewer", lang)
        return out + t["end"]


def cannot_answer(guard: RouterGuard, lang: str = "en") -> str:
    """The plain 'nobody can answer' text, naming what is missing (fixed wording, no numbers)."""
    from wp_core.canal_ops_checker import check_internal         # audit C2: agents' words pass the Checker too
    miss = "; ".join(m for m in guard.missing() if check_internal(m, {}).ok) or text("no_tool", lang)
    who = ", ".join(_asked(a, lang) for a in guard.asked) or text("no_agent", lang)
    return text("cannot", lang).format(who=who, miss=miss)


def delegation_target(tool_input: dict) -> str | None:
    """The agent named in an Agent / Task tool call."""
    v = (tool_input or {}).get("subagent_type") or ""                # audit M1: the field the SDK uses
    v = str(v).strip()
    return v if re.fullmatch(r"[a-z_]{2,40}", v) else (v or None)
