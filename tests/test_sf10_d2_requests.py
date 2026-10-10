"""Schedule fix round 10/10/26, workstream D2: staff requests that never break
the floor.

BUGSLIFE-1  a swap is judged as ONE change: it can never leave the floor
            without a manager (the owner's highest rule) or a closer.
ARCH-5      a swap or a claim builds its rules with no write lock held, then
            compares-and-swaps the week inside a short transaction.
ARCH-14     one legality engine for replacement checks (the rule sweep).
BUGSLIFE-13 approving a non-manager drop warns on every hard rule about who
            is on (a role under its floor, nobody to close).
BUGSLIFE-14 a claim into overtime while a same-role teammate has room waits on
            a decider.
BUGSLIFE-15 decisions that rewrite the live week and message staff need
            SCHEDULE_PUBLISH.
BUGSLIFE-5  approved time off over a published shift: a persistent follow-up,
            and excused (never a no-show) in attendance.
BUGSLIFE-20 a worked lunch and a missed dinner: the dinner is a no-show.
MARKET-14   drops and swaps that approve themselves by rule, once earned, and
            lapse on a reversal.
"""
import datetime as dt
import json
import sys

import pytest

import activity, covers, decisions, delayed, demand_signals, goals, issues, metrics, outcomes  # noqa: E401,F401
import push, schedule_economics, schedule_intel, schedule_rules, schedule_versions  # noqa: E401,F401
import shift_requests, shift_quality, staff_schedule, staff_settings, strategy_jobs, time_off  # noqa: E401,F401
import attendance, automation_trust, request_autonomy, action_queue  # noqa: E401,F401

import models
import schedule_engine as se
import schedule_rules as sr
import schedule_versions as sv
import shift_requests as srq
import staff_settings as ss
from models import create_restaurant, Restaurant

HEADER = "date,day,employee,role,shift_start,shift_end,scheduled_hours,notes"
W1 = ["2026-10-05", "2026-10-06", "2026-10-07", "2026-10-08", "2026-10-09", "2026-10-10", "2026-10-11"]
TODAY = dt.date(2026, 10, 1)


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
    import time_utils
    monkeypatch.setattr(time_utils, "restaurant_now_by_id",
                        lambda *a, **k: dt.datetime.combine(TODAY, dt.time(9, 0)))
    # Nobody is reached in a test.
    monkeypatch.setattr(srq, "_tell_staff", lambda *a, **k: [])
    monkeypatch.setattr(srq, "_tell_managers", lambda *a, **k: None)
    return db_path


def _day(d):
    return dt.date.fromisoformat(d).strftime("%A")


def _restaurant(db_path, roster, **cols):
    rid = create_restaurant(Restaurant(name="D2 Co", owner_email="d2@x.com"), db_path=db_path)
    if cols:
        conn = models.get_conn(db_path)
        conn.execute("UPDATE restaurants SET " + ", ".join(f"{k}=?" for k in cols) + " WHERE id=?",
                     (*cols.values(), rid))
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


def _csv_rows(db_path, hid):
    conn = models.get_conn(db_path)
    try:
        return sv.rows_from_csv(conn.execute("SELECT schedule_csv FROM schedule_history WHERE id=?", (hid,)).fetchone()[0])
    finally:
        conn.close()


# ── BUGSLIFE-1: a swap can never leave the floor without a manager ─────────

def _mia_and_ben(db):
    rid = _restaurant(db, [("Mia", "Server"), ("Ben", "Server"), ("Sam", "Server"), ("Max", "Manager")])
    ss.upsert(rid, "Mia", floor_manager=True)
    ss.upsert(rid, "Max", floor_manager=True)
    # Saturday dinner: Mia manages, Sam serves. Tuesday dinner: Max manages,
    # Ben serves.
    hid = _publish(db, rid, [(W1[5], "Mia", "Server", "5:00pm", "11:00pm", 6),
                             (W1[5], "Sam", "Server", "5:00pm", "11:00pm", 6),
                             (W1[1], "Ben", "Server", "5:00pm", "11:00pm", 6),
                             (W1[1], "Max", "Manager", "5:00pm", "11:00pm", 6)])
    return rid, hid


def test_a_swap_that_takes_the_only_manager_off_saturday_is_refused(db):
    rid, hid = _mia_and_ben(db)
    req = {"employee_name": "Mia", "date": W1[5], "shift_start": "5:00pm",
           "target_name": "Ben", "target_date": W1[1], "target_start": "5:00pm"}
    with pytest.raises(srq.ShiftRequestError) as e:
        srq._swap_legal(rid, req, db)
    assert "manager" in str(e.value)
    # Asked for, it is refused before the colleague is ever asked.
    with pytest.raises(srq.ShiftRequestError):
        srq.request_swap(rid, "Mia", W1[5], "5:00pm", "Ben", W1[1], "5:00pm", today=TODAY)
    # And the week never moved.
    assert {(r["date"], r["employee"]) for r in _csv_rows(db, hid)} >= {(W1[5], "Mia"), (W1[1], "Ben")}


def test_the_swap_is_judged_like_the_one_sided_cover_of_the_same_shift(db):
    rid, _hid = _mia_and_ben(db)
    rows = sv.rows_from_csv(HEADER + "\n" + "\n".join(
        f"{d},{_day(d)},{n},Server,5:00pm,11:00pm,6," for d, n in ((W1[5], "Mia"), (W1[5], "Sam"))))
    ok, why = se.replacement_is_legal(rid, rows, 0, "Ben")
    assert not ok and "manager" in why


def test_a_swap_between_two_people_who_both_manage_still_goes_through(db):
    rid, hid = _mia_and_ben(db)
    ss.upsert(rid, "Ben", floor_manager=True)
    req = {"employee_name": "Mia", "date": W1[5], "shift_start": "5:00pm",
           "target_name": "Ben", "target_date": W1[1], "target_start": "5:00pm"}
    assert srq._swap_legal(rid, req, db) is not None


# ── ARCH-5: the rules are built with no write lock held ─────────────────────

def test_a_swap_builds_its_rules_before_taking_the_write_lock(db, monkeypatch):
    rid = _restaurant(db, [("Ana", "Server"), ("Ben", "Server")])
    hid = _publish(db, rid, [(W1[1], "Ana", "Server", "11:00am", "3:00pm", 4),
                             (W1[3], "Ben", "Server", "11:00am", "3:00pm", 4)])
    req = srq.request_swap(rid, "Ana", W1[1], "11:00am", "Ben", W1[3], "11:00am", today=TODAY)
    srq.respond_swap(rid, req["id"], "Ben", True, today=TODAY)
    real_build = sr.build_constraints
    seen = []

    def watch(*a, **k):
        # A second connection may still take the write lock: nobody holds it.
        probe = models.get_conn(db)
        try:
            probe.execute("BEGIN IMMEDIATE")
            probe.rollback()
            seen.append("free")
        except Exception:
            seen.append("held")
        finally:
            probe.close()
        return real_build(*a, **k)
    monkeypatch.setattr(sr, "build_constraints", watch)
    out = srq.decide(rid, req["id"], True, decided_by="mgr", today=TODAY)
    assert out["status"] == "covered"
    assert seen and set(seen) == {"free"}, seen
    assert {(r["date"], r["employee"]) for r in _csv_rows(db, hid)} == {(W1[1], "Ben"), (W1[3], "Ana")}


def test_a_week_that_moves_between_the_check_and_the_write_is_judged_again(db, monkeypatch):
    rid = _restaurant(db, [("Ana", "Server"), ("Ben", "Server"), ("Cara", "Server")])
    hid = _publish(db, rid, [(W1[2], "Ana", "Server", "11:00am", "3:00pm", 4),
                             (W1[2], "Ben", "Server", "5:00pm", "9:00pm", 4)])
    req = srq.decide(rid, srq.request_drop(rid, "Ana", W1[2], "11:00am", today=TODAY)["id"], True,
                     decided_by="mgr", today=TODAY)
    real_judge = srq._judge
    judged = []

    def sneak(*a, **k):
        out = real_judge(*a, **k)
        judged.append(1)
        if len(judged) == 1:
            # Somebody saves the week right after the check read it.
            conn = models.get_conn(db)
            text = conn.execute("SELECT schedule_csv FROM schedule_history WHERE id=?", (hid,)).fetchone()[0]
            conn.execute("UPDATE schedule_history SET schedule_csv=? WHERE id=?", (text.replace("9:00pm", "10:00pm"), hid))
            conn.commit()
            conn.close()
        return out
    monkeypatch.setattr(srq, "_judge", sneak)
    assert srq.claim(rid, req["id"], "Cara")["status"] == "covered"
    assert len(judged) == 2, "the moved week is judged again, never written back stale"
    rows = {(r["employee"], r["shift_end"]) for r in _csv_rows(db, hid)}
    assert ("Ben", "10:00pm") in rows and ("Cara", "3:00pm") in rows


# ── ARCH-14: one legality engine ────────────────────────────────────────────

def test_the_replacement_check_never_asks_shift_quality_s_swap_index(db, monkeypatch):
    rid = _restaurant(db, [("Ana", "Server"), ("Ben", "Server")])
    rows = sv.rows_from_csv(HEADER + f"\n{W1[2]},{_day(W1[2])},Ana,Server,11:00am,3:00pm,4,")

    class Boom:
        def __init__(self, *a, **k):
            raise AssertionError("the swap index is not a legality judge")
    monkeypatch.setattr(shift_quality, "_SwapIndex", Boom)
    assert se.replacement_is_legal(rid, rows, 0, "Ben") == (True, "")


def test_a_pending_time_off_request_still_refuses_the_taker(db):
    rid = _restaurant(db, [("Ana", "Server"), ("Ben", "Server")])
    conn = models.get_conn(db)
    conn.execute("INSERT INTO staff_time_off (restaurant_id, employee_name, start_date, end_date, status) "
                 "VALUES (?,?,?,?, 'pending')", (rid, "Ben", W1[2], W1[2]))
    conn.commit()
    conn.close()
    rows = sv.rows_from_csv(HEADER + f"\n{W1[2]},{_day(W1[2])},Ana,Server,11:00am,3:00pm,4,")
    ok, why = se.replacement_is_legal(rid, rows, 0, "Ben")
    assert not ok and why == sr.PENDING_OFF_LABEL


# ── BUGSLIFE-13: a drop that leaves a role under its floor says so ──────────

def test_approving_the_only_cooks_drop_warns_the_floor_goes_short(db):
    rid = _restaurant(db, [("Cole", "Cook"), ("Ana", "Server")],
                      role_floors_json=json.dumps({"Cook": {"morning": 1, "night": 1}}))
    _publish(db, rid, [(W1[6], "Cole", "Cook", "10:00am", "3:00pm", 5),
                       (W1[6], "Ana", "Server", "10:00am", "3:00pm", 5)])
    ask = srq.request_drop(rid, "Cole", W1[6], "10:00am", today=TODAY)
    out = srq.decide(rid, ask["id"], True, decided_by="mgr", today=TODAY)
    assert out["status"] == "open"
    assert out.get("drop_warning") and "Sunday" in out["drop_warning"] and "10/11/26" in out["drop_warning"]
    assert not out.get("manager_gap")


def test_the_decide_route_says_the_warning_with_the_answer(db, monkeypatch):
    import strategy_routes
    rid = _restaurant(db, [("Cole", "Cook"), ("Ana", "Server")],
                      role_floors_json=json.dumps({"Cook": {"morning": 1}}))
    _publish(db, rid, [(W1[6], "Cole", "Cook", "10:00am", "3:00pm", 5),
                       (W1[6], "Ana", "Server", "10:00am", "3:00pm", 5)])
    ask = srq.request_drop(rid, "Cole", W1[6], "10:00am", today=TODAY)
    monkeypatch.setattr(strategy_routes, "_body", lambda: {"decision": "approve"})
    monkeypatch.setattr(strategy_routes, "_today", lambda *a, **k: TODAY, raising=False)
    import client_api
    monkeypatch.setattr(client_api, "log_account_event", lambda *a, **k: None)
    out, code = strategy_routes._do_shift_request_decide({"is_admin": True, "restaurant_id": rid, "id": 1}, ask["id"])
    assert code == 200 and out.get("warning") and "Sunday" in out["warning"]


# ── BUGSLIFE-14: a claim into overtime while a teammate has room waits ──────

def _ot_week(db):
    rid = _restaurant(db, [("Ana", "Server"), ("Maria", "Server"), ("Ben", "Server"), ("Cara", "Server")])
    ss.upsert(rid, "Maria", max_hours=50)
    rows = [(W1[2], "Ana", "Server", "5:00pm", "11:00pm", 6)]
    # Maria at 38h across the week; Ben at 24h; Cara at 24h.
    for d in W1[0:2] + W1[3:6]:
        rows.append((d, "Maria", "Server", "10:00am", "5:36pm", 7.6))
    for d in W1[0:2] + W1[3:5]:
        rows.append((d, "Ben", "Server", "10:00am", "4:00pm", 6))
        rows.append((d, "Cara", "Server", "10:00am", "4:00pm", 6))
    hid = _publish(db, rid, rows)
    req = srq.decide(rid, srq.request_drop(rid, "Ana", W1[2], "5:00pm", today=TODAY)["id"], True,
                     decided_by="mgr", today=TODAY)
    return rid, hid, req


def test_a_claim_into_overtime_while_a_teammate_has_room_is_held(db):
    rid, hid, req = _ot_week(db)
    out = srq.claim(rid, req["id"], "Maria")
    assert out.get("held") and out["status"] == "open"
    assert out["held_claimant"] == "Maria" and "overtime" in out["held_claim_note"]
    assert ("Ana" in {r["employee"] for r in _csv_rows(db, hid) if r["date"] == W1[2]})
    # A teammate with room still takes it, and the hold ends with it.
    covered = srq.claim(rid, req["id"], "Ben")
    assert covered["status"] == "covered" and covered["replacement_name"] == "Ben"


def test_a_decider_can_give_a_held_claim_overtime_and_all(db):
    rid, hid, req = _ot_week(db)
    srq.claim(rid, req["id"], "Maria")
    out = srq.decide(rid, req["id"], True, decided_by="mgr", today=TODAY)
    assert out["status"] == "covered" and out["replacement_name"] == "Maria"


def test_a_decider_can_say_no_to_a_held_claim(db):
    rid, hid, req = _ot_week(db)
    srq.claim(rid, req["id"], "Maria")
    out = srq.decide(rid, req["id"], False, decided_by="mgr", today=TODAY)
    assert out["status"] == "open" and not out.get("held_claimant")


def test_overtime_is_fine_when_nobody_else_has_room(db):
    rid, hid, req = _ot_week(db)
    ss.upsert(rid, "Ben", active=False)
    ss.upsert(rid, "Cara", active=False)
    out = srq.claim(rid, req["id"], "Maria")
    assert out["status"] == "covered" and out["replacement_name"] == "Maria"


# ── BUGSLIFE-15: decisions need SCHEDULE_PUBLISH ────────────────────────────

def test_a_drafter_cannot_decide_post_offer_or_answer_time_off(db, monkeypatch):
    import strategy_routes
    member = {"role": "member", "restaurant_id": 1, "id": 9}
    monkeypatch.setattr(strategy_routes, "_body", lambda: {"decision": "approve", "date": W1[1],
                                                            "shift_start": "5:00pm", "name": "Ben"})
    from permissions import has_permission, SCHEDULE_DRAFT, SCHEDULE_PUBLISH
    assert has_permission(member, SCHEDULE_DRAFT) and not has_permission(member, SCHEDULE_PUBLISH)
    for fn, args in ((strategy_routes._do_shift_request_decide, (1,)), (strategy_routes._do_open_shift_post, ()),
                     (strategy_routes._do_shift_request_offer, (1,)),
                     (strategy_routes._do_shift_request_colleague_agreed, (1,)),
                     (strategy_routes._do_shift_request_cancel, (1,)), (strategy_routes._do_time_off_decide, (1,)),
                     (strategy_routes._do_shift_request_reverse, (1,))):
        out = fn(member, *args)
        assert out[1] == 403, fn.__name__


# ── BUGSLIFE-5: approved time off over a published shift ──────────────────

def test_time_off_approved_over_a_published_shift_stays_on_the_list_until_handled(db):
    rid = _restaurant(db, [("Ana", "Server"), ("Ben", "Server")], module_labor=1)
    _publish(db, rid, [(W1[5], "Ana", "Server", "5:00pm", "11:00pm", 6)])
    conn = models.get_conn(db)
    tid = conn.execute("INSERT INTO staff_time_off (restaurant_id, employee_name, start_date, end_date, status) "
                       "VALUES (?,?,?,?, 'pending')", (rid, "Ana", W1[5], W1[5])).lastrowid
    conn.commit()
    conn.close()
    time_off.decide(rid, tid, True, decided_by=1)
    items = time_off.follow_ups(rid, today=TODAY)
    assert [(i["employee_name"], i["date"], i["shift_start"]) for i in items] == [("Ana", W1[5], "5:00pm")]
    q = {i["key"]: i for i in action_queue.items(rid, viewer={"is_admin": True}, db_path=db, today=TODAY)["items"]}
    item = q[f"time_off_shift:{tid}:{W1[5]}:5:00pm"]
    assert item["action"]["body"] == {"date": W1[5], "shift_start": "5:00pm", "employee": "Ana"}
    assert item["action"]["route"]["mobile"] == "/mobile/api/labor/open-shifts"
    # Posted open, it is the open shift's own item now.
    srq.post_open_shift(rid, W1[5], "5:00pm", employee="Ana", actor="mgr", today=TODAY, broadcast=False)
    assert time_off.follow_ups(rid, today=TODAY) == []


def test_an_approved_day_off_is_excused_never_a_no_show(db, monkeypatch):
    rid = _restaurant(db, [("Ana", "Server")])
    _publish(db, rid, [(W1[5], "Ana", "Server", "5:00pm", "11:00pm", 6)])
    conn = models.get_conn(db)
    conn.execute("INSERT INTO staff_time_off (restaurant_id, employee_name, start_date, end_date, status) "
                 "VALUES (?,?,?,?, 'approved')", (rid, "Ana", W1[5], W1[5]))
    conn.commit()
    conn.close()
    releases = srq.shift_release(rid, W1[5], db_path=db)
    assert attendance._unworked(releases, "Ana", "5:00pm", "11:00pm")[0] == "excused"


def test_part_day_time_off_excuses_only_the_shift_it_reaches(db):
    rid = _restaurant(db, [("Ana", "Server")])
    conn = models.get_conn(db)
    conn.execute("INSERT INTO staff_time_off (restaurant_id, employee_name, start_date, end_date, status, daypart) "
                 "VALUES (?,?,?,?, 'approved', 'night')", (rid, "Ana", W1[5], W1[5]))
    conn.commit()
    conn.close()
    releases = srq.shift_release(rid, W1[5], db_path=db)
    assert attendance._unworked(releases, "Ana", "5:00pm", "11:00pm")[0] == "excused"
    assert attendance._unworked(releases, "Ana", "10:00am", "2:00pm")[0] == "no_show"


# ── BUGSLIFE-20: a worked lunch never stands in for a missed dinner ─────────

def test_a_worked_lunch_and_a_missed_dinner_match_one_to_one():
    rows = [{"employee": "Ana", "shift_start": "11:00am", "shift_end": "3:00pm", "role": "Server"},
            {"employee": "Ana", "shift_start": "5:00pm", "shift_end": "11:00pm", "role": "Server"}]
    punches = {"ana": [{"employee": "Ana", "shift_start": "10:58am", "shift_end": "3:05pm"}]}
    got = attendance.match_punches(rows, punches)
    assert set(got) == {0}, "the dinner has no punch of its own: unworked, a no-show"


def test_two_punches_go_to_their_own_shifts():
    rows = [{"employee": "Ana", "shift_start": "11:00am", "shift_end": "3:00pm"},
            {"employee": "Ana", "shift_start": "5:00pm", "shift_end": "11:00pm"}]
    lunch = {"shift_start": "11:02am", "shift_end": "3:00pm"}
    dinner = {"shift_start": "5:20pm", "shift_end": "11:00pm"}
    got = attendance.match_punches(rows, {"ana": [dinner, lunch]})
    assert got == {0: lunch, 1: dinner}


# ── MARKET-14: earned, rule-based automatic approval ────────────────────────

def _earn(db, rid, kind="drop", n=request_autonomy.EARN_APPROVALS):
    conn = models.get_conn(db)
    hid = conn.execute("SELECT MAX(id) FROM schedule_history WHERE restaurant_id=?", (rid,)).fetchone()[0]
    for i in range(n):
        conn.execute("INSERT INTO shift_change_requests (restaurant_id, history_id, employee_name, date, shift_start, "
                     "status, kind, decided_by, decided_at, decided_how) VALUES (?,?,?,?,?,?,?,?,datetime('now'),"
                     "'manager')", (rid, hid, f"Old{i}", "2026-09-01", f"{i + 1}:00pm", "covered", kind, "mgr"))
    conn.commit()
    conn.close()


def test_a_legal_drop_approves_itself_only_once_the_owner_turned_it_on_and_it_was_earned(db):
    rid = _restaurant(db, [("Ana", "Server"), ("Ben", "Server"), ("Max", "Manager")])
    ss.upsert(rid, "Max", floor_manager=True)
    _publish(db, rid, [(W1[5], "Ana", "Server", "5:00pm", "11:00pm", 6),
                       (W1[5], "Ben", "Server", "5:00pm", "11:00pm", 6),
                       (W1[5], "Max", "Manager", "4:00pm", "11:30pm", 7.5)])
    # Off by default: the manager decides.
    assert srq.request_drop(rid, "Ana", W1[5], "5:00pm", today=TODAY)["status"] == "pending"
    conn = models.get_conn(db)
    conn.execute("DELETE FROM shift_change_requests WHERE restaurant_id=?", (rid,))
    conn.commit()
    conn.close()
    request_autonomy.save_policy(rid, {"auto_approve": True, "max_quality_drop": 60}, db_path=db)
    # On, but not earned: still the manager's, with the reason kept.
    out = srq.request_drop(rid, "Ana", W1[5], "5:00pm", today=TODAY)
    assert out["status"] == "pending"
    why = json.loads(srq._get_row(rid, out["id"], db)["auto_reasons_json"])
    assert not why["ok"] and "not earned" in why["refusals"][0]
    srq.withdraw(rid, out["id"], "Ana")
    _earn(db, rid)
    out = srq.request_drop(rid, "Ana", W1[5], "5:00pm", today=TODAY)
    assert out["status"] == "open" and out.get("auto_approved"), srq._get_row(rid, out["id"], db)["auto_reasons_json"]
    row = srq._get_row(rid, out["id"], db)
    assert row["decided_how"] == "auto" and json.loads(row["auto_reasons_json"])["ok"]


def test_a_drop_that_weakens_the_shift_past_the_limit_waits_on_the_manager(db):
    rid = _restaurant(db, [("Ana", "Server"), ("Ben", "Server")])
    _publish(db, rid, [(W1[5], "Ana", "Server", "5:00pm", "11:00pm", 6), (W1[5], "Ben", "Server", "5:00pm", "11:00pm", 6)])
    request_autonomy.save_policy(rid, {"auto_approve": True, "max_quality_drop": 1}, db_path=db)
    _earn(db, rid)
    out = srq.request_drop(rid, "Ana", W1[5], "5:00pm", today=TODAY)
    assert out["status"] == "pending"
    why = json.loads(srq._get_row(rid, out["id"], db)["auto_reasons_json"])
    assert "Shift Quality falls" in why["refusals"][0]


def test_a_drop_that_takes_the_manager_off_never_approves_itself(db):
    rid = _restaurant(db, [("Ana", "Server"), ("Max", "Manager")])
    ss.upsert(rid, "Max", floor_manager=True)
    _publish(db, rid, [(W1[5], "Ana", "Server", "5:00pm", "11:00pm", 6),
                       (W1[5], "Max", "Manager", "5:00pm", "11:00pm", 6)])
    request_autonomy.save_policy(rid, {"auto_approve": True}, db_path=db)
    _earn(db, rid)
    out = srq.request_drop(rid, "Max", W1[5], "5:00pm", today=TODAY)
    assert out["status"] == "pending"
    why = json.loads(srq._get_row(rid, out["id"], db)["auto_reasons_json"])
    assert "no manager" in why["refusals"][0]


def test_a_drop_inside_the_notice_window_waits_on_the_manager(db, monkeypatch):
    rid = _restaurant(db, [("Ana", "Server"), ("Ben", "Server")])
    _publish(db, rid, [(W1[5], "Ana", "Server", "5:00pm", "11:00pm", 6), (W1[5], "Ben", "Server", "5:00pm", "11:00pm", 6)])
    request_autonomy.save_policy(rid, {"auto_approve": True, "notice_hours": 300}, db_path=db)
    _earn(db, rid)
    assert srq.request_drop(rid, "Ana", W1[5], "5:00pm", today=TODAY)["status"] == "pending"


def test_reversing_an_automatic_drop_puts_the_holder_back_and_lapses_the_trust(db):
    rid = _restaurant(db, [("Ana", "Server"), ("Ben", "Server")])
    _publish(db, rid, [(W1[5], "Ana", "Server", "5:00pm", "11:00pm", 6), (W1[5], "Ben", "Server", "5:00pm", "11:00pm", 6)])
    request_autonomy.save_policy(rid, {"auto_approve": True, "max_quality_drop": 60}, db_path=db)
    _earn(db, rid)
    out = srq.request_drop(rid, "Ana", W1[5], "5:00pm", today=TODAY)
    assert out["status"] == "open"
    rev = request_autonomy.reverse(rid, out["id"], actor="mgr", db_path=db)
    assert rev["ok"] and rev["undone"] and rev["request"]["status"] == "denied"
    t = request_autonomy.trust(rid, "drop", db_path=db)
    assert not t["earned"] and t["lapse_reason"]
    assert any(i["scope"] == "shift_request" for i in automation_trust.lapsed_items(rid, db_path=db))
    # The next drop waits on the manager again.
    srq.withdraw(rid, out["id"], "Ana")
    assert srq.request_drop(rid, "Ben", W1[5], "5:00pm", today=TODAY)["status"] == "pending"


def test_an_earned_same_role_swap_approves_itself_and_still_asks_the_colleague(db):
    rid = _restaurant(db, [("Ana", "Server"), ("Ben", "Server")])
    hid = _publish(db, rid, [(W1[1], "Ana", "Server", "11:00am", "3:00pm", 4), (W1[3], "Ben", "Server", "11:00am", "3:00pm", 4)])
    request_autonomy.save_policy(rid, {"auto_approve": True, "max_quality_drop": 60}, db_path=db)
    _earn(db, rid, kind="swap")
    out = srq.request_swap(rid, "Ana", W1[1], "11:00am", "Ben", W1[3], "11:00am", today=TODAY)
    assert out["status"] == "approved" and out.get("auto_approved")
    assert {(r["date"], r["employee"]) for r in _csv_rows(db, hid)} == {(W1[1], "Ana"), (W1[3], "Ben")}
    assert srq.respond_swap(rid, out["id"], "Ben", True, today=TODAY)["status"] == "covered"
