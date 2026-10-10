"""Schedule fix round 10/10/26, workstream J3 — MARKET-31: the voice
manager. Push to talk in Ask (the browser's speech recognition) reaching the
same tools; "Maria's out tonight, who can close bar?" answered by find_cover
with a ranked legal list, and the offer sent only through a confirm card."""
import datetime as dt
import os
import re

import pytest

import ask_cavnar_tools as act
import models
from models import Restaurant, create_restaurant

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


@pytest.fixture(autouse=True)
def _db(db_path, monkeypatch):
    real = models.get_conn
    monkeypatch.setattr(models, "get_conn", lambda *a, **k: real(db_path))
    monkeypatch.setattr(models, "DB_PATH", db_path)
    yield


def test_find_cover_names_who_can_legally_take_the_shift_of_the_person_out(monkeypatch):
    rid = create_restaurant(Restaurant(name="Voice Co", owner_email="v@x.test", module_labor=1))
    for n in ("Maria", "Bob", "Cy"):
        models.add_manual_team_member(rid, n, role="Bartender")
    day = dt.date.today()
    import intraday
    import labor_replacements
    monkeypatch.setattr(intraday, "published_rows", lambda *a, **k: [
        {"date": day.isoformat(), "employee": "Maria", "role": "Bartender", "shift_start": "5:00pm",
         "shift_end": "11:00pm"}])
    seen = {}

    def fake_for_gap(restaurant_id, role, weekday, exclude=(), **kw):
        seen.update(role=role, exclude=set(exclude), shift=kw.get("shift"))
        return [{"name": "Bob", "score": 4, "covers_taken": 2, "no_show_rate": 0.0}]
    monkeypatch.setattr(labor_replacements, "for_gap", fake_for_gap)
    out = act._find_cover(rid, employee="maria")
    assert out["candidates"][0]["name"] == "Bob" and out["role"] == "Bartender"
    assert seen["role"] == "Bartender" and "Maria" in seen["exclude"] and seen["shift"]["shift_start"] == "5:00pm"
    assert out["date_iso"] == day.isoformat() and "offer_cover" in out["note"]
    # Somebody with no shift that day is said, never guessed.
    assert "no shift" in act._find_cover(rid, employee="Nobody")["error"]


def test_the_offer_is_a_confirmed_write_on_the_open_shift_route():
    tool = next(t for t in act.TOOLS if t["spec"]["name"] == "offer_cover")
    assert tool["kind"] == "write" and tool["confirm"] is True
    assert tool["route"]["web"] == "/api/labor/open-shifts" and tool["route"]["mobile"] == "/mobile/api/labor/open-shifts"
    assert set(tool["spec"]["input_schema"]["required"]) == {"date", "shift_start", "offer_to"}
    read = next(t for t in act.TOOLS if t["spec"]["name"] == "find_cover")
    assert read["kind"] == "read" and read["module"] == "module_labor"


def test_push_to_talk_fills_and_sends_the_question_and_reads_the_answer_back():
    src = open(os.path.join(ROOT, "templates", "dashboard.html"), encoding="utf-8").read()
    assert 'id="ask-cavnar-mic"' in src and 'aria-label="Speak your question"' in src
    i = src.index("function askMicToggle()")
    block = src[i:src.index("window.sendAskCavnar = function()", i)]
    assert "window.SpeechRecognition || window.webkitSpeechRecognition" in block
    assert "window._askSpeakNext = true; sendAskCavnar();" in block
    assert "Tap the card to send it." in block
    assert not re.search(r"=>|`|\bconst\s|\blet\s", block)
    assert "if (window._askSpeakNext) { window._askSpeakNext = false; askSpeak(" in src
