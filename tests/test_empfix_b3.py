"""Employee audit fix B3 — shifts and requests (staff app).

C5   "today" is the restaurant's service date (shifts, time off).
C6   every leg carries its own start, request state and actions; days say
     whether they are posted; the week's hours; `upcoming` past 7 days.
C8   an approved drop says you're still on it; an unclaimed open shift goes
     back to the deciders once, 12 hours out (one job).
C12  employees never see another person's reason or a manager's login.
H2   managers post open shifts and offer one to a named person; the
     coverage issue's ask is that offer, answered in the app.
H9   start-time gates, expiry, the republish-proof duplicate check, the
     vanished-shift refusal, dependent requests voided, the role both ways,
     the swap picker, the in-person yes, and every waiting party told.
M6   approved time off can be called off; the approval names the shifts
     still scheduled.   M7  reasons reach the deciders; notes reach staff.
COM-07  the app showing a week counts as seen.   H7  who's on with me.

Every clock is pinned: the shift dates are October 2026 and `now` is
passed in (or time_utils.restaurant_now is patched for the routes).
"""
import datetime as dt
import json
import sys

import pytest
from flask import Flask

# Imported before any fixture patches models.get_conn (bound imports).
import activity, covers, decisions, delayed, demand_signals, goals, issues, metrics, outcomes  # noqa: E401,F401
import push, schedule_economics, schedule_intel, schedule_rules, schedule_versions  # noqa: E401,F401
import shift_requests, shift_quality, staff_schedule, staff_settings, strategy_jobs, time_off  # noqa: E401,F401
import labor_replacements, intraday, people, task_sheets  # noqa: E401,F401

import auth
import models
import schedule_versions as sv
import shift_requests as srq
import time_utils
from auth import create_staff_session, create_user, init_auth, upsert_membership
from models import Restaurant, create_restaurant

HEADER = "date,day,employee,role,shift_start,shift_end,scheduled_hours,notes"
W1 = ["2026-10-05", "2026-10-06", "2026-10-07", "2026-10-08", "2026-10-09", "2026-10-10", "2026-10-11"]
W2 = ["2026-10-12", "2026-10-13", "2026-10-14", "2026-10-15", "2026-10-16", "2026-10-17", "2026-10-18"]
NOW = dt.datetime(2026, 10, 4, 9, 0)         # the Sunday before W1, 9am local


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
    monkeypatch.setattr(auth, "DB_PATH", db_path)
    monkeypatch.setattr(models, "_cached_shifts", lambda r: [])
    init_auth(db_path=db_path)
    return db_path


@pytest.fixture
def told(monkeypatch):
    """Every staff notice and manager notice, instead of sending them."""
    box = {"staff": [], "managers": [], "tell": []}

    def staff(rid, names, subject, lines, db_path, channels=None):
        names = [n for n in (names or []) if (n or "").strip()]
        box["staff"].append({"to": names, "subject": subject, "lines": list(lines)})
        return names

    def managers(rid, title, body, db_path, req=None):
        box["managers"].append({"title": title, "body": body, "req": req})

    def tell(rid, name, title, lines, **kw):
        box["tell"].append({"to": name, "title": title, "lines": list(lines)})
        return None
    monkeypatch.setattr(srq, "_tell_staff", staff, raising=False)
    monkeypatch.setattr(srq, "_email_staff", lambda rid, names, subject, lines, db_path: len(
        staff(rid, names, subject, lines, db_path)))
    monkeypatch.setattr(srq, "_tell_managers", managers)
    monkeypatch.setattr(people, "tell", tell)
    return box


@pytest.fixture
def clock(monkeypatch):
    """The restaurant's wall clock for code that reads it itself (routes)."""
    state = {"now": NOW}
    monkeypatch.setattr(time_utils, "restaurant_now", lambda r=None, naive=False: state["now"])
    return state


def _day(d):
    return dt.date.fromisoformat(d).strftime("%A")


def _restaurant(db_path, roster=(), **cols):
    rid = create_restaurant(Restaurant(name="Shifts Co", owner_email="q@x.com"), db_path=db_path)
    if cols:
        conn = models.get_conn(db_path)
        conn.execute("UPDATE restaurants SET " + ", ".join(f"{k}=?" for k in cols) + " WHERE id=?", (*cols.values(), rid))
        conn.commit()
        conn.close()
    for name, role in roster:
        models.add_manual_team_member(rid, name, role=role, db_path=db_path)
    return rid


def _publish(db_path, rid, rows, dates=W1):
    text = HEADER + "\n" + "\n".join(f"{d},{_day(d)},{n},{role},{s},{e},{h}," for d, n, role, s, e, h in rows)
    hid = models.save_schedule_history(rid, dates[0], dates[-1], 0, 0, 30, text, [], db_path=db_path)
    conn = models.get_conn(db_path)
    conn.execute("UPDATE schedule_history SET published_at=datetime('now') WHERE id=?", (hid,))
    conn.commit()
    conn.close()
    sv.append(rid, hid, "published", text, db_path=db_path)
    return hid


def _csv(db_path, rid, day):
    conn = models.get_conn(db_path)
    try:
        hist = srq._published(conn, rid, day)
        return sv.rows_from_csv(hist["schedule_csv"])
    finally:
        conn.close()


def _member(db_path, rid, name):
    uid = create_user(rid, name.lower().replace(" ", ""), f"{name.lower()}@x.test", "unused", db_path=db_path)
    upsert_membership(uid, rid, "employee", employee_name=name, db_path=db_path)
    return uid


def _row(db_path, req_id):
    conn = models.get_conn(db_path)
    try:
        return dict(conn.execute("SELECT * FROM shift_change_requests WHERE id=?", (req_id,)).fetchone())
    finally:
        conn.close()


def _staff_client(db_path, rid, uid):
    app = Flask(__name__, template_folder="../templates")
    from staff_routes import staff_bp
    app.register_blueprint(staff_bp)
    c = app.test_client()
    c.set_cookie("staff_session", create_staff_session(uid, rid, db_path=db_path))
    return c


SERVERS = [("Ana", "Server"), ("Ben", "Server"), ("Cara", "Server"), ("Bo", "Dishwasher")]


# ── C5: today on the restaurant's clock ────────────────────────────────────

def test_the_portals_today_is_the_restaurants_service_date(db):
    rid = _restaurant(db, SERVERS)
    _publish(db, rid, [(W1[0], "Ana", "Server", "5:00pm", "11:00pm", 6)])
    evening = staff_schedule.shifts_for_employee(rid, "Ana", now=dt.datetime(2026, 10, 5, 20, 15))
    assert evening["week"][0]["date"] == W1[0] and evening["today"]["start"] == "5:00pm"
    # 12:30am: the closer is still on last night's shift.
    late = staff_schedule.shifts_for_employee(rid, "Ana", now=dt.datetime(2026, 10, 6, 0, 30))
    assert late["week"][0]["date"] == W1[0] and late["today"]["start"] == "5:00pm"


def test_time_off_reads_the_restaurants_date(db, monkeypatch):
    rid = _restaurant(db, SERVERS)
    monkeypatch.setattr(time_utils, "restaurant_now", lambda r=None, naive=False: dt.datetime(2027, 3, 1, 12, 0))
    conn = models.get_conn(db)
    for end in ("2026-12-30", "2026-12-31"):
        conn.execute("INSERT INTO staff_time_off (restaurant_id, employee_name, start_date, end_date, status) "
                     "VALUES (?, 'Ana', ?, ?, 'denied')", (rid, end, end))
    conn.commit()
    conn.close()
    assert [r["end_date"] for r in time_off.mine(rid, "Ana")] == ["2026-12-31"]


# ── C6: legs, posted days, the week's hours, beyond seven days ─────────────

def test_every_leg_carries_its_own_start_request_and_actions(db, told):
    rid = _restaurant(db, SERVERS)
    _publish(db, rid, [(W1[1], "Ana", "Server", "10:00am", "2:00pm", 4), (W1[1], "Ana", "Server", "5:00pm", "10:00pm", 5)])
    _publish(db, rid, [(W2[1], "Ana", "Server", "10:00am", "2:00pm", 4)], dates=W2)
    req = srq.request_drop(rid, "Ana", W1[1], "5:00pm", now=NOW)
    out = staff_schedule.shifts_for_employee(rid, "Ana", now=NOW)
    tue = next(d for d in out["week"] if d["date"] == W1[1])
    lunch, dinner = tue["shifts"]
    assert (lunch["shift_start"], dinner["shift_start"]) == ("10:00am", "5:00pm")
    assert lunch["actions"] == ["drop", "swap"] and lunch["request"] is None
    assert dinner["request"] == {"id": req["id"], "kind": "drop", "status": "pending"} and dinner["actions"] == []
    assert out["week_hours"] == 9
    assert out["week"][0]["date"] == "2026-10-04" and out["week"][0]["posted"] is False
    assert tue["posted"] is True
    assert any(u["date_iso"] == W2[1] for u in out["upcoming"]), "upcoming goes past the 7 days"


# ── C8: the approved drop, the escalation, the job ─────────────────────────

def test_an_approved_drop_says_you_are_still_on_it(db, told):
    rid = _restaurant(db, SERVERS)
    _publish(db, rid, [(W1[1], "Ana", "Server", "5:00pm", "10:00pm", 5)])
    req = srq.request_drop(rid, "Ana", W1[1], "5:00pm", now=NOW)
    srq.decide(rid, req["id"], True, decided_by="erik", now=NOW)
    mine = next(n for n in told["staff"] if n["to"] == ["Ana"])
    text = mine["subject"] + " " + " ".join(mine["lines"])
    assert "still on it" in text and "off your schedule" not in text
    leg = next(d for d in staff_schedule.shifts_for_employee(rid, "Ana", now=NOW)["week"] if d["date"] == W1[1])["shift"]
    assert leg["request"]["status"] == "open"


def test_an_unclaimed_open_shift_goes_back_to_the_deciders_once_12_hours_out(db, told):
    rid = _restaurant(db, SERVERS)
    _publish(db, rid, [(W1[1], "Ana", "Server", "5:00pm", "10:00pm", 5)])
    req = srq.request_drop(rid, "Ana", W1[1], "5:00pm", now=NOW)
    srq.decide(rid, req["id"], True, now=NOW)
    told["managers"].clear()
    assert srq.escalate_unclaimed(rid, now=dt.datetime(2026, 10, 6, 4, 0)) == 0      # 13 hours out
    assert srq.escalate_unclaimed(rid, now=dt.datetime(2026, 10, 6, 6, 0)) == 1      # 11 hours out
    assert srq.escalate_unclaimed(rid, now=dt.datetime(2026, 10, 6, 7, 0)) == 0      # once
    assert [m["title"] for m in told["managers"]] == ["Open shift still unclaimed"]
    assert "still on the schedule" in told["managers"][0]["body"]


def test_past_requests_expire_and_the_watch_job_reports_standard_counts(db, told):
    import inspect
    import jobs_registry
    import scheduler
    rid = _restaurant(db, SERVERS)
    _publish(db, rid, [(W1[0], "Ana", "Server", "11:00am", "3:00pm", 4)])
    req = srq.request_drop(rid, "Ana", W1[0], "11:00am", now=NOW)
    assert srq.expire_past(rid, now=dt.datetime(2026, 10, 5, 14, 0)) == 0          # still on
    assert srq.expire_past(rid, now=dt.datetime(2026, 10, 5, 15, 30)) == 1
    assert _row(db, req["id"])["status"] == "expired"
    out = srq.run_open_shift_watch(db_path=db)
    assert {"attempted", "ok", "failed", "skipped", "hit_bound"} <= set(out)
    assert "open_shift_watch" in jobs_registry.JOBS
    assert 'run_job("open_shift_watch"' in inspect.getsource(scheduler.scheduler_loop)


# ── C12: what an employee may see ──────────────────────────────────────────

def test_open_shifts_and_asks_never_carry_a_reason_or_a_managers_login(db, told):
    rid = _restaurant(db, SERVERS)
    _publish(db, rid, [(W1[1], "Ana", "Server", "5:00pm", "10:00pm", 5), (W1[2], "Ana", "Server", "5:00pm", "10:00pm", 5),
                       (W1[3], "Ben", "Server", "5:00pm", "10:00pm", 5)])
    req = srq.request_drop(rid, "Ana", W1[1], "5:00pm", reason="my kid is sick", now=NOW)
    srq.decide(rid, req["id"], True, decided_by="erik@owner.test", now=NOW)
    srq.request_swap(rid, "Ana", W1[2], "5:00pm", "Ben", W1[3], "5:00pm", reason="doctor at 4", now=NOW)
    ben = srq.for_staff(rid, "Ben", now=NOW)
    blob = json.dumps(ben)
    assert ben["open"] and ben["asks"]
    assert "kid" not in blob and "doctor" not in blob and "erik" not in blob
    assert not any("reason" in o or "decided_by" in o for o in ben["open"] + ben["asks"])
    ana = srq.for_staff(rid, "Ana", now=NOW)
    assert "erik" not in json.dumps(ana) and {r["reason"] for r in ana["requests"]} == {"my kid is sick", "doctor at 4"}


# ── H2: posted open shifts and offers ──────────────────────────────────────

def test_a_manager_posts_an_extra_shift_and_a_teammate_claims_it(db, told):
    rid = _restaurant(db, SERVERS)
    _publish(db, rid, [(W1[1], "Ana", "Server", "11:00am", "3:00pm", 4)])
    out = srq.post_open_shift(rid, W1[1], "5:00pm", shift_end="10:00pm", role="Server", actor="erik", now=NOW)
    assert out["request"]["kind"] == "post" and out["offer"] is None
    board = srq.for_staff(rid, "Cara", now=NOW)["open"]
    assert [(o["id"], o["employee_name"], o["can_take"]) for o in board] == [(out["request"]["id"], None, True)]
    srq.claim(rid, out["request"]["id"], "Cara", now=NOW)
    assert ("Cara", "5:00pm") in {(r["employee"], r["shift_start"]) for r in _csv(db, rid, W1[1])}


def test_an_offer_to_one_person_is_answered_in_the_app_and_writes_the_week(db, told):
    rid = _restaurant(db, SERVERS)
    _publish(db, rid, [(W1[1], "Ana", "Server", "5:00pm", "10:00pm", 5)])
    cara = _member(db, rid, "Cara")
    out = srq.post_open_shift(rid, W1[1], "5:00pm", employee="Ana", actor="erik", offer_to="cara", now=NOW)
    offer = out["offer"]
    assert offer["name"] == "Cara" and offer["via"] == "app"
    assert told["tell"][-1]["to"] == "Cara" and "Reply" not in " ".join(told["tell"][-1]["lines"])
    assert srq.for_staff(rid, "Ben", now=NOW)["open"] == [], "an offer-only shift is not on the board"
    client = _staff_client(db, rid, cara)
    listed = client.get("/staff/api/shift-requests").get_json()["offers"]
    assert [o["id"] for o in listed] == [offer["id"]]
    r = client.post(f"/staff/api/offers/{offer['id']}/respond", json={"accept": True}).get_json()
    assert r["ok"] and r["offer"]["status"] == "accepted"
    assert [x["employee"] for x in _csv(db, rid, W1[1])] == ["Cara"]


def _coverage_issue(db_path, rid, day, holder="Ana", start="5:00pm", covers=("Cara",)):
    issue, _t = issues.create_issue(rid, "coverage", f"{holder} hasn't clocked in", severity="high",
                                    source_key=f"coverage:{day}:{holder.lower()}", notify=False,
                                    meta={"missing": holder, "role": "Server", "shift_start": start,
                                          "covers": [{"name": c, "score": 4.0} for c in covers]}, db_path=db_path)
    return issue


def test_ask_to_cover_is_an_in_app_offer_and_a_yes_resolves_the_issue(db, told, clock, monkeypatch):
    import notify
    sent = []
    monkeypatch.setattr(notify, "send_sms", lambda *a, **k: sent.append(a) or True)
    rid = _restaurant(db, SERVERS)
    _publish(db, rid, [(W1[1], "Ana", "Server", "5:00pm", "10:00pm", 5)])
    _member(db, rid, "Cara")
    clock["now"] = dt.datetime(2026, 10, 6, 17, 20)       # 20 minutes into the shift
    issue = _coverage_issue(db, rid, W1[1])
    out = intraday.ask_to_cover(rid, issue["id"], "Cara", db_path=db, actor="erik")
    assert out["ok"] and out["via"] == "app" and out["offer_id"] and not sent
    srq.respond_offer(rid, out["offer_id"], "Cara", True, db_path=db)
    after = issues.get_issue(rid, issue["id"], db_path=db)
    asked = json.loads(after["meta_json"])["asked"]
    assert after["status"] == "resolved" and asked[0]["answer"] == "took"
    assert [x["employee"] for x in _csv(db, rid, W1[1])] == ["Cara"]


def test_a_declined_offer_is_kept_on_the_issue_and_tells_the_manager(db, told, clock):
    rid = _restaurant(db, SERVERS)
    _publish(db, rid, [(W1[1], "Ana", "Server", "5:00pm", "10:00pm", 5)])
    _member(db, rid, "Cara")
    clock["now"] = dt.datetime(2026, 10, 6, 17, 20)
    issue = _coverage_issue(db, rid, W1[1])
    out = intraday.ask_to_cover(rid, issue["id"], "Cara", db_path=db)
    srq.respond_offer(rid, out["offer_id"], "Cara", False, db_path=db)
    after = issues.get_issue(rid, issue["id"], db_path=db)
    assert after["status"] != "resolved" and json.loads(after["meta_json"])["asked"][0]["answer"] == "declined"
    assert any(m["title"] == "Offer declined" for m in told["managers"])
    assert [x["employee"] for x in _csv(db, rid, W1[1])] == ["Ana"]


def test_a_staffing_gap_the_week_already_had_does_not_block_a_cover(db, told):
    """Nobody is on until close on Tuesday whoever holds the 11-5: that gap
    was charged to the holder, so it read as new for anyone taking the
    shift and no cover could be claimed or offered (replacement_is_legal)."""
    rid = _restaurant(db, SERVERS, open_times_json='{"Tuesday": "10:00am"}', close_times_json='{"Tuesday": "10:00pm"}')
    _publish(db, rid, [(W1[1], "Ana", "Server", "11:00am", "5:00pm", 6)])
    req = srq.request_drop(rid, "Ana", W1[1], "11:00am", now=NOW)
    srq.decide(rid, req["id"], True, now=NOW)
    assert srq.claim(rid, req["id"], "Cara", now=NOW)["status"] == "covered"


def test_staff_notices_carry_the_request_where_people_tell_takes_it(db, monkeypatch):
    """The notification rebuild (B2) lets people.tell route a notice to the
    request and send a same-day change through quiet hours; the old tell
    takes none of it, and nothing is passed that it doesn't take."""
    seen = []

    def new_tell(rid, name, title, lines, *, email_type="staff_notice", channel=None, db_path=None,
                 data=None, nav=None, purpose=None, shift_date=None):
        seen.append({"data": data, "nav": nav, "purpose": purpose, "shift_date": shift_date})
        return "push"
    monkeypatch.setattr(people, "tell", new_tell)
    monkeypatch.setattr(srq, "_tell_managers", lambda *a, **k: None)
    rid = _restaurant(db, SERVERS)
    _publish(db, rid, [(W1[1], "Ana", "Server", "5:00pm", "10:00pm", 5)])
    req = srq.request_drop(rid, "Ana", W1[1], "5:00pm", now=NOW)
    srq.decide(rid, req["id"], False, now=NOW)
    assert seen[-1] == {"data": {"request_id": req["id"]}, "nav": "requests", "purpose": "request",
                        "shift_date": W1[1]}
    monkeypatch.setattr(people, "tell", lambda rid, name, title, lines, **kw: seen.append(kw) or "push")
    srq.decide(rid, srq.request_drop(rid, "Ana", W1[1], "5:00pm", now=NOW)["id"], False, now=NOW)
    assert "data" not in seen[-1], "an older tell is never handed what it can't take"


def test_the_owner_routes_are_web_and_mobile_twins():
    import strategy_routes
    paths = {p for p, _m, _f, _e in strategy_routes._ROUTES}
    assert {"/labor/open-shifts", "/labor/shift-requests/<int:request_id>/offer",
            "/labor/shift-requests/<int:request_id>/colleague-agreed",
            "/labor/shift-requests/<int:request_id>/cancel"} <= paths


# ── H9: shift changes that can't go wrong ──────────────────────────────────

def test_a_shift_that_already_started_cannot_be_dropped_approved_or_claimed(db, told):
    rid = _restaurant(db, SERVERS)
    _publish(db, rid, [(W1[1], "Ana", "Server", "10:00am", "4:00pm", 6), (W1[1], "Ben", "Server", "5:00pm", "10:00pm", 5)])
    on_it = dt.datetime(2026, 10, 6, 11, 0)
    with pytest.raises(srq.ShiftRequestError, match="already started"):
        srq.request_drop(rid, "Ana", W1[1], "10:00am", now=on_it)
    req = srq.request_drop(rid, "Ben", W1[1], "5:00pm", now=NOW)
    with pytest.raises(srq.ShiftRequestError, match="already started"):
        srq.decide(rid, req["id"], True, now=dt.datetime(2026, 10, 6, 17, 30))
    srq.decide(rid, req["id"], True, now=NOW)
    with pytest.raises(srq.ShiftRequestError, match="already started"):
        srq.claim(rid, req["id"], "Cara", now=dt.datetime(2026, 10, 6, 23, 30))


def test_a_swap_for_a_shift_already_started_never_executes_and_expires(db, told):
    rid = _restaurant(db, SERVERS)
    _publish(db, rid, [(W1[1], "Ana", "Server", "11:00am", "3:00pm", 4), (W1[3], "Ben", "Server", "11:00am", "3:00pm", 4)])
    _member(db, rid, "Ben")
    req = srq.request_swap(rid, "Ana", W1[1], "11:00am", "Ben", W1[3], "11:00am", now=NOW)
    srq.decide(rid, req["id"], True, now=NOW)
    later = dt.datetime(2026, 10, 9, 9, 0)
    with pytest.raises(srq.ShiftRequestError):
        srq.respond_swap(rid, req["id"], "Ben", True, now=later)
    assert srq.expire_past(rid, now=later) == 1
    assert {(r["date"], r["employee"]) for r in _csv(db, rid, W1[1])} == {(W1[1], "Ana"), (W1[3], "Ben")}
    assert srq.asked_of_me(rid, "Ben", today=later.date()) == []


def test_a_republish_does_not_let_one_shift_carry_two_requests(db, told):
    rid = _restaurant(db, SERVERS)
    rows = [(W1[1], "Ana", "Server", "5:00pm", "10:00pm", 5)]
    _publish(db, rid, rows)
    srq.request_drop(rid, "Ana", W1[1], "5:00pm", now=NOW)
    _publish(db, rid, rows)                                   # republished: a new history id
    with pytest.raises(srq.ShiftRequestError, match="already asked"):
        srq.request_drop(rid, "Ana", W1[1], "5:00pm", now=NOW)
    conn = models.get_conn(db)
    names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='index'").fetchall()}
    conn.close()
    assert "uq_shift_requests_live" in names


def test_approving_a_drop_whose_shift_moved_is_refused(db, told):
    rid = _restaurant(db, SERVERS)
    _publish(db, rid, [(W1[1], "Ana", "Server", "5:00pm", "10:00pm", 5)])
    req = srq.request_drop(rid, "Ana", W1[1], "5:00pm", now=NOW)
    _publish(db, rid, [(W1[1], "Ana", "Server", "4:00pm", "10:00pm", 6)])      # moved to 4pm
    with pytest.raises(srq.ShiftRequestError, match="no longer on the schedule"):
        srq.decide(rid, req["id"], True, now=NOW)
    assert srq.decide(rid, req["id"], False, now=NOW)["status"] == "denied"


def test_a_cover_cancels_other_live_requests_on_the_shift_it_moved(db, told):
    rid = _restaurant(db, SERVERS)
    hid = _publish(db, rid, [(W1[1], "Ana", "Server", "5:00pm", "10:00pm", 5), (W1[3], "Ben", "Server", "5:00pm", "10:00pm", 5)])
    req = srq.request_drop(rid, "Ana", W1[1], "5:00pm", now=NOW)
    srq.decide(rid, req["id"], True, now=NOW)
    # A swap filed against Ana's shift before the one-request rule (legacy).
    conn = models.get_conn(db)
    cur = conn.execute("INSERT INTO shift_change_requests (restaurant_id, history_id, employee_name, date, shift_start, "
                       "shift_end, role, kind, target_name, target_date, target_start, target_end) VALUES "
                       "(?,?,?,?,?,?,?,?,?,?,?,?)", (rid, hid, "Ben", W1[3], "5:00pm", "10:00pm", "Server", "swap", "Ana",
                                                     W1[1], "5:00pm", "10:00pm"))
    conn.commit()
    conn.close()
    srq.claim(rid, req["id"], "Cara", now=NOW)
    assert _row(db, cur.lastrowid)["status"] == "cancelled"
    assert any(n["to"] == ["Ben"] and "no longer applies" in n["subject"] for n in told["staff"])


def test_a_swap_checks_the_role_both_ways(db, told):
    rid = _restaurant(db, SERVERS)
    _publish(db, rid, [(W1[1], "Bo", "Dishwasher", "5:00pm", "10:00pm", 5), (W1[2], "Ana", "Server", "5:00pm", "10:00pm", 5)])
    with pytest.raises(srq.ShiftRequestError, match="role"):
        srq.request_swap(rid, "Bo", W1[1], "5:00pm", "Ana", W1[2], "5:00pm", now=NOW)


def test_a_colleagues_early_yes_tells_the_requester_and_the_deciders(db, told):
    rid = _restaurant(db, SERVERS)
    _publish(db, rid, [(W1[1], "Ana", "Server", "11:00am", "3:00pm", 4), (W1[3], "Ben", "Server", "11:00am", "3:00pm", 4)])
    req = srq.request_swap(rid, "Ana", W1[1], "11:00am", "Ben", W1[3], "11:00am", now=NOW)
    told["staff"].clear(), told["managers"].clear()
    srq.respond_swap(rid, req["id"], "Ben", True, now=NOW)
    assert any(n["to"] == ["Ana"] and "said yes" in n["subject"] for n in told["staff"])
    assert any(m["req"] and m["req"]["id"] == req["id"] for m in told["managers"])


def test_the_in_person_yes_runs_an_approved_swap(db, told):
    rid = _restaurant(db, SERVERS)
    _publish(db, rid, [(W1[1], "Ana", "Server", "11:00am", "3:00pm", 4), (W1[3], "Carl", "Server", "11:00am", "3:00pm", 4)])
    models.add_manual_team_member(rid, "Carl", role="Server", db_path=db)
    req = srq.request_swap(rid, "Ana", W1[1], "11:00am", "Carl", W1[3], "11:00am", now=NOW)
    assert "isn't on the Cavnar AI app" in told["managers"][-1]["body"]
    srq.decide(rid, req["id"], True, now=NOW)
    out = srq.colleague_agreed(rid, req["id"], "erik", now=NOW)
    assert out["status"] == "covered" and _row(db, req["id"])["target_agreed_by"] == "erik"


def test_withdraw_and_decline_tell_the_people_waiting(db, told):
    rid = _restaurant(db, SERVERS)
    _publish(db, rid, [(W1[1], "Ana", "Server", "5:00pm", "10:00pm", 5), (W1[2], "Ana", "Server", "11:00am", "3:00pm", 4),
                       (W1[3], "Ben", "Server", "11:00am", "3:00pm", 4)])
    req = srq.request_drop(rid, "Ana", W1[1], "5:00pm", now=NOW)
    srq.decide(rid, req["id"], True, now=NOW)
    offered_to = next(n["to"] for n in told["staff"] if n["subject"].startswith("Open shift:"))
    assert offered_to
    told["staff"].clear()
    assert srq.withdraw(rid, req["id"], "Ana")
    gone = next(n for n in told["staff"] if n["subject"].startswith("Open shift gone"))
    # The same people, whatever order: the open-shift notice goes round in
    # turn now (schedule audit 10/3/26 E-3), the "gone" one by name.
    assert sorted(gone["to"]) == sorted(offered_to)
    swap = srq.request_swap(rid, "Ana", W1[2], "11:00am", "Ben", W1[3], "11:00am", now=NOW)
    told["managers"].clear()
    srq.respond_swap(rid, swap["id"], "Ben", False, now=NOW)
    assert any(m["title"] == "Swap declined" for m in told["managers"])


def test_the_open_board_leaves_out_your_own_and_other_roles_and_labels_the_rest(db, told):
    rid = _restaurant(db, SERVERS)
    _publish(db, rid, [(W1[1], "Ana", "Server", "5:00pm", "10:00pm", 5), (W1[1], "Bo", "Dishwasher", "5:00pm", "10:00pm", 5),
                       (W1[1], "Ben", "Server", "11:00am", "4:00pm", 5)])
    req = srq.request_drop(rid, "Ana", W1[1], "5:00pm", now=NOW)
    srq.decide(rid, req["id"], True, now=NOW)
    assert srq.for_staff(rid, "Ana", now=NOW)["open"] == []
    assert srq.for_staff(rid, "Bo", now=NOW)["open"] == []
    cara = srq.for_staff(rid, "Cara", now=NOW)["open"]
    assert [(o["id"], o["can_take"]) for o in cara] == [(req["id"], True)]


def test_the_swap_picker_lists_people_on_the_app_in_the_same_week_and_legal_pairs(db, told, clock):
    rid = _restaurant(db, SERVERS + [("Carl", "Server")])
    _publish(db, rid, [(W1[1], "Ana", "Server", "11:00am", "3:00pm", 4), (W1[3], "Ben", "Server", "11:00am", "3:00pm", 4),
                       (W1[3], "Carl", "Server", "11:00am", "3:00pm", 4), (W1[4], "Bo", "Dishwasher", "11:00am", "3:00pm", 4)])
    _publish(db, rid, [(W2[1], "Ben", "Server", "11:00am", "3:00pm", 4)], dates=W2)
    ana = _member(db, rid, "Ana")
    _member(db, rid, "Ben"), _member(db, rid, "Bo")
    client = _staff_client(db, rid, ana)
    every = client.get("/staff/api/colleagues").get_json()["colleagues"]
    assert {c["name"] for c in every} == {"Ben", "Bo"}, "Carl has no login"
    picked = client.get(f"/staff/api/colleagues?shift_date={W1[1]}&shift_start=11:00am").get_json()["colleagues"]
    assert [(c["name"], [s["date"] for s in c["shifts"]]) for c in picked] == [("Ben", [W1[3]])]


def test_the_sibling_conflict_check_reads_every_overlapping_published_week(db):
    rid = _restaurant(db, SERVERS, location_group="grp")
    sib = _restaurant(db, [("Dana", "Server")], location_group="grp")
    _publish(db, sib, [(W1[2], "Dana", "Server", "4:00pm", "11:00pm", 7)])
    _publish(db, sib, [(W2[2], "Dana", "Server", "4:00pm", "11:00pm", 7)], dates=W2)   # next week, published later
    c = schedule_rules.build_constraints(rid, W1, [_day(d) for d in W1])
    # Read as a row of the tail, checked by overlap and rest — no longer a
    # whole-date block (schedule audit 10/3/26 D-40).
    assert W1[2] in [r["date"] for r in c.base_rows.get("dana") or []]
    clash = {"date": W1[2], "day": _day(W1[2]), "employee": "Dana", "role": "Server", "shift_start": "5:00pm",
             "shift_end": "10:00pm", "scheduled_hours": "5", "notes": ""}
    assert not c.can_add(clash, [])[0]


# ── M6: time off ───────────────────────────────────────────────────────────

def test_approved_time_off_can_be_called_off_before_it_starts(db, told):
    rid = _restaurant(db, SERVERS)
    row, _ = time_off.request_time_off(rid, "Ana", "2026-10-20", "2026-10-22", today=NOW.date())
    time_off.decide(rid, row["id"], True, decided_by=1)
    out, err = time_off.cancel_approved(rid, row["id"], "Ana", today=NOW.date())
    assert err is None and out["status"] == "withdrawn"
    assert any(m["title"] == "Time off cancelled" for m in told["managers"])
    assert time_off.approved_in_window(rid, "2026-10-19", "2026-10-25") == {}
    row2, _ = time_off.request_time_off(rid, "Ana", "2026-10-05", "2026-10-06", today=NOW.date())
    time_off.decide(rid, row2["id"], True, decided_by=1)
    _out, err = time_off.cancel_approved(rid, row2["id"], "Ana", today=dt.date(2026, 10, 5))
    assert err and "already started" in err


def test_the_approval_names_the_shifts_still_scheduled(db, told):
    rid = _restaurant(db, SERVERS)
    _publish(db, rid, [(W1[1], "Ana", "Server", "5:00pm", "10:00pm", 5)])
    row, _ = time_off.request_time_off(rid, "Ana", W1[1], W1[2], today=NOW.date())
    time_off.decide(rid, row["id"], True, decided_by=1)
    notice = next(t for t in told["tell"] if t["to"] == "Ana")
    assert any("still on the published schedule" in ln and "5:00pm" in ln for ln in notice["lines"])
    listed = time_off.mine(rid, "Ana", today=NOW.date())[0]
    assert listed["can_cancel"] is True and [s["shift_start"] for s in listed["still_scheduled"]] == ["5:00pm"]


# ── M7: reasons up, notes down ─────────────────────────────────────────────

def test_the_reason_reaches_the_deciders_and_the_note_reaches_the_employee(db, told):
    rid = _restaurant(db, SERVERS)
    _publish(db, rid, [(W1[1], "Ana", "Server", "5:00pm", "10:00pm", 5)])
    req = srq.request_drop(rid, "Ana", W1[1], "5:00pm", reason="dentist", now=NOW)
    assert "dentist" in told["managers"][-1]["body"]
    time_off.request_time_off(rid, "Ana", "2026-10-20", "2026-10-20", reason="wedding", today=NOW.date())
    assert "wedding" in told["managers"][-1]["body"]
    srq.decide(rid, req["id"], False, note="We're short that night — try a swap", now=NOW)
    denied = next(n for n in told["staff"] if n["subject"] == "Your shift request was declined")
    assert "Their note: We're short that night — try a swap" in denied["lines"]


# ── COM-07 and H7, through the staff routes ────────────────────────────────

def test_the_app_showing_the_week_counts_as_seen(db, told, clock):
    rid = _restaurant(db, SERVERS)
    hid = _publish(db, rid, [(W1[1], "Ana", "Server", "5:00pm", "10:00pm", 5)])
    models.create_schedule_share(rid, hid, "Ana", db_path=db)
    client = _staff_client(db, rid, _member(db, rid, "Ana"))
    body = client.get("/staff/api/shifts").get_json()
    assert body["ok"] and "_week_ids" not in body
    assert models.get_schedule_share_status(rid, hid, db_path=db)[0]["viewed_at"]


def test_whos_on_with_me_lists_that_days_coworkers_and_nothing_else(db, told, clock):
    rid = _restaurant(db, SERVERS)
    _publish(db, rid, [(W1[1], "Ana", "Server", "5:00pm", "10:00pm", 5), (W1[1], "Ben", "Server", "4:00pm", "11:00pm", 7),
                       (W1[1], "Bo", "Dishwasher", "5:00pm", "11:00pm", 6), (W1[2], "Cara", "Server", "5:00pm", "10:00pm", 5)])
    client = _staff_client(db, rid, _member(db, rid, "Ana"))
    out = client.get(f"/staff/api/colleagues?date={W1[1]}").get_json()
    assert out["date"] == W1[1] and out["posted"] is True
    assert [c["name"] for c in out["coworkers"]] == ["Ben", "Bo"]
    assert set(out["coworkers"][0]) == {"name", "role", "station", "shift_start", "shift_end"}
    assert client.get("/staff/api/colleagues?date=2026-11-30").get_json()["posted"] is False
