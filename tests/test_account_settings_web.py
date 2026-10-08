"""The five account settings that were iOS-only until now.

Web-only clients could not see or change any of these, while the server
kept acting on them regardless — the auto-approve rule publishes review
replies on their behalf from a scheduled job. These tests pin down both
that the web routes work and that they share one implementation with the
iOS routes, so the two surfaces can't drift into meaning different things.
"""
import json

import pytest
from flask import Flask

import auth
import client_api
import mobile_api
import models
from client_api import client_bp
from models import create_restaurant, Restaurant, get_restaurant


@pytest.fixture(autouse=True)
def _redirect_db(monkeypatch, db_path):
    real_get_conn = models.get_conn
    redirect = lambda *a, **k: real_get_conn(db_path)
    for mod in (models, auth, client_api, mobile_api):
        monkeypatch.setattr(mod, "get_conn", redirect)


@pytest.fixture
def app():
    flask_app = Flask(__name__, template_folder="../templates")
    flask_app.register_blueprint(client_bp)
    return flask_app


@pytest.fixture
def client(app):
    return app.test_client()


def _restaurant(db_path, **kw):
    return create_restaurant(Restaurant(name="Web Settings Co", owner_email="w@x.com", **kw), db_path=db_path)


def _login_as(monkeypatch, rid):
    monkeypatch.setattr(auth, "get_current_user",
                        lambda: {"id": 1, "restaurant_id": rid, "is_admin": 0, "username": "webuser"})


def test_auto_approve_is_visible_and_settable_from_the_web(client, db_path, monkeypatch):
    rid = _restaurant(db_path)
    _login_as(monkeypatch, rid)

    # It starts off, and the web can now actually see that.
    shown = client.get("/api/account-settings").get_json()
    # include_4star joined the rule in the retention work: 4-star under the
    # same cap and the same urgency gate, off unless the rule itself is on.
    aa = shown["auto_approve"]
    assert {k: aa[k] for k in ("enabled", "include_4star", "earned", "daily_cap", "paused")} == \
        {"enabled": False, "include_4star": False, "earned": False, "daily_cap": 5, "paused": False}
    # The owner's own record per band rides along (the automation audit's
    # graduated trust) — never a band below 3★.
    assert set(aa["trust"]) == {"3", "4", "5"}

    resp = client.post("/api/account-settings/auto-approve",
                       json={"enabled": True, "daily_cap": 12, "paused": False})
    assert resp.get_json()["ok"] is True
    r = get_restaurant(rid, db_path=db_path)
    assert (r.auto_approve_5star, r.auto_approve_daily_cap, r.auto_approve_paused) == (1, 12, 0)
    assert client.get("/api/account-settings").get_json()["auto_approve"]["daily_cap"] == 12


def test_auto_approve_cap_is_clamped_to_a_sane_range(client, db_path, monkeypatch):
    rid = _restaurant(db_path)
    _login_as(monkeypatch, rid)
    client.post("/api/account-settings/auto-approve", json={"enabled": True, "daily_cap": 9999})
    assert get_restaurant(rid, db_path=db_path).auto_approve_daily_cap == 50
    client.post("/api/account-settings/auto-approve", json={"enabled": True, "daily_cap": 0})
    assert get_restaurant(rid, db_path=db_path).auto_approve_daily_cap == 1
    client.post("/api/account-settings/auto-approve", json={"enabled": True, "daily_cap": "nonsense"})
    assert get_restaurant(rid, db_path=db_path).auto_approve_daily_cap == 5


def test_web_and_ios_auto_approve_run_the_same_code(client, db_path, monkeypatch):
    """The whole point of the shared handler: a rule set on one surface
    means exactly the same thing on the other."""
    rid = _restaurant(db_path)
    _login_as(monkeypatch, rid)
    client.post("/api/account-settings/auto-approve", json={"enabled": True, "daily_cap": 7, "paused": True})
    web_state = get_restaurant(rid, db_path=db_path)

    # Reset, then drive the identical payload through the iOS handler.
    client_api._do_auto_approve(rid, {"enabled": False, "daily_cap": 5, "paused": False})
    client_api._do_auto_approve(rid, {"enabled": True, "daily_cap": 7, "paused": True})
    ios_state = get_restaurant(rid, db_path=db_path)
    assert (web_state.auto_approve_5star, web_state.auto_approve_daily_cap, web_state.auto_approve_paused) == \
           (ios_state.auto_approve_5star, ios_state.auto_approve_daily_cap, ios_state.auto_approve_paused)


def test_hours_and_closures_round_trip(client, db_path, monkeypatch):
    rid = _restaurant(db_path)
    _login_as(monkeypatch, rid)
    resp = client.post("/api/account-settings/hours", json={
        "open": {"Monday": "09:00", "Saturday": "10:00", "NotADay": "05:00"},
        "close": {"Monday": "22:00"},
        "closures": ["2026-12-25", "2026-12-25", "2026-11-26"],
    })
    assert resp.get_json()["ok"] is True
    shown = client.get("/api/account-settings").get_json()["hours"]
    # Unknown day names are dropped, closures are de-duplicated and sorted.
    assert shown["open"] == {"Monday": "09:00", "Saturday": "10:00"}
    assert shown["close"] == {"Monday": "22:00"}
    assert shown["closures"] == ["2026-11-26", "2026-12-25"]


def test_account_closures_are_the_schedulers_closed_dates(client, db_path, monkeypatch):
    """Friction audit U2-3: Account's closures box said "used when generating
    schedules" and wrote the marketing holiday-skip list, which the scheduler
    never reads - and saving hours with it empty wiped that list."""
    import schedule_rules
    from models import update_restaurant
    rid = _restaurant(db_path)
    _login_as(monkeypatch, rid)
    update_restaurant(rid, {"skip_holidays": "Christmas Day"}, db_path=db_path)
    resp = client.post("/api/account-settings/hours", json={
        "open": {}, "close": {}, "closures": ["12/25/26", "2027-01-01", "someday"]}).get_json()
    assert resp["ok"] is True and resp["ignored"] == ["someday"]
    r = get_restaurant(rid, db_path=db_path)
    assert schedule_rules.closures(r)["closed_dates"] == ["2026-12-25", "2027-01-01"]
    assert r.skip_holidays == "Christmas Day", "the marketing list is its own setting"
    # A date Labor's rules added shows in Account: one list, two doors.
    schedule_rules.save_closures(rid, closed_dates=["2026-11-26"], db_path=db_path)
    assert client.get("/api/account-settings").get_json()["hours"]["closures"] == ["2026-11-26"]
    # Saving hours without sending closures leaves them alone.
    client.post("/api/account-settings/hours", json={"open": {"Monday": "09:00"}, "close": {}})
    assert schedule_rules.closures(get_restaurant(rid, db_path=db_path))["closed_dates"] == ["2026-11-26"]


def test_data_retention_accepts_only_the_offered_choices(client, db_path, monkeypatch):
    rid = _restaurant(db_path)
    _login_as(monkeypatch, rid)
    assert client.post("/api/account-settings/data-retention", json={"months": 12}).get_json()["ok"] is True
    assert client.get("/api/account-settings").get_json()["data_retention_months"] == 12

    bad = client.post("/api/account-settings/data-retention", json={"months": 7})
    assert bad.status_code == 400
    # The rejected value never lands.
    assert get_restaurant(rid, db_path=db_path).data_retention_months == 12


def test_marketing_opt_out_and_login_notify_round_trip(client, db_path, monkeypatch):
    rid = _restaurant(db_path)
    _login_as(monkeypatch, rid)
    client.post("/api/account-settings/marketing-opt-out", json={"opted_out": True})
    client.post("/api/account-settings/login-notify", json={"enabled": True})
    shown = client.get("/api/account-settings").get_json()
    assert shown["marketing_emails_opt_out"] is True
    assert shown["login_notify"] is True
    client.post("/api/account-settings/marketing-opt-out", json={"opted_out": False})
    assert client.get("/api/account-settings").get_json()["marketing_emails_opt_out"] is False


def test_every_change_is_recorded_in_the_account_activity_log(client, db_path, monkeypatch):
    rid = _restaurant(db_path)
    _login_as(monkeypatch, rid)
    client.post("/api/account-settings/auto-approve", json={"enabled": True, "daily_cap": 5})
    client.post("/api/account-settings/data-retention", json={"months": 24})

    from models import get_account_activity
    events = get_account_activity(rid, db_path=db_path)
    by_type = {e["type"]: e for e in events}
    assert "auto_approve_changed" in by_type
    assert by_type["auto_approve_changed"]["actor"] == "webuser"
    assert "cap 5/day" in by_type["auto_approve_changed"]["detail"]
    assert by_type["data_retention_changed"]["detail"] == "24 months"


def test_settings_are_scoped_to_the_signed_in_restaurant(client, db_path, monkeypatch):
    mine = _restaurant(db_path)
    theirs = create_restaurant(Restaurant(name="Other Co", owner_email="o@x.com"), db_path=db_path)
    _login_as(monkeypatch, mine)
    client.post("/api/account-settings/auto-approve", json={"enabled": True, "daily_cap": 20})
    assert get_restaurant(theirs, db_path=db_path).auto_approve_5star in (0, None)
    assert get_restaurant(mine, db_path=db_path).auto_approve_5star == 1


# ── closed dates save one at a time (owner, 9/28/26) ────────────────────────
# Erik added two closed dates and they were gone after a reload: a chip only
# lived in the page until Save hours, a date picked but not "Add"-ed never
# became one, and every Save hours / Save rules wrote back the whole list the
# page had loaded - over dates added anywhere since.

def test_a_closed_date_saves_the_moment_it_is_added_and_removed(client, db_path, monkeypatch):
    import schedule_rules
    rid = _restaurant(db_path)
    _login_as(monkeypatch, rid)
    d = client.post("/api/account-settings/closures", json={"add": "2026-11-26"}).get_json()
    assert d["ok"] and d["closures"] == ["2026-11-26"] and d["added"] == ["2026-11-26"]
    d = client.post("/api/account-settings/closures", json={"add": "12/25/26"}).get_json()
    assert d["closures"] == ["2026-11-26", "2026-12-25"]
    # Stored, not just echoed: a fresh read shows both.
    assert client.get("/api/account-settings").get_json()["hours"]["closures"] == ["2026-11-26", "2026-12-25"]
    d = client.post("/api/account-settings/closures", json={"remove": "2026-11-26"}).get_json()
    assert d["closures"] == ["2026-12-25"]
    assert schedule_rules.closures(get_restaurant(rid, db_path=db_path))["closed_dates"] == ["2026-12-25"]
    bad = client.post("/api/account-settings/closures", json={"add": "someday"})
    assert bad.status_code == 400 and "someday" in bad.get_json()["error"]
    assert client.post("/api/account-settings/closures", json={}).status_code == 400


def test_closed_dates_are_the_owners(client, db_path, monkeypatch):
    rid = _restaurant(db_path)
    monkeypatch.setattr("permissions.is_principal", lambda u: False)
    out, status = client_api._do_account_closures(rid, {"add": "2026-11-26"}, {"id": 2, "restaurant_id": rid})
    assert status == 403 and not out["ok"]


def test_a_sheet_that_sends_its_list_only_applies_its_own_changes(client, db_path, monkeypatch):
    """The iPhone Hours sheet sends the list it opened with as closures_base:
    a date added on the web since it opened is kept, the sheet's own add and
    remove are applied."""
    import schedule_rules
    rid = _restaurant(db_path)
    _login_as(monkeypatch, rid)
    schedule_rules.save_closures(rid, closed_dates=["2026-11-26", "2026-12-24"], db_path=db_path)  # sheet opens here
    client.post("/api/account-settings/closures", json={"add": "2026-12-31"})                         # web adds one
    client.post("/api/account-settings/hours", json={
        "open": {}, "close": {},
        "closures": ["2026-11-26", "2027-01-01"],             # sheet removed 12/24, added 1/1
        "closures_base": ["2026-11-26", "2026-12-24"]})
    assert schedule_rules.closures(get_restaurant(rid, db_path=db_path))["closed_dates"] == \
        ["2026-11-26", "2026-12-31", "2027-01-01"]


def test_the_web_never_sends_a_whole_closed_dates_list_again():
    import os
    src = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            "templates", "dashboard.html"), encoding="utf-8").read()
    save = src[src.index("function saveAccountHours()"):src.index("function asClosureChip(")]
    assert "closures:" not in save and "asAddClosure()" in save   # a picked date is added, not dropped
    add = src[src.index("function asAddClosure()"):src.index("// Hours prefill (U2-19)")]
    assert "cavClosedDateChange({ add: v }" in add
    change = src[src.index("function cavClosedDateChange("):src.index("function asAddClosure()")]
    assert "/api/account-settings/closures" in change
    rules_save = src[src.index("body.closed_weekdays="):src.index("jsend('/api/labor/rules'")]
    assert "closed_dates" not in rules_save
    listener = src[src.index("t.getAttribute('data-cdate-x')===null"):][:400]
    assert "cavClosedDateChange({remove:iso}" in listener


def test_the_iphone_sheet_saves_each_closed_date_on_its_own():
    """iOS parity #7: the sheet no longer sends a closures list with Save
    hours (closures_base stays on the server for an older build) — each add
    or remove posts {add}/{remove} to /account/closures, as the web does."""
    import os
    root = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "ios", "CavnarAI", "CavnarAI", "Features", "Account")
    vm = open(os.path.join(root, "AccountViewModel.swift"), encoding="utf-8").read()
    sheet = open(os.path.join(root, "AccountHoursSheet.swift"), encoding="utf-8").read()
    assert "closures_base" not in vm and "closuresBase" not in sheet
    assert '"/mobile/api/account/closures"' in vm
    assert "changeClosure(" in sheet and ".init(add: s)" in sheet and ".init(remove: date)" in sheet
