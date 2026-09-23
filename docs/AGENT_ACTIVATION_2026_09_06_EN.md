# Isolated agent activation

The owner approved the reviewed external payload and activation with:
“Ok make sure our previous agent configuration doesn’t play role and do it”.
This supersedes the earlier pending-approval state. The existing credential and
selected GPT-5.4 mini model are reused; no older prompts, assistant IDs, hosted
prompt IDs, conversations, automation configuration or parent runtime files are
loaded. Request construction reads only the independent product's local `.env`.

The current agent knowledge is `wp_core/agent_knowledge_v2.py`, based on the actual
8526 hierarchy and five analytical sections. Its only numerical tools are
`measure_active_area`, `analyze_land`, and `get_parcel_profile`. Calculations reuse
the pinned Python queries and prepared index. Analytical methods remain unchanged.

The first live v1 test caught an incorrect tool choice: the model requested only
the active-class subset for a question about measured active hectares. The v2
agent has a dedicated all-class measurement tool and clearer instructions. All
v1 code and outputs are preserved. GPT-5.4 mini uses low reasoning effort with
stateless encrypted continuation, following the official [reasoning guide](https://developers.openai.com/api/docs/guides/reasoning).

The four live v2 smoke questions passed: Aknalich measured active area, Arshaluys
follow-up and chart breakdown, platform hierarchy/available tabs, and an Armenian
I/II potential comparison including all communities and the 1 km expansion.
These verify representative behavior; they do not establish perfect answers to
every future question. Source data and model-derived classification limitations
remain visible in the interface and knowledge.

The v2 frontend adds safe React rendering of response paragraphs, emphasis, lists
and tables. It executes no returned HTML. All previous map tiles, exclusions,
cadastral properties, five tabs and official hectare summaries remain preserved.

Launcher: `run_agent_review_v2.py --port 8526` after final preview checks and seal.
Live tests and activation receipts are stored under `server_data/agent_v1/`.
Rollback: `run_hectares_review_v2.py --port 8526`. 8525 and both cancelled
automations remain unchanged.

Activation completed and verified: 8526 serves the sealed v2 frontend and service.
A real HTTP-to-GPT-to-Python question completed in 4.8 seconds with Aknalich's
354.1724 observed-active ha and 1039.124351 official ha for 1,406 scoped parcels.
Its CSV and map selection contained the same 1,406 cadastral codes, and CSV areas
matched the answer table. This is an example response time, not a general latency
guarantee. The 8525 HTML hash is unchanged. See `activation_verified.json`.
