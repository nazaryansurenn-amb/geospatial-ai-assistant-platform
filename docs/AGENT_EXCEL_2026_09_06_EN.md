# Excel downloads for agent results

The owner requested Excel downloads for the community comparison table and charts,
and no visible CSV option. The additive v5 review changes the result-card download
to «Ներբեռնել Excel». Each result downloads a real `.xlsx` workbook with an Armenian
summary, the complete grouped table, native editable charts and parcel details.
All grouped rows are included, including rows beyond the chart preview's first 12.
Activity exports distinguish measured active hectares from official hectares.
Missing measurements stay blank. Cadastral codes remain literal strings, including
leading zeros. Historical yearly rows are not summed into an invented total; the
existing unique-parcel summary is exported separately.

`wp_core/agent_excel.py` fills the packaged Excel template from the existing result's
approved summary/table and parcel CSV. This uses only the independent product's
Python standard library at runtime. It performs no model call, numerical query,
EO recalculation or external-runtime call. The template was authored using the
spreadsheet tooling once during preparation and is sealed inside the product.
The `/xlsx` route applies the existing same-origin and session/result ownership
checks before reading or creating the workbook. The legacy CSV endpoint and stored
outputs are preserved for compatibility; CSV is no longer a visible result action.

The runtime is `run_agent_review_v5.py`, `agent_service_v5.py`,
`agent_knowledge_v5.py` and the separate `frontend_agent_review_20260906_v5`.
v4 identity and topic boundaries remain. All prior seals, maps, five tabs,
analytical methods, source/index hashes, exclusion rules and 8525 are preserved.
No scheduled task was changed or restarted.

Twenty focused tests passed. The reported community example reconciles to 23 rows,
3,169 parcels and 3,043.159368 official hectares. The workbook was reopened with an
independent spreadsheet reader and its summary, table, chart and parcel views were
rendered and inspected. Desktop/mobile download checks are recorded in
`output/playwright/agent_excel_v5/verification.json`; analysis API and immutable-data
checks are in `server_data/agent_v5/http_verification.json`. Actual 8526 activation
is recorded separately in `server_data/agent_v5/activation.json` once verified.

Activation passed on 8526: the genuine downloaded workbook contains all 23 community
rows, 3,169 parcel records and two native editable charts. The download took 0.204
seconds in this check and required no additional model call. Missing-token and
other-session requests were rejected. Identity remained correct and 8525 unchanged.
Refresh 8526 and request the comparison again to establish a fresh agent session.
