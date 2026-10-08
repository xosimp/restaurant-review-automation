"""Seven settings only the web could edit, now on iOS too (iOS parity,
10/7/26; tests/test_ios_request_body_parity.py held them as web-only).

The phone adopts what each save answers, so these pin the answers it reads:
the closed weekdays come back as `closures`, the section count as
`section_count` with the floor/section conflicts, a role's start date as
`since_label` (M/D/YY). The floor-section names route only takes a whole
list, so the phone re-reads it before each add or remove: the GET says
whether this login may change it (`can_edit`, the POST's own check), and
a 31st name is the owner's sentence and a 400 — it was a 500."""
from flask import Flask
import pytest

import models
import people
import shift_facts
import staff_settings
from models import Restaurant, create_restaurant


@pytest.fixture(autouse=True)
def _db(db_path, monkeypatch):
    real = models.get_conn
    fake = lambda *a, **k: real(db_path)  # noqa: E731
    monkeypatch.setattr(models, "get_conn", fake)
    monkeypatch.setattr(models, "DB_PATH", db_path)
    monkeypatch.setattr(staff_settings, "get_conn", fake)
    monkeypatch.setattr("client_api.log_account_event", lambda *a, **k: None)
    yield


def _rid():
    return create_restaurant(Restaurant(name="Gaps Co", owner_email="g@x.test", module_labor=1))


def _rules_post(monkeypatch, user, body):
    import strategy_routes as srt
    monkeypatch.setattr(srt, "_body", lambda: body)
    return srt._do_compliance_set(user)


def test_closed_weekdays_answer_with_the_stored_closures(monkeypatch):
    rid = _rid()
    owner = {"id": 1, "restaurant_id": rid, "role": "client"}
    out, code = _rules_post(monkeypatch, owner, {"closed_weekdays": ["Sunday", "Monday"]})
    assert code == 200 and out["closures"]["closed_weekdays"] == ["Monday", "Sunday"]
    # The phone sends the server's list with one day changed.
    out, code = _rules_post(monkeypatch, owner, {"closed_weekdays": ["Sunday"]})
    assert code == 200 and out["closures"]["closed_weekdays"] == ["Sunday"]
    out, code = _rules_post(monkeypatch, owner, {"closed_weekdays": list(
        ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"])})
    assert code == 400 and "every day" in out["error"]
    manager = {"id": 2, "restaurant_id": rid, "role": "manager"}
    assert _rules_post(monkeypatch, manager, {"closed_weekdays": ["Monday"]})[1] == 403


def test_the_section_count_answers_with_itself_and_null_clears_it(monkeypatch):
    rid = _rid()
    owner = {"id": 1, "restaurant_id": rid, "role": "client"}
    out, code = _rules_post(monkeypatch, owner, {"section_count": 6})
    assert code == 200 and out["section_count"] == 6 and "floor_cap_conflicts" in out
    out, code = _rules_post(monkeypatch, owner, {"section_count": None})
    assert code == 200 and out["section_count"] is None
    assert models.get_restaurant(rid).section_count in (None, 0)
    out, code = _rules_post(monkeypatch, owner, {"section_count": 31})
    assert code == 400 and "1 to 30" in out["error"]


def _sections_app(user, method="GET", json=None):
    import mobile_api
    app = Flask(__name__)
    view = mobile_api.mobile_schedule_sections_save if method == "POST" else mobile_api.mobile_schedule_sections
    with app.test_request_context("/mobile/api/labor/schedule/sections", method=method, json=json):
        out = getattr(view, "__wrapped__", view)(user)
    return out[0].get_json(), out[1]


def test_the_sections_read_says_who_may_change_them():
    rid = _rid()
    admin = {"id": 1, "restaurant_id": rid, "is_admin": True, "username": "o"}
    staff = {"id": 2, "restaurant_id": rid, "role": "employee"}
    assert _sections_app(admin)[0]["can_edit"] is True
    body, code = _sections_app(staff)
    assert code == 200 and body["can_edit"] is False
    assert _sections_app(staff, "POST", {"sections": ["Patio"]})[1] == 403


def test_a_thirty_first_section_is_a_sentence_not_a_server_error():
    rid = _rid()
    admin = {"id": 1, "restaurant_id": rid, "is_admin": True, "username": "o"}
    names = [f"Section {i}" for i in range(1, 31)]
    assert _sections_app(admin, "POST", {"sections": names}) == ({"ok": True, "sections": names}, 200)
    body, code = _sections_app(admin, "POST", {"sections": names + ["Roof"]})
    assert code == 400 and body["error"] == "You can name up to 30 sections."
    body, code = _sections_app(admin, "POST", {"sections": "Patio"})
    assert code == 400 and body["error"] == "Send the sections as a list of names."
    assert _sections_app(admin)[0]["sections"] == names


def test_a_role_added_with_a_start_date_reads_it_back(monkeypatch):
    import strategy_routes as srt
    rid = _rid()
    shift_facts.ingest(rid, [{"date": "2026-09-01", "day": "Tuesday", "employee": "Ana B.", "role": "Server",
                              "shift_start": "16:00", "shift_end": "22:00", "scheduled_hours": "6",
                              "actual_hours": "6", "sales": "1000"}], "upload")
    owner = {"id": 1, "restaurant_id": rid, "role": "client"}
    monkeypatch.setattr(srt, "_body", lambda: {"role": "Bartender", "since": "2026-09-01", "primary": False})
    out, code = srt._do_person_roles(owner, "ana-b")
    assert code == 200, out
    assert out["since"] == "2026-09-01" and out["since_label"] == "9/1/26"
    held = people.get_person(rid, "ana-b")["roles_held"]
    assert any(r["role"] == "Bartender" and r["since"] == "2026-09-01" for r in held)
    monkeypatch.setattr(srt, "_body", lambda: {"role": "Host", "since": "not a date"})
    out, code = srt._do_person_roles(owner, "ana-b")
    assert code == 400 and "M/D/YY" in out["error"]
