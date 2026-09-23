# Platform identity and relevant conversation

The owner requested that basic identity questions return only the product role and
that the assistant stop answering unrelated trivia, such as France's capital.

The Armenian identity is: «Ես ջրային և հողային ռեսուրսների կառավարման համակարգի AI գործակալն եմ։»
English and Russian use the same role in those languages. This is a role identity,
not an invented model manufacturer. Explicit underlying-technology questions must
still be answered truthfully without exposing private configuration.

The additive v4 service keeps conversations about the platform, its prepared data,
land, water, irrigation, agriculture, soils, satellite observations and related
weather. Greetings, thanks, follow-ups, charts and comparisons remain supported.
Unrelated questions receive a short redirect without the unrelated answer. A mixed
question should receive only its relevant part. Fresh weather is unavailable because
no live weather feed is connected; no weather feature was added in this update.

Common identity and capital-trivia requests are handled locally without a model
call. Other turns use a strict structured response to separate domain answers from
identity, redirect and social replies. The server substitutes approved text for the
latter responses and never displays the private routing fields. This uses the
[Responses structured-output format](https://developers.openai.com/api/docs/guides/structured-outputs).
Semantic routing remains model-dependent; the checks establish the tested behavior,
not a guarantee against every possible adversarial phrasing.

New runtime files: `run_agent_review_v4.py`, `wp_core/agent_service_v4.py`,
`wp_core/agent_knowledge_v4.py`, `wp_core/agent_scope.py`. Prior sealed versions,
the v2 frontend, v3 plain-language check, numerical tools, index, cadastral records,
map classes and all five analytical tabs are preserved. Existing external-payload
authorization and locally stored API credentials continue to apply. No automation
was changed or restarted.

Verification: 16 focused tests passed. The live model checks cover identity,
unrelated capitals and follow-ups, film trivia, unrelated chemistry, a recipe
override, current-weather unavailability, an irrigation explanation and Aknalich's
measured active hectares (354.1724 ha across the scoped 1,406 parcels).
Reports: `server_data/agent_v1/scope_verification_v4.json`,
`output/playwright/agent_scope_v4/verification.json`,
`server_data/agent_v1/http_verification_v4.json`.
The last two reports and `server_data/agent_v1/scope_activation_v4.json` must be
checked for successful desktop/mobile validation and activation on 8526.

Activation completed: the sealed `run_agent_review_v4.py --port 8526` replaced only
the former v3 review worker. Actual HTTP checks passed for the exact Armenian
identity, the capital-question redirect and the unchanged measured-hectare result.
Desktop and mobile preview checks passed; all five analytical API responses,
source/index hashes and the 8525 page hash remained unchanged. Refresh the review
page to establish a new conversation session after the worker restart.
