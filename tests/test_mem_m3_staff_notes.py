"""Staff notes are dated, end, and get asked about; attendance and usual days
weigh the recent past (memory audit 9/29/26, staff_notes; and the identity
interim rule — one name_key match, no first-name fallback).

"Out until 6/1 after surgery" still blocked a cook in September: the note had
no date and no end, and the schedule prompt said it outranks every rule. A
server's three no-shows last winter kept them "unreliable" after six clean
months, and a cook who moved to nights in June was still offered mornings.
"""
import types
from datetime import date, timedelta

import pytest
from flask import Flask

import action_queue
import auth
import labor
import mobile_api
import models
import staff_settings
import strategy_routes
from models import Restaurant, create_restaurant


@pytest.fixture(autouse=True)
def _db(db_path, monkeypatch):
    real = models.get_conn
    fake = lambda *a, **k: real(db_path)  # noqa: E731
    monkeypatch.setattr(models, "get_conn", fake)
    monkeypatch.setattr(models, "DB_PATH", db_path)
    monkeypatch.setattr(action_queue, "get_conn", fake)
    monkeypatch.setattr(staff_settings, "get_conn", fake)
    yield


def _rid(**kw):
    fields = dict(name="Notes Co", owner_email="n@x.test", module_labor=1)
    fields.update(kw)
    return create_restaurant(Restaurant(**fields))


# ── dated constraints that end ───────────────────────────────────────────────

def test_a_constraint_that_says_until_ends_then_and_its_neighbour_does_not():
    rid = _rid()
    models.save_staff_note(rid, "Luis R.", "mornings only", today=date(2026, 5, 2))
    out = models.save_staff_note(rid, "Luis R.", "Out until 6/1 after surgery", today=date(2026, 5, 20))
    assert out["appended"] is True and out["expires_on"] == "2026-06-01"
    may = models.get_staff_notes(rid, today=date(2026, 5, 25))
    assert may[0]["notes"] == "mornings only; Out until 6/1 after surgery"
    assert may[0]["expires_on"] == "2026-06-01"
    sept = models.get_staff_notes(rid, today=date(2026, 9, 14))
    assert sept[0]["notes"] == "mornings only"          # the surgery note is gone; mornings stays
    # The admin view keeps the ended one, marked.
    everything = models.get_staff_notes(rid, include_expired=True, today=date(2026, 9, 14))
    assert [p["ended"] for p in everything[0]["parts"]] == [False, True]


def test_a_person_whose_only_note_ended_is_left_out_of_every_reader():
    rid = _rid()
    models.save_staff_note(rid, "Ana P.", "no shifts through 6/15/26", today=date(2026, 6, 1))
    assert models.get_staff_notes(rid, today=date(2026, 9, 1)) == []
    assert mobile_api._staff_constraints_index(rid) == {} or \
        models.get_staff_notes(rid) == []


def test_an_explicit_end_date_wins_and_a_bad_one_is_ignored():
    rid = _rid()
    out = models.save_staff_note(rid, "Sam", "closer this month", expires_on="10/31/26", today=date(2026, 10, 1))
    assert out["expires_on"] == "2026-10-31"
    assert models.staff_note_expiry("back on Oct 3", "2026-09-29") == "2026-10-03"
    assert models.staff_note_expiry("until 6/1", "2026-07-02") == "2027-06-01"   # next June
    assert models.staff_note_expiry("mornings only", "2026-07-02") is None


def test_both_prompts_print_the_day_each_constraint_was_noted(monkeypatch):
    rid = _rid()
    models.save_staff_note(rid, "Luis R.", "mornings only", today=date(2026, 5, 2))
    notes = models.get_staff_notes(rid, today=date(2026, 9, 1))
    line = models.staff_note_line(notes[0])
    assert line == "Luis R.: mornings only (noted 5/2/26)"
    captured = {}

    def fake(client, **kwargs):
        captured.update(kwargs)
        return types.SimpleNamespace(content=[types.SimpleNamespace(
            text="date,day,employee,role,shift_start,shift_end,scheduled_hours,notes\n---SUMMARY---\n- ok")],
            stop_reason="end_turn")
    monkeypatch.setattr(labor, "create_with_retry", fake)
    monkeypatch.setattr(labor, "get_client", lambda *a, **k: None)
    monkeypatch.setattr(labor, "model_for", lambda k: "m")
    analysis = {"overall_labor_pct": 28.0, "overstaffed_days": [], "understaffed_days": [], "dow_summary": {},
                "total_sales": 60000, "period_days": 21, "by_day": {}}
    labor.generate_optimized_schedule(analysis, [], restaurant_name="T", hourly_rate=20.0, labor_target=30.0,
                                      week_start="2026-10-05", staff_notes=notes, roster=[("Luis R.", "Cook")])
    prompt = captured["messages"][0]["content"]
    assert "- Luis R.: mornings only (noted 5/2/26)" in prompt
    assert "Each is dated the day it was noted" in prompt


# ── the owner's "still true?" ────────────────────────────────────────────────

def test_a_note_nobody_confirmed_in_90_days_is_asked_about_until_confirmed():
    rid = _rid()
    note = models.save_staff_note(rid, "Maria G.", "no Sundays", today=date(2026, 5, 1))
    today = date(2026, 9, 1)
    stale = models.stale_staff_notes(rid, today=today)
    assert [(s["employee_name"], s["noted"]) for s in stale] == [("Maria G.", "5/1/26")]
    q = action_queue.items(rid, today=today, present=False)["items"]
    item = next(i for i in q if i["key"] == "staff_note:stale")
    assert "still true" in item["title"] and item["answerable"] is False
    models.update_staff_note_part(rid, note["id"], 0, confirm=True, today=today)
    assert models.stale_staff_notes(rid, today=today) == []
    # Still in force the whole time — stale is a question, never a removal.
    assert models.get_staff_notes(rid, today=today)[0]["notes"] == "no Sundays"


def test_saying_the_same_constraint_again_confirms_it():
    rid = _rid()
    models.save_staff_note(rid, "Maria G.", "no Sundays", today=date(2026, 5, 1))
    again = models.save_staff_note(rid, "maria g.", "No Sundays", today=date(2026, 8, 20))
    assert again["appended"] is False
    assert models.stale_staff_notes(rid, today=date(2026, 9, 1)) == []


# ── the owner's routes (web and mobile twins share one body) ─────────────────

@pytest.fixture
def http(monkeypatch, db_path):
    rid = _rid()
    user = {"id": 7, "restaurant_id": rid, "base_restaurant_id": rid, "username": "owner", "role": "owner",
            "is_admin": 0, "email": "o@x.test"}
    monkeypatch.setattr(auth, "get_current_user", lambda: user)
    monkeypatch.setattr(auth, "get_session_user", lambda *a, **k: user, raising=False)
    app = Flask(__name__)
    app.register_blueprint(strategy_routes.strategy_bp)
    app.register_blueprint(strategy_routes.strategy_mobile_bp)
    c = app.test_client()
    c.rid = rid
    return c


def test_the_owner_adds_lists_ends_and_removes_a_constraint(http):
    r = http.post("/api/labor/staff-notes", json={"employee_name": "Luis R.", "notes": "mornings only",
                                                   "expires_on": "12/31/26"})
    assert r.status_code == 200 and r.get_json()["note"]["parts"][0]["expires"] == "12/31/26"
    bad = http.post("/api/labor/staff-notes", json={"employee_name": "Luis R.", "notes": "x", "expires_on": "soon"})
    assert bad.status_code == 400
    listed = http.get("/mobile/api/labor/staff-notes", headers={"Authorization": "Bearer t"}).get_json()
    note = listed["notes"][0]
    assert note["employee_name"] == "Luis R." and note["parts"][0]["noted"]      # M/D/YY
    assert "/" in note["parts"][0]["noted"] and "-" not in note["parts"][0]["noted"]
    done = http.post(f"/api/labor/staff-notes/{note['id']}", json={"action": "remove", "part": 0}).get_json()
    assert done["removed"] is True
    assert http.get("/api/labor/staff-notes").get_json()["notes"] == []


# ── the interim identity rule: one name_key match, no first-name fallback ────

def test_ot_allowed_on_maria_garcia_does_not_cover_maria_lopez():
    rid = _rid()
    models.save_staff_note(rid, "Maria Garcia", "OT allowed")
    idx = mobile_api._staff_constraints_index(rid)
    assert mobile_api._has_ot_allowance("maria  garcia", idx) is True
    assert mobile_api._has_ot_allowance("Maria Lopez", idx) is False
    assert mobile_api._has_ot_allowance("Maria", idx) is False


# ── recency: reliability and usual days ─────────────────────────────────────

def test_old_no_shows_fade_and_recent_ones_count():
    today = date(2026, 9, 29)
    old = [("Pat", (today - timedelta(days=250 + i)).isoformat(), "no_show") for i in range(3)]
    clean = [("Pat", (today - timedelta(days=7 * i)).isoformat(), "worked") for i in range(1, 9)]
    others = [(f"P{j}", (today - timedelta(days=7 * i)).isoformat(), "worked") for j in range(4) for i in range(1, 10)]
    rel = staff_settings.weighted_attendance(old + clean + others, today=today)
    assert rel["Pat"]["no_shows"] == 3 and rel["Pat"]["unreliable"] is False
    recent = [("Pat", (today - timedelta(days=i)).isoformat(), "no_show") for i in (2, 9, 16)]
    rel2 = staff_settings.weighted_attendance(recent + clean + others, today=today)
    assert rel2["Pat"]["unreliable"] is True
    # Past the window a miss says nothing at all.
    ancient = [("Pat", (today - timedelta(days=400)).isoformat(), "no_show")]
    rel3 = staff_settings.weighted_attendance(ancient + clean + others, today=today)
    assert rel3["Pat"]["no_shows"] == 0


def test_usual_days_are_the_last_twelve_weeks_recurring():
    today = date(2026, 9, 29)
    rows = []
    # Mornings through June, nights since July.
    for i in range(20, 14, -1):
        d = today - timedelta(weeks=i)
        rows.append({"employee": "Cook A", "date": d.isoformat(), "shift_start": "09:00"})
    for i in range(12, 0, -1):
        d = today - timedelta(weeks=i)
        rows.append({"employee": "Cook A", "date": d.isoformat(), "shift_start": "17:00"})
    pat = models.usual_pattern(rows, today=today)
    assert pat["Cook A"]["dayparts"] == ["night"]
