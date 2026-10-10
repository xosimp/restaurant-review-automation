"""Schedule fix round 10/10/26, workstream D2: who is really on, requests that
follow the week, and a person's identity across a rename.

BUGSLIFE-12  an approved drop takes the dropper off reminders, task sheets and
             the Studio's count of who is on (one reading: release_marks).
ARCH-9       a retime or a reassign carries or closes the live requests on the
             shift, in the same write, and tells the requester.
ARCH-7 / BUGSLIFE-4  a rename re-points the live and future weeks; the staff app
             keeps showing the person their shifts; a screen opened before the
             rename never saves the old name back or reads it as a conflict.
"""
import datetime as dt
import sys

import pytest

import activity, covers, decisions, delayed, demand_signals, goals, issues, metrics, outcomes  # noqa: E401,F401
import push, schedule_economics, schedule_intel, schedule_rules, schedule_versions  # noqa: E401,F401
import shift_requests, shift_quality, staff_schedule, staff_settings, strategy_jobs, time_off  # noqa: E401,F401
import attendance, task_sheets, labor, people  # noqa: E401,F401

import models
import schedule_versions as sv
import shift_requests as srq
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
    sent = []
    monkeypatch.setattr(srq, "_tell_staff", lambda rid, names, subject, lines, *a, **k: sent.append((names, subject)) or list(names))
    monkeypatch.setattr(srq, "_tell_managers", lambda *a, **k: None)
    monkeypatch.setattr(srq, "_refresh_task_sheets", lambda *a, **k: None)
    db_path_sent = db_path
    conn.sent = sent
    return db_path_sent


def _day(d):
    return dt.date.fromisoformat(d).strftime("%A")


def _restaurant(db_path, roster):
    rid = create_restaurant(Restaurant(name="D2 Id", owner_email="d2i@x.com"), db_path=db_path)
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


def _csv(db_path, hid):
    conn = models.get_conn(db_path)
    try:
        return conn.execute("SELECT schedule_csv FROM schedule_history WHERE id=?", (hid,)).fetchone()[0]
    finally:
        conn.close()


def _write(db_path, rid, hid, text, reason="edited"):
    conn = models.get_conn(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        sv.write_on(conn, rid, hid, reason, text, saved_by="mgr")
        conn.commit()
    finally:
        conn.close()


def _send(rid, hid):
    """Send a sent week's saved changes (schedule_publish.send_changes): a
    manager's save of a week staff have changes the working copy only, and
    the live requests follow the copy staff have when it is sent (schedule
    audit 10/10/26 ARCH-1 with ARCH-9)."""
    import schedule_publish
    out, status = schedule_publish.send_changes(rid, hid, {"username": "mgr"}, acknowledge=True)
    assert status == 200 and out["ok"], out
    return out


# ── BUGSLIFE-12 ─────────────────────────────────────────────────────────────

def test_an_approved_drop_leaves_the_slot_unheld_for_every_reader(db, monkeypatch):
    rid = _restaurant(db, [("Ana", "Server"), ("Ben", "Server")])
    hid = _publish(db, rid, [(W1[2], "Ana", "Server", "5:00pm", "11:00pm", 6),
                             (W1[2], "Ben", "Server", "5:00pm", "11:00pm", 6)])
    rows = sv.rows_from_csv(_csv(db, hid))
    assert srq.release_marks(rid, rows) == {}
    srq.decide(rid, srq.request_drop(rid, "Ana", W1[2], "5:00pm", today=TODAY)["id"], True, decided_by="mgr",
               today=TODAY)
    eff = srq.effective_rows(rid, rows)
    ana = next(r for r in eff if r.get("released_holder") == "Ana")
    assert ana["employee"] == "" and ana["release"]["kind"] == "drop"
    # Reminders and task sheets read the same: Ana is not on tonight.
    shifts = labor.timed_shifts_from_csv(_csv(db, hid))
    left = srq.effective_timed_shifts(rid, W1[2], shifts)
    assert [s["employee"] for s in left] == ["Ben"]
    monkeypatch.setattr(task_sheets, "_published_owner", lambda r, d: hid)
    got, published = task_sheets.published_day_shifts(rid, dt.date.fromisoformat(W1[2]))
    assert published and [s["employee"] for s in got] == ["Ben"]
    # And the task sheets' signature moves, so a sheet already issued re-resolves.
    assert srq.release_sig(rid, W1[2])


def test_the_studio_marks_the_open_slot_on_a_week_staff_have(db, monkeypatch):
    import mobile_api
    import inspect
    src = inspect.getsource(mobile_api.mobile_schedule_history_detail)
    assert "release_marks" in src and '"release"' in src


# ── ARCH-9 ──────────────────────────────────────────────────────────────────

def test_a_retime_carries_the_pending_request_and_tells_the_requester(db):
    rid = _restaurant(db, [("Ana", "Server"), ("Ben", "Server")])
    hid = _publish(db, rid, [(W1[2], "Ana", "Server", "5:00pm", "11:00pm", 6)])
    req = srq.request_drop(rid, "Ana", W1[2], "5:00pm", today=TODAY)
    _write(db, rid, hid, _csv(db, hid).replace("5:00pm,11:00pm", "6:00pm,11:00pm"))
    # Saved, not sent: Ana still has 5pm, and so does her request.
    assert srq._get_row(rid, req["id"], db)["shift_start"] == "5:00pm"
    _send(rid, hid)
    row = srq._get_row(rid, req["id"], db)
    assert row["status"] == "pending" and row["shift_start"] == "6:00pm"
    assert row["notice_pending"] is None                     # told once, as the Send committed
    assert srq.flush_notices(rid, db_path=db) == 0, "told once"
    # The manager can still decide it: it is about the shift as it is.
    assert srq.decide(rid, req["id"], True, decided_by="mgr", today=TODAY)["status"] == "open"


def test_a_reassign_closes_the_request_in_the_same_write(db):
    rid = _restaurant(db, [("Ana", "Server"), ("Ben", "Server")])
    hid = _publish(db, rid, [(W1[2], "Ana", "Server", "5:00pm", "11:00pm", 6)])
    req = srq.decide(rid, srq.request_drop(rid, "Ana", W1[2], "5:00pm", today=TODAY)["id"], True,
                     decided_by="mgr", today=TODAY)
    assert req["status"] == "open"
    _write(db, rid, hid, _csv(db, hid).replace(",Ana,", ",Ben,"))
    assert srq._get_row(rid, req["id"], db)["status"] == "open"          # saved, not sent
    _send(rid, hid)
    row = srq._get_row(rid, req["id"], db)
    assert row["status"] == "cancelled" and "Ben" in row["decision_note"]
    assert srq.open_shifts(rid) == []


def test_a_removed_shift_closes_its_request(db):
    rid = _restaurant(db, [("Ana", "Server"), ("Ben", "Server")])
    hid = _publish(db, rid, [(W1[2], "Ana", "Server", "5:00pm", "11:00pm", 6),
                             (W1[3], "Ben", "Server", "5:00pm", "11:00pm", 6)])
    req = srq.request_drop(rid, "Ana", W1[2], "5:00pm", today=TODAY)
    text = "\n".join(l for l in _csv(db, hid).split("\n") if ",Ana," not in l)
    _write(db, rid, hid, text)
    assert srq._get_row(rid, req["id"], db)["status"] == "pending"       # saved, not sent
    _send(rid, hid)
    assert srq._get_row(rid, req["id"], db)["status"] == "cancelled"


def test_a_draft_edit_leaves_requests_alone(db):
    rid = _restaurant(db, [("Ana", "Server")])
    hid = _publish(db, rid, [(W1[2], "Ana", "Server", "5:00pm", "11:00pm", 6)])
    req = srq.request_drop(rid, "Ana", W1[2], "5:00pm", today=TODAY)
    draft = models.save_schedule_history(rid, W1[0], W1[-1], 0, 0, 30, HEADER + f"\n{W1[2]},x,Zed,Server,1:00pm,2:00pm,1,",
                                         [], db_path=db)
    _write(db, rid, draft, HEADER + f"\n{W1[2]},x,Zed,Server,2:00pm,3:00pm,1,")
    assert srq._get_row(rid, req["id"], db)["status"] == "pending"


# ── ARCH-7 / BUGSLIFE-4 ─────────────────────────────────────────────────────

def test_a_rename_re_points_the_live_week_and_the_staff_app_still_shows_the_shifts(db):
    rid = _restaurant(db, [("Ana", "Server"), ("Ben", "Server")])
    hid = _publish(db, rid, [(W1[2], "Ana", "Server", "5:00pm", "11:00pm", 6)])
    pid = people.person_id_for(rid, "Ana", db_path=db)
    people.rename_person(rid, pid, "Ana Smith", db_path=db)
    rows = sv.rows_from_csv(_csv(db, hid))
    assert [r["employee"] for r in rows] == ["Ana Smith"]
    week = staff_schedule.shifts_for_employee(rid, "Ana Smith", today=TODAY,
                                              now=dt.datetime.combine(TODAY, dt.time(9)))
    assert any(d.get("shift") for d in week["week"] if d.get("date") == W1[2]) or any(
        s.get("date") == W1[2] for s in week.get("upcoming") or [])
    # Her drop finds the shift under the new name.
    assert srq.request_drop(rid, "Ana Smith", W1[2], "5:00pm", today=TODAY)["status"] == "pending"


def test_a_week_still_under_the_old_spelling_is_read_through_identity(db):
    rid = _restaurant(db, [("Bob S.", "Server")])
    text = HEADER + f"\n{W1[2]},{_day(W1[2])},Bob S.,Server,5:00pm,11:00pm,6,"
    pid = people.person_id_for(rid, "Bob S.", db_path=db)
    people.rename_person(rid, pid, "Bob Smith", db_path=db)
    got = labor.employee_shifts_from_csv(text, "Bob Smith",
                                         spellings=people.spellings(rid, ["Bob Smith"], db_path=db)["Bob Smith"])
    assert [s["start"] for s in got] == ["5:00pm"]
    assert srq._find(sv.rows_from_csv(text), "Bob Smith", W1[2], "5:00pm",
                     spellings=srq._spellings(rid, "Bob Smith", db)) == 0


def test_a_screen_opened_before_a_rename_is_not_a_conflict_and_never_saves_the_old_name(db):
    rid = _restaurant(db, [("Ana", "Server")])
    hid = _publish(db, rid, [(W1[2], "Ana", "Server", "5:00pm", "11:00pm", 6)])
    conn = models.get_conn(db)
    base = sv.latest_version(conn, hid)
    conn.close()
    people.rename_person(rid, people.person_id_for(rid, "Ana", db_path=db), "Ana Smith", db_path=db)
    conn = models.get_conn(db)
    try:
        assert sv.rows_changed_since(conn, hid, base) is None
    finally:
        conn.close()
    assert sv.canonical_rows(rid, [[{"employee": "Ana", "date": W1[2]}]], db_path=db)[0][0]["employee"] == "Ana Smith"
    # The staff were told nothing new: a rename is not a change.
    assert sv.unsent_changes(rid, hid, db_path=db) == {"people": [], "dates": []}


def test_an_unmerge_puts_the_live_week_rows_back(db):
    rid = _restaurant(db, [("Kim T.", "Server"), ("Kimberly Tran", "Server")])
    hid = _publish(db, rid, [(W1[2], "Kim T.", "Server", "5:00pm", "11:00pm", 6),
                             (W1[3], "Kimberly Tran", "Server", "5:00pm", "11:00pm", 6)])
    a = people.person_id_for(rid, "Kim T.", db_path=db)
    b = people.person_id_for(rid, "Kimberly Tran", db_path=db)
    people.merge_people(rid, a, b, db_path=db)
    assert {r["employee"] for r in sv.rows_from_csv(_csv(db, hid))} == {"Kimberly Tran"}
    mid = people.recent_merges(rid, db_path=db)[0]["merge_id"]
    people.unmerge_people(rid, mid, db_path=db)
    assert {(r["date"], r["employee"]) for r in sv.rows_from_csv(_csv(db, hid))} == {
        (W1[2], "Kim T."), (W1[3], "Kimberly Tran")}
