"""Water delivery planner on the page (owner approval 2026-11-02, B1 and B5; "Page first"): the supply card needs the
owner's one-time token before any plan; the plan data, report and Excel come from fixed code with simulation labels."""
import io
import json
import sys
import threading
import zipfile
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import canal_ops_view as v  # noqa: E402
from test_canal_ops_view_v1 import call  # noqa: E402
from wp_core import canal_ops_world as CW  # noqa: E402
from wp_core import canal_planner_state as ST  # noqa: E402

pytestmark = pytest.mark.skipif(not (CW.available() and ST.SR.DAILY[2025].exists()), reason="real network or series missing")


@pytest.fixture(scope="module")
def server(tmp_path_factory):
    app = v.App(tmp_path_factory.mktemp("p") / "ledger.sqlite", network="real", map_dir=None)
    srv = ThreadingHTTPServer(("127.0.0.1", 0), None)
    port = srv.server_address[1]
    srv.RequestHandlerClass = v.make_handler(app, port)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield app, port
    srv.shutdown()


def post(port, path, body):
    s, b, _ = call(port, path, body)
    return s, json.loads(b)


@pytest.fixture(scope="module")
def planned(server):
    app, port = server
    s, card = post(port, "/api/planner/parse", {"text": "5 Խորանարդ մետր վայրկյան 48 ժամ", "date": "2025-07-01", "lang": "hy"})
    assert s == 200 and "864,000" in card["text"]
    s, _ = post(port, "/api/planner/confirm", {"supply_id": card["supply_id"], "token": card["token"]})
    assert s == 200
    s, plan = post(port, "/api/planner/run", {"supply_id": card["supply_id"]})
    assert s == 200
    return app, port, card, plan


def test_state_offers_the_planner(server):
    app, port = server
    st = json.loads(call(port, "/api/state")[1])
    assert st["planner"] == {"from": "2024-03-25", "to": "2026-10-31"}


def test_no_plan_without_the_owners_token(server):
    app, port = server
    s, card = post(port, "/api/planner/parse", {"text": "4 m3/s for 24 hours", "date": "2025-07-01"})
    assert s == 200
    assert post(port, "/api/planner/run", {"supply_id": card["supply_id"]})[0] == 400        # draft only: refused
    assert post(port, "/api/planner/confirm", {"supply_id": card["supply_id"], "token": "made-up"})[0] == 403
    s, e = post(port, "/api/planner/parse", {"text": "please plan", "date": "2025-07-01"})
    assert s == 400 and "flow" in e["error"]
    assert post(port, "/api/planner/parse", {"text": "5 m3/s 48 hours", "date": "2023-07-01"})[0] == 400   # no archive
    assert post(port, "/api/planner/parse", {"text": "5 m3/s for 3 weeks", "date": "2025-07-01"})[0] == 400  # later step
    assert call(port, "/api/planner/parse", {"text": "5 m3/s 48 hours", "date": "2025-07-01"},
                origin="http://evil.example")[0] == 403


def test_plan_data_for_the_page(planned):
    app, port, card, plan = planned
    assert plan["account"]["closes"] and plan["supply"]["head_m3"] == 864_000
    assert plan["summary"]["label"].startswith("Simulation")
    statuses = {o["s"] for o in plan["outlets"]}
    assert statuses <= {"served", "partly", "not reached", "not reached: canal level"}
    served = [o for o in plan["outlets"] if o["d"] > 0]
    assert served and all(o["o"] and o["c"] and o["o"] < o["c"] for o in served)
    assert sum(o["d"] for o in plan["outlets"]) == pytest.approx(plan["account"]["delivered_field_inlets_m3"], abs=len(served))
    assert plan["offtakes"] and all(c["here"] >= c["head"] for o in plan["offtakes"] for c in o["changes"])
    text = json.dumps(plan, ensure_ascii=False)
    assert "internal_parcel_id" not in text and "cadastr" not in text.lower()


def test_report_and_excel(planned):
    app, port, card, plan = planned
    for lang, word in (("hy", "Մոդելավորում, ոչ ջրի հրաման"), ("en", "not a water order"),
                       ("ru", "Моделирование")):
        s, page, h = call(port, f"/api/planner/report?plan={plan['plan_id']}&lang={lang}")
        page = page.decode()
        assert s == 200 and word in page
        assert page.count(word) >= 2                                                     # top and bottom
    page = call(port, f"/api/planner/report?plan={plan['plan_id']}&lang=hy")[1].decode()
    assert "Ջրբաշխի մատյան" in page and "հերթը" not in page
    assert "lower in the order" not in page and "supply exceeds" not in page                    # engine texts translated
    s, x, h = call(port, f"/api/planner/excel?plan={plan['plan_id']}")
    assert s == 200 and "simulation" in h["Content-Disposition"]
    sheet = zipfile.ZipFile(io.BytesIO(x)).read("xl/worksheets/sheet1.xml").decode()
    assert "not a water order" in sheet and plan["outlets"][0]["n"] in sheet
    assert call(port, "/api/planner/report?plan=Wnope")[0] == 404


def test_parcels_that_get_water_layer(planned):
    """Owner 2026-11-03: the parcels that get water are coloured; the layer carries no parcel id or code."""
    import gzip
    app, port, card, plan = planned
    s, body, h = call(port, f"/api/planner/parcels?plan={plan['plan_id']}")
    data = json.loads(gzip.decompress(body) if h.get("Content-Encoding") == "gzip" else body)
    assert s == 200 and data["features"]
    keys = set().union(*(f["properties"].keys() for f in data["features"]))
    assert keys == {"s", "a", "b", "c"}
    by = {k: sum(1 for f in data["features"] if f["properties"]["s"] == k) for k in "wpnx"}
    assert by["w"] > 0 and by["n"] > 0 and by["x"] > 0
    assert {f["properties"]["c"] for f in data["features"]} == {"P", "F"}
    for f in data["features"]:
        pr = f["properties"]
        assert (pr["a"] is None) == (pr["s"] in "nx") and (pr["a"] is None or pr["a"] < pr["b"])


def test_demonstration_page_and_report(planned):
    """Owner 2026-11-03: a separate demonstration front-end; its report uses readable names, no network ids,
    no community names and no plan id."""
    import re
    app, port, card, plan = planned
    assert "canal_ops_demo" not in call(port, "/")[1].decode()                  # the normal page by default
    app.demo = True
    try:
        page = call(port, "/")[1].decode()
        assert "Ջրաբաշխման պլանավորում" in page and "/api/chat" not in page
        for lang, branch in (("hy", "Ճյուղ"), ("en", "Branch")):
            rep = call(port, f"/api/planner/report?plan={plan['plan_id']}&lang={lang}")[1].decode()
            assert branch in rep and plan["plan_id"] not in rep
            assert not re.search(r"\b(?:I|II)-[GP]\d{2}", rep)                      # no network ids
            assert "Ջրթողները բացում են մարդիկ" in rep or "People open the gates" in rep
    finally:
        app.demo = False
    normal = call(port, f"/api/planner/report?plan={plan['plan_id']}&lang=en")[1].decode()
    assert plan["plan_id"] in normal and re.search(r"\b(?:I|II)-[GP]\d{2}", normal)    # the staff report keeps ids


def test_crop_groups_and_dates_in_words(server, planned):
    """Owner 2026-11-03: hectares per crop group; the chat sentence carries the date in words."""
    app, port, card, plan = planned
    c = plan["crops"]
    assert set(c) == {"perennial", "field"}
    for x in c.values():
        assert 0 <= x["turn_ha"] <= x["due_ha"] <= x["active_ha"]
    total_due = round(c["perennial"]["due_ha"] + c["field"]["due_ha"], 1)
    assert total_due == pytest.approx(plan["summary"]["due_ha"], abs=0.1)
    s, j = post(port, "/api/planner/parse", {"text": "հուլիսի 1 ից ջրանցքի գլխամասում 48 ժամով ջրի հոսքը կազմելու է 5 խմ մ վրկյ",
                                            "lang": "hy", "year": 2025})
    assert s == 200 and j["planning"] == "2025-07-01" and j["hours"] == 48 and j["m3s"] == [5.0]
    s, e = post(port, "/api/planner/parse", {"text": "5 m3/s 48 hours", "lang": "en", "year": 2025})
    assert s == 400 and "date" in e["error"]


def test_community_boundaries_and_names(server):
    """Owner 2026-11-03: the demonstration map shows community boundaries and names (Armenian and English);
    nothing else from the community layer is sent."""
    app, port = server
    s, b, _ = call(port, "/api/planner/communities")
    j = json.loads(b)
    assert s == 200 and len(j["areas"]["features"]) == len(j["labels"]["features"]) == 44
    for f in j["areas"]["features"] + j["labels"]["features"]:
        assert set(f["properties"]) == {"hy", "en"} and f["properties"]["en"].isascii()
    assert {f["geometry"]["type"] for f in j["labels"]["features"]} == {"Point"}
    names = {f["properties"]["en"] for f in j["labels"]["features"]}
    assert {"Aygeshat", "Shahumyan", "Mrgastan", "Vagharshapat"} <= names
