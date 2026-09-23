# Owner agent on the existing 8526 product

Owner authorization: develop the agreed conversational and computational agent on
the nearly ready 8526 product. The baseline is the verified hectare review built
on consolidation v2, including all five tabs, land-potential results and hectare labels.
8525 is not changed. Existing releases, exclusions, methods and data remain sealed.

The agent understands English, Armenian, Russian and mixed transliteration. It can
discuss the platform's domain, explain results, calculate from prepared tables,
compare areas/years, and prepare consistent charts, parcel exports and reversible
map selections. It is not restricted to a menu of prewritten questions. The language
model selects validated Python functions; it has no arbitrary SQL, shell, file,
network-search, reclassification or publication tool.

The first version uses the owner's GPT-5.4 mini configuration. Credentials are
provisioned once into the independent product's ignored local environment file;
runtime never reads the parent project. Only the question, curated platform
context and sanitized tool results go to the API. No raw EO, private operational
records, credentials, internal diagnostics or geometries are sent to the model.

## Numerical contract

- Current activity is the saved 2026 observation through 2026-08-23. Observed
  active area is the preserved measurement, distinct from official parcel area.
  An unresolved or absent measurement is null, never zero/inactive.
- Historical annual states cover 2021-2025. Their area summaries sum current
  official parcel areas by annual state; they are not historical active-pixel areas.
- Transitions v3 predominant classes and the approved annual-uncertain-to-single
  aggregation are reused unchanged. No specific crop identification is offered.
- Potential reuses the inner and 1 km expansion candidates. Outside parcels have
  no equivalent served activity/use-type table; these values remain unavailable.
- Consolidation reuses all 31 groups as one class. No obsolete size limits or
  internal priority classes are introduced into the interface.
- Roads are always excluded from analytical queries. Household land defaults to
  exclusion and can be included or queried separately by an explicit question.
- Community grouping uses greatest polygon intersection with the 44 saved project
  community areas, in EPSG:32638. It is analytical membership, not legal cadastral
  assignment. Whole official parcel area is retained; boundary-crossing counts and
  overlap support are recorded. No nearest-place-label assignment is used.
- A community answer covers only parcels within the selected saved I/II scope
  (and the 1 km expansion for potential when selected), not its entire territory.
- Every answer card, chart and export uses the same deterministic result table.
  Percentages specify their denominator. Empty/missing support is stated explicitly.

## Interaction and privacy

The existing layers bar stays in place. The agent opens separately, and closing it
does not reset the map. Results are shown only by an explicit map action, and clear
restores the prior display. The current private review remains local-only. All HTTP
agent mutations validate host/origin, JSON bodies, bounded inputs and session tokens.
Sessions and conversational context are temporary; numerical provenance is stored
privately without raw conversations. API call/tool limits and cancellation bound
work. The cancelled scheduled follow-ups remain paused.

Verification must cover real Python totals, missing years, exclusions, community
scope, invalid arguments, exports, map selection, conversational follow-ups, live
GPT-5.4 mini calls, all five existing tabs, desktop/mobile layout and prior seals.

The hierarchy and current rules are reviewed in `AGENT_8526_LOGIC_EN.md`. Live API
verification is pending explicit owner approval of the sanitized external payload;
the agent connection remains disabled until that approval and activation.
