"""The manager rule after a week is published (blind re-audit 10/26/26, lens
B2 — the owner's highest rule: no minute on the floor without a manager).

  * a drop whose rules can't be read is never approved — not by a manager,
    not by the automatic approval (it failed OPEN);
  * time off, an availability change, a deactivation, a floor-manager flag
    cleared and a manager's availability save are each judged against the
    live week before they are written: refused with the gap, or let through
    only with who runs the floor named (the drop rule's own override);
  * the hourly backstop tells the managers of a stretch today or tomorrow
    with no manager on, once;
  * support (an admin, view-as) can't clear a gap by naming themselves.

Every week here is in the restaurant's own future (time_utils), so the
tests hold whatever the clock and the zone (TZ=Pacific/Kiritimati too).
"""
import json
import sys
from datetime import datetime, time, timedelta

import pytest
from flask import Flask

import auth
import availability_changes
import client_api
import models
import request_autonomy
import schedule_rules
import shift_requests as srq
import staff_settings as ss
import time_off
from models import Restaurant, create_restaurant
from time_utils import restaurant_now_by_id

HEAD = "date,day,employee,role,shift_start,shift_end,scheduled_hours,notes\n"


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
    return db_path


@pytest.fixture
def told(monkeypatch):
    out = []
    # Only what the manager rule says: the "time off asked" notices are not it.
    monkeypatch.setattr(srq, "_tell_managers", lambda rid, title, body, *a, **k:
                        out.append((title, body)) if title == "No manager on" else None)
    monkeypatch.setattr(srq, "_email_staff", lambda *a, **k: 0)
    monkeypatch.setattr(srq, "_broadcast_open", lambda *a, **k: None)
    monkeypatch.setattr(time_off, "_tell_requester", lambda *a, **k: None)
    monkeypatch.setattr(availability_changes, "_tell", lambda *a, **k: None)
    return out


def _restaurant(db_path):
    rid = create_restaurant(Restaurant(name="Floor Rule Grill", owner_email="fr@x.test", timezone="America/Chicago"),
                            db_path=db_path)
    models.update_restaurant(rid, {"module_labor": 1}, db_path=db_path)
    for name, role in (("Ana", "Server"), ("Max", "Manager"), ("Erik", "Manager")):
        models.add_manual_team_member(rid, name, role=role, db_path=db_path)
    ss.upsert(rid, "Max", floor_manager=True, db_path=db_path)
    return rid


def _today(rid):
    return restaurant_now_by_id(rid).date()


def _next_week(rid):
    """The seven dates from the Monday after the restaurant's today."""
    t = _today(rid)
    monday = t + timedelta(days=(7 - t.weekday()) or 7)
    return [(monday + timedelta(days=i)).isoformat() for i in range(7)]


def _publish(db_path, rid, week, rows):
    text = HEAD + "\n".join(
        f"{d},{datetime.strptime(d, '%Y-%m-%d').strftime('%A')},{n},{role},{s},{e},{h}," for d, n, role, s, e, h in rows)
    hid = models.save_schedule_history(rid, week[0], week[-1], 0, 0, 30, text, [], db_path=db_path)
    conn = models.get_conn(db_path)
    conn.execute("UPDATE schedule_history SET published_at=datetime('now'), published_csv=schedule_csv WHERE id=?",
                 (hid,))
    conn.commit()
    conn.close()
    return hid


def _saturday_week(db_path, rid):
    """Saturday dinner with Max the only manager on, beside Ana."""
    week = _next_week(rid)
    sat = week[5]
    _publish(db_path, rid, week, [(sat, "Ana", "Server", "4:00pm", "11:00pm", 7),
                                  (sat, "Max", "Manager", "4:00pm", "11:00pm", 7)])
    return week, sat


# ── P1: a drop whose rules can't be read fails CLOSED ─────────────────────

def test_a_drop_the_rules_cant_judge_is_never_approved_by_a_manager_or_automatically(db, told, monkeypatch):
    rid = _restaurant(db)
    _week, sat = _saturday_week(db, rid)
    today = _today(rid)
    ask = srq.request_drop(rid, "Max", sat, "4:00pm", reason="wedding", db_path=db, today=today)
    assert ask["status"] == "pending"

    def broken(*a, **k):
        raise RuntimeError("simulated rule-engine failure")
    monkeypatch.setattr(schedule_rules, "judge_change", broken)
    # The decider is told it couldn't be checked — never nothing.
    assert "couldn't check" in srq.drop_manager_gap(rid, ask, db)
    with pytest.raises(srq.ShiftRequestError) as refused:
        srq.decide(rid, ask["id"], True, decided_by="will", db_path=db, today=today)
    assert not isinstance(refused.value, srq.ManagerGapRefused)
    assert "couldn't check the rules" in str(refused.value)
    # Even with who runs the floor named: an unread rule is not a checked one.
    with pytest.raises(srq.ShiftRequestError):
        srq.decide(rid, ask["id"], True, decided_by="will", db_path=db, today=today, covered_by="Erik")
    assert srq._get(models.get_conn(db), ask["id"])["status"] == "pending"
    # The automatic approval, earned and on: it refuses and leaves it pending.
    monkeypatch.setattr(request_autonomy, "policy_for", lambda *a, **k: dict(
        request_autonomy.DEFAULTS, auto_approve=True, kinds=["drop", "swap"], notice_hours=0, max_quality_drop=100))
    monkeypatch.setattr(request_autonomy, "trust", lambda *a, **k: {"earned": True, "approvals": 9, "needed": 3})
    verdict = request_autonomy.judge(rid, ask, now=datetime.combine(today, time(9, 0)), db_path=db)
    assert verdict["ok"] is False and any("couldn't" in r for r in verdict["refusals"]), verdict
    assert request_autonomy.try_auto(rid, ask, now=datetime.combine(today, time(9, 0)), db_path=db) is None
    assert srq._get(models.get_conn(db), ask["id"])["status"] == "pending"
    rows = models.get_conn(db).execute("SELECT error FROM job_failures WHERE job='shift_request_rules'").fetchall()
    assert any("simulated rule-engine failure" in (r[0] or "") for r in rows), "captured, never only printed"


# ── P1: time off over a published shift ───────────────────────────────────

def test_approving_the_only_managers_time_off_is_refused_without_who_runs_the_floor(db, told):
    rid = _restaurant(db)
    _week, sat = _saturday_week(db, rid)
    row, err = time_off.request_time_off(rid, "Max", sat, sat, db_path=db, today=_today(rid))
    assert not err
    with pytest.raises(srq.ManagerGapRefused) as refused:
        time_off.decide(rid, row["id"], True, decided_by=1, db_path=db)
    gap = refused.value.gap
    assert "no manager on the published schedule" in gap and "4:00pm to 11:00pm" in gap
    assert time_off.pending(rid, db_path=db)[0]["status"] == "pending"
    assert not told
    # With who runs the floor named it goes, and the managers are told.
    out = time_off.decide(rid, row["id"], True, decided_by=1, db_path=db, covered_by="Erik")
    assert out["status"] == "approved" and out["floor_covered_by"] == "Erik"
    assert "Erik runs the floor instead" in out["manager_gap"]
    assert told and told[-1][0] == "No manager on" and "Erik runs the floor" in told[-1][1]


def test_a_servers_time_off_or_a_manager_with_a_second_on_needs_nothing(db, told):
    rid = _restaurant(db)
    week = _next_week(rid)
    sat = week[5]
    _publish(db, rid, week, [(sat, "Ana", "Server", "4:00pm", "11:00pm", 7),
                             (sat, "Max", "Manager", "4:00pm", "11:00pm", 7),
                             (sat, "Erik", "Manager", "4:00pm", "11:00pm", 7)])
    for name in ("Ana", "Max"):
        row, _e = time_off.request_time_off(rid, name, sat, sat, db_path=db, today=_today(rid))
        out = time_off.decide(rid, row["id"], True, db_path=db)
        assert out["status"] == "approved" and not out.get("manager_gap")
    assert not told


def test_time_off_the_rules_cant_judge_is_refused(db, told, monkeypatch):
    rid = _restaurant(db)
    _week, sat = _saturday_week(db, rid)
    row, _e = time_off.request_time_off(rid, "Ana", sat, sat, db_path=db, today=_today(rid))

    def broken(*a, **k):
        raise RuntimeError("live weeks unreadable")
    monkeypatch.setattr(srq, "live_manager_gaps", broken)
    with pytest.raises(srq.ShiftRequestError) as refused:
        time_off.decide(rid, row["id"], True, db_path=db, covered_by="Erik")
    assert "couldn't check the manager rule" in str(refused.value)
    assert time_off.pending(rid, db_path=db)[0]["status"] == "pending"


# ── P1: an availability change approved into the live week ──────────────

def test_an_availability_cut_moved_into_the_live_week_is_judged_by_the_manager_rule(db, told):
    rid = _restaurant(db)
    week, sat = _saturday_week(db, rid)
    ch = availability_changes.record(rid, "Max", ["Saturday"], {"Saturday"}, {}, {}, today=_today(rid), db_path=db,
                                     approval=True)
    with pytest.raises(srq.ManagerGapRefused) as refused:
        availability_changes.decide(rid, ch["id"], True, decided_by="will", effective_from=week[0], db_path=db,
                                    today=_today(rid))
    assert "4:00pm to 11:00pm" in refused.value.gap
    assert availability_changes.pending(rid, db_path=db)[0]["id"] == ch["id"]
    out = availability_changes.decide(rid, ch["id"], True, decided_by="will", effective_from=week[0], db_path=db,
                                      today=_today(rid), covered_by="Erik")
    assert out["status"] == "approved" and "Erik runs the floor instead" in out["manager_gap"]
    assert told[-1][0] == "No manager on"


def test_a_cut_the_restaurant_needs_no_approval_for_still_goes_to_a_manager_when_it_leaves_no_manager(db, told):
    rid = _restaurant(db)
    week, sat = _saturday_week(db, rid)
    planned = availability_changes.effective_default(rid, today=_today(rid), db_path=db)
    # The cut starts after the live week (MARKET-15): it leaves it as it is.
    assert planned > week[-1]
    assert not availability_changes.leaves_no_manager(rid, "Max", ["Saturday"], {"Saturday"}, {}, {},
                                                      today=_today(rid), db_path=db)


# ── P1: who counts as the manager changed on the roster ──────────────────

@pytest.fixture
def client(db, monkeypatch):
    from strategy_routes import strategy_bp
    app = Flask(__name__, template_folder="../templates")
    app.register_blueprint(strategy_bp)
    return app.test_client()


def _as(monkeypatch, rid, **extra):
    user = dict({"id": 5, "restaurant_id": rid, "is_admin": 0, "role": "client", "username": "owner",
                 "email": "o@x.test"}, **extra)
    monkeypatch.setattr(auth, "get_current_user", lambda: user)
    return user


@pytest.mark.parametrize("patch", [{"active": False}, {"floor_manager": False},
                                   {"daypart_availability": {"Saturday": "off"}}])
def test_taking_the_only_manager_off_the_floor_on_a_live_week_needs_who_runs_it(db, told, client, monkeypatch,
                                                                                patch):
    rid = _restaurant(db)
    _saturday_week(db, rid)
    _as(monkeypatch, rid)
    body = dict({"employee_name": "Max"}, **patch)
    r = client.post("/api/labor/staff-settings", json=body)
    assert r.status_code == 409, r.get_json()
    got = r.get_json()
    assert got["needs_cover"] and "4:00pm to 11:00pm" in got["manager_gap"]
    st = {e["name"]: e for e in ss.roster(rid, db_path=db, include_inactive=True)}["Max"]
    assert st["active"] and st["settings"].get("floor_manager") is True and not st["settings"].get(
        "daypart_availability"), "nothing saved"
    r = client.post("/api/labor/staff-settings", json=dict(body, covered_by="Erik"))
    assert r.status_code == 200 and "Erik runs the floor instead" in r.get_json()["manager_gap"]
    assert told[-1][0] == "No manager on"


def test_a_settings_save_that_leaves_the_floor_managed_is_saved_as_before(db, told, client, monkeypatch):
    rid = _restaurant(db)
    _saturday_week(db, rid)
    _as(monkeypatch, rid)
    for body in ({"employee_name": "Erik", "active": False}, {"employee_name": "Ana", "daypart_availability":
                                                               {"Saturday": "off"}},
                 {"employee_name": "Max", "max_hours": 45}):
        r = client.post("/api/labor/staff-settings", json=body)
        assert r.status_code == 200 and not r.get_json().get("manager_gap"), (body, r.get_json())
    assert not told


def test_a_managers_availability_save_blocking_a_published_day_needs_who_runs_the_floor(db, told, monkeypatch):
    rid = _restaurant(db)
    _saturday_week(db, rid)
    app = Flask(__name__, template_folder="../templates")
    app.register_blueprint(client_api.client_bp)
    _as(monkeypatch, rid)
    days = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Sunday"]
    c = app.test_client()
    r = c.post("/api/labor/availability", json={"employee_name": "Max", "available_days": days,
                                               "unavailable_days": ["Saturday"]})
    assert r.status_code == 409, r.get_data(as_text=True)[:300]
    assert r.get_json()["needs_cover"] and not models.get_staff_availability(rid, db_path=db)
    r = c.post("/api/labor/availability", json={"employee_name": "Max", "available_days": days,
                                               "unavailable_days": ["Saturday"], "covered_by": "Erik"})
    assert r.status_code == 200 and "Erik runs the floor instead" in r.get_json()["manager_gap"]


# ── support can't run the floor ──────────────────────────────────────────

def test_support_acting_as_the_restaurant_cannot_cover_a_gap_by_naming_themselves(db, told, client, monkeypatch):
    rid = _restaurant(db)
    _week, sat = _saturday_week(db, rid)
    row, _e = time_off.request_time_off(rid, "Max", sat, sat, db_path=db, today=_today(rid))
    _as(monkeypatch, rid, acting_admin_id=99, acting_admin_role="admin")
    r = client.post(f"/api/labor/time-off/{row['id']}/decide", json={"decision": "approve", "covered_by": "me"})
    assert r.status_code == 400 and "support can't cover" in r.get_json()["error"]
    assert time_off.pending(rid, db_path=db)[0]["status"] == "pending"
    # Naming a manager on the roster works for them as for anyone.
    r = client.post(f"/api/labor/time-off/{row['id']}/decide", json={"decision": "approve", "covered_by": "Erik"})
    assert r.status_code == 200 and json.dumps(r.get_json()).count("Erik runs the floor") >= 1


def test_the_publish_gate_takes_me_from_the_account_owner_only():
    import schedule_publish as sp
    b = {"key": "rule:no_manager:2026-11-07:960", "date": "2026-11-07", "gap": [960, 1380],
         "cover_candidates": ["Erik"]}
    reasons = {b["key"]: {"covered_by": "me"}}
    owner = {"id": 1, "role": "client", "username": "will", "is_admin": 0}
    support = dict(owner, acting_admin_id=7, acting_admin_role="admin")
    import permissions
    orig = permissions.answer_authority
    try:
        permissions.answer_authority = lambda u: "admin" if (u or {}).get("acting_admin_id") else "principal"
        assert sp.override_problem(b, reasons, actor=owner) is None
        assert "support can't cover" in sp.override_problem(b, reasons, actor=support)
        assert "support can't cover" in sp.override_problem(b, {b["key"]: {"covered_by": "Owner"}}, actor=support)
        assert sp.override_problem(b, {b["key"]: {"covered_by": "Erik"}}, actor=support) is None
    finally:
        permissions.answer_authority = orig


# ── the hourly backstop ──────────────────────────────────────────────────

def test_the_hourly_watch_tells_the_managers_of_a_stretch_with_no_manager_once(db, told):
    rid = _restaurant(db)
    today = _today(rid)
    tomorrow = (today + timedelta(days=1)).isoformat()
    week = [(today + timedelta(days=i)).isoformat() for i in range(7)]
    _publish(db, rid, week, [(tomorrow, "Ana", "Server", "4:00pm", "11:00pm", 7),
                             (tomorrow, "Max", "Manager", "4:00pm", "10:00pm", 6)])
    nine = datetime.combine(today, time(9, 0))
    assert srq.manager_watch(rid, db_path=db, now=nine) == 0       # the closing manager's hour: not a gap
    # Max is taken off the floor by something nothing judged (a role change).
    ss.upsert(rid, "Max", floor_manager=False, db_path=db)
    assert srq.manager_watch(rid, db_path=db, now=nine) == 1
    title, body = told[-1]
    assert title == "No manager on" and "4:00pm to 11:00pm" in body and "say who runs the floor" in body
    # Once per stretch.
    assert srq.manager_watch(rid, db_path=db, now=nine) == 0
    assert len(told) == 1


# ── P2: a manager named to cover a gap at Send is judged like any shift ───

def _draft_with_a_gap(db_path, rid, extra=()):
    """A draft whose Saturday has Ana 2pm-10pm and Max managing only to 5pm:
    5pm to 10pm (or to Erik's start) with no manager on."""
    week = _next_week(rid)
    sat = week[5]
    rows = [(sat, "Ana", "Server", "2:00pm", "10:00pm", 8), (sat, "Max", "Manager", "2:00pm", "5:00pm", 3)]
    rows += [(sat,) + tuple(x) for x in extra]
    text = HEAD + "\n".join(
        f"{d},{datetime.strptime(d, '%Y-%m-%d').strftime('%A')},{n},{role},{s},{e},{h}," for d, n, role, s, e, h in rows)
    hid = models.save_schedule_history(rid, week[0], week[-1], 0, 0, 30, text, [], db_path=db_path)
    return hid, sat


def _send_naming(rid, hid, name, monkeypatch):
    import emails
    import schedule_publish as sp
    from tests.publish_overrides import overrides_for
    monkeypatch.setattr(emails, "send_staff_schedule_email", lambda **kw: emails.SendResult(True))
    reasons = overrides_for(rid, hid)
    gaps = [k for k in reasons if k.startswith("rule:no_manager:")]
    assert gaps, reasons
    for k in gaps:
        reasons[k]["covered_by"] = name
    return sp.publish(rid, hid, actor={"username": "owner", "role": "client", "id": 5}, acknowledge=True,
                      override_reasons=reasons)


def test_a_cover_touching_the_named_managers_own_shift_extends_it_and_never_overlaps(db, told, monkeypatch):
    rid = _restaurant(db)
    hid, sat = _draft_with_a_gap(db, rid, extra=[("Erik", "Manager", "9:00pm", "11:00pm", 2)])
    payload, status = _send_naming(rid, hid, "Erik", monkeypatch)
    assert status == 200, payload
    import schedule_shifts
    live = [r for r in schedule_shifts.rows_of(hid, "live") if r["employee"] == "Erik" and r["date"] == sat]
    assert [(r["shift_start"], r["shift_end"]) for r in live] == [("5:00pm", "11:00pm")], live
    assert "manager gap covered at Send" in live[0]["notes"]


def test_a_cover_the_named_manager_cant_work_refuses_the_send(db, told, monkeypatch):
    rid = _restaurant(db)
    six = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Sunday"]
    models.save_staff_availability(rid, "Erik", available_days=six, unavailable_days=["Saturday"], db_path=db)
    hid, sat = _draft_with_a_gap(db, rid)
    payload, status = _send_naming(rid, hid, "Erik", monkeypatch)
    assert status == 409 and payload.get("cover_refused"), payload
    assert "Erik can't run the floor" in payload["error"] and "5:00pm" in payload["error"]
    conn = models.get_conn(db)
    assert conn.execute("SELECT published_at FROM schedule_history WHERE id=?", (hid,)).fetchone()[0] is None
    conn.close()
    # The claim was given back: the owner can send it naming Max instead.
    payload, status = _send_naming(rid, hid, "Max", monkeypatch)
    assert status == 200, payload


def test_a_cover_into_overtime_is_refused_while_another_manager_has_the_room(db, told, monkeypatch):
    import schedule_publish as sp
    rid = _restaurant(db)
    hid, sat = _draft_with_a_gap(db, rid)
    cover = {"date": sat, "start": 17 * 60, "end": 22 * 60, "name": "Erik", "candidates": ["Erik", "Max"]}
    real = schedule_rules.overtime_added
    monkeypatch.setattr(schedule_rules, "overtime_added",
                        lambda c, name, row, rows, line=None: 3.0 if name == "Erik" else real(c, name, row, rows, line))
    got = sp.cover_refusal(rid, hid, [cover])
    assert got and got["cover_refused"] and "3h into overtime while Max can cover it" in got["error"], got
    # Nobody else could: the owner's named cover stands, overtime and all.
    assert sp.cover_refusal(rid, hid, [dict(cover, candidates=["Erik"])]) is None


def test_the_hourly_job_runs_the_backstop_for_a_restaurant_with_no_requests(db, told):
    rid = _restaurant(db)
    today = _today(rid)
    tomorrow = (today + timedelta(days=1)).isoformat()
    _publish(db, rid, [(today + timedelta(days=i)).isoformat() for i in range(7)],
             [(tomorrow, "Ana", "Server", "4:00pm", "11:00pm", 7), (tomorrow, "Max", "Manager", "4:00pm", "10:00pm", 6)])
    ss.upsert(rid, "Max", floor_manager=False, db_path=db)
    out = srq.run_open_shift_watch(db_path=db)
    assert out["failed"] == 0 and out["manager_gaps"] == 1, out
    assert told and told[-1][0] == "No manager on"
