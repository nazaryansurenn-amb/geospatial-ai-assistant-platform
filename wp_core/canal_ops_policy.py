"""Control layer for the Irrigation Assistant agents (canal_ops_sim_v1): fixed code, never a model.

- AGENTS: each agent's allowed tools (default deny; a call without a known agent type is the main agent).
- pre_tool_use / post_tool_use: the hook decisions the SDK adapter wires to PreToolUse / PostToolUse.
- scrub: removes names, phone numbers, emails and cadastral codes from staff free text before any model call.
- fixed_refusal: polite fixed answers (with one alternative) for requests the system never fulfils.
"""
from __future__ import annotations

import re

from wp_core.canal_ops_checker import RESTRICTED

MAIN_TOOLS = {"get_network", "deliverable", "compare_rules", "get_history", "get_constraints", "save_proposal"}
AGENT_TOOLS = {
    "main": MAIN_TOOLS,
    "planner": {"get_network", "deliverable", "compare_rules", "simulate_plan", "get_history", "get_constraints"},
    "reviewer": {"get_network", "compare_rules", "simulate_plan", "tail_risk", "get_history"},
    "season_monitor": set(), "agronomy": set(), "auditor": set(), "season_prep": {"compare_rules"},
    "report_writer": set(), "staff_qa": {"get_network", "get_history"}, "intake": set(), "public_qa": set(),
    "watchdog": set(),
}
BUILTIN = {"Bash", "Read", "Write", "Edit", "Glob", "Grep", "WebFetch", "WebSearch", "NotebookEdit", "Task", "Agent"}


def tool_name(full: str) -> str:
    """mcp__canal__simulate_plan -> simulate_plan; built-ins are returned unchanged."""
    return full.split("__", 2)[-1] if full.startswith("mcp__") else full


def pre_tool_use(agent_type: str | None, tool: str, args: dict, as_of: str) -> tuple[bool, str]:
    """Allow or deny a tool call. Missing agent_type means the main agent (SDK convention)."""
    agent = agent_type or "main"
    name = tool_name(tool)
    if name in BUILTIN and not (agent == "main" and name in ("Agent", "Task")):
        return False, "built-in tools are not available"
    if name in ("Agent", "Task"):
        return True, ""
    if name not in AGENT_TOOLS.get(agent, set()):
        return False, f"{name} is not allowed for {agent}"
    period = args.get("period")
    if period and period > as_of:
        return False, "no hindsight: period after the planning date"
    for k, v in args.items():
        if isinstance(v, str) and len(v) > 200:
            return False, f"argument {k} too long"
    return True, ""


def post_tool_use(result_text: str) -> tuple[bool, list]:
    """Backstop scan of a tool result before the model sees it (the tool allowlists are the main control)."""
    hits = [name for name, rx in RESTRICTED.items() if rx.search(result_text or "")]
    return (not hits), hits


PHONE = re.compile(r"(?:\+?374[\s-]?)?\(?\d{2,3}\)?[\s-]?\d{2,3}[\s-]?\d{2,3}[\s-]?\d{0,3}")
EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+")
DATE = re.compile(r"(?<!\d)20\d\d-[01]\d-[0-3]\d(?!\d)")          # a date (2025-07-21 07:00) is not a phone number
SURNAME = re.compile(r"(?:\b[A-ZԱ-ՖА-Я][a-zա-ֆа-я]+\s+)?\b[\wԱ-Ֆա-ֆА-Яа-я]+(?:յան|եան|յանց|yan|ian|ян|янц)"
                     r"(?:ը|ն|ի|ին|ից|ով|ենց|ա|у|ом|е)?\b", re.I)
HONORIFIC = re.compile(r"\b(?:պարոն|տիկին|Mr\.?|Mrs\.?|Ms\.?|господин|госпожа)\s+\S+", re.I)


def scrub(text: str) -> str:
    """Remove personal and parcel identifiers from free text. Applied in code before any model call (G3.13)."""
    t = RESTRICTED["cadastral_code"].sub("[code]", text or "")
    t = EMAIL.sub("[email]", t)
    t = HONORIFIC.sub("[name]", t)
    t = SURNAME.sub("[name]", t)
    t = re.sub(r"(?<![\w.])(?=(?:[\d\s()+-]*\d){7,})[\d\s()+-]{7,}(?![\w.])", lambda m: " [phone] " if
               len(re.sub(r"\D", "", m.group(0))) >= 7 and not DATE.search(m.group(0)) else m.group(0), t)
    return re.sub(r"\s{2,}", " ", t).strip()


REFUSALS = {
    "order": {"en": ("I can't send orders or open gates; people do that. I can prepare a draft turn sheet for the WUA head.",
                     "Prepare draft sheet"),
              "hy": ("Ես չեմ կարող հրահանգներ ուղարկել կամ փականներ բացել, դա մարդիկ են անում։ Կարող եմ ՋՕԸ ղեկավարի համար "
                     "պատրաստել հերթերի նախագիծ։", "Պատրաստել նախագիծը"),
              "ru": ("Я не отправляю распоряжения и не открываю затворы, это делают люди. Могу подготовить проект графика "
                     "для руководителя АВП.", "Подготовить проект")},
    "farmers": {"en": ("I don't send messages to farmers. I can draft a section-level note for the WUA head to decide on.",
                       "Draft note for the head"),
                "hy": ("Ես հաղորդագրություններ չեմ ուղարկում ֆերմերներին։ Կարող եմ ՋՕԸ ղեկավարի համար գրել նախագիծ։",
                       "Գրել նախագիծը"),
                "ru": ("Я не отправляю сообщения фермерам. Могу подготовить записку для руководителя АВП.",
                       "Подготовить записку")},
    "real_data": {"en": ("This is an illustrative simulation built from satellite observations and an estimated branch "
                         "network; not operational records or a water order.", "Show how it works"),
                  "hy": ("Սա պատկերավոր մոդելավորում է՝ արբանյակային դիտարկումներով և ճյուղերի գնահատված ցանցով, ոչ "
                         "գործառնական գրառումներ և ոչ ջրի հրահանգ։", "Ցույց տալ ինչպես է աշխատում"),
                  "ru": ("Это иллюстративное моделирование на основе спутниковых наблюдений и оценочной сети каналов, "
                         "а не эксплуатационные данные и не распоряжение о подаче воды.", "Показать, как это работает")},
    "hydraulic": {"en": ("Head, pumping energy and engineering design are outside this simulation. I can show estimated "
                         "volumes and turn hours.", "Show volumes and hours"),
                  "hy": ("Ճնշումը, պոմպերի էներգիան և ինժեներական նախագծումը այս մոդելից դուրս են։ Կարող եմ ցույց տալ "
                         "գնահատված ծավալները և ժամերը։", "Ցույց տալ ծավալները"),
                  "ru": ("Напор, энергия насосов и проектирование вне этой модели. Могу показать оценочные объёмы и часы.",
                         "Показать объёмы")},
}
TRIGGERS = {
    "order": re.compile(r"open (the )?gate|close (the )?gate|send (the )?order|բաց(եք|իր)? .*փական|открой(те)? затвор", re.I),
    "farmers": re.compile(r"(send|tell|message).{0,20}farmers|ֆերմերներին .*(ուղարկ|ասա)|сообщи(те)? фермерам", re.I),
    "real_data": re.compile(r"real data|is this real|իրական տվյալ|реальные данные", re.I),
    "hydraulic": re.compile(r"\bhead\b.*(pressure|loss)|pump(ing)? energy|pressure|ճնշում|напор", re.I),
}


def fixed_refusal(text: str, lang: str = "en"):
    """Returns (kind, message, alternative button) when a request must get a fixed answer, else None."""
    for kind, rx in TRIGGERS.items():
        if rx.search(text or ""):
            msg, alt = REFUSALS[kind].get(lang, REFUSALS[kind]["en"])
            return kind, msg, alt
    return None
