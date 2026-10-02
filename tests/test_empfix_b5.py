"""Employee audit fixes B5 (10/1/26): running late (COM-05), announcements
with acknowledgement (INV-03, COM-04a, COM-07, MISS-5), and one thread per
employee with the managers on duty (INV-02, COM-04b, MISS-6) — staff_comms.

Every clock is pinned: the staff_comms seam (_local_now) and, for the
coverage check, time_utils.restaurant_now.
"""
from datetime import date, datetime

import pytest
from flask import Flask

import auth
import models
from auth import create_session, create_staff_session, create_user, upsert_membership
from models import Restaurant, create_restaurant, get_conn

DAY = date(2026, 9, 21)                      # a Monday
NOON_ISH = datetime(2026, 9, 21, 10, 50)      # before an 11:00am start


@pytest.fixture(autouse=True)
def _redirect(db_path, monkeypatch):
    import intraday, issues, notify, push, strategy_jobs, staff_comms
    real = models.get_conn
    redirect = lambda *a, **k: real(db_path)
    for mod in (models, auth, intraday, issues, notify, push, strategy_jobs):
        monkeypatch.setattr(mod, "get_conn", redirect, raising=False)
    monkeypatch.setattr(models, "DB_PATH", db_path)
    monkeypatch.setattr(auth, "DB_PATH", db_path)
    monkeypatch.setattr("notify.send_sms", lambda *a, **k: True)
    monkeypatch.setattr(staff_comms, "_local_now", lambda rid: NOON_ISH)
    monkeypatch.setattr(staff_comms, "_limited", lambda *a, **k: False)
    auth.init_auth(db_path=db_path)


@pytest.fixture
def told(monkeypatch):
    """What the managers (strategy_jobs._reach) and the staff (people.tell)
    were told. people.tell here takes `data` and `priority`, as the rebuilt
    pipeline (fix B2) does."""
    import people, strategy_jobs
    out = {"managers": [], "staff": []}

    def _reach(rid, alert_type, title, body, data, db_path, **kw):
        out["managers"].append({"rid": rid, "type": alert_type, "title": title, "body": body, "data": data, **kw})
        return 1

    def _tell(rid, name, title, lines, *, email_type="staff_notice", channel=None, db_path=None, data=None,
              priority=None):
        out["staff"].append({"rid": rid, "name": name, "title": title, "lines": lines, "data": data,
                             "priority": priority, "email_type": email_type})
        return "push"
    monkeypatch.setattr(strategy_jobs, "_reach", _reach)
    monkeypatch.setattr(people, "tell", _tell)
    monkeypatch.setattr(people, "reach", lambda rid, names, db_path=None: {n: {} for n in names})
    return out


@pytest.fixture
def app():
    from staff_routes import staff_bp
    from staff_comms_routes import staff_comms_bp, staff_comms_mobile_bp
    flask_app = Flask(__name__, template_folder="../templates")
    flask_app.register_blueprint(staff_bp)
    flask_app.register_blueprint(staff_comms_bp)
    flask_app.register_blueprint(staff_comms_mobile_bp)
    return flask_app


def _rid(db_path, name="Comms Co"):
    rid = create_restaurant(Restaurant(name=name, owner_email=f"{name[:3].lower()}@x.test", module_labor=1),
                            db_path=db_path)
    return rid


def _staff(db_path, rid, name, username=None, job_role=None):
    uid = create_user(rid, username or name.split()[0].lower() + str(rid), f"{name.split()[0].lower()}{rid}@x.test",
                      "unused-pass-9", db_path=db_path)
    m = upsert_membership(uid, rid, "employee", employee_name=name, job_role=job_role, db_path=db_path)
    return uid, m["id"]


def _owner(db_path, rid, username="boss", role="client"):
    uid = create_user(rid, username + str(rid), f"{username}{rid}@x.test", "owner-pw-long-9", db_path=db_path)
    upsert_membership(uid, rid, role, db_path=db_path)
    return uid


def _staff_client(app, db_path, rid, uid):
    c = app.test_client()
    c.set_cookie("staff_session", create_staff_session(uid, rid, db_path=db_path))
    return c


def _owner_client(app, db_path, uid):
    c = app.test_client()
    c.set_cookie("session_token", create_session(uid, db_path=db_path))
    return c


def _schedule(db_path, rid, day, rows):
    csv = "date,day,employee,role,shift_start,shift_end,scheduled_hours,notes\n" + "\n".join(
        f"{day.isoformat()},{day.strftime('%A')},{e},{r},{start},{end},8," for e, r, start, end in rows)
    hid = models.save_schedule_history(rid, day.isoformat(), day.isoformat(), 40, 40, 30, csv, [], db_path=db_path)
    conn = get_conn(db_path)
    conn.execute("UPDATE schedule_history SET published_at=datetime('now') WHERE id=?", (hid,))
    conn.commit(); conn.close()
    return hid


def _attendance(db_path, rid):
    conn = get_conn(db_path)
    try:
        return [dict(r) for r in conn.execute("SELECT * FROM attendance_events WHERE restaurant_id=?", (rid,))]
    finally:
        conn.close()


# ═══ boot ═══════════════════════════════════════════════════════════════════

def test_the_tables_are_made_at_boot_by_init_db(db_path):
    conn = get_conn(db_path)
    have = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    conn.close()
    assert {"staff_running_late", "staff_announcements", "staff_announcement_recipients", "staff_threads",
            "staff_thread_messages"} <= have
    # Every one carries restaurant_id, so delete_restaurant takes it.
    conn = get_conn(db_path)
    for t in ("staff_running_late", "staff_announcements", "staff_announcement_recipients", "staff_threads",
              "staff_thread_messages"):
        assert "restaurant_id" in {r[1] for r in conn.execute(f"PRAGMA table_info({t})")}, t
    conn.close()


def test_no_ddl_on_the_request_path():
    import inspect, staff_comms, staff_comms_routes
    src = inspect.getsource(staff_comms).split("def init_staff_comms", 1)
    rest = src[1].split("\n# ── small helpers", 1)[1]
    assert "CREATE TABLE" not in src[0] + rest
    assert "CREATE TABLE" not in inspect.getsource(staff_comms_routes)


# ═══ H1 running late (COM-05) ═══════════════════════════════════════════════

def test_running_late_records_a_self_report_and_tells_the_deciders_once(app, db_path, told):
    rid = _rid(db_path)
    uid, _mid = _staff(db_path, rid, "Dana K")
    _schedule(db_path, rid, DAY, [("Dana K", "Server", "11:00am", "7:00pm")])
    c = _staff_client(app, db_path, rid, uid)
    r = c.post("/staff/api/running-late", json={"date": DAY.isoformat(), "shift_start": "11:00am",
                                                "eta_minutes": 20, "note": "bus is late"})
    assert r.status_code == 200, r.get_json()
    body = r.get_json()
    assert body["created"] is True and body["managers_told"] is True
    assert body["report"]["eta_minutes"] == 20 and body["report"]["shift_start"] == "11:00am"
    # Attendance: late, from the person's own word, minutes = the ETA.
    (row,) = _attendance(db_path, rid)
    assert (row["outcome"], row["source"], row["minutes_late"]) == ("late", "self_report", 20)
    # The decider set, once: P2 shift_request type, deciders=True, SCHEDULE_DRAFT, the ETA in the text.
    (m,) = told["managers"]
    from permissions import SCHEDULE_DRAFT
    assert m["type"] == "employee_message" and m["deciders"] is True and m["permissions"] == [SCHEDULE_DRAFT]
    assert "20 minutes late" in m["body"] and "11:20am" in m["body"] and "bus is late" in m["body"]
    assert "request_id" not in m["data"]          # not an Approve/Deny request push
    assert m["data"]["nav"] == "labor/inbox"
    # An update replaces the ETA, tells nobody again, and moves the attendance minutes.
    r2 = c.post("/staff/api/running-late", json={"shift_start": "11:00am", "eta_minutes": 30})
    assert r2.status_code == 200 and r2.get_json()["created"] is False
    assert r2.get_json()["report"]["eta_minutes"] == 30
    assert len(told["managers"]) == 1
    (row,) = _attendance(db_path, rid)
    assert row["minutes_late"] == 30
    conn = get_conn(db_path)
    assert conn.execute("SELECT COUNT(*) FROM staff_running_late").fetchone()[0] == 1
    conn.close()
    got = c.get("/staff/api/running-late").get_json()
    assert [x["eta_minutes"] for x in got["reports"]] == [30] and got["eta_choices"] == [10, 20, 30, 45]


@pytest.mark.parametrize("payload,needle", [
    ({"shift_start": "4:00pm", "eta_minutes": 10}, "isn't on your published schedule"),
    ({"date": "2026-09-22", "shift_start": "11:00am", "eta_minutes": 10}, "today's shift only"),
    ({"shift_start": "11:00am", "eta_minutes": 0}, "between 1 and 120"),
    ({"shift_start": "11:00am", "eta_minutes": 121}, "between 1 and 120"),
    ({"shift_start": "11:00am", "eta_minutes": "soon"}, "Say how late"),
])
def test_running_late_refuses_what_is_not_their_shift_today(app, db_path, told, payload, needle):
    rid = _rid(db_path)
    uid, _ = _staff(db_path, rid, "Dana K")
    _schedule(db_path, rid, DAY, [("Dana K", "Server", "11:00am", "7:00pm"), ("Jo P", "Cook", "4:00pm", "11:00pm")])
    r = _staff_client(app, db_path, rid, uid).post("/staff/api/running-late", json=payload)
    assert r.status_code == 400 and needle in r.get_json()["error"]
    assert told["managers"] == [] and _attendance(db_path, rid) == []


def test_running_late_is_refused_once_the_shift_is_over(app, db_path, told, monkeypatch):
    import staff_comms
    rid = _rid(db_path)
    uid, _ = _staff(db_path, rid, "Dana K")
    _schedule(db_path, rid, DAY, [("Dana K", "Server", "11:00am", "3:00pm")])
    monkeypatch.setattr(staff_comms, "_local_now", lambda r: datetime(2026, 9, 21, 15, 30))
    r = _staff_client(app, db_path, rid, uid).post("/staff/api/running-late",
                                                    json={"shift_start": "11:00am", "eta_minutes": 10})
    assert r.status_code == 400 and "already over" in r.get_json()["error"]


def test_a_self_report_is_weaker_than_any_clock_in_reading(db_path):
    import attendance
    rid = _rid(db_path)
    # A clock-in reading stands against a later self-report...
    attendance.record(rid, "Dana K", DAY.isoformat(), "on_time", "schedule_vs_punch_join", shift_start="11:00am",
                      db_path=db_path)
    assert attendance.record(rid, "Dana K", DAY.isoformat(), "late", "self_report", shift_start="11:00am",
                             minutes_late=20, db_path=db_path) is False
    (row,) = _attendance(db_path, rid)
    assert (row["outcome"], row["source"]) == ("on_time", "schedule_vs_punch_join")
    # ...a measured late's missing minutes are not filled with a guess...
    attendance.record(rid, "Jo P", DAY.isoformat(), "late", "coverage_check", shift_start="4:00pm", db_path=db_path)
    attendance.record(rid, "Jo P", DAY.isoformat(), "late", "self_report", shift_start="4:00pm", minutes_late=45,
                      db_path=db_path)
    jo = [r for r in _attendance(db_path, rid) if r["employee_name"] == "Jo P"][0]
    assert jo["source"] == "coverage_check" and jo["minutes_late"] is None
    # ...and the punch join overwrites a self-report.
    attendance.record(rid, "Ana R", DAY.isoformat(), "late", "self_report", shift_start="5:00pm", minutes_late=10,
                      db_path=db_path)
    assert attendance.record(rid, "Ana R", DAY.isoformat(), "no_show", "schedule_vs_punch_join",
                             shift_start="5:00pm", db_path=db_path) is True
    ana = [r for r in _attendance(db_path, rid) if r["employee_name"] == "Ana R"][0]
    assert (ana["outcome"], ana["source"]) == ("no_show", "schedule_vs_punch_join")


def _coverage_setup(db_path, monkeypatch, at):
    import intraday, issues, time_utils
    rid = _rid(db_path, "Service Co")
    models.update_restaurant(rid, {"open_times_json": '{"Monday": "10:00am"}',
                                   "close_times_json": '{"Monday": "10:00pm"}'}, db_path=db_path)
    conn = get_conn(db_path)
    cid = conn.execute("INSERT INTO alert_contacts (restaurant_id, name, phone, sms_consent) "
                       "VALUES (?, 'GM', '+15555550100', 1)", (rid,)).lastrowid
    conn.commit(); conn.close()
    issues.set_routing(rid, "manager", cid, db_path=db_path)
    monkeypatch.setattr(time_utils, "restaurant_now", lambda r, naive=False: at)
    monkeypatch.setattr(intraday, "coverage_gaps", lambda *a, **k: {
        "available": True, "missing": [{"employee": "Dana K", "role": "Server", "shift_start": "11:00am",
                                        "minutes_late": int((at - datetime(2026, 9, 21, 11, 0)).seconds // 60)}]})
    return rid


def _say_late(db_path, rid, eta):
    import staff_comms
    uid, mid = _staff(db_path, rid, "Dana K")
    _schedule(db_path, rid, DAY, [("Dana K", "Server", "11:00am", "7:00pm")])
    staff_comms.report_late(rid, {"id": mid, "user_id": uid, "employee_name": "Dana K"}, DAY.isoformat(),
                            "11:00am", eta, now_local=NOON_ISH, db_path=db_path)


def test_the_no_show_issue_waits_for_start_plus_eta_plus_grace(db_path, monkeypatch, told):
    import issues, strategy_jobs
    # 11:30: 20 minutes late + 15 grace runs to 11:35 — held.
    rid = _coverage_setup(db_path, monkeypatch, datetime(2026, 9, 21, 11, 30))
    _say_late(db_path, rid, 20)
    assert strategy_jobs.run_coverage_check(db_path=db_path)["opened"] == 0
    assert issues.list_issues(rid, db_path=db_path) == []


def test_after_the_hold_the_issue_opens_and_says_what_they_told_us(db_path, monkeypatch, told):
    import issues, strategy_jobs
    rid = _coverage_setup(db_path, monkeypatch, datetime(2026, 9, 21, 11, 40))
    _say_late(db_path, rid, 20)
    assert strategy_jobs.run_coverage_check(db_path=db_path)["opened"] == 1
    (opened,) = issues.list_issues(rid, db_path=db_path)
    assert "hasn't clocked in" in opened["title"] and "about 20 minutes late" in opened["detail"]


def test_revert_check_without_a_report_the_issue_opens_at_once(db_path, monkeypatch, told):
    """The hold is what stops it: the same 11:30 pass with no report opens."""
    import strategy_jobs
    _coverage_setup(db_path, monkeypatch, datetime(2026, 9, 21, 11, 30))
    assert strategy_jobs.run_coverage_check(db_path=db_path)["opened"] == 1


def test_a_report_for_another_shift_does_not_hold_this_one(db_path):
    import staff_comms
    rid = _rid(db_path)
    uid, mid = _staff(db_path, rid, "Dana K")
    _schedule(db_path, rid, DAY, [("Dana K", "Server", "11:00am", "3:00pm"), ("Dana K", "Server", "5:00pm", "9:00pm")])
    staff_comms.report_late(rid, {"id": mid, "user_id": uid, "employee_name": "Dana K"}, DAY.isoformat(),
                            "5:00pm", 20, now_local=NOON_ISH, db_path=db_path)
    assert staff_comms.late_hold(rid, "Dana K", "11:00am", datetime(2026, 9, 21, 11, 20), db_path=db_path) is None
    h = staff_comms.late_hold(rid, "Dana K", "5:00pm", datetime(2026, 9, 21, 17, 30), db_path=db_path)
    assert h["holding"] is True and h["until"] == datetime(2026, 9, 21, 17, 35)


# ═══ H8 announcements with acknowledgement ══════════════════════════════════

def test_an_announcement_reaches_its_audience_and_counts_who_read_it(app, db_path, told):
    rid = _rid(db_path)
    a_uid, _ = _staff(db_path, rid, "Ana R", job_role="Server")
    b_uid, _ = _staff(db_path, rid, "Ben T", job_role="Cook")
    owner = _owner_client(app, db_path, _owner(db_path, rid))
    r = owner.post("/api/labor/inbox/announcements", json={"title": "New menu tomorrow",
                                                           "body": "Tasting at 3pm.\nWear black."})
    assert r.status_code == 200, r.get_json()
    out = r.get_json()
    ann = out["announcement"]
    assert ann["recipients"] == 2 and ann["read"] == 0 and ann["read_line"] == "0 of 2 read"
    assert ann["unread_names"] == ["Ana R", "Ben T"] and out["delivery"]["push"] == 2
    # Through people.tell, carrying the announcement id; not urgent.
    assert {s["name"] for s in told["staff"]} == {"Ana R", "Ben T"}
    assert all(s["data"]["announcement_id"] == ann["id"] and s["priority"] is None for s in told["staff"])
    # The staff inbox, then "Got it".
    ana = _staff_client(app, db_path, rid, a_uid)
    inbox = ana.get("/staff/api/inbox").get_json()
    assert inbox["unread"] == 1 and inbox["unread_messages"] == 0
    (item,) = inbox["announcements"]
    assert set(item) == {"id", "title", "body", "priority", "created_at", "created_by_name", "acked_at",
                         "expires_on"}
    assert item["acked_at"] is None and item["created_at"].endswith("Z")
    ack = ana.post(f"/staff/api/announcements/{item['id']}/ack")
    assert ack.status_code == 200 and ack.get_json()["acked_at"]
    assert ana.post(f"/staff/api/announcements/{item['id']}/ack").get_json()["acked_at"] == ack.get_json()["acked_at"]
    assert ana.get("/staff/api/inbox").get_json()["unread"] == 0
    listed = owner.get("/api/labor/inbox/announcements").get_json()["announcements"][0]
    assert listed["read_line"] == "1 of 2 read" and listed["unread_names"] == ["Ben T"]
    assert [x["name"] for x in listed["read_by"]] == ["Ana R"]


def test_urgent_goes_at_p1_and_role_and_date_audiences_pick_the_right_people(app, db_path, told):
    import staff_comms
    rid = _rid(db_path)
    _staff(db_path, rid, "Ana R", job_role="Server")
    _staff(db_path, rid, "Ben T", job_role="Cook")
    _staff(db_path, rid, "Cy V", job_role="Server")
    _schedule(db_path, rid, DAY, [("Ben T", "Cook", "4:00pm", "11:00pm"), ("Cy V", "Server", "5:00pm", "10:00pm")])
    owner = {"id": _owner(db_path, rid), "restaurant_id": rid, "username": "boss"}
    out = staff_comms.create_announcement(rid, owner, "POS down", priority="urgent", audience="role",
                                          audience_value="server", db_path=db_path)
    assert sorted(out["announcement"]["unread_names"]) == ["Ana R", "Cy V"]
    assert {s["priority"] for s in told["staff"]} == {"urgent"} and told["staff"][0]["title"].startswith("Urgent: ")
    out = staff_comms.create_announcement(rid, owner, "Bears game", audience="shift_date",
                                          audience_value=DAY.isoformat(), db_path=db_path)
    assert sorted(out["announcement"]["unread_names"]) == ["Ben T", "Cy V"]
    # A one-night note expires after that night.
    assert out["announcement"]["expires_on"] == DAY.isoformat()
    assert staff_comms.roles(rid, db_path=db_path) == ["Cook", "Server"]
    with pytest.raises(staff_comms.CommsError):
        staff_comms.create_announcement(rid, owner, "x", audience="role", audience_value="Host", db_path=db_path)
    with pytest.raises(staff_comms.CommsError):
        staff_comms.create_announcement(rid, owner, "", db_path=db_path)


def test_expired_and_withdrawn_announcements_leave_the_inbox(app, db_path, told, monkeypatch):
    import staff_comms
    rid = _rid(db_path)
    uid, mid = _staff(db_path, rid, "Ana R")
    owner = {"id": _owner(db_path, rid), "restaurant_id": rid, "username": "boss"}
    a1 = staff_comms.create_announcement(rid, owner, "Closing early", expires_on="2026-09-21",
                                         db_path=db_path)["announcement"]
    a2 = staff_comms.create_announcement(rid, owner, "Meeting Friday", db_path=db_path)["announcement"]
    assert staff_comms.staff_inbox(rid, mid, db_path=db_path)["unread"] == 2
    monkeypatch.setattr(staff_comms, "_local_now", lambda r: datetime(2026, 9, 22, 9, 0))
    assert [a["id"] for a in staff_comms.staff_inbox(rid, mid, db_path=db_path)["announcements"]] == [a2["id"]]
    staff_comms.withdraw_announcement(rid, a2["id"], owner, db_path=db_path)
    assert staff_comms.staff_inbox(rid, mid, db_path=db_path)["announcements"] == []
    with pytest.raises(staff_comms.NotFound):
        staff_comms.ack(rid, mid, a2["id"], db_path=db_path)
    assert a1["id"] != a2["id"]


def test_one_restaurants_announcement_is_not_anothers(app, db_path, told):
    import staff_comms
    a, b = _rid(db_path, "Alpha"), _rid(db_path, "Beta")
    _staff(db_path, a, "Ana R")
    b_uid, _ = _staff(db_path, b, "Bo S")
    ann = staff_comms.create_announcement(a, {"id": _owner(db_path, a), "restaurant_id": a}, "Alpha only",
                                          db_path=db_path)["announcement"]
    bo = _staff_client(app, db_path, b, b_uid)
    assert bo.get("/staff/api/inbox").get_json()["announcements"] == []
    assert bo.post(f"/staff/api/announcements/{ann['id']}/ack").status_code == 404
    other_owner = _owner_client(app, db_path, _owner(db_path, b, "bboss"))
    assert other_owner.get(f"/api/labor/inbox/announcements/{ann['id']}").status_code == 404


# ═══ H15 the thread with the managers ═══════════════════════════════════════

def test_a_staff_message_reaches_the_deciders_and_a_reply_reaches_them_back(app, db_path, told):
    rid = _rid(db_path)
    uid, _ = _staff(db_path, rid, "Dana K")
    owner_uid = _owner(db_path, rid)
    dana = _staff_client(app, db_path, rid, uid)
    r = dana.post("/staff/api/messages", json={"body": "Can I come in at 5 instead of 4?", "shift_date": "2026-09-21"})
    assert r.status_code == 200, r.get_json()
    sent = r.get_json()
    assert sent["managers_told"] is True and sent["message"]["from"] == "staff"
    (m,) = told["managers"]
    assert m["deciders"] is True and m["title"] == "Message from Dana K" and "9/21/26" in m["body"]
    assert m["data"]["thread_id"] == sent["thread_id"] and m["data"]["nav"].startswith("labor/inbox")
    # A second line in the same burst, still unread, is not pushed again.
    dana.post("/staff/api/messages", json={"body": "Or 5:30"})
    assert len(told["managers"]) == 1
    boss = _owner_client(app, db_path, owner_uid)
    inbox = boss.get("/api/labor/inbox").get_json()
    assert inbox["unread"] == 2 and inbox["threads"][0]["employee_name"] == "Dana K"
    th = boss.get(f"/api/labor/inbox/threads/{sent['thread_id']}").get_json()
    assert [x["body"] for x in th["messages"]] == ["Can I come in at 5 instead of 4?", "Or 5:30"]
    assert boss.get("/api/labor/inbox").get_json()["unread"] == 0          # opening it read it
    rep = boss.post(f"/api/labor/inbox/threads/{sent['thread_id']}/reply", json={"body": "5 is fine."})
    assert rep.status_code == 200 and rep.get_json()["delivered_via"] == "push"
    (s,) = told["staff"]
    assert s["name"] == "Dana K" and s["data"]["thread_id"] == sent["thread_id"] and "5 is fine." in s["lines"][0]
    assert dana.get("/staff/api/inbox").get_json()["unread_messages"] == 1
    mine = dana.get("/staff/api/messages").get_json()
    assert [x["from"] for x in mine["messages"]] == ["staff", "staff", "manager"]
    assert mine["messages"][0]["read_at"] is not None                         # the manager read it
    assert dana.get("/staff/api/inbox").get_json()["unread_messages"] == 0
    # A new message after the managers read the burst is pushed again.
    dana.post("/staff/api/messages", json={"body": "Thanks!"})
    assert len(told["managers"]) == 2


def test_a_message_can_name_only_the_employees_own_request(app, db_path, told):
    rid = _rid(db_path)
    uid, _ = _staff(db_path, rid, "Dana K")
    hid = _schedule(db_path, rid, DAY, [("Jo P", "Cook", "4:00pm", "11:00pm")])
    conn = get_conn(db_path)
    theirs = conn.execute("INSERT INTO shift_change_requests (restaurant_id, history_id, employee_name, date, "
                          "shift_start) VALUES (?,?,?,?,?)", (rid, hid, "Jo P", DAY.isoformat(), "4:00pm")).lastrowid
    mine = conn.execute("INSERT INTO shift_change_requests (restaurant_id, history_id, employee_name, date, "
                        "shift_start) VALUES (?,?,?,?,?)", (rid, hid, "Dana K", DAY.isoformat(), "4:00pm")).lastrowid
    conn.commit(); conn.close()
    dana = _staff_client(app, db_path, rid, uid)
    bad = dana.post("/staff/api/messages", json={"body": "about this", "request_id": theirs})
    assert bad.status_code == 400 and "isn't yours" in bad.get_json()["error"]
    ok = dana.post("/staff/api/messages", json={"body": "about this", "request_id": mine})
    assert ok.status_code == 200 and ok.get_json()["message"]["request_id"] == mine
    assert ok.get_json()["message"]["request_kind"] == "shift"


def test_threads_are_per_employee_and_per_restaurant(app, db_path, told):
    rid, other = _rid(db_path, "Alpha"), _rid(db_path, "Beta")
    d_uid, _ = _staff(db_path, rid, "Dana K")
    j_uid, _ = _staff(db_path, rid, "Jo P")
    r = _staff_client(app, db_path, rid, d_uid).post("/staff/api/messages", json={"body": "hi"})
    thread_id = r.get_json()["thread_id"]
    assert _staff_client(app, db_path, rid, j_uid).get("/staff/api/messages").get_json()["messages"] == []
    stranger = _owner_client(app, db_path, _owner(db_path, other, "beta"))
    assert stranger.get(f"/api/labor/inbox/threads/{thread_id}").status_code == 404
    assert stranger.post(f"/api/labor/inbox/threads/{thread_id}/reply", json={"body": "x"}).status_code == 404
    assert stranger.get("/api/labor/inbox").get_json()["threads"] == []


# ═══ the owner surface: permissions, twins, guards ══════════════════════════

def test_the_inbox_needs_the_decider_permission_and_refuses_a_staff_session(app, db_path, told, monkeypatch):
    rid = _rid(db_path)
    uid, _ = _staff(db_path, rid, "Dana K")
    c = app.test_client()
    c.set_cookie("session_token", create_staff_session(uid, rid, db_path=db_path))
    assert c.get("/api/labor/inbox").status_code in (401, 403)
    import permissions
    real = permissions.has_permission
    monkeypatch.setattr(permissions, "has_permission",
                        lambda u, p: False if p == permissions.SCHEDULE_DRAFT else real(u, p))
    boss = _owner_client(app, db_path, _owner(db_path, rid))
    assert boss.get("/api/labor/inbox").status_code == 403
    assert boss.post("/api/labor/inbox/announcements", json={"title": "x"}).status_code == 403


def test_every_owner_route_has_a_mobile_twin_that_answers_a_bearer(app, db_path, told):
    web = {r.rule[len("/api"):] for r in app.url_map.iter_rules() if r.rule.startswith("/api/labor/inbox")}
    mob = {r.rule[len("/mobile/api"):] for r in app.url_map.iter_rules() if r.rule.startswith("/mobile/api/labor/inbox")}
    assert web == mob and len(web) == 6
    rid = _rid(db_path)
    _staff(db_path, rid, "Ana R")
    tok = create_session(_owner(db_path, rid), db_path=db_path)
    c = app.test_client()
    r = c.post("/mobile/api/labor/inbox/announcements", json={"title": "From the phone"},
               headers={"Authorization": f"Bearer {tok}"})
    assert r.status_code == 200 and r.get_json()["announcement"]["recipients"] == 1
    assert c.get("/mobile/api/labor/inbox", headers={"Authorization": f"Bearer {tok}"}).status_code == 200
    assert c.get("/mobile/api/labor/inbox").status_code == 401


def test_the_owner_routes_are_labor_gated(app, db_path):
    assert auth._required_module("/api/labor/inbox/announcements") == "labor"
    assert auth._required_module("/mobile/api/labor/inbox") == "labor"


def test_a_json_body_that_is_not_an_object_is_a_4xx(app, db_path, told):
    rid = _rid(db_path)
    uid, _ = _staff(db_path, rid, "Dana K")
    boss = _owner_client(app, db_path, _owner(db_path, rid))
    r = boss.post("/api/labor/inbox/announcements", data='"x"', content_type="application/json")
    assert 400 <= r.status_code < 500
    dana = _staff_client(app, db_path, rid, uid)
    for path in ("/staff/api/messages", "/staff/api/running-late"):
        r = dana.post(path, data="[1]", content_type="application/json")
        assert r.status_code == 400, path


def test_tell_staff_drops_keywords_people_tell_does_not_take(db_path, monkeypatch):
    """Before the rebuilt pipeline lands, people.tell has no data/priority:
    the notice still goes."""
    import people, staff_comms
    seen = []

    def old_tell(rid, name, title, lines, *, email_type="staff_notice", channel=None, db_path=None):
        seen.append((name, email_type))
        return "email"
    monkeypatch.setattr(people, "tell", old_tell)
    assert staff_comms._tell_staff(1, "Ana R", "t", ["l"], email_type="staff_announcement",
                                   data={"announcement_id": 3}, priority="urgent") == "email"
    assert seen == [("Ana R", "staff_announcement")]
