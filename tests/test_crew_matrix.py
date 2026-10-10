"""The Schedule Studio's Crew stage (crew_matrix, owner 10/10/26): each role's
usual crew per weekday and shift beside the draft; where the draft runs
leaner the owner answers once - Keep usual becomes that shift's minimum,
Leaner is fine keeps the saving, Undo puts the earlier minimum back."""
import json

import pytest

import crew_matrix as cm
import models
import schedule_rules as sr
from models import Restaurant, create_restaurant, get_conn, get_restaurant


@pytest.fixture(autouse=True)
def _redirect(db_path, monkeypatch):
    real = models.get_conn
    monkeypatch.setattr(models, "get_conn", lambda *a, **k: real(db_path))
    monkeypatch.setattr(models, "DB_PATH", db_path)


BASE = {"typical_headcount": {("Monday", "morning"): {"Kitchen": 3, "Server AM": 2},
                              ("Monday", "night"): {"Kitchen": 5, "Server PM": 4},
                              ("Saturday", "night"): {"Kitchen": 6, "Server PM": 5}},
        "headcount_trends": [{"day": "Saturday", "part": "night", "role": "Server PM", "now": 5, "was": 4,
                              "since": "2026-09-12", "direction": "up"}]}


def _week(rid, rows):
    head = "date,day,employee,role,shift_start,shift_end,scheduled_hours,notes\n"
    body = "".join(f"{d},{day},{e},{role},{st},{en},6,\n" for d, day, e, role, st, en in rows)
    return models.save_schedule_history(rid, "2026-10-12", "2026-10-18", 40, 100, 30, head + body, [])


def _setup(monkeypatch):
    rid = create_restaurant(Restaurant(name="EJ", owner_email="e@x.test"))
    monkeypatch.setattr("labor.staffing_baseline", lambda *_a, **_k: BASE)
    rows = [("2026-10-12", "Monday", f"Cook{i}", "Kitchen", "9:00am", "3:00pm") for i in range(2)]
    rows += [("2026-10-12", "Monday", f"Cook{i}", "Kitchen", "4:00pm", "10:00pm") for i in range(5)]
    rows += [("2026-10-12", "Monday", f"Srv{i}", "Server AM", "10:00am", "3:00pm") for i in range(3)]
    rows += [("2026-10-17", "Saturday", f"S{i}", "Server PM", "4:00pm", "11:00pm") for i in range(3)]
    rows += [("2026-10-17", "Saturday", "Trainee", "Training", "4:00pm", "11:00pm")]
    _week(rid, rows)
    return rid


def _cell(out, role, day, part):
    r = next(x for x in out["roles"] if x["role"] == role)
    return next(c for c in r["cells"] if c["day"] == day and c["part"] == part)


def test_each_shift_is_the_usual_beside_the_draft(monkeypatch):
    rid = _setup(monkeypatch)
    out = cm.build(rid)
    assert out["ok"] and out["has_usual"] and out["week_start"] == "2026-10-12"
    assert _cell(out, "Kitchen", "Monday", "morning")["status"] == "under"
    assert _cell(out, "Kitchen", "Monday", "night")["status"] == "match"
    assert _cell(out, "Server AM", "Monday", "morning")["status"] == "over"
    sat = _cell(out, "Server PM", "Saturday", "night")
    assert (sat["usual"], sat["draft"], sat["status"]) == (5, 3, "under") and sat["trend"]["was"] == 4
    assert not any(r["role"] == "Training" for r in out["roles"])        # training is nobody's crew
    # Leaner: Kitchen Monday lunch and Saturday dinner (none drafted),
    # Server PM Monday and Saturday dinner.
    assert out["open"] == 4 and _cell(out, "Kitchen", "Saturday", "night")["draft"] == 0


def test_keep_usual_becomes_that_shifts_minimum_and_undo_puts_back_the_old_one(monkeypatch):
    rid = _setup(monkeypatch)
    sr.save_role_floors(rid, {"Server PM": {"morning": 0, "night": 0, "days": {"Saturday": {"night": 4}}}})
    out, status = cm.answer(rid, "Server PM", "Saturday", "night", "hold")
    assert status == 200 and _cell(out, "Server PM", "Saturday", "night")["answer"] == "hold"
    floors = sr.role_floors(get_restaurant(rid))
    assert sr.floor_for(floors, "Server PM", "Saturday", "night") == 5
    assert _cell(out, "Server PM", "Saturday", "night")["status"] == "below_rule"   # the draft is now under it
    out, _ = cm.answer(rid, "server pm", "saturday", "night", "clear")
    assert sr.floor_for(sr.role_floors(get_restaurant(rid)), "Server PM", "Saturday", "night") == 4
    assert _cell(out, "Server PM", "Saturday", "night")["answer"] is None


def test_leaner_is_fine_keeps_the_saving_and_sets_no_minimum(monkeypatch):
    rid = _setup(monkeypatch)
    out, status = cm.answer(rid, "Kitchen", "Monday", "morning", "lean")
    assert status == 200 and _cell(out, "Kitchen", "Monday", "morning")["answer"] == "lean"
    assert sr.floor_for(sr.role_floors(get_restaurant(rid)), "Kitchen", "Monday", "morning") == 0
    assert out["open"] == 3
    stored = json.loads(get_restaurant(rid).crew_answers_json)
    assert stored["Kitchen|Monday|morning"]["answer"] == "lean"
    # Moving a held shift to leaner takes its minimum away again.
    cm.answer(rid, "Kitchen", "Monday", "night", "hold")
    assert sr.floor_for(sr.role_floors(get_restaurant(rid)), "Kitchen", "Monday", "night") == 5
    cm.answer(rid, "Kitchen", "Monday", "night", "lean")
    assert sr.floor_for(sr.role_floors(get_restaurant(rid)), "Kitchen", "Monday", "night") == 0


def test_only_someone_who_builds_the_schedule_answers_and_bad_input_is_refused(monkeypatch):
    rid = _setup(monkeypatch)
    out, status = cm.api_body(rid, "POST", {"role": "Kitchen", "day": "Monday", "part": "morning", "answer": "hold"},
                              may_answer=False)
    assert status == 403
    out, status = cm.api_body(rid, "POST", {"role": "Kitchen", "day": "Funday", "part": "morning", "answer": "hold"},
                              may_answer=True)
    assert status == 400
    out, status = cm.api_body(rid, "POST", {"role": "Barback", "day": "Monday", "part": "morning", "answer": "hold"},
                              may_answer=True)
    assert status == 400 and "no usual crew" in out["error"]
    out, status = cm.api_body(rid, "GET", args={"history_id": "x"})
    assert status == 200 and out["ok"]


def test_a_restaurant_with_no_history_says_so(monkeypatch):
    rid = create_restaurant(Restaurant(name="New", owner_email="n@x.test"))
    monkeypatch.setattr("labor.staffing_baseline", lambda *_a, **_k: {})
    out = cm.build(rid)
    assert out["ok"] and not out["has_usual"] and out["roles"] == [] and out["week_start"] is None


def test_both_routes_exist_and_the_studio_has_the_stage():
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    assert '@client_bp.route("/api/labor/crew-matrix"' in (root / "client_api.py").read_text()
    assert '@mobile_bp.route("/labor/crew-matrix"' in (root / "mobile_api.py").read_text()
    html = (root / "templates" / "dashboard.html").read_text()
    assert 'data-ss-go="crew"' in html and 'data-stage="crew"' in html and "function ssRenderCrew()" in html


def test_with_no_week_built_it_shows_the_usual_crew_and_asks_nothing(monkeypatch):
    rid = create_restaurant(Restaurant(name="Fresh", owner_email="f@x.test"))
    monkeypatch.setattr("labor.staffing_baseline", lambda *_a, **_k: BASE)
    out = cm.build(rid)
    assert out["has_usual"] and out["week_start"] is None
    assert out["open"] == 0 and out["differences"] == 0
    assert {c["status"] for r in out["roles"] for c in r["cells"]} == {"usual"}


def test_managers_and_owners_are_the_manager_rules_not_the_grids(monkeypatch):
    rid = create_restaurant(Restaurant(name="Mgr", owner_email="m@x.test"))
    monkeypatch.setattr("labor.staffing_baseline", lambda *_a, **_k: BASE)
    _week(rid, [("2026-10-12", "Monday", "Jim", "Manager FOH", "3:00pm", "11:00pm"),
                ("2026-10-12", "Monday", "Erik", "Owner", "10:00am", "4:00pm")])
    roles = {r["role"] for r in cm.build(rid)["roles"]}
    assert "Manager FOH" not in roles and "Owner" not in roles and "Kitchen" in roles
