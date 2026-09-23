"""Add the new tab's published facts without replacing existing agent tools."""
MODE = "activity_change_2025"
NAMES = {"hy": "2025 թ. մշակման փոփոխություն", "en": "Cultivation changes in 2025", "ru": "Изменения обработки в 2025 году"}


def install(service, summary, lookup):
    from wp_core import agent_presentation
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
            "hy": ("2025-ին ակտիվացած", "2025-ին ակտիվությունն այլևս չի դիտվում", "հողամաս", "կադաստրային հա"),
            "en": ("Activity appeared in 2025", "Activity no longer observed in 2025", "parcels", "cadastral ha"),
            "ru": ("Активность появилась в 2025", "Активность больше не наблюдается в 2025", "участков", "кадастровых га"),
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
        return text + "\nThese are precomputed public totals for the selected area, not every 2025 active/inactive parcel. Translate naturally. Existing history tools cannot filter this five-year transition cohort: do not substitute their yearly totals, tables, exports or map selections for this result. Community breakdowns and agent-generated CSV/map selections for this new cohort are not connected yet. Use the sixth tab's two class controls for the displayed map."

    service.clean_context = clean
    service.reader_context = reader
    service.instructions = lambda: old_instructions() + "\nThe product also has a sixth analysis tab: cultivation changes in 2025. It requires four consistent adequately observed seasons in 2021-2024 and the opposite observed state in 2025, excluding roads and household land. Activity includes partial activity. Blue means activity appeared; red means activity is no longer observed. It is not proof of first-ever cultivation or permanent abandonment. Use its supplied public totals only when present in current context; do not derive this cohort by subtracting independent yearly totals."
