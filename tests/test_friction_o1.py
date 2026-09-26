"""Friction audit 9/25/26, workstream O1 — scheduling and people.

#4  a saved draft can be reopened and sent (publish-check finds the open
    draft; the queue's "Send now" posts that week's id)
#17 staff get the week on the channel they signed up with (app, a text they
    agreed to, email as the fallback) — never a real send here
#18 requests answered where they appear (queue Approve/Deny, Ask tools)
#25 one person record (people.py + /api/people, web and mobile twins)
#26 owners set their own targets and rates
#40 close-out prefill from what is already known
"""
import json
import sys
from datetime import date, timedelta

import pytest

import activity, covers, decisions, delayed, demand_signals, goals, issues, metrics, outcomes  # noqa: E401,F401
import push, schedule_economics, schedule_intel, schedule_rules, schedule_versions  # noqa: E401,F401
import shift_requests, shift_quality, staff_schedule, staff_settings, strategy_jobs, time_off  # noqa: E401,F401
import action_queue, closeout, people, rec_ledger, demand  # noqa: E401,F401
from flask import Flask

import auth
import client_api
import models
import notify
from models import create_restaurant, Restaurant

HEADER = "date,day,employee,role,shift_start,shift_end,scheduled_hours,notes"


@pytest.fixture
def db(db_path, monkeypatch):
    real = models.get_conn

    def conn(*a, **k):
        return real(db_path)
    for mod in list(sys.modules.values()):
        bound = getattr(mod, "get_conn", None) if mod is not None else None
        if bound is real or str(getattr(bound, "__module__", "")).startswith(("test_", "tests.", "conftest")):
            monkeypatch.setattr(mod, "get_conn", conn)
    monkeypatch.setattr(models, "get_conn", conn)
    monkeypatch.setattr(models, "DB_PATH", db_path)
    monkeypatch.setattr(models, "_cached_shifts", lambda r: [])
    monkeypatch.setattr(client_api, "log_account_event", lambda *a, **k: None)
    auth.init_auth(db_path=db_path)
    push.init_push(db_path=db_path)
    return db_path


def _restaurant(db_path, roster=(), **cols):
    rid = create_restaurant(Restaurant(name="People Co", owner_email="p@x.com"), db_path=db_path)
    cols.setdefault("module_labor", 1)
    c = models.get_conn(db_path)
    c.execute("UPDATE restaurants SET " + ", ".join(f"{k}=?" for k in cols) + " WHERE id=?", (*cols.values(), rid))
    c.commit()
    c.close()
    for name, role in roster:
        models.add_manual_team_member(rid, name, role=role, db_path=db_path)
    return rid


def _staff_login(db_path, rid, name, phone=None, texts=False, job_role=None):
    uid = auth.create_user(rid, name.lower().replace(" ", "").replace(".", "") + f".{rid}",
                           f"{name[:3].lower()}{rid}@staff.invalid", "x" * 32, db_path=db_path)
    m = auth.upsert_membership(uid, rid, "employee", employee_name=name, job_role=job_role, db_path=db_path)
    auth.set_membership_pin(m["id"], rid, "4827", db_path=db_path)
    c = models.get_conn(db_path)
    if phone:
        c.execute("UPDATE users SET phone=? WHERE id=?", (phone, uid))
    if texts:
        c.execute("UPDATE memberships SET schedule_texts_at=datetime('now') WHERE id=?", (m["id"],))
    c.commit()
    c.close()
    return uid, m["id"]


def _as(monkeypatch, rid, role, uid=5):
    monkeypatch.setattr(auth, "get_current_user",
                        lambda: {"id": uid, "restaurant_id": rid, "is_admin": 0, "role": role,
                                 "username": "u", "email": "u@x.com"})


@pytest.fixture
def web(db):
    from strategy_routes import strategy_bp, strategy_mobile_bp
    app = Flask(__name__, template_folder="../templates")
    app.register_blueprint(strategy_bp)
    app.register_blueprint(strategy_mobile_bp)
    return app


# ── #25 one person record ────────────────────────────────────────────────────

def test_one_person_is_composed_from_every_store_that_owns_a_fact(db):
    rid = _restaurant(db, [("Dana K.", "Server"), ("Eli", "Cook")])
    models.set_staff_contact(rid, "dana k.", email="dana@x.test", phone="555-201-3344", pos_id="77", db_path=db)
    staff_settings.upsert(rid, "Dana K.", max_hours=32, certifications=["alcohol"], db_path=db)
    models.set_capability(rid, "Dana K.", "overall", score=4, updated_by="o", db_path=db)
    models.update_restaurant(rid, {"role_rates_json": json.dumps({"server": 14.5})}, db_path=db)
    _staff_login(db, rid, "Dana K.")

    listed = people.list_people(rid, db_path=db)
    dana = [p for p in listed if p["name"] == "Dana K."]
    assert len(dana) == 1, "one person however many stores know them"
    assert dana[0]["key"] == "dana-k" and dana[0]["has_login"] is True and dana[0]["email"] == "dana@x.test"
    assert next(p for p in listed if p["name"] == "Eli")["has_login"] is False

    p = people.get_person(rid, "dana-k", db_path=db)
    assert p["pin_set"] is True and p["pos_id"] == "77" and p["phone"] == "555-201-3344"
    assert p["hours"]["max_hours"] == 32 and p["certifications"] == ["alcohol"]
    assert p["rating"]["score"] == 4
    assert p["pay_rate"] == {"role": "server", "rate": 14.5, "source": "role"}
    assert people.get_person(rid, "nobody-here", db_path=db) is None


def test_a_person_sheet_writes_each_field_back_to_the_store_that_owns_it(db, web, monkeypatch):
    rid = _restaurant(db, [("Dana K.", "Server")])
    _staff_login(db, rid, "Dana K.")
    _as(monkeypatch, rid, "client")
    c = web.test_client()
    r = c.post("/api/people/dana-k", json={"email": "d@x.test", "pos_id": "12", "max_hours": 30,
                                            "rating": 5, "pin": "9031"})
    assert r.status_code == 200, r.get_json()
    assert sorted(r.get_json()["changed"]) == ["email", "max_hours", "pin", "pos_id", "rating"]
    assert models.get_staff_contacts(rid, db_path=db)[0]["pos_id"] == "12"
    assert staff_settings.for_name(rid, "Dana K.", db_path=db)["max_hours"] == 30
    assert models.get_operational_scores(rid, db_path=db) == {"Dana K.": 5}
    # The mobile twin is the same body, behind the bearer token.
    uid = auth.create_user(rid, "owner", "owner@x.com", "pw", db_path=db)
    bearer = {"Authorization": f"Bearer {auth.create_session(uid, db_path=db)}"}
    got = c.get("/mobile/api/people/dana-k", headers=bearer).get_json()
    assert got["ok"] and got["person"]["email"] == "d@x.test"
    assert c.get("/mobile/api/people", headers=bearer).get_json()["people"][0]["key"] == "dana-k"


def test_a_manager_edits_the_person_but_not_their_login(db, web, monkeypatch):
    rid = _restaurant(db, [("Dana K.", "Server")])
    _staff_login(db, rid, "Dana K.")
    _as(monkeypatch, rid, "manager")
    c = web.test_client()
    assert c.post("/api/people/dana-k", json={"max_hours": 20}).status_code == 200
    r = c.post("/api/people/dana-k", json={"pin": "1234"})
    assert r.status_code == 400 and "owner" in r.get_json()["error"]
    assert c.get("/api/people/dana-k").get_json()["person"]["can_manage_login"] is False


def test_an_employee_session_cannot_read_the_team(db, web, monkeypatch):
    rid = _restaurant(db, [("Dana K.", "Server")])
    _as(monkeypatch, rid, "employee")
    assert web.test_client().get("/api/people").status_code in (302, 401, 403)


# ── #17 the channel a published week goes out on ────────────────────────────

def _week(db, rid, names):
    today = date.today()
    rows = [f"{(today + timedelta(days=2)).isoformat()},Mon,{n},Server,4:00pm,10:00pm,6," for n in names]
    return models.save_schedule_history(rid, (today + timedelta(days=2)).isoformat(),
                                        (today + timedelta(days=8)).isoformat(), 12, 20, 30,
                                        HEADER + "\n" + "\n".join(rows), [], db_path=db)


@pytest.fixture
def sends(monkeypatch):
    out = {"sms": [], "email": [], "push": []}
    import emails
    monkeypatch.setattr(emails, "send_staff_schedule_email", lambda **kw: out["email"].append(kw["employee_name"]) or emails.SendResult(True))
    monkeypatch.setattr(notify, "send_sms", lambda to, msg, use_case="alert": out["sms"].append((to, use_case, msg)) or True)
    monkeypatch.setattr(push, "fire_push", lambda rid, t, title, body, data=None, user_ids=None, **k:
                        out["push"].append((t, list(user_ids or []), data)) or 1)
    monkeypatch.setattr(notify, "TWILIO_SID", "AC1")
    monkeypatch.setattr(notify, "TWILIO_TOKEN", "tok")
    return out


def test_a_consented_employee_is_texted_and_the_rest_fall_back_to_email(db, sends, monkeypatch):
    monkeypatch.setattr(notify, "TWILIO_STAFF_MESSAGING_SERVICE_SID", "MG-staff")
    rid = _restaurant(db, [("Ana", "Server"), ("Ben", "Server"), ("Cy", "Server")])
    _staff_login(db, rid, "Ana", phone="+15555550101", texts=True)
    _staff_login(db, rid, "Ben", phone="+15555550102", texts=False)   # signed up, never ticked texts
    models.set_staff_contact(rid, "Ben", email="ben@x.test", db_path=db)
    models.set_staff_contact(rid, "Ana", email="ana@x.test", db_path=db)
    sid = _week(db, rid, ["Ana", "Ben", "Cy"])
    out, status = client_api._publish_schedule(rid, sid, {"id": 1, "username": "o"})
    assert status == 200 and out["ok"], out
    assert [(t, u) for t, u, _m in sends["sms"]] == [("+15555550101", "staff")], "only the consented phone"
    assert "Reply STOP" in sends["sms"][0][2] and "/s/" in sends["sms"][0][2]
    assert sends["email"] == ["Ben"], "Ana was reached by text, so no second copy by email"
    by = {s["employee_name"]: s["channel"] for s in out["sent"]}
    assert by == {"Ana": "sms", "Ben": "email"}
    assert [u["employee_name"] for u in out["unreachable"]] == ["Cy"]


def test_no_staff_messaging_service_means_no_texts_only_email(db, sends, monkeypatch):
    monkeypatch.setattr(notify, "TWILIO_STAFF_MESSAGING_SERVICE_SID", "")
    rid = _restaurant(db, [("Ana", "Server")])
    _staff_login(db, rid, "Ana", phone="+15555550101", texts=True)
    models.set_staff_contact(rid, "Ana", email="ana@x.test", db_path=db)
    out, _ = client_api._publish_schedule(rid, _week(db, rid, ["Ana"]), {"id": 1, "username": "o"})
    assert sends["sms"] == [] and sends["email"] == ["Ana"]
    assert out["sent"][0]["channel"] == "email"


def test_an_employee_with_the_app_gets_a_push_to_their_own_login_only(db, sends, monkeypatch):
    rid = _restaurant(db, [("Ana", "Server")])
    uid, _m = _staff_login(db, rid, "Ana")
    push.register_device_token(uid, rid, "a" * 64, db_path=db)
    models.set_staff_contact(rid, "Ana", email="ana@x.test", db_path=db)
    out, _ = client_api._publish_schedule(rid, _week(db, rid, ["Ana"]), {"id": 1, "username": "o"})
    assert sends["push"] and sends["push"][0][1] == [uid], "narrowed to her login, never the restaurant"
    assert sends["email"] == [] and out["sent"][0]["channel"] == "push"


def test_the_staff_portal_texts_switch_is_the_employees_own(db, monkeypatch):
    from staff_routes import staff_bp
    rid = _restaurant(db, [("Ana", "Server")])
    uid, mid = _staff_login(db, rid, "Ana", phone="+15555550101")
    app = Flask(__name__, template_folder="../templates")
    app.register_blueprint(staff_bp)
    c = app.test_client()
    c.set_cookie("staff_session", auth.create_staff_session(uid, rid, db_path=db))
    assert c.get("/staff/api/preferences").get_json()["schedule_texts"] is False, "off until they tick it"
    assert c.post("/staff/api/preferences", json={"schedule_texts": True}).get_json()["schedule_texts"] is True
    assert people.get_person(rid, "ana", db_path=db)["schedule_texts"] is True
    assert c.post("/staff/api/preferences", json={"schedule_texts": False}).get_json()["schedule_texts"] is False


# ── #4 the open draft, and publish-check ─────────────────────────────────────

def test_publish_check_finds_the_open_draft_and_what_send_would_do(db, web, sends, monkeypatch):
    monkeypatch.setattr(notify, "TWILIO_STAFF_MESSAGING_SERVICE_SID", "")
    rid = _restaurant(db, [("Ana", "Server"), ("Ben", "Server")])
    models.set_staff_contact(rid, "Ana", email="ana@x.test", db_path=db)
    sid = _week(db, rid, ["Ana", "Ben"])
    _as(monkeypatch, rid, "client")
    d = web.test_client().get("/api/labor/publish-check").get_json()
    assert d["schedule_id"] == sid and d["can_publish"] is True
    assert d["reach"]["total"] == 2 and d["reach"]["reachable"] == 1 and d["reach"]["unreachable"] == ["Ben"]
    client_api._publish_schedule(rid, sid, {"id": 1, "username": "o"})
    assert web.test_client().get("/api/labor/publish-check").get_json()["schedule_id"] is None, \
        "a published week is not an open draft"


def test_the_queue_send_now_posts_that_weeks_id_and_opens_it(db):
    rid = _restaurant(db)
    sid = _week(db, rid, ["Ana"])
    items = {i["key"]: i for i in action_queue.items(rid, db_path=db)["items"]}
    act = items[f"schedule_unsent:{sid}"]["action"]
    assert act["route"]["web"] == "/api/labor/publish-schedule" and act["body"] == {"schedule_id": sid}
    assert act["nav"] == f"schedule/{sid}" and act["alt"]["nav"] == f"schedule/{sid}"


# ── #18 requests answered where they appear ──────────────────────────────────

def test_a_request_on_the_queue_carries_approve_and_deny(db, web, monkeypatch):
    rid = _restaurant(db, [("Bo", "Server")])
    today = date.today()
    c = models.get_conn(db)
    to = c.execute("INSERT INTO staff_time_off (restaurant_id, employee_name, start_date, end_date, status) "
                   "VALUES (?, 'Bo', ?, ?, 'pending')",
                   (rid, (today + timedelta(days=5)).isoformat(), (today + timedelta(days=6)).isoformat())).lastrowid
    c.commit()
    c.close()
    act = {i["key"]: i for i in action_queue.items(rid, db_path=db)["items"]}[f"time_off:{to}"]["action"]
    assert act["label"] == "Approve" and act["body"] == {"decision": "approve"}
    assert act["alt"]["label"] == "Deny" and act["alt"]["body"] == {"decision": "deny"}
    assert act["nav"] == f"request/time_off-{to}"
    hid = _week(db, rid, ["Bo"])
    c = models.get_conn(db)
    sr = c.execute("INSERT INTO shift_change_requests (restaurant_id, history_id, employee_name, date, shift_start, "
                   "kind, status) VALUES (?, ?, 'Bo', ?, '4:00pm', 'drop', 'pending')",
                   (rid, hid, (today + timedelta(days=2)).isoformat())).lastrowid
    c.commit()
    c.close()
    sact = {i["key"]: i for i in action_queue.items(rid, db_path=db)["items"]}[f"shift_request:{sr}"]["action"]
    assert sact["nav"] == f"request/shift_request-{sr}", "the nav form workstream N's router names"
    assert sact["route"]["web"] == f"/api/labor/shift-requests/{sr}/decide"
    _as(monkeypatch, rid, "client")
    r = web.test_client().post(act["alt"]["route"]["web"], json=act["alt"]["body"])
    assert r.status_code == 200 and r.get_json()["request"]["status"] == "denied"
    assert web.test_client().post(act["route"]["mobile"], json=act["body"]).status_code in (401, 404), \
        "the mobile twin exists (bearer-gated), and an answered request is not answered twice"


def test_ask_proposes_a_time_off_answer_naming_the_stored_person(db, web):
    import ask_cavnar_tools as t
    rid = _restaurant(db, [("Bo", "Server")])
    today = date.today()
    c = models.get_conn(db)
    to = c.execute("INSERT INTO staff_time_off (restaurant_id, employee_name, start_date, end_date, status) "
                   "VALUES (?, 'Bo', ?, ?, 'pending')", (rid, today.isoformat(), today.isoformat())).lastrowid
    c.commit()
    c.close()
    p = t.build_proposal("decide_time_off", {"request_id": to, "decision": "approve", "acknowledge": True}, rid)
    assert p["route"]["web"] == f"/api/labor/time-off/{to}/decide" and p["body"] == {"decision": "approve"}
    assert p["summary"] == "Approve Bo's time off"
    assert {"label": "Who", "value": "Bo"} in p["details"]
    rules = {r.rule for r in web.url_map.iter_rules()}
    assert f"/api/labor/time-off/<int:request_id>/decide" in rules
    assert t.build_proposal("decide_time_off", {"request_id": to + 99, "decision": "approve"}, rid) is None
    assert t.build_proposal("decide_time_off", {"request_id": to, "decision": "maybe"}, rid) is None
    reads = json.loads(t.run_read_tool("read_time_off", rid, {}))
    assert reads["time_off"][0]["request_id"] == to and "reason" not in reads["time_off"][0]


# ── #26 the owner's targets and rates ────────────────────────────────────────

def test_the_owner_sets_targets_and_per_role_rates(db, web, monkeypatch):
    rid = _restaurant(db, [("Ana", "Server"), ("Eli", "Cook")])
    _as(monkeypatch, rid, "client")
    c = web.test_client()
    r = c.post("/api/account/targets", json={"labor_target_pct": 28, "week_start_day": 2,
                                              "role_rates": {"Server": 14.5, "Cook": "19", "Host": ""}})
    assert r.status_code == 200, r.get_json()
    got = models.get_restaurant(rid, db_path=db)
    assert got.labor_target_pct == 28 and got.week_start_day == 2
    assert json.loads(got.role_rates_json) == {"Server": 14.5, "Cook": 19.0}
    assert got.labor_target_source == "set", "the owner's own number"
    assert r.get_json()["targets"]["roles"] == ["Cook", "Server"]
    assert c.post("/api/account/targets", json={"labor_target_pct": 300}).status_code == 400
    assert c.post("/mobile/api/account/targets", json={}).status_code in (400, 401)


def test_the_owner_sets_the_notes_every_schedule_draft_reads(db, web, monkeypatch):
    # The schedule workspace's Advanced AI panel writes restaurants.sched_notes
    # (labor.py's ADDITIONAL SCHEDULING NOTES) through the targets endpoint.
    rid = _restaurant(db)
    _as(monkeypatch, rid, "client")
    c = web.test_client()
    r = c.post("/api/account/targets", json={"sched_notes": "  Two cooks on Friday lunch.\x07\nMarcus closes weekends.  "})
    assert r.status_code == 200, r.get_json()
    assert models.get_restaurant(rid, db_path=db).sched_notes == "Two cooks on Friday lunch.\nMarcus closes weekends."
    assert c.get("/api/account/targets").get_json()["targets"]["sched_notes"].startswith("Two cooks")
    c.post("/api/account/targets", json={"sched_notes": "x" * 5000})
    assert len(models.get_restaurant(rid, db_path=db).sched_notes) == 2000
    c.post("/api/account/targets", json={"sched_notes": ""})
    assert not models.get_restaurant(rid, db_path=db).sched_notes


def test_a_manager_reads_targets_but_cannot_change_them(db, web, monkeypatch):
    rid = _restaurant(db)
    _as(monkeypatch, rid, "manager")
    c = web.test_client()
    assert c.get("/api/account/targets").get_json()["can_edit"] is False
    assert c.post("/api/account/targets", json={"labor_target_pct": 20}).status_code == 403


# ── #40 close-out prefill ────────────────────────────────────────────────────

def test_closeout_suggests_tonights_no_shows_and_whats_on(db):
    rid = _restaurant(db, [("Dana K.", "Server")])
    day = date.today().isoformat()
    issues.create_issue(rid, "coverage", "Dana K. hasn't clocked in", source_key=f"coverage:{day}:dana k.",
                        db_path=db)
    issues.create_issue(rid, "coverage", "Old", source_key=f"coverage:2020-01-01:eli", db_path=db)
    demand_signals.save(rid, [{"date": day, "kind": "event", "label": "Cubs home game"}], db_path=db)
    s = closeout.suggestions(rid, day, db_path=db)
    assert s["callouts"] == "Dana K." and "Cubs home game" in s["influence"]
    assert set(s["sources"]) == {"callouts", "influence"}
    assert closeout.suggestions(rid, "2031-01-02", db_path=db)["callouts"] is None


# ── the web surfaces (source-level: these read dashboard.html itself) ─────────

import os
import re

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _src(name="dashboard.html"):
    return open(os.path.join(_ROOT, "templates", name), encoding="utf-8").read()


def _fn(name):
    s = _src()
    m = re.search(r"function %s\(" % re.escape(name), s) or \
        re.search(r"window\.%s\s*=\s*function\s*\(" % re.escape(name), s)
    assert m, "function not found: " + name
    i, depth = s.index("{", m.start()), 0
    for j in range(i, len(s)):
        depth += {"{": 1, "}": -1}.get(s[j], 0)
        if depth == 0:
            return s[m.start():j + 1]
    raise AssertionError(name)


def test_the_preview_has_one_send_button_that_saves_first_and_no_pos_id_in_the_drawer():
    s = _src()
    # The one send button is the Schedule Studio's Publish step (9/26/26).
    head = s[s.index('data-stage="publish"'):s.index('id="ps-result"')]
    assert 'onclick="saveAndSendSchedule(this)" id="ps-send-btn" class="cbtn cbtn-primary"' in head
    assert s.count('id="ps-send-btn"') == 1
    assert "Download CSV" not in head, "CSV is demoted out of the send step (#19)"
    assert "pos_id" not in _fn("openPublishSchedule"), "POS id moved to the person sheet (#16)"
    send = _fn("saveAndSendSchedule")
    assert "rescoreSchedule(btn, function(){ publishScheduleNow(false); })" in send
    assert "Save your edits first" not in _fn("publishScheduleNow"), "a dirty week is saved, not refused"
    assert "after()" in _fn("rescoreSchedule")


def test_the_week_opens_with_its_grid_and_the_grades_sit_in_the_right_panel():
    # The schedule workspace (9/26/26): what grades the week moved from a
    # Details fold under the table into the right-hand panel beside it.
    body = _fn("_schedHandleResult")
    assert "_schedRenderTable(true)" not in body and "toggleSchedTable()" in body
    s = _src()
    assert 'id="sched-details"' not in s
    right = s[s.index('id="sw-right"'):s.index("</aside>", s.index('id="sw-right"'))]
    for part in ('id="sched-summary"', 'id="sched-par-banner"', 'id="sched-econ"', 'id="sched-quality"', 'id="sched-review"'):
        assert part in right
    assert s.index('id="sched-table-wrap"') < s.index('id="sw-right"')


def test_a_draft_reopens_from_history_home_and_the_labor_tab():
    s = _src()
    assert "schedOpenInEditor('+r.id+')" in _fn("_schedHistRowHtml")
    assert "openScheduleDraft('+id+')" in s
    for head in ("schedule", "request", "person"):
        assert "cavNavRegister('%s'" % head in s
    assert "/api/labor/publish-check" in _fn("autoOpenDraft")
    assert "lb2OnOpen" in _fn("loadLaborInsight")
    for sec in ("labor/schedule", "labor/requests", "labor/team"):
        assert 'data-nav="%s"' % sec in s


def test_home_queue_items_render_their_alt_and_answer_a_needs_ack_in_place():
    q = _fn("hbQueueActs")
    assert "alt.route" in q and "alt.nav" in q and "cavNav(" in q
    assert "hbAckInPlace" in _fn("hbActionDo")


def test_account_carries_targets_and_the_automation_switches_moved():
    s = _src()
    auto = s[s.index('id="acct-automation"'):s.index('id="account-settings-card"') + 4000]
    for rid in ("as-autodraft-row", "as-autoorder-row", "as-autopublish-row"):
        assert rid in auto
    assert 'id="as-tg-card"' in s and 'data-tg="labor_target_pct"' in s
    assert "'as-auto-ops-status'" in _fn("saveAutoDraft")
    assert 'id="lb2-autodraft"' in s and 'id="fc2-autoorder"' in s


def test_closeout_prefills_only_an_unfiled_night_and_says_so():
    body = _fn("renderCloseout")
    assert "(!e.created_at&&c.suggested)" in body and "Suggested" in body


def test_the_staff_portal_texts_box_starts_unchecked():
    s = _src("staff_portal.html")
    assert "id=\"pref-texts\"' + (d.schedule_texts ? ' checked' : '')" in s
    assert "reply STOP" in s
