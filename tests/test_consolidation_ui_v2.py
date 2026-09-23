"""Regression checks for a single consolidation class in the fifth tab."""
from pathlib import Path
import re

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "server_data/review/consolidation_review_20260906_v2/frontend_source/src"


def test_consolidation_is_fifth_analysis_tab():
    schema = (SOURCE / "landResourcesSchema.js").read_text(encoding="utf-8")
    modes = schema.split("export const LAND_MODES =", 1)[1]
    ids = re.findall(r'^    id: "([^"]+)"', modes, re.MULTILINE)
    assert ids == ["activity_2026", "land_use_type", "history_2021_2025", "potential", "consolidation"]
    app = (SOURCE / "App.jsx").read_text(encoding="utf-8")
    assert app.count("<ConsolidationView") == 1
    assert app.index('<ConsolidationView') > app.index('aria-label="Հողային վերլուծություն"')
    assert 'landMode === "consolidation" && <ConsolidationView' in app
    assert "toggleConsolidation" not in app


def test_one_class_one_color_and_retained_controls():
    source = (SOURCE / "ConsolidationView.jsx").read_text(encoding="utf-8")
    assert source.count('type="checkbox"') == 1
    assert 'const COLOR = "#bd83f3"' in source
    assert '"fill-color": COLOR' in source and '"line-color": COLOR' in source
    assert "priority" not in source and "LABELS" not in source
    assert "scope === \"lower_hrazdan\" || g.stage === scope" in source
    assert "groups.reduce((n, g) => n + g.parcel_count, 0)" in source
    assert "groups.reduce((n, g) => n + g.area_ha, 0)" in source
    assert 'type="range"' in source and '<select' in source
    assert 'active && visible && numbers.length' in source
    assert 'setFeatureState' not in source


def test_long_fifth_tab_label_has_full_row():
    css = (SOURCE / "consolidation.css").read_text(encoding="utf-8")
    assert 'button[data-mode="consolidation"]' in css
    assert 'grid-column: 1 / -1' in css
    assert 'white-space: normal' in css
