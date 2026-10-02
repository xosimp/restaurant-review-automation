"""A scheduling note about one person, held by code the way availability is
(owner, 10/2/26). Read by an allowlist; confirmed by the owner; then the
person cannot be scheduled then — the same hard check as availability and
approved time off, named as the note in the breach."""
import sys
from datetime import date, timedelta

import pytest

import models
import person_note_holds as pnh
import schedule_rules
from models import Restaurant, create_restaurant

TODAY = date(2026, 10, 2)


@pytest.fixture
def rid(db_path, monkeypatch):
    real = models.get_conn

    def conn(*a, **k):
        return real(db_path)
    for mod in list(sys.modules.values()):
        if mod is not None and getattr(mod, "get_conn", None) is real:
            monkeypatch.setattr(mod, "get_conn", conn)
    monkeypatch.setattr(models, "get_conn", conn)
    monkeypatch.setattr(models, "DB_PATH", db_path)
    return create_restaurant(Restaurant(name="EJ Co", owner_email="e@x.com", timezone="America/Chicago"), db_path=db_path)


def _r(text):
    return pnh.read_part(text, noted=TODAY.isoformat(), today=TODAY)


@pytest.mark.parametrize("text,days,parts,start,end", [
    ("no Tuesdays until 10/31 — class", ["Tuesday"], None, "2026-10-02", "2026-10-31"),
    ("out 12/20–12/28", None, None, "2026-12-20", "2026-12-28"),
    ("no nights", None, ["night"], "2026-10-02", None),
    ("weekends only", list(pnh.DAYS[:5]), None, "2026-10-02", None),
    ("off 10/15", None, None, "2026-10-15", "2026-10-15"),
    ("can't work Tuesday nights", ["Tuesday"], ["night"], "2026-10-02", None),
    ("available fri-sun only", ["Monday", "Tuesday", "Wednesday", "Thursday"], None, "2026-10-02", None),
    ("no Sundays (church)", ["Sunday"], None, "2026-10-02", None),
    ("vacation 1/3-1/9", None, None, "2027-01-03", "2027-01-09"),
])
def test_a_plain_day_off_is_read_as_a_hold(text, days, parts, start, end):
    got = _r(text)
    assert got["kind"] == "hold", got
    h = got["hold"]
    assert (h["days"], h["dayparts"], h["start"], h["end"]) == (days, parts, start, end)


@pytest.mark.parametrize("text", ["max 25 hours", "not with Mike", "only closes on weekends with Ana", "Note: no tuesdays",
                                   "prefers mornings", "training on the line this month", "no doubles",
                                   "only weekends but not Saturdays"])
def test_anything_else_says_it_is_not_held(text):
    assert _r(text)["kind"] == "unchecked", text


def test_a_hold_is_validated_and_never_doubled(rid, db_path):
    h = pnh.add_hold(rid, "Marcus Lee", "no Tuesdays", days=["Tuesday"], start="2026-10-02", db_path=db_path)
    again = pnh.add_hold(rid, "marcus  lee", "no  Tuesdays", days=["Tuesday"], db_path=db_path)
    assert again["id"] == h["id"] and again.get("existing")
    for bad, msg in (({"days": ["Tues"]}, "isn't a day"), ({"dayparts": ["late"]}, "Lunch, dinner"),
                     ({"start": "2026-10-10", "end": "2026-10-01"}, "before the start"), ({}, "needs a day")):
        with pytest.raises(ValueError, match=msg):
            pnh.add_hold(rid, "Ana", "x", db_path=db_path, **bad)
    assert pnh.remove_hold(rid, h["id"], db_path=db_path) and not pnh.remove_hold(rid, h["id"], db_path=db_path)


def _c(rid, monday):
    dates = [(monday + timedelta(days=i)).isoformat() for i in range(7)]
    return schedule_rules.Constraints(restaurant_id=rid, week_dates=dates, week_days=list(pnh.DAYS))


def test_a_held_day_is_a_hard_unavailability_named_as_the_note(rid, db_path):
    mon = date(2026, 10, 5)
    pnh.add_hold(rid, "Marcus Lee", "no Tuesdays until 10/31 — class", days=["Tuesday"], start="2026-10-02",
                 end="2026-10-31", db_path=db_path)
    pnh.add_hold(rid, "Ana Ruiz", "no nights", dayparts=["night"], start="2026-10-02", db_path=db_path)
    c = _c(rid, mon)
    pnh.apply_holds(c, rid, db_path=db_path)
    ok, why = c.can_work("Marcus Lee", "2026-10-06")
    assert not ok and why.startswith("your note: no Tuesdays")
    assert c.can_work("Marcus Lee", "2026-10-07")[0]
    assert not c.can_work("Ana Ruiz", "2026-10-07", "night")[0] and c.can_work("Ana Ruiz", "2026-10-07", "morning")[0]
    rows = [{"date": "2026-10-06", "day": "Tuesday", "employee": "Marcus Lee", "role": "Server",
             "shift_start": "16:00", "shift_end": "22:00", "scheduled_hours": 6}]
    v = [x for x in schedule_rules.violations(rows, c) if x["kind"] == "note_unavailable"]
    assert v and v[0]["hard"]
    # After its end date the hold is gone.
    later = _c(rid, date(2026, 11, 2))
    pnh.apply_holds(later, rid, db_path=db_path)
    assert later.can_work("Marcus Lee", "2026-11-03")[0]


def test_build_constraints_applies_the_holds():
    import inspect
    assert "person_note_holds.apply_holds(c, restaurant_id" in inspect.getsource(schedule_rules.build_constraints)


def test_the_routes(rid, db_path, monkeypatch):
    from flask import Flask
    import client_api
    import strategy_routes
    monkeypatch.setattr(client_api, "log_account_event", lambda *a, **k: None)
    monkeypatch.setattr(strategy_routes, "_may_rate", lambda u: True)
    u = {"restaurant_id": rid, "role": "owner", "id": 1, "email": "e@x.com"}
    app = Flask(__name__)
    body = {"employee_name": "Marcus Lee", "part_text": "no Tuesdays", "days": ["Tuesday"], "start": "2026-10-02"}
    with app.test_request_context("/labor/staff-note-holds", method="POST", json=body):
        out, status = strategy_routes._do_staff_note_hold(u)
    assert status == 200 and out["hold"]["days"] == ["Tuesday"]
    with app.test_request_context("/labor/staff-note-holds", method="POST", json=dict(body, days=["Tues"])):
        assert strategy_routes._do_staff_note_hold(u)[1] == 400
    with app.test_request_context("/x", method="POST", json={}):
        assert strategy_routes._do_staff_note_unhold(u, out["hold"]["id"])[1] == 200
    monkeypatch.setattr(strategy_routes, "_may_rate", lambda u: False)
    with app.test_request_context("/labor/staff-note-holds", method="POST", json=body):
        assert strategy_routes._do_staff_note_hold(u)[1] == 403


def test_readings_mark_the_part_a_hold_came_from(rid, db_path):
    pnh.add_hold(rid, "Marcus Lee", "no Tuesdays", days=["Tuesday"], db_path=db_path)
    notes = [{"employee_name": "Marcus Lee", "parts": [{"text": "no Tuesdays", "noted_on": "2026-10-02"},
                                                       {"text": "max 25 hours", "noted_on": "2026-10-02"}]}]
    pnh.readings(rid, notes, today=TODAY, db_path=db_path)
    assert notes[0]["parts"][0]["held"]["words"].startswith("off Tue")
    assert notes[0]["parts"][1]["reading"]["kind"] == "unchecked"
