"""Employee audit fix round B2 — staff notifications (10/1/26).

C4  staff push that is safe (a tier on every device; a restaurant-wide push
    never reaches a staff phone; no owner badge on one; the two console
    broadcasts name their logins), registrable (/staff/api/device-tokens),
    truthful (counted only when Apple took it, else the text/email chain)
    and routable (staff types: uncollapsible, prioritised, a staff nav).
M8  quiet hours for staff notices: a passive push and a text held until 8am
    between 10pm and 8am restaurant time, except a shift before then.
H3  the staff_reminders job: shift-start and task-due pushes, claimed once.
H14 a consent that says what it covers, `sms_available`, and a `purpose`
    on people.reach.

Every clock is pinned: people.staff_local_now, or the job's now_utc.
"""
import json
import sys
from datetime import date, datetime, timedelta

import pytest
from flask import Flask

import auth
import client_api
import models
import notify
import people
import preferences
import push
import staff_reminders
from models import Restaurant, create_restaurant

NOON = datetime(2026, 10, 1, 12, 0)
LATE = datetime(2026, 10, 1, 23, 0)
HEADER = "date,day,employee,role,shift_start,shift_end,scheduled_hours,notes"


@pytest.fixture
def db(db_path, monkeypatch):
    real = models.get_conn

    def conn(*a, **k):
        return real(db_path)
    for mod in list(sys.modules.values()):
        bound = getattr(mod, "get_conn", None) if mod is not None else None
        if bound is real:
            monkeypatch.setattr(mod, "get_conn", conn)
    monkeypatch.setattr(models, "get_conn", conn)
    monkeypatch.setattr(models, "DB_PATH", db_path)
    monkeypatch.setattr(push, "DB_PATH", db_path)
    monkeypatch.setattr(models, "_cached_shifts", lambda r: [])
    monkeypatch.setattr(client_api, "log_account_event", lambda *a, **k: None)
    auth.init_auth(db_path=db_path)
    push.init_push(db_path=db_path)
    monkeypatch.setattr(people, "staff_local_now", lambda rid: NOON)
    return db_path


@pytest.fixture
def apns(monkeypatch):
    """The push pool, run inline, with Apple's answer chosen per test:
    out["ok"] = True (delivered) or False (no device took it)."""
    out = {"ok": True, "sent": []}

    class _Inline:
        def submit(self, fn, *args):
            fn(*args)

    def _deliver(token_row, alert_type, title, body, data, db_path=None):
        out["sent"].append({"token": token_row["apns_token"], "tier": token_row.get("tier"),
                            "user_id": token_row.get("user_id"), "type": alert_type, "title": title,
                            "data": dict(data or {}), "badge": push._badge_for(token_row, db_path)})
        return {"ok": out["ok"], "error": None if out["ok"] else "Unregistered"}
    monkeypatch.setattr(push, "_push_executor", lambda: _Inline())
    monkeypatch.setattr(push, "_deliver", _deliver)
    monkeypatch.setattr(push, "_queued", 0)
    return out


@pytest.fixture
def wire(monkeypatch):
    """Texts and emails, recorded; nothing leaves."""
    import emails
    out = {"sms": [], "email": []}
    monkeypatch.setattr(notify, "send_sms", lambda to, msg, use_case="alert": out["sms"].append((to, msg)) or True)
    monkeypatch.setattr(emails, "deliver", lambda payload=None, restaurant_id=None, email_type=None, **k:
                        out["email"].append((payload["to"][0], payload["subject"])) or emails.SendResult(True))
    monkeypatch.setattr(emails, "send_staff_schedule_email",
                        lambda **kw: out["email"].append((kw["to_email"], "week")) or emails.SendResult(True))
    monkeypatch.setattr(notify, "TWILIO_SID", "AC1")
    monkeypatch.setattr(notify, "TWILIO_TOKEN", "tok")
    monkeypatch.setattr(notify, "TWILIO_STAFF_MESSAGING_SERVICE_SID", "MG-staff")
    return out


def _restaurant(db_path, roster=()):
    rid = create_restaurant(Restaurant(name="Staff Co", owner_email="o@x.com"), db_path=db_path)
    c = models.get_conn(db_path)
    c.execute("UPDATE restaurants SET module_labor=1, timezone='America/Chicago' WHERE id=?", (rid,))
    c.commit()
    c.close()
    import staff_settings as _ss_fx
    for name in roster:
        models.add_manual_team_member(rid, name, role="Server", db_path=db_path)
        # Everyone here runs the floor: a roster with nobody counted as a
        # floor manager holds the publish (schedule re-audit 10/4/26 RULES-13).
        _ss_fx.upsert(rid, name, floor_manager=True, db_path=db_path)
    return rid


def _owner(db_path, rid):
    return auth.create_user(rid, f"owner{rid}", f"owner{rid}@x.com", "x" * 32, db_path=db_path)


def _staff(db_path, rid, name, phone=None, texts=False):
    uid = auth.create_user(rid, name.lower().replace(" ", "").replace(".", "") + f".{rid}",
                           f"{name[:3].lower()}{rid}@staff.invalid", "x" * 32, db_path=db_path)
    m = auth.upsert_membership(uid, rid, "employee", employee_name=name, db_path=db_path)
    auth.set_membership_pin(m["id"], rid, "4827", db_path=db_path)
    c = models.get_conn(db_path)
    if phone:
        c.execute("UPDATE users SET phone=? WHERE id=?", (phone, uid))
    if texts:
        c.execute("UPDATE memberships SET schedule_texts_at=datetime('now') WHERE id=?", (m["id"],))
    c.commit()
    c.close()
    return uid, m["id"]


def _staff_app(db_path, rid, uid):
    from staff_routes import staff_bp
    app = Flask(__name__, template_folder="../templates")
    app.register_blueprint(staff_bp)
    c = app.test_client()
    c.set_cookie("staff_session", auth.create_staff_session(uid, rid, db_path=db_path))
    return c


def _rows(db_path, sql, args=()):
    c = models.get_conn(db_path)
    try:
        return [dict(r) for r in c.execute(sql, args).fetchall()]
    finally:
        c.close()


# ── C4 (1): the tier filter ──────────────────────────────────────────────────

def test_a_restaurant_wide_push_never_reaches_a_staff_device(db, apns):
    rid = _restaurant(db)
    owner = _owner(db, rid)
    emp, _m = _staff(db, rid, "Ana")
    push.register_device_token(owner, rid, "o" * 64, db_path=db)
    push.register_device_token(emp, rid, "s" * 64, db_path=db, tier=push.TIER_STAFF)
    for t in ("login", "staff_signin", "morning_brief", "1star", "labor_over"):
        push.fire_push(rid, t, "x", "y", db_path=db)
    assert apns["sent"] and {s["token"] for s in apns["sent"]} == {"o" * 64}
    # Naming the employee's login does not let an owner type through either.
    apns["sent"].clear()
    push.fire_push(rid, "morning_brief", "x", "y", db_path=db, user_ids=[emp, owner])
    assert {s["token"] for s in apns["sent"]} == {"o" * 64}
    # Only a staff type, addressed to them, reaches the staff phone — with no
    # owner badge on it.
    apns["sent"].clear()
    push.fire_push(rid, "staff_request", "x", "y", db_path=db, user_ids=[emp])
    assert [(s["token"], s["badge"]) for s in apns["sent"]] == [("s" * 64, None)]
    # And every "who has a phone here" reader means the console's phones.
    assert {t["apns_token"] for t in push.get_device_tokens(rid, db, for_delivery=True)} == {"o" * 64}
    assert {t["apns_token"] for t in push.get_device_tokens(rid, db)} == {"o" * 64}


def test_a_deactivated_membership_stops_its_staff_phone_at_once(db, apns):
    rid = _restaurant(db)
    emp, mid = _staff(db, rid, "Ana")
    push.register_device_token(emp, rid, "s" * 64, db_path=db, tier=push.TIER_STAFF)
    c = models.get_conn(db)
    c.execute("UPDATE memberships SET is_active=0 WHERE id=?", (mid,))
    c.commit()
    c.close()
    assert push.fire_push(rid, "staff_request", "x", "y", db_path=db, user_ids=[emp]) == 0


def test_the_two_console_broadcasts_name_owners_and_managers_only(db, monkeypatch):
    import emails
    rid = _restaurant(db)
    owner = _owner(db, rid)
    emp, _m = _staff(db, rid, "Ana")
    push.register_device_token(owner, rid, "o" * 64, db_path=db)
    push.register_device_token(emp, rid, "s" * 64, db_path=db, tier=push.TIER_STAFF)
    calls = []
    monkeypatch.setattr(push, "fire_push", lambda rid_, t, title, body, data=None, db_path=None, user_ids=None, **k:
                        calls.append((t, user_ids)) or 1)
    monkeypatch.setattr(emails, "send_login_notification", lambda *a, **k: None)
    notify.send_login_alert(rid, "Staff Co", "o@x.com", "203.0.113.9", "ua", db_path=db)
    notify.send_staff_signin_alert(rid, "Staff Co", "o@x.com", "Ana", "203.0.113.9", db_path=db)
    assert calls == [("login", [owner]), ("staff_signin", [owner])]


# ── C4 (2): registration ────────────────────────────────────────────────────

def test_the_staff_app_registers_its_phone_under_its_own_login(db, monkeypatch):
    rid = _restaurant(db)
    emp, _m = _staff(db, rid, "Ana")
    other, _m2 = _staff(db, rid, "Ben")
    c = _staff_app(db, rid, emp)
    r = c.post("/staff/api/device-tokens", json={"apns_token": "a" * 64, "environment": "sandbox"})
    body = r.get_json()
    assert r.status_code == 200 and body["ok"] and body["reminders_server_side"] is True
    assert body["push_registered"] is True and "staff_request" in body["alert_types"]
    row = _rows(db, "SELECT user_id, restaurant_id, tier, environment FROM device_tokens")[0]
    assert row == {"user_id": emp, "restaurant_id": rid, "tier": "staff", "environment": "sandbox"}
    assert c.post("/staff/api/device-tokens", json={"environment": "sandbox"}).status_code == 400
    # Sign-out removes only this employee's own staff row.
    push.register_device_token(other, rid, "b" * 64, db_path=db, tier=push.TIER_STAFF)
    assert c.delete("/staff/api/device-tokens", json={"apns_token": "b" * 64}).get_json()["removed"] == 0
    assert c.delete(f"/staff/api/device-tokens/{'a' * 64}").get_json()["removed"] == 1
    assert [r["apns_token"] for r in _rows(db, "SELECT apns_token FROM device_tokens")] == ["b" * 64]
    # The helper B1's sign-out / PIN change / deactivation call.
    assert push.unregister_staff_devices(other, rid, db_path=db) == 1


def test_a_pin_session_still_cannot_use_the_console_registration(db):
    import mobile_api
    rid = _restaurant(db)
    emp, _m = _staff(db, rid, "Ana")
    app = Flask(__name__)
    app.register_blueprint(mobile_api.mobile_bp)
    tok = auth.create_staff_session(emp, rid, db_path=db)
    r = app.test_client().post("/mobile/api/device-tokens", json={"apns_token": "a" * 64},
                               headers={"Authorization": f"Bearer {tok}"})
    assert r.status_code in (401, 403)


# ── C4 (3): counted only when delivered ─────────────────────────────────────

def test_a_push_no_device_took_falls_back_to_text_then_email(db, apns, wire):
    rid = _restaurant(db)
    emp, _m = _staff(db, rid, "Ana", phone="+15555550101", texts=True)
    push.register_device_token(emp, rid, "s" * 64, db_path=db, tier=push.TIER_STAFF)
    models.set_staff_contact(rid, "Ana", email="ana@x.test", db_path=db)
    outcomes = []
    apns["ok"] = False
    got = people.tell_staff(rid, "Ana", "schedule", "Your shift moved", "Fri is now 4–10pm.",
                            on_outcome=outcomes.append, db_path=db)
    assert got == "push", "queued: the chain is armed"
    assert outcomes == ["sms"] and wire["sms"] and not wire["email"], "Apple refused, so it was texted"
    # Delivered: nothing else goes, and the outcome says push.
    apns["ok"], outcomes[:] = True, []
    wire["sms"].clear()
    people.tell_staff(rid, "Ana", "schedule", "Your shift moved", "Fri.", on_outcome=outcomes.append, db_path=db)
    assert outcomes == ["push"] and not wire["sms"] and not wire["email"]


def test_publish_records_the_app_only_once_the_push_was_delivered(db, apns, wire):
    rid = _restaurant(db, ["Ana"])
    emp, _m = _staff(db, rid, "Ana")
    push.register_device_token(emp, rid, "s" * 64, db_path=db, tier=push.TIER_STAFF)
    models.set_staff_contact(rid, "Ana", email="ana@x.test", db_path=db)
    day = (date.today() + timedelta(days=3)).isoformat()
    sid = models.save_schedule_history(rid, day, (date.today() + timedelta(days=9)).isoformat(), 12, 20, 30,
                                       HEADER + f"\n{day},Mon,Ana,Server,4:00pm,10:00pm,6,", [], db_path=db)
    apns["ok"] = False
    out, status = client_api._publish_schedule(rid, sid, {"id": 1, "username": "o"})
    assert status == 200 and out["sent"][0]["channel"] == "push" and out["sent"][0]["delivery"] == "pending"
    shares = {r["sent_to"] for r in _rows(db, "SELECT sent_to FROM schedule_shares WHERE schedule_id=?", (sid,))}
    assert shares == {"ana@x.test"}, "no device took it: emailed, and never recorded as sent to the app"
    assert wire["email"] == [("ana@x.test", "week")]
    assert apns["sent"][0]["data"]["tab"] == "today" and apns["sent"][0]["data"]["module"] == "staff"


# ── C4 (4): staff types ─────────────────────────────────────────────────────

def test_staff_types_are_uncollapsible_prioritised_and_open_the_staff_app():
    for t in push.STAFF_ALERT_TYPES:
        assert t in push._UNCOLLAPSIBLE_TYPES and push.module_of(t) == "staff"
        assert push.priority_of(t) == (push.P1_ACT_NOW if t == "staff_urgent" else push.P2_OPPORTUNITY)
    a = push._collapse_id(1, "staff_request", {"request_id": 4})
    b = push._collapse_id(1, "staff_request", {"request_id": 5})
    assert a != b
    assert push.nav_for("staff_request", {"request_id": 12}) == "staff/requests/12"
    assert push.nav_for("staff_announcement", {"announcement_id": 3}) == "staff/inbox/3"
    assert push.nav_for("staff_reminder", {"tab": "tasks", "assignment_id": 9}) == "staff/tasks/9"
    assert push.nav_for("staff_schedule", {}) == "staff/today"
    # The manager-side notice of an employee's message (staff_comms).
    assert push.priority_of("employee_message") == push.P2_OPPORTUNITY
    assert push.nav_for("employee_message", {"thread_id": 7}) == "labor/inbox/7"
    assert "employee_message" in notify.BRIEFING_ALWAYS and "employee_message" in push._UNCOLLAPSIBLE_TYPES


def test_a_staff_notice_payload_carries_kind_tab_and_its_id(db, apns, wire, monkeypatch):
    rid = _restaurant(db)
    emp, _m = _staff(db, rid, "Ana")
    push.register_device_token(emp, rid, "s" * 64, db_path=db, tier=push.TIER_STAFF)
    people.tell(rid, "Ana", "Ben asked to swap", ["Answer in Requests."], email_type="shift_request",
                data={"request_id": 41, "event": "swap_asked"}, db_path=db)
    d = apns["sent"][-1]["data"]
    assert apns["sent"][-1]["type"] == "staff_request"
    assert (d["kind"], d["tab"], d["request_id"], d["nav"], d["module"]) == \
        ("request", "requests", 41, "staff/requests/41", "staff")
    # An announcement a caller marks urgent is P1 and never quiet.
    monkeypatch.setattr(people, "staff_local_now", lambda rid_: LATE)
    people.tell(rid, "Ana", "Closed tonight", ["Storm."], email_type="staff_announcement",
                data={"announcement_id": 5, "nav": "inbox"}, priority="urgent", db_path=db)
    last = apns["sent"][-1]
    assert last["type"] == "staff_urgent" and "quiet" not in last["data"]
    assert last["data"]["nav"] == "staff/inbox/5" and last["data"]["kind"] == "announcement"


# ── M8: quiet hours ─────────────────────────────────────────────────────────

def test_at_night_the_push_is_silent_and_the_text_waits_for_8am(db, apns, wire, monkeypatch):
    rid = _restaurant(db)
    emp, _m = _staff(db, rid, "Ana", phone="+15555550101", texts=True)
    _staff(db, rid, "Ben", phone="+15555550102", texts=True)
    push.register_device_token(emp, rid, "s" * 64, db_path=db, tier=push.TIER_STAFF)
    monkeypatch.setattr(people, "staff_local_now", lambda rid_: LATE)
    people.tell_staff(rid, "Ana", "schedule", "Your week is posted", "3 shifts.", db_path=db)
    assert apns["sent"][-1]["data"]["quiet"] is True
    assert people.tell_staff(rid, "Ben", "schedule", "Your week is posted", "3 shifts.",
                             shift_date="2026-10-05", db_path=db) == "sms_held"
    assert not wire["sms"]
    held = _rows(db, "SELECT * FROM staff_notices WHERE kind='held_text'")
    assert len(held) == 1 and held[0]["state"] == "held"
    assert held[0]["release_at"] == "2026-10-02 13:00:00", "8am Chicago (CDT) the next morning, in UTC"
    # A change to a shift before then goes now.
    assert people.tell_staff(rid, "Ben", "schedule", "Your shift moved", "Tomorrow 7am.",
                             shift_date="2026-10-02", db_path=db) == "sms"
    # 8am: the held one goes, once.
    monkeypatch.setattr(staff_reminders, "_allowed", lambda: True)
    wire["sms"].clear()
    assert staff_reminders.release_held(now_utc=datetime(2026, 10, 2, 12, 59), db_path=db)["ok"] == 0
    assert staff_reminders.release_held(now_utc=datetime(2026, 10, 2, 13, 0), db_path=db)["ok"] == 1
    assert staff_reminders.release_held(now_utc=datetime(2026, 10, 2, 13, 10), db_path=db)["attempted"] == 0
    assert [to for to, _m in wire["sms"]] == ["+15555550102"]


def test_a_held_text_whose_consent_went_falls_back_to_email(db, wire, monkeypatch):
    rid = _restaurant(db)
    _uid, mid = _staff(db, rid, "Ben", phone="+15555550102", texts=True)
    models.set_staff_contact(rid, "Ben", email="ben@x.test", db_path=db)
    monkeypatch.setattr(people, "staff_local_now", lambda rid_: LATE)
    assert people.tell_staff(rid, "Ben", "request", "Approved", "Your time off is approved.",
                             channel={"sms": "+15555550102", "sms_scope": ["schedule", "request"],
                                      "email": "ben@x.test"}, db_path=db) == "sms_held"
    c = models.get_conn(db)
    c.execute("UPDATE memberships SET schedule_texts_at=NULL WHERE id=?", (mid,))
    c.commit()
    c.close()
    out = staff_reminders.release_held(now_utc=datetime(2026, 10, 3), db_path=db)
    assert out["ok"] == 1 and not wire["sms"] and wire["email"] == [("ben@x.test", "Approved — Staff Co")]


# ── H3: reminders ───────────────────────────────────────────────────────────

def _task(db_path, rid, name, due_iso, label="Temp the walk-in", critical=1):
    c = models.get_conn(db_path)
    sheet = c.execute("INSERT INTO task_sheets (restaurant_id, job_code, shift_kind, title) VALUES (?,?,?,?)",
                      (rid, "Server", "opening", "Opening")).lastrowid
    a = c.execute("INSERT INTO task_assignments (restaurant_id, sheet_id, task_date, job_code, shift_kind, title, "
                  "assignees_json, sheet_version, lines_json, line_count) VALUES (?,?,?,?,?,?,?,1,?,1)",
                  (rid, sheet, due_iso[:10], "Server", "opening", "Opening", json.dumps([name]),
                   json.dumps([{"line_id": 7, "label": label, "critical": critical, "due_at": due_iso}]))).lastrowid
    c.commit()
    c.close()
    return a


def test_reminders_go_once_before_the_shift_and_before_a_critical_line(db, apns, monkeypatch):
    import task_sheets
    rid = _restaurant(db)
    emp, _m = _staff(db, rid, "Ana")
    push.register_device_token(emp, rid, "s" * 64, db_path=db, tier=push.TIER_STAFF)
    now = datetime(2026, 10, 1, 15, 0)
    monkeypatch.setattr(task_sheets, "published_day_shifts", lambda r, day: (
        [{"employee": "Ana", "role": "Server", "start": "2026-10-01T16:00:00", "end": "2026-10-01T22:00:00"},
         {"employee": "Ana", "role": "Server", "start": "2026-10-01T19:00:00", "end": "2026-10-01T22:00:00"}]
        if day == now.date() else [], True))
    _task(db, rid, "Ana", "2026-10-01T15:15:00")
    _task(db, rid, "Ana", "2026-10-01T15:15:00", label="Not critical", critical=0)
    got = staff_reminders.remind_restaurant(rid, now, db_path=db)
    assert (got["attempted"], got["ok"]) == (2, 2)
    titles = sorted(s["title"] for s in apns["sent"])
    assert titles == ["Due in 15 min: Temp the walk-in — Staff Co", "Your shift starts at 4pm — Staff Co"]
    assert {s["type"] for s in apns["sent"]} == {"staff_reminder"}
    task = next(s for s in apns["sent"] if s["title"].startswith("Due"))["data"]
    assert (task["tab"], task["nav"].split("/")[:2]) == ("tasks", ["staff", "tasks"])
    # A second pass (or a second scheduler) sends nothing again.
    assert staff_reminders.remind_restaurant(rid, now + timedelta(minutes=10), db_path=db)["attempted"] == 0
    assert len(apns["sent"]) == 2
    assert {r["state"] for r in _rows(db, "SELECT state FROM staff_notices")} == {"sent"}


def test_a_muted_or_appless_employee_gets_no_reminder_and_no_claim(db, apns, monkeypatch):
    import task_sheets
    rid = _restaurant(db)
    emp, _m = _staff(db, rid, "Ana")
    _staff(db, rid, "Ben")                                     # no app
    push.register_device_token(emp, rid, "s" * 64, db_path=db, tier=push.TIER_STAFF)
    preferences.set_staff_muted(emp, rid, "staff_reminder", True, db_path=db)
    monkeypatch.setattr(task_sheets, "published_day_shifts", lambda r, day: (
        [{"employee": n, "role": "Server", "start": "2026-10-01T16:00:00", "end": "2026-10-01T22:00:00"}
         for n in ("Ana", "Ben")], True))
    got = staff_reminders.remind_restaurant(rid, datetime(2026, 10, 1, 15, 0), db_path=db)
    assert got["skipped"] == 2 and got["attempted"] == 0 and not apns["sent"]
    assert not _rows(db, "SELECT 1 FROM staff_notices")


def test_the_reminder_job_is_registered_gated_and_reports_standard_counts(db, monkeypatch):
    import jobs_registry
    spec = jobs_registry.JOBS["staff_reminders"]
    assert spec["sends"] is True and spec["target"] == ("staff_reminders", "run_job")
    monkeypatch.setattr(staff_reminders, "_allowed", lambda: False)
    out = staff_reminders.run_job(db_path=db)
    assert out["attempted"] == 0 and "not the production scheduler" in out["reason"]
    monkeypatch.setattr(staff_reminders, "_allowed", lambda: True)
    out = staff_reminders.run_job(db_path=db, now_utc=datetime(2026, 10, 1, 20, 0))
    assert {"attempted", "ok", "failed", "skipped", "hit_bound"} <= set(out)


def test_the_staff_app_can_switch_reminders_off(db):
    rid = _restaurant(db)
    emp, _m = _staff(db, rid, "Ana")
    c = _staff_app(db, rid, emp)
    assert c.get("/staff/api/notifications").get_json()["reminders"] is True
    assert c.post("/staff/api/notifications", json={"reminders": False}).get_json()["reminders"] is False
    assert not preferences.push_allowed(emp, rid, "staff_reminder", now_local=NOON, db_path=db)
    assert preferences.push_allowed(emp, rid, "staff_request", now_local=NOON, db_path=db)


# ── H14: consent that matches ───────────────────────────────────────────────

def test_preferences_say_whether_texts_can_send_and_what_the_consent_covers(db, monkeypatch):
    rid = _restaurant(db)
    emp, _m = _staff(db, rid, "Ana", phone="+15555550101")
    c = _staff_app(db, rid, emp)
    monkeypatch.setattr(notify, "TWILIO_STAFF_MESSAGING_SERVICE_SID", "")
    body = c.get("/staff/api/preferences").get_json()
    assert body["sms_available"] is False and body["schedule_texts_scope"] == []
    assert "my requests" in body["schedule_texts_consent"] and body["schedule_texts_consent_version"] == 2
    monkeypatch.setattr(notify, "TWILIO_SID", "AC1")
    monkeypatch.setattr(notify, "TWILIO_TOKEN", "tok")
    monkeypatch.setattr(notify, "TWILIO_STAFF_MESSAGING_SERVICE_SID", "MG-staff")
    assert c.get("/staff/api/preferences").get_json()["sms_available"] is True
    # An older app's box (no version): the schedule only.
    on = c.post("/staff/api/preferences", json={"schedule_texts": True}).get_json()
    assert on["schedule_texts"] is True and on["schedule_texts_scope"] == ["schedule"]
    assert people.reach(rid, ["Ana"], purpose="request", db_path=db)["Ana"]["sms"] is None
    assert people.reach(rid, ["Ana"], purpose="schedule", db_path=db)["Ana"]["sms"] == "+15555550101"
    # The current wording covers request notices too.
    on = c.post("/staff/api/preferences", json={"schedule_texts": True, "consent_version": 2}).get_json()
    assert on["schedule_texts_scope"] == ["schedule", "request"]
    assert people.reach(rid, ["Ana"], purpose="request", db_path=db)["Ana"]["sms"] == "+15555550101"
    assert people.reach(rid, ["Ana"], purpose="announcement", db_path=db)["Ana"]["sms"] is None
    off = c.post("/staff/api/preferences", json={"schedule_texts": False}).get_json()
    assert off["schedule_texts"] is False and off["schedule_texts_scope"] == []


def test_a_consent_from_before_scopes_never_texts_a_request_notice(db, wire):
    rid = _restaurant(db)
    _staff(db, rid, "Ana", phone="+15555550101", texts=True)       # ticked on the old wording
    models.set_staff_contact(rid, "Ana", email="ana@x.test", db_path=db)
    assert people.tell(rid, "Ana", "Your swap went through", ["You now work Fri."],
                       email_type="shift_request", db_path=db) == "email"
    assert not wire["sms"]
    assert people.tell(rid, "Ana", "Your schedule", ["Posted."], email_type="staff_schedule", db_path=db) == "sms"


# ── the helpers other agents call ───────────────────────────────────────────

def test_tell_deciders_reaches_the_schedule_deciders_with_the_request(db, monkeypatch):
    import strategy_jobs
    got = []
    monkeypatch.setattr(strategy_jobs, "_reach", lambda rid, t, title, body, data, dbp, **k:
                        got.append((t, data, k)) or 2)
    assert people.tell_deciders(1, "Ana is running late", "ETA 20 min.", request_id=9, request_kind="shift") == 2
    t, data, kw = got[0]
    assert t == "shift_request" and data["request_id"] == 9 and kw["deciders"] is True
    from permissions import SCHEDULE_DRAFT
    assert kw["permissions"] == [SCHEDULE_DRAFT]
