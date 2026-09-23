"""Add the new tab's published facts without replacing existing agent tools."""
MODE = "activity_change"
NAMES = {"hy": "Մշակման փոփոխություն 2021–2025", "en": "Cultivation changes 2021-2025", "ru": "Изменения обработки 2021–2025"}


def install(service, summary, lookup):
    from wp_core import agent_presentation_v7 as agent_presentation
    old_clean = service.clean_context
    old_reader = service.reader_context
    old_instructions = service.instructions
    for language, name in NAMES.items():
        agent_presentation.TAB_NAMES[language][MODE] = name

    def clean(value):
        if isinstance(value, dict) and value.get("mode") == MODE:
            checked = old_clean({**value, "mode": "history_2021_2025"})
            checked["mode"] = MODE
            return checked
        return old_clean(value)

    def reader(context):
        text = old_reader(context)
        if context["mode"] != MODE:
            return text
        s = summary["summaries"][context["scope"]]
        labels = {
            "hy": ("Ակտիվացած հողամասեր", "Ակտիվությունն այլևս չի դիտվում", "հողամաս", "կադաստրային հա"),
            "en": ("Activity appeared during 2021-2025", "Activity ceased during 2021-2025", "parcels", "cadastral ha"),
            "ru": ("Активность появилась за 2021–2025", "Активность прекратилась за 2021–2025", "участков", "кадастровых га"),
        }[context["language"]]
        for key, label in zip(("became_active", "became_inactive"), labels[:2]):
            row = s["classes"][key]
            text += f'\n{label}: {row["parcel_count"]} {labels[2]}, {row["area_ha"]:.2f} {labels[3]}.'
        code = context.get("selected_code")
        if code:
            state = lookup.get(code, {}).get("changeClass")
            text += "\nSelected parcel: " + ({"became_active": labels[0], "became_inactive": labels[1],
                "insufficient": "insufficient five-year observations", "excluded": "excluded from this screening",
                "not_selected": "does not meet this strict transition pattern"}.get(state, "not covered by this screening")) + "."
            year = lookup.get(code, {}).get("changeYear")
            if year:
                text += f"\nFirst observed year of the new state: {year}."
        return text + "\nThese are precomputed public totals for the selected area, not every 2025 active/inactive parcel. Translate naturally. Existing history tools cannot filter this five-year transition cohort: do not substitute their yearly totals, tables, exports or map selections for this result. Community breakdowns and agent-generated Excel/map selections for this new cohort are not connected yet. Use the cultivation-change tab's two class controls for the displayed map."

    service.clean_context = clean
    service.reader_context = reader
    service.instructions = lambda: old_instructions() + "\nThe product also has a cultivation-change analysis tab: cultivation changes 2021-2025. It requires all five seasons to be adequately observed and exactly one transition, without reversal through 2025, between active/partial and no observed activity. The change can be in 2022, 2023, 2024 or 2025; its year is retained. Roads and household land are excluded. Blue means activity appeared; red means activity is no longer observed. Alternating histories are excluded. This is not proof of first-ever cultivation or permanent abandonment; persistence after 2025 is unknown. Use its supplied public totals only when present in current context; do not derive this cohort by subtracting independent yearly totals."
