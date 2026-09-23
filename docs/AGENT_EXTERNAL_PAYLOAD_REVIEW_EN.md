# Agent connection: concrete payload for owner review

Destination: OpenAI Responses API, using the already selected GPT-5.4 mini and the
owner's existing server-side credential. The independent product holds its local
configuration. No runtime dependency on the parent project was added.

Sent for a conversation:

- The user's question and bounded recent conversation.
- The selected Lower Hrazdan I/II scope, analysis tab, 1 km setting and selected
  cadastral code if the user selected a parcel.
- Curated platform hierarchy, available methods and limitations.
- Derived tool results: community/stage/year/class names, parcel counts, official
  hectares, observed-active hectares when available, percentages and their
  denominators, observation dates, coverage and explanatory limitations.
- For a requested parcel profile: cadastral code, community, official hectares,
  prepared activity and type categories, annual states, potential/consolidation
  membership and applicable limitations. These are derived map attributes, not
  delivery, contract, person or ownership records.

Example for “How many hectares are actively used in Aknalich in 2026?”:

```json
{
  "scope": "Lower Hrazdan I + II only",
  "community": "Ակնալիճ",
  "households": "excluded",
  "roads": "excluded",
  "observation_through": "2026-08-23",
  "parcel_count": 1406,
  "official_area_ha": 1039.124351,
  "observed_active_area_ha": 354.1724,
  "observed_measurement_parcels": 1336,
  "unresolved_parcels": 70
}
```

The actual result also supplies breakdown rows, denominators and limitations,
including whole-parcel community grouping and incomplete-season coverage. No raw
EO rasters or scene observations, geometries, private operational records,
database connection details, source paths, internal thresholds or credentials are
sent as conversation content. The API key is used only for API authentication.
The request uses `store:false`; this does not assert zero provider retention.

Local numerical and UI tests pass. The live GPT test was rejected by automatic
approval review because permission for this external analytical payload was not
explicit. The external connection remains disabled. After owner approval, enable
the local connection, run the prepared live-model verification and only then
activate the verified agent on 8526. The existing five-tab hectare product is
still serving 8526; 8525 stays preserved.

Subsequent decision: the owner explicitly approved this payload and activation.
The live v2 agent is now verified on 8526; the pending status above is historical.
See `AGENT_ACTIVATION_2026_09_06_EN.md` for isolation checks and the activation receipt.
