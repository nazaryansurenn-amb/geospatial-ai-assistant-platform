# Verified active logic for the 8526 assistant

Reviewed 2026-09-06 against the running hectare review, its source, preserved
delivery tables and current decision records. This extends agent knowledge only;
it changes no analytical methods, cadastral properties or map classes.

## Territory hierarchy

Echmiadzin is the overall water-and-land project context. The current analytical
scope is the **Lower Hrazdan observed zone**, with stages I and II. The UI's whole
WUA selector is disabled. The agent must not equate a Lower Hrazdan result with a
whole-Echmiadzin total, nor imply a confirmed hydraulic command area.

Communities are a separate geographic dimension, intersecting the stages. The
agent index assigns a whole parcel to the greatest intersection with the saved
project community polygons; exact ties remain unassigned. This is analytical
membership, not legal cadastral assignment. A community answer concerns only its
parcels in the requested study scope. Boundary crossings and full official areas
are retained. No nearest settlement-label assignment is used.

Potential alone supports the optional adjacent 1 km search. It selects whole
parcels intersecting that distance band, without clipping or changing their area.
An outside stage denotes its canal-reference association, not proven supply.

## Active sections and query meanings

| 8526 section | Existing logic reused by the agent | Deterministic query |
| --- | --- | --- |
| 2026 ակտիվություն | Saved observations through 2026-08-23. Active, partial, no observed activity, unresolved. Official parcel area and observed active area are different quantities. | activity |
| Օգտագործման տեսակ | Predominant adequately observed 2021–2025 use, Transitions v3; annual, perennial, undetermined, household. Owner's uncertain annual cycle → single assignment occurs after aggregation. | use_type; cycles |
| 2021–2025 պատմություն | Five-year map categories are distinct from annual activity states. Missing years stay unresolved. Annual hectare summaries use current official parcel area, not historical active-pixel area. | history_summary; history |
| Ներուժ | Preserved low/non-use screening using supported historical and current observations, land cover and terrain; inner area and optional 1 km expansion. Below canal terrain → gravity candidate; above → mechanical; ambiguous/missing range → review. | potential |
| Հողատարածքների կոնսոլիդացիայի հնարավորություն | Connected small active parcels with compatible multi-year EO behavior, existing exclusions and one purple candidate class. | consolidation |

Current consolidation choices are member official area <=1 ha and group area
>=3 ha, with shared pixels allowed as joint evidence. Earlier 0.5 ha / 5 ha rules
are superseded. Long/narrow shape remains internal priority, not another visible
class. Full results remain 31 groups, 262 parcels, 120.358241 ha. These thresholds
remain in this private decision record; the model-facing knowledge uses a
descriptive explanation and does not expose internal thresholds.

The household and road exclusions remain unchanged. Inner scope is 43,984 parcels:
22,802 eligible, 20,096 household, 1,086 roads. Query defaults exclude households;
an explicit request may include or isolate them, with separate use-type labelling.
Potential and consolidation retain their household exclusions without override.

## Data and claims boundaries

The agent does not perform EO reclassification, event detection, bulk numerical
work through LLM calls, new downloads or database exploration. It does not expose
private source-system information. Actual irrigation dates, delivered volumes,
applicable norms, soil infiltration and confirmed above-normative demand are not
connected analytical capabilities. The requested high-water-demand section is not
one of the current five tabs. Weather and radar experiments are not silently
substituted for a water-consumption measurement. Both cancelled automations remain
paused. The 250 m restriction to irrigation stress remains in the knowledge rules.

No observed activity proves neither abandonment nor legal availability. Potential
does not establish water availability or hydraulic feasibility; consolidation does
not establish crop identity, ownership agreement or legal feasibility. No new
ground-truth accuracy claim is made. Cadastral code, geometry and official area
remain immutable.

## Implementation and evidence

- `wp_core/agent_knowledge.py`: curated hierarchy, active section explanations,
  unavailable capabilities and shared reasoning/privacy contract. This is supplied
  as server instructions; internal source files are never sent to the model.
- `wp_core/agent_queries.py`: validated Python queries over an additive private
  SQLite index. No arbitrary SQL, shell, file or network-search tool is exposed.
- `server_data/agent_v1/manifest.json`: original source hashes and spatial grouping.
- `tests/test_agent_queries.py`: independent counts, hectare totals, exclusions,
  scope partition, missing years, matching CSV/map records and session ownership.
- `server_data/review/hectares_review_20260906_v1/frontend_source/src/landResourcesSchema.js`:
  actual UI hierarchy and five sections used for this review.
- Current owner rules: `CLASSIFICATION_SELECTION_EN.md`,
  `TRANSITIONS_AREA_APPLICATION_EN.md`, `LAND_POTENTIAL_RESULT_2026_09_06_EN.md`,
  `LAND_CONSOLIDATION_STRIP_PRIORITY_EN.md`, `CONSOLIDATION_UI_V2_EN.md`,
  `HECTARES_UI_2026_09_06_EN.md`, plus the independent product's reference playbook.

The prepared agent uses the hectare UI as its baseline. The existing 8526 product
stays live during preview verification; 8525 remains unchanged. On 2026-09-06 the
automatic approval reviewer rejected a live GPT test that would send prepared
aggregate land summaries externally. No successful live GPT test occurred. The
connection is explicitly disabled until owner approval and local activation;
credentials stay only in the ignored product environment file.
