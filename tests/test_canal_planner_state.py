"""Water delivery planner, steps 3b and 4 (owner approval 2026-11-02): the state of every parcel on the planning date.
Only land active on that date (or expected from last season, labelled) gets water; nothing later than the date is read."""
import sys
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from wp_core import canal_planner_state as S  # noqa: E402

pytestmark = pytest.mark.skipif(not all(S.SR.DAILY[y].exists() for y in (2024, 2025)), reason="satellite series missing")
D = date(2025, 7, 1)


def test_season_end_matches_the_yearly_states():
    """On 31 October, every v2 'cultivated' parcel is active and every v2 'idle' parcel is idle so far."""
    for y in (2024, 2025):
        c = S.check_against_yearly(y)
        assert c["cultivated_called_active"] >= 0.99 and c["idle_called_idle_so_far"] >= 0.99, c


def test_activity_never_reads_later_views(monkeypatch):
    base = S.activity(D)
    real = S.views

    def with_future(year):
        v = real(year)
        fake = v.drop_duplicates("pid").assign(date=D + timedelta(days=1), ndvi=0.9, vf=1.0, green=True)
        fake2 = fake.assign(date=D + timedelta(days=5))
        return pd.concat([v, fake, fake2], ignore_index=True) if year == D.year else v
    monkeypatch.setattr(S, "views", with_future)
    again = S.activity(D)
    assert (base.activity == again.activity).all() and (base.n_green == again.n_green).all()


def test_states_and_water_rule():
    a = S.activity(D)
    p = S.parcels()
    assert set(a.activity) <= set(S.STATES)
    assert (a.activity[p.rule == "zero"] == "no_farm_water").all()                       # zero-need classes stay zero
    act = a[a.activity == "active"]
    assert (act.n_green >= S.ACT["active_min_green"]).all()
    spring = S.activity(date(2025, 4, 15))
    exp = spring[spring.activity == "expected"]
    assert len(exp) and (exp.last_season == "active").all() and (exp.n_green == 0).all()
    assert (exp.basis.str.contains("last season")).all()


@pytest.fixture(scope="module")
def snap():
    return S.snapshot(D, "normal", 10)


def test_snapshot_soil_water_and_satellite(snap):
    water = snap.activity.isin(S.ACT["water"])
    farm = snap.activity != "no_farm_water"
    assert snap.days_to_stress[~water].isna().all()                                       # no water, no urgency
    assert (snap.depletion_mm[farm] <= snap.taw_mm[farm] + 1e-6).all() and (snap.depletion_mm[farm] >= 0).all()
    assert snap.days_to_stress[water].dropna().between(0, 10).all()
    assert (snap.view_age_days[farm].dropna() >= S.DM.CFG["no_hindsight"]["s2_delay_days"]).all()
    assert set(snap.kc_source[water]) <= {"observed", "type-based", "fallback"}
    s = S.summary(snap)
    assert s["ha_by_activity"]["active"] > 0 and set(s["weather_days_after_planning_date"]) <= {"forecast", "normal"}
    total = sum(s["ha_by_activity"].values())
    assert total == pytest.approx(float(S.parcels().area_ha.sum()), abs=0.1)
