"""Skip tests that need the product's private inputs when those inputs are absent.

The public repository holds code, tests and documentation only. The parcel
databases, analysis outputs, preserved releases and the local .venv stay on the
owner's machine. Where they are present, every test runs as before.
"""
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PRIVATE_INPUTS = ("data", "server_data", "releases", "output")
HAS_PRIVATE_INPUTS = all((ROOT / name).is_dir() for name in PRIVATE_INPUTS)

# Tests that read private parcel data, analysis outputs, preserved releases or
# the product's own .venv. Everything else runs in a plain checkout.
REQUIRES_PRIVATE_INPUTS = {
    "tests/test_activity_change_2025.py::test_agent_context_addition_preserves_other_modes",
    "tests/test_activity_change_2025.py::test_register_covers_all_immutable_parcels",
    "tests/test_activity_change_2025.py::test_ui_has_sixth_tab_and_vector_only_delivery",
    "tests/test_activity_change_area50.py::test_agent_replaces_stale_totals_and_preserves_other_sections",
    "tests/test_activity_change_area50.py::test_saved_result_preserves_parcels_and_enforces_every_partial_season",
    "tests/test_activity_change_history.py::test_agent_context_addition_preserves_other_modes",
    "tests/test_activity_change_history.py::test_register_covers_all_immutable_parcels",
    "tests/test_activity_change_history.py::test_ui_has_sixth_tab_and_vector_only_delivery",
    "tests/test_agent_excel.py::ExcelExport::test_community_export_reconciles_full_table_and_parcels",
    "tests/test_agent_excel.py::ExcelExport::test_history_summary_is_unique_and_empty_export_has_no_charts",
    "tests/test_agent_excel.py::ExcelExport::test_missing_active_measurements_and_codes_are_preserved",
    "tests/test_agent_queries.py::Queries::test_communities_partition_not_nearest_label",
    "tests/test_agent_queries.py::Queries::test_exclusions_scope_and_exports",
    "tests/test_agent_queries.py::Queries::test_history_unique_and_missing",
    "tests/test_agent_queries.py::Queries::test_newer_rules_and_map_history",
    "tests/test_agent_queries.py::Queries::test_preserved_live_population",
    "tests/test_agent_queries.py::Queries::test_profiles_keep_unavailable_fields_null",
    "tests/test_agent_service_v2.py::IsolatedAgent::test_measurement_tool_preserves_all_activity_classes",
    "tests/test_agent_service_v2.py::IsolatedAgent::test_request_ignores_previous_agent_configuration",
    "tests/test_consolidation_delivery.py::test_tile_templates_remain_unescaped_and_hook_survives_panel_closure",
    "tests/test_consolidation_ui_v2.py::test_consolidation_is_fifth_analysis_tab",
    "tests/test_consolidation_ui_v2.py::test_long_fifth_tab_label_has_full_row",
    "tests/test_consolidation_ui_v2.py::test_one_class_one_color_and_retained_controls",
    "tests/test_degradation_delivery.py::DegradationDelivery::test_excel_has_complete_numeric_table_and_candidates",
    "tests/test_degradation_delivery.py::DegradationDelivery::test_population_and_union_reconcile",
    "tests/test_degradation_delivery.py::DegradationDelivery::test_previous_queries_unchanged",
    "tests/test_degradation_delivery.py::DegradationDelivery::test_scope_and_unsupported_requests",
    "tests/test_irrigation_stress_delivery.py::StressDelivery::test_excel_complete_numeric_and_dated",
    "tests/test_irrigation_stress_delivery.py::StressDelivery::test_previous_queries_are_unchanged",
    "tests/test_irrigation_stress_delivery.py::StressDelivery::test_scope_counts_years_and_exclusions",
    "tests/test_readability_review.py::test_original_bundle_is_reused_with_one_local_stylesheet",
    "tests/test_standalone.py::test_complete_releases[use_type_review]",
    "tests/test_standalone.py::test_complete_releases[working]",
    "tests/test_standalone.py::test_copied_inputs_are_byte_identical",
    "tests/test_standalone.py::test_environment_cannot_mix_profiles",
    "tests/test_standalone.py::test_existing_delivery_cannot_be_rebuilt",
    "tests/test_standalone.py::test_http_runtime_and_internal_files_not_served[use_type_review]",
    "tests/test_standalone.py::test_http_runtime_and_internal_files_not_served[working]",
    "tests/test_standalone.py::test_local_environment_not_old_site_packages",
}


def pytest_collection_modifyitems(config, items):
    if HAS_PRIVATE_INPUTS:
        return
    skip = pytest.mark.skip(reason="needs private inputs that are not in the public repository")
    for item in items:
        if item.nodeid in REQUIRES_PRIVATE_INPUTS:
            item.add_marker(skip)
