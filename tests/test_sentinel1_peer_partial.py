import pandas as pd
import run_sentinel1_peer_partial as partial


def test_only_same_year_completed_months_are_selected():
    frame = pd.DataFrame({"datetime": ["2024-04-05T00:00Z", "2025-03-31T23:00Z", "2025-04-01T00:00:00Z", "2025-04-30T23:00Z", "2025-05-01T00:00Z"], "value": range(5)})
    got = partial.select_covered_observations(frame, ["2025_03", "2025_04"])
    assert got.value.tolist() == [1, 2, 3]
    assert len(frame) == 5


def test_gap_month_is_not_filled_by_later_weather():
    frame = pd.DataFrame({"datetime": ["2025-04-15T00:00Z", "2025-05-15T00:00Z", "2025-06-15T00:00Z"]})
    got = partial.select_covered_observations(frame, ["2025_04", "2025_06"])
    assert got.index.tolist() == [0, 2]
