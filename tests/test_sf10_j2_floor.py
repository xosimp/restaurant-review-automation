"""Schedule fix round 10/10/26, workstream J2: the schedule on the floor.

MARKET-7 / RM-16  the published week written to the POS time clock — owner
                  opt-in, preview first, idempotent, per-shift status, failures
                  told, never holding a publish; overtime drift for the report.
MARKET-20         one shift edited from Ask, through the legality engine and the
                  Studio's save steps (version + learning attribution).
MARKET-12         the reservation-feed adapter contract.
No network anywhere: every POS is a stub."""
import datetime as dt
import json
import sys

import pytest

import activity, covers, decisions, delayed, demand_signals, goals, issues, metrics, outcomes  # noqa: E401,F401
import push, schedule_economics, schedule_intel, schedule_rules, schedule_versions  # noqa: E401,F401
import shift_requests, shift_quality, staff_schedule, staff_settings, strategy_jobs, time_off  # noqa: E401,F401
import attendance, automation_trust, request_autonomy, action_queue  # noqa: E401,F401
import floor_settings, labor_live, pos_schedule_push, shift_edits, cross_site, reservation_feeds  # noqa: E401,F401
import intraday, labor_replacements, people, pos, scheduler  # noqa: E401,F401

import models
import schedule_versions as sv
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
    monkeypatch.setattr(shift_requests, "_tell_staff", lambda *a, **k: [])
    monkeypatch.setattr(shift_requests, "_tell_managers", lambda *a, **k: None)
    monkeypatch.setattr(pos, "PROVIDERS", None)
    return db_path


def _day(d):
    return dt.date.fromisoformat(d).strftime("%A")


def _restaurant(db_path, roster, **cols):
    rid = create_restaurant(Restaurant(name="J2 Co", owner_email="j2@x.com", module_labor=1), db_path=db_path)
    if cols:
        conn = models.get_conn(db_path)
        conn.execute("UPDATE restaurants SET " + ", ".join(f"{k}=?" for k in cols) + " WHERE id=?",
                     (*cols.values(), rid))
        conn.commit()
        conn.close()
    for name, role in roster:
        models.add_manual_team_member(rid, name, role=role, db_path=db_path)
    return rid


def _publish(db_path, rid, rows, dates=W1, published=True):
    text = HEADER + "\n" + "\n".join(f"{d},{_day(d)},{n},{role},{s},{e},{h}," for d, n, role, s, e, h in rows)
    hid = models.save_schedule_history(rid, dates[0], dates[-1], 0, 0, 30, text, [], db_path=db_path)
    if published:
        conn = models.get_conn(db_path)
        conn.execute("UPDATE schedule_history SET published_at=datetime('now') WHERE id=?", (hid,))
        conn.commit()
        conn.close()
    return hid


def _rows(db_path, hid):
    conn = models.get_conn(db_path)
    try:
        return sv.latest_rows(conn, hid)
    finally:
        conn.close()


# ── MARKET-7: the POS schedule push ─────────────────────────────────────────

class _FakePOS:
    """A provider that can take a schedule: records what it was sent."""
    def __init__(self, skip=(), fail=None):
        self.pushed, self.skip, self.fail = [], set(skip), fail

    def is_connected(self, rid):
        return True

    def sync_to_db(self, rid):
        return {"ok": True}

    def build_shifts_csv(self, rid, days=60):
        return None

    def preview_labor_schedule(self, rid, shifts, job_codes=None):
        return {"ok": True, "sent": False, "skipped": [{"employee": s["employee"], "why": "no payroll id at the POS"}
                                                        for s in shifts if s["employee"] in self.skip],
                "problems": []}

    def push_labor_schedule(self, rid, shifts, job_codes=None):
        if self.fail:
            return {"ok": False, "error": self.fail, "skipped": []}
        self.pushed.append([dict(s) for s in shifts])
        return {"ok": True, "employees": len({s["employee"] for s in shifts}), "shifts": len(shifts), "skipped": []}


def _pos(monkeypatch, fake):
    monkeypatch.setattr(pos, "PROVIDERS", {"rpower": fake})
    monkeypatch.setattr(scheduler, "scheduling_allowed", lambda: True)
    told = []
    monkeypatch.setattr(pos_schedule_push, "_tell_failure", lambda rid, row, why, db_path=None: told.append(why))
    return told


def _push_week(db, monkeypatch, fake, **cols):
    rid = _restaurant(db, [("Ana", "Server"), ("Bo", "Server"), ("Gm", "Manager")],
                      salaried_staff_json=json.dumps([{"name": "Gm", "annual": 60000}]), **cols)
    hid = _publish(db, rid, [(W1[4], "Ana", "Server", "5:00pm", "11:00pm", 6),
                             (W1[4], "Bo", "Server", "4:00pm", "10:00pm", 6),
                             (W1[4], "Gm", "Manager", "3:00pm", "11:30pm", 8.5),
                             (W1[0], "Ana", "Server", "11:00am", "3:00pm", 4)])
    told = _pos(monkeypatch, fake)
    return rid, hid, told


NOW = dt.datetime(2026, 10, 7, 12, 0)    # Wednesday noon of the week


def test_the_preview_says_what_happens_to_every_shift_and_sends_nothing(db, monkeypatch):
    fake = _FakePOS(skip={"Bo"})
    rid, hid, _told = _push_week(db, monkeypatch, fake)
    pv = pos_schedule_push.preview(rid, hid, now_local=NOW)
    assert pv["ok"] and pv["supported"] and pv["enabled"] is False
    by = {(s["employee"], (s.get("start") or "")[:10]): s for s in pv["shifts"]}
    assert by[("Ana", W1[4])]["status"] == "would_send" and by[("Ana", W1[4])]["end"] == "2026-10-09T23:00:00"
    assert by[("Bo", W1[4])]["status"] == "skipped" and "payroll" in by[("Bo", W1[4])]["reason"]
    assert by[("Gm", W1[4])]["status"] == "salaried"           # they don't clock in
    assert by[("Ana", W1[0])]["status"] == "started"           # Monday is past
    assert fake.pushed == []


def test_the_switch_is_off_until_the_owner_turns_it_on_and_a_publish_only_queues(db, monkeypatch):
    fake = _FakePOS()
    rid, hid, _told = _push_week(db, monkeypatch, fake)
    assert pos_schedule_push.after_publish(rid, hid) is None          # off by default
    floor_settings.save(rid, {"pos_push": True})
    pid = pos_schedule_push.after_publish(rid, hid)
    assert pid and fake.pushed == []                                   # queued, never sent inline
    # A second publish of the same week re-points the one queued row.
    assert pos_schedule_push.after_publish(rid, hid) == pid


def test_the_job_sends_once_and_the_same_week_is_never_sent_twice(db, monkeypatch):
    fake = _FakePOS()
    rid, hid, _told = _push_week(db, monkeypatch, fake)
    floor_settings.save(rid, {"pos_push": True})
    pid = pos_schedule_push.enqueue(rid, hid)
    out = pos_schedule_push.run_push(pid, now_local=NOW)
    assert out["status"] == "sent" and out["sent"] == 2
    assert {s["employee"] for s in fake.pushed[0]} == {"Ana", "Bo"}
    last = pos_schedule_push.last_push(rid, W1[0])
    assert {(s["employee"], s["status"]) for s in last["shifts"]} >= {("Ana", "sent"), ("Gm", "salaried")}
    pid2 = pos_schedule_push.enqueue(rid, hid)
    assert pos_schedule_push.run_push(pid2, now_local=NOW)["status"] == "unchanged"
    assert len(fake.pushed) == 1


def test_a_corrected_week_is_sent_again_and_somebody_taken_off_is_flagged_not_assumed(db, monkeypatch):
    fake = _FakePOS()
    rid, hid, _told = _push_week(db, monkeypatch, fake)
    floor_settings.save(rid, {"pos_push": True})
    pos_schedule_push.run_push(pos_schedule_push.enqueue(rid, hid), now_local=NOW)
    conn = models.get_conn(db)
    csv_now = conn.execute("SELECT schedule_csv FROM schedule_history WHERE id=?", (hid,)).fetchone()[0]
    conn.execute("UPDATE schedule_history SET schedule_csv=? WHERE id=?",
                 ("\n".join(x for x in csv_now.split("\n") if ",Bo," not in x), hid))
    conn.commit()
    conn.close()
    pv = pos_schedule_push.preview(rid, hid, now_local=NOW)
    gone = [s for s in pv["shifts"] if s["status"] == "removed_unconfirmed"]
    assert [s["employee"] for s in gone] == ["Bo"] and "may still hold" in gone[0]["reason"]
    assert pos_schedule_push.run_push(pos_schedule_push.enqueue(rid, hid), now_local=NOW)["status"] == "sent"
    assert len(fake.pushed) == 2


def test_a_failed_push_is_told_and_retried_and_a_local_copy_never_writes(db, monkeypatch):
    fake = _FakePOS(fail="RPOWER returned HTTP 503")
    rid, hid, told = _push_week(db, monkeypatch, fake)
    floor_settings.save(rid, {"pos_push": True})
    pid = pos_schedule_push.enqueue(rid, hid)
    assert pos_schedule_push.run_push(pid, now_local=NOW)["status"] == "failed"
    assert told and "503" in told[0]
    conn = models.get_conn(db)
    row = conn.execute("SELECT status, next_try_at, attempts FROM pos_schedule_pushes WHERE id=?", (pid,)).fetchone()
    shifts = conn.execute("SELECT status, reason FROM pos_schedule_push_shifts WHERE push_id=? AND status='failed'",
                          (pid,)).fetchall()
    conn.close()
    assert row["status"] == "failed" and row["next_try_at"] and row["attempts"] == 1
    assert shifts and "503" in shifts[0]["reason"]
    # A laptop holding production's tokens never writes to a POS.
    monkeypatch.setattr(scheduler, "scheduling_allowed", lambda: False)
    fake.fail = None
    pid2 = pos_schedule_push.enqueue(rid, hid)
    assert pos_schedule_push.run_push(pid2, now_local=NOW)["status"] == "held_local" and fake.pushed == []


def test_a_publish_is_never_held_by_the_push(db, monkeypatch):
    fake = _FakePOS()
    rid, hid, _told = _push_week(db, monkeypatch, fake)
    floor_settings.save(rid, {"pos_push": True})
    monkeypatch.setattr(pos_schedule_push, "enqueue", lambda *a, **k: 1 / 0)
    assert pos_schedule_push.after_publish(rid, hid) is None          # swallowed, recorded


def test_a_pos_without_a_schedule_write_cannot_be_switched_on(db, monkeypatch):
    import flask
    import floor_api
    rid, hid, _told = _push_week(db, monkeypatch, _FakePOS())

    class _NoWrite(_FakePOS):
        push_labor_schedule = None
    monkeypatch.setattr(pos, "PROVIDERS", {"square": _NoWrite()})
    u = {"restaurant_id": rid, "is_admin": True, "username": "o"}
    app = flask.Flask(__name__)
    with app.test_request_context(json={"pos_push": True, "previewed": True}):
        body, status = floor_api.floor_settings_set(u)
    assert status == 400 and "no schedule write" in body["error"]
    monkeypatch.setattr(pos, "PROVIDERS", {"rpower": _FakePOS()})
    with app.test_request_context(json={"pos_push": True}):
        body, status = floor_api.floor_settings_set(u)
    assert status == 400 and "preview first" in body["error"]
    with app.test_request_context(json={"pos_push": True, "previewed": True}):
        body, status = floor_api.floor_settings_set(u)
    assert status == 200 and body["settings"]["pos_push"] is True and body["settings"]["pos_push_on_by"] == "o"


def test_overtime_drift_counts_minutes_outside_the_shift_by_person_and_skips_the_salaried(db, monkeypatch):
    rid, hid, _told = _push_week(db, monkeypatch, _FakePOS(), role_rates_json=json.dumps({"Server": 12.0}))
    rows = [dict(r) for r in sv.rows_from_csv(_rows(db, hid) and models.get_conn(db).execute(
        "SELECT schedule_csv FROM schedule_history WHERE id=?", (hid,)).fetchone()[0]) if r["date"] == W1[4]]
    punches = [{"employee_name": "Ana", "clock_in": "2026-10-09T16:40:00", "clock_out": "2026-10-09T23:30:00",
                "reg_rate": 12.0},
               {"employee_name": "Bo", "clock_in": "2026-10-09T16:03:00", "clock_out": "2026-10-09T22:05:00"},
               {"employee_name": "Gm", "clock_in": "2026-10-09T14:00:00", "clock_out": "2026-10-10T00:30:00"}]
    d = pos_schedule_push.overtime_drift(rid, W1[4], rows=rows, punches=punches)
    assert d["available"] and [p["employee"] for p in d["people"]] == ["Ana"]
    ana = d["people"][0]
    assert (ana["early"], ana["late"]) == (20, 30) and ana["dollars"] == 10.0
    assert d["minutes"] == 50


# ── MARKET-20: one shift, through the one engine and the Studio's save ─────

def _studio_week(db):
    rid = _restaurant(db, [("Ana", "Server"), ("Bo", "Server"), ("Cy", "Server"), ("Max", "Manager")])
    ss.upsert(rid, "Max", floor_manager=True)
    hid = _publish(db, rid, [(W1[4], "Ana", "Server", "5:00pm", "11:00pm", 6),
                             (W1[4], "Max", "Manager", "4:00pm", "11:30pm", 7.5),
                             (W1[5], "Bo", "Server", "5:00pm", "11:00pm", 6),
                             (W1[5], "Max", "Manager", "4:00pm", "11:30pm", 7.5)], published=False)
    return rid, hid


def test_each_edit_builds_the_week_it_says(db):
    rid, hid = _studio_week(db)
    rows = _rows(db, hid)
    after, takers, _t, s = shift_edits.build(rid, rows, "add", {"date": W1[4], "employee": "Cy", "role": "Server",
                                                                "shift_start": "5pm", "shift_end": "10pm"})
    assert after[-1]["employee"] == "Cy" and after[-1]["shift_end"] == "10:00pm" and takers == [(len(after) - 1, "Cy")]
    assert "Add Cy" in s
    after, _tk, touched, _s = shift_edits.build(rid, rows, "move", {"date": W1[4], "employee": "Ana",
                                                                    "new_date": W1[6]})
    assert any(r["employee"] == "Ana" and r["date"] == W1[6] for r in after) and touched == {W1[4], W1[6]}
    after, *_ = shift_edits.build(rid, rows, "retime", {"date": W1[4], "employee": "Ana", "new_end": "9:00pm"})
    assert next(r for r in after if r["employee"] == "Ana")["scheduled_hours"] == "4"
    after, *_ = shift_edits.build(rid, rows, "reassign", {"date": W1[5], "employee": "Bo", "to_employee": "Cy"})
    assert {r["employee"] for r in after if r["date"] == W1[5]} == {"Cy", "Max"}
    with pytest.raises(shift_edits.EditError):
        shift_edits.build(rid, rows, "remove", {"date": W1[4], "employee": "Nobody"})


def test_an_edit_that_takes_the_only_manager_off_is_refused_by_the_engine(db):
    rid, hid = _studio_week(db)
    p = shift_edits.plan(rid, "remove", {"date": W1[4], "employee": "Max"})
    assert p["ok"] and not p["legal"] and any("manager" in x.lower() for x in p["problems"])
    out = shift_edits.apply(rid, {"op": "remove", "date": W1[4], "employee": "Max", "history_id": hid,
                                  "version": p["version"]})
    assert out["illegal"] and not out["ok"]
    assert any(r["employee"] == "Max" and r["date"] == W1[4] for r in _rows(db, hid))


def test_a_legal_edit_is_priced_saved_as_a_version_and_taught_as_the_managers(db, monkeypatch):
    rid, hid = _studio_week(db)
    import schedule_learning
    taught = []
    monkeypatch.setattr(schedule_learning, "capture_save",
                        lambda rid_, hid_, v, step, user=None, published=False, **k: taught.append((v, step, user)))
    p = shift_edits.plan(rid, "add", {"date": W1[5], "employee": "Ana", "role": "Server", "shift_start": "6:00pm",
                                      "shift_end": "10:00pm"})
    assert p["legal"] and p["cost"]["hours_delta"] == 4.0
    user = {"id": 7, "username": "gm", "role": "owner", "restaurant_id": rid}
    out = shift_edits.apply(rid, {"op": "add", "date": W1[5], "employee": "Ana", "role": "Server",
                                  "shift_start": "6:00pm", "shift_end": "10:00pm", "history_id": hid,
                                  "version": p["version"]}, user=user)
    assert out["ok"] and out["version"] == (p["version"] or 0) + 1
    assert any(r["employee"] == "Ana" and r["date"] == W1[5] for r in _rows(db, hid))
    (v, step, who), = taught
    assert who is user and [c["origin"] for c in step["changes"]] == ["manager"]
    # A card judged on an older version is refused, never written over a newer week.
    stale = shift_edits.apply(rid, {"op": "remove", "date": W1[5], "employee": "Ana", "shift_start": "6:00pm",
                                    "history_id": hid, "version": p["version"]}, user=user)
    assert stale["conflict"] and not stale["ok"]


def test_ask_proposes_one_shift_with_the_week_and_version_it_was_judged_on(db):
    import ask_cavnar_tools as tools
    rid, hid = _studio_week(db)
    card = tools.build_proposal("edit_shift", {"op": "retime", "date": W1[4], "employee": "Ana",
                                               "new_end": "10:00pm"}, restaurant_id=rid) \
        if hasattr(tools, "build_proposal") else None
    if card is None:
        body, summary, why = shift_edits.card(rid, {"op": "retime", "date": W1[4], "employee": "Ana",
                                                    "new_end": "10:00pm"})
    else:
        body, summary, why = card["body"], card["summary"], None
    assert why is None and body["history_id"] == hid and "version" in body and "10:00pm" in summary
    assert tools._BY_NAME["edit_shift"]["route"]["mobile"] == "/mobile/api/labor/shift-edit"
    refused = tools.proposal_refusal("edit_shift", {"op": "remove", "date": W1[4], "employee": "Max"},
                                     restaurant_id=rid)
    assert refused and "breaks a rule" in refused


# ── MARKET-12: the reservation-feed adapter contract ───────────────────────

def test_a_reservation_system_plugs_in_with_one_registration_and_a_bad_row_stores_nothing(db, monkeypatch):
    rid = _restaurant(db, [], reservation_provider="sevenrooms")
    today = dt.date.today()
    rows = [{"date": today.isoformat(), "covers": 40}, {"date": today.isoformat(), "covers": 6}]
    monkeypatch.setitem(reservation_feeds.PROVIDERS, "sevenrooms", None)
    reservation_feeds.register_provider("sevenrooms", "SevenRooms", lambda r, s, e: rows)
    out = reservation_feeds.sync(rid)
    assert out["error"] is None and out["written"] >= 1
    sig = demand_signals.by_date(rid, [today.isoformat()]).get(today.isoformat())
    assert sig and int(sig["covers"]) == 46
    reservation_feeds.register_provider("sevenrooms", "SevenRooms", lambda r, s, e: [{"date": "nope", "covers": 1}])
    assert "sync failed" in reservation_feeds.sync(rid)["error"]
    assert {p["code"]: p["live"] for p in reservation_feeds.available()}["opentable"] is False
