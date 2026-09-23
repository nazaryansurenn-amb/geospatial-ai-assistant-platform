"""Supply updated change totals while retaining the sealed v6 agent tools."""
from types import SimpleNamespace
from wp_core import agent_knowledge_v7, agent_presentation_v7
from wp_core.activity_change_history_agent_v7 import install as install_history


def install(service, summary, lookup):
    # Start from the unwrapped v6 presentation functions, not the old transition
    # adapter, which would append stale cohort totals to the current context.
    facade = SimpleNamespace(clean_context=service.clean_context,
                             reader_context=agent_presentation_v7.reader_context,
                             instructions=agent_knowledge_v7.instructions)
    install_history(facade, summary, lookup)
    previous = facade.instructions
    service.clean_context = facade.clean_context
    service.reader_context = facade.reader_context
    service.instructions = lambda: previous() + "\nCultivation-change eligibility was narrowed: every partially active season must now have repeated observed vegetation over at least half of its whole parcel sampling support. Never treat the below-cutoff or unavailable cases as inactive. Only use the current supplied totals, never the superseded cohort totals. This is an EO area-screening estimate, not a precise cultivated-area survey. Existing active seasonal states and the other analyses are unchanged. Do not expose internal cutoff parameters or dated evidence."
