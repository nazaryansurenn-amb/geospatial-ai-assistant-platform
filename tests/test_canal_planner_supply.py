"""Water delivery planner, step 2 (owner approval 2026-11-02): the typed supply is read by fixed code in hy/en/ru,
capped at the main canal, started at a change time, confirmed by the owner with a one-time token; no hindsight."""
import sys
from datetime import datetime
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from wp_core import canal_planner_supply as S  # noqa: E402
from wp_core.canal_ops_ledger import Ledger  # noqa: E402

JULY1 = S.planning_moment("2025-07-01")


@pytest.mark.parametrize("text, q", [
    ("5 m3/s for 48 hours", 5.0),
    ("5 Խորանարդ մետր վայրկյան ջուր կա գլխամասում 48 ժամ ժամկետում", 5.0),        # the owner's own question
    ("4 hour 5 cub /sec", 5.0),
    ("5,5 м³/с на 2 дня", 5.5),
    ("4500 l/s for a week", 4.5),
    ("6 խմ/վ 7 օր", 6.0),
    ("5 մ³/վ 48 ժամ", 5.0),                                                          # the page's own example
    ("4,5 մ3/վ 2 օր", 4.5),
])
def test_flow_is_read_in_three_languages(text, q):
    assert S.read_flow(text) == pytest.approx(q)


def test_volumes_dates_and_times_are_not_flows():
    assert S.read_flow("864000 m3 released") is None
    assert S.read_flow("from 2025-07-21 07:00 for 48 hours") is None


@pytest.mark.parametrize("text, hours, horizon", [
    ("5 m3/s 48 hours", 48, "operational"),
    ("5 m3/s for 4 hours", 12, "operational"),                                          # whole 12-h slots
    ("5 м³/с на 2 недели", 336, "tactical"),
    ("5 m3/s next month", 720, "tactical"),
])
def test_window_and_horizon(text, hours, horizon):
    s = S.parse_supply(text, JULY1)
    assert s["hours"] == hours and s["horizon"] == horizon
    assert s["start"] == "2025-07-01 07:00" and s["head_m3"] == round(5 * hours * 3600 if "4 hours" not in text else 5 * 12 * 3600)


def test_the_owners_48_hour_case():
    s = S.parse_supply("5 Խորանարդ մետր վայրկյան 48 ժամ", JULY1)
    assert s["head_m3"] == 864_000 and s["end"] == "2025-07-03 07:00" and s["supply_id"].startswith("F")
    c = S.card(s, "hy")
    assert "864,000" in c["text"] and "Հաստատե՞լ" in c["text"]


def test_season_runs_to_the_season_end_and_waits_for_its_start():
    s = S.parse_supply("4 m3/s for the whole season", JULY1)
    assert s["end"] == "2025-11-01 07:00" and s["horizon"] == "season"
    s = S.parse_supply("4 m3/s for the whole season", S.planning_moment("2025-02-10"))
    assert s["start"] == "2025-04-01 07:00" and any(k == "season_start" for k, _ in s["notes"])
    with pytest.raises(S.SupplyRefused):
        S.parse_supply("4 m3/s for the whole season", S.planning_moment("2025-11-05"))


def test_cap_spill_and_change_times():
    s = S.parse_supply("15 m3/s for 24 hours", JULY1)
    assert s["segments"][0]["m3s"] == 12.0 and s["spill_m3"] == round(3 * 24 * 3600)
    assert "spill" in S.card(s, "en")["notes"][0]
    s = S.parse_supply("5 m3/s from 2025-07-02 10:30 for 24 hours", JULY1)
    assert s["start"] == "2025-07-02 19:00" and any(k == "snapped" for k, _ in s["notes"])
    assert S.next_change(datetime(2025, 7, 1, 19, 1)) == datetime(2025, 7, 2, 7)


def test_no_hindsight_and_missing_parts_are_refused():
    for text in ("5 m3/s from 2025-06-30 07:00 for 24 hours", "48 hours please", "5 m3/s"):
        with pytest.raises(S.SupplyRefused):
            S.parse_supply(text, JULY1)
    with pytest.raises(S.SupplyRefused):
        S.make_supply([{"start": "2025-07-01 09:00", "hours": 24, "m3s": 5}], JULY1)        # not a change time
    with pytest.raises(S.SupplyRefused):
        S.make_supply([{"start": "2025-07-01 07:00", "hours": 24, "m3s": 5},
                       {"start": "2025-07-03 07:00", "hours": 24, "m3s": 4}], JULY1)       # a gap


def test_stepped_supply_and_stable_id():
    segs = [{"start": "2025-07-01 07:00", "hours": 24, "m3s": 4}, {"start": "2025-07-02 07:00", "hours": 48, "m3s": 6}]
    a, b = S.make_supply(segs, JULY1), S.make_supply(list(reversed(segs)), JULY1)
    assert a["supply_id"] == b["supply_id"] and a["head_m3"] == round((4 * 24 + 6 * 48) * 3600)
    assert a["horizon"] == "operational" and "4 / 6" in S.card(a, "ru")["text"]


def test_only_the_owners_token_confirms_a_supply(tmp_path):
    led = Ledger(tmp_path / "l.sqlite")
    s = S.parse_supply("5 m3/s 48 hours", JULY1)
    with pytest.raises(S.SupplyRefused):
        S.confirmed(led, s["supply_id"])                                                  # a draft is not usable
    tok = S.propose(led, s)["token"]
    with pytest.raises(PermissionError):
        S.confirm(led, s["supply_id"], "made-up")
    S.confirm(led, s["supply_id"], tok)
    assert S.confirmed(led, s["supply_id"])["head_m3"] == 864_000
    with pytest.raises(PermissionError):
        S.confirm(led, s["supply_id"], tok)                                               # single use
    assert led.verify()


def test_cards_in_every_language_and_dekads():
    s = S.parse_supply("15 m3/s for 4 hours", JULY1)
    for lang in ("en", "hy", "ru"):
        c = S.card(s, lang)
        assert c["text"] and len(c["notes"]) == 2 and set(S.TEXT[lang]) == set(S.TEXT["en"])
    assert S.dekad_of(datetime(2025, 7, 31)) == "2025-07-D3" and S.dekad_of(datetime(2025, 7, 11)) == "2025-07-D2"


@pytest.mark.parametrize("text, day, q, hours", [
    ("հուլիսի 1 ից ջրանցքի գլխամասում 48 ժամով ջրի հոսքը կազմելու է 5 խմ մ վրկյ, կազմիր մաքսիմալ էֆֆեկտիվ ոռոգման "
     "ջրամատակարարման պլան", "2025-07-01", 5.0, 48),                                   # the owner's own sentence
    ("From 1 July the flow at the canal head will be 5 m3/s for 48 hours. Make a plan with the lowest losses.",
     "2025-07-01", 5.0, 48),
    ("с 1 августа 6 м3/с на 2 дня", "2025-08-01", 6.0, 48),
    ("5 m3/s 48 hours from August 1 2024", "2024-08-01", 5.0, 48),
    ("օգոստոսի 15-ից 4 մ³/վ 24 ժամ", "2025-08-15", 4.0, 24),
])
def test_dates_written_in_words(text, day, q, hours):
    d = S.read_date(text, 2025)
    assert str(d) == day
    s = S.parse_supply(text, S.planning_moment(d))
    assert s["segments"][0]["m3s"] == q and s["hours"] == hours and s["start"] == f"{day} 07:00"


def test_no_date_in_words():
    assert S.read_date("5 m3/s 48 hours", 2025) is None
