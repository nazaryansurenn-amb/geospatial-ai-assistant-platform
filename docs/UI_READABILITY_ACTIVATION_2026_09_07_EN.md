# Approved readability activation

The owner reviewed 8529 and requested: "okay apply on 8526".
The sealed readability_20260907_v1 presentation is now active on 8526 through
run_readability_review.py. This supersedes the pending-approval status in the
preserved preview documentation without modifying its sealed files.

Post-deployment verification: 22 API responses match the pre-switch snapshot,
approved HTML and CSS are served, private routes return 404, and 8525 is unchanged.
The refreshed working browser loads the new stylesheet, displays seven sections,
uses 16 px chat and input text, and reports no console errors.

Evidence: server_data/review/readability_20260907_v1/http_activation.json.
No calculations, map classes, agent logic, weather jobs or automations changed.
Previous versions remain intact; rollback is run_activity_change_area50_review.py.
