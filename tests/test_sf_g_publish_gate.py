"""Schedule fix round 10/3/26, workstream G — the publish gate and the
week's hours split by pay.

E-7 / P-6  schedule_history keeps hourly and salaried hours apart; the gate
           holds the week's hourly hours against the hourly budget, and the
           History line, Home and the phone compare hourly too.
SQ-29      the quality verdict (weak, low confidence, below the bar) is a
           note, never a blocker.
P-16       a week the quality engine could not check is a blocker with the
           cause; so is a publish check that could not run.
P-1        a setting or source the rules could not read is named.
E-22       a regenerated draft of a week staff already have carries the
           late-change warning over the dates that changed — never "starts in
           -2 days".
"""
import datetime as dt
import json

import pytest

import client_api
import models
import schedule_rules as sr
import schedule_versions as sv
from models import Restaurant, create_restaurant, get_conn

HEADER = "date,day,employee,role,shift_start,shift_end,scheduled_hours,notes\n"
WEEK = ["2026-10-05", "2026-10-06", "2026-10-07", "2026-10-08", "2026-10-09", "2026-10-10", "2026-10-11"]


@pytest.fixture
def rid(db_path, monkeypatch):
    import importlib
    real = models.get_conn
    for name in ("models", "client_api", "mobile_api", "schedule_rules", "schedule_versions", "schedule_engine",
                 "staff_settings", "labor", "time_off", "auth", "ops", "shift_requests", "people"):
        monkeypatch.setattr(importlib.import_module(name), "get_conn", lambda *a, **k: real(db_path), raising=False)
    monkeypatch.setattr(models, "DB_PATH", db_path)
    monkeypatch.setattr(models, "_cached_shifts", lambda r: [])
    rid = create_restaurant(Restaurant(name="Gate Co", owner_email="g@x.com"), db_path=db_path)
    for n in ("Ana", "Bob", "Erik"):
        models.add_manual_team_member(rid, n, role="Server", db_path=db_path)
    return rid


def _rows(*rows):
    return HEADER + "".join(f"{d},{dt.date.fromisoformat(d).strftime('%A')},{e},Server,{s},{end},{h},\n"
                            for d, e, s, end, h in rows)


def _history(db_path, rid, csv_text, budget=40.0, quality=None, published=False, hours=None):
    conn = get_conn(db_path)
    models._ensure_history_columns(conn)
    cur = conn.execute(
        "INSERT INTO schedule_history (restaurant_id, week_start, week_end, hours_scheduled, hours_budget, labor_target, "
        "schedule_csv, summary_json, quality_json, published_at) VALUES (?,?,?,?,?,?,?,'[]',?,?)",
        (rid, WEEK[0], WEEK[6], hours if hours is not None else 0, budget, 30, csv_text,
         json.dumps(quality) if quality else None, "2026-10-01 10:00:00" if published else None))
    conn.commit()
    hid = cur.lastrowid
    conn.close()
    return hid


def _salaried(db_path, rid, *names):
    models.update_restaurant(rid, {"salaried_staff_json": json.dumps([{"name": n, "annual": 150000} for n in names])},
                             db_path=db_path)


# Erik (salaried) 5 x 10h; Ana and Bob 30h of hourly shifts between them.
WEEK_ROWS = [(WEEK[i], "Erik", "8:00am", "6:00pm", 10.0) for i in range(5)] + \
            [(WEEK[i], "Ana", "4:00pm", "10:00pm", 6.0) for i in range(3)] + \
            [(WEEK[i], "Bob", "4:00pm", "10:00pm", 6.0) for i in range(3, 5)]


# ── E-7 / P-6 ───────────────────────────────────────────────────────────────

def test_salaried_hours_are_not_held_against_the_hourly_budget(db_path, rid):
    _salaried(db_path, rid, "Erik")
    hid = _history(db_path, rid, _rows(*WEEK_ROWS), budget=40.0, hours=80.0)
    review = client_api.publish_review(rid, hid)
    assert review["hours"] == {"hourly": 30.0, "salaried": 50.0, "total": 80.0}
    assert not any(b["key"] == "hours_over" for b in review["blockers"]), "80 all-in hours against a 40h hourly budget"
    # Past the budget on HOURLY hours it is still the ceiling, and says the
    # salaried hours were left out.
    hid = _history(db_path, rid, _rows(*WEEK_ROWS), budget=25.0, hours=80.0)
    [over] = [b for b in client_api.publish_review(rid, hid)["blockers"] if b["key"] == "hours_over"]
    assert over["text"] == ("30h of hourly shifts against a 25h hourly budget — 5h over the ceiling "
                            "(the 50h salaried aren't counted against it)")


def test_history_keeps_the_split_and_every_edit_keeps_it_current(db_path, rid):
    _salaried(db_path, rid, "Erik")
    hid = models.save_schedule_history(rid, WEEK[0], WEEK[6], 80.0, 40.0, 30, _rows(*WEEK_ROWS), [], db_path=db_path)
    conn = get_conn(db_path)
    row = conn.execute("SELECT hours_hourly, hours_salaried FROM schedule_history WHERE id=?", (hid,)).fetchone()
    conn.close()
    assert (row["hours_hourly"], row["hours_salaried"]) == (30.0, 50.0)
    # An edit (Ana's third shift gone) moves the split with the rows.
    conn = get_conn(db_path)
    conn.execute("BEGIN IMMEDIATE")
    sv.write_on(conn, rid, hid, "edited", _rows(*[r for r in WEEK_ROWS if not (r[1] == "Ana" and r[0] == WEEK[2])]))
    conn.commit()
    row = conn.execute("SELECT hours_scheduled, hours_hourly, hours_salaried FROM schedule_history WHERE id=?",
                       (hid,)).fetchone()
    conn.close()
    assert (row["hours_scheduled"], row["hours_hourly"], row["hours_salaried"]) == (74.0, 24.0, 50.0)
    # The History line holds the budget against the hourly hours.
    [h] = [x for x in models.get_schedule_history(rid, db_path=db_path) if x["id"] == hid]
    assert h["hours_hourly"] == 24.0 and h["summary_line"] == "16 hrs under budget"


def test_a_week_saved_before_the_split_is_backfilled_at_boot(db_path, rid):
    _salaried(db_path, rid, "Erik")
    hid = _history(db_path, rid, _rows(*WEEK_ROWS), hours=80.0)
    conn = get_conn(db_path)
    conn.execute("UPDATE schedule_history SET hours_hourly=NULL, hours_salaried=NULL WHERE id=?", (hid,))
    conn.commit(); conn.close()
    assert models.backfill_history_hours(db_path=db_path) >= 1
    conn = get_conn(db_path)
    row = conn.execute("SELECT hours_hourly, hours_salaried FROM schedule_history WHERE id=?", (hid,)).fetchone()
    conn.close()
    assert (row["hours_hourly"], row["hours_salaried"]) == (30.0, 50.0)
    assert models.history_hourly({"hours_hourly": None, "hours_scheduled": 12}) == 12.0
    assert models.history_hourly({"hours_hourly": 9.5, "hours_scheduled": 12}) == 9.5


def test_one_hourly_sum_splits_the_hours():
    rows = [{"employee": "Erik", "scheduled_hours": "10"}, {"employee": "Ana", "scheduled_hours": "6"}]
    assert sr.hours_split(rows, salaried=["erik"]) == {"hourly": 6.0, "salaried": 10.0, "total": 16.0}
    assert sr.hours_split(rows) == {"hourly": 16.0, "salaried": 0.0, "total": 16.0}


# ── SQ-29 / P-16 ────────────────────────────────────────────────────────────

CLEAN = _rows((WEEK[0], "Ana", "4:00pm", "10:00pm", 6.0), (WEEK[1], "Bob", "4:00pm", "10:00pm", 6.0))


def test_the_quality_verdict_is_a_note_never_a_blocker_even_unattended(db_path, rid):
    hid = _history(db_path, rid, CLEAN, quality={"checked": True, "band": "weak", "score": 41,
                                                "confidence": {"level": "low"}, "below_profile": [{"x": 1}]})
    for unattended in (False, True):
        review = client_api.publish_review(rid, hid, unattended=unattended)
        assert review["blockers"] == []
        assert [n["key"] for n in review["notes"]] == ["quality_weak", "quality_low", "below_profile"]
        assert "1 shift below the bar set for it" in review["soft"]


def test_a_week_the_quality_engine_could_not_check_is_a_blocker_with_the_cause(db_path, rid):
    hid = _history(db_path, rid, CLEAN, quality={"checked": False, "error": "KeyError: 'dinner'"})
    [b] = client_api.publish_review(rid, hid)["blockers"]
    assert b["key"] == "quality_unchecked"
    assert b["text"] == ("Shift Quality couldn't check this week (KeyError: 'dinner') — read it yourself "
                         "before it goes to staff")
    # A week from before quality was stored is not a failed check.
    assert client_api.publish_review(rid, _history(db_path, rid, CLEAN))["blockers"] == []


def test_a_publish_check_that_cannot_run_is_never_read_as_clear(db_path, rid, monkeypatch):
    import strategy_routes
    from flask import Flask
    hid = _history(db_path, rid, CLEAN)

    def _boom(*a, **k):
        raise RuntimeError("database is locked")
    monkeypatch.setattr(client_api, "publish_review", _boom)
    with Flask(__name__).test_request_context(f"/?schedule_id={hid}"):
        out, status = strategy_routes._do_publish_check({"restaurant_id": rid, "is_admin": True})
    assert status == 200 and out["blocker_keys"] == ["check_failed"]


# ── P-1 ─────────────────────────────────────────────────────────────────────

def test_settings_the_rules_could_not_read_are_named_in_the_gate(db_path, rid, monkeypatch):
    real = sr._person_settings

    def _broken(c, e, expired, _ss):
        if e["name"] == "Bob":
            raise ValueError("time window '25:00' does not parse")
        return real(c, e, expired, _ss)
    monkeypatch.setattr(sr, "_person_settings", _broken)
    hid = _history(db_path, rid, CLEAN)
    blockers = client_api.publish_review(rid, hid)["blockers"]
    assert {"key": "input:settings:bob",
            "text": "Settings for Bob couldn't be read — fix them in Team; this week wasn't checked against "
                    "their rules"} in blockers
    assert client_api._input_problem_text({"source": "time off"}) == \
        "Time off couldn't be read, so approved days off weren't checked"
    assert client_api._input_problem_text({"source": "something new"}) == \
        "Something new couldn't be read, so the rules check ran without it"


# ── E-22 ────────────────────────────────────────────────────────────────────

def test_a_regenerated_draft_of_a_sent_week_warns_about_the_late_changes(db_path, rid):
    sr.save_compliance(rid, {"notice_days": 14}, db_path=db_path)
    _history(db_path, rid, CLEAN, published=True)
    moved = CLEAN.replace(f"{WEEK[1]},Tuesday,Bob,Server,4:00pm,10:00pm,6.0", f"{WEEK[1]},Tuesday,Bob,Server,5:00pm,10:00pm,5.0")
    draft = _history(db_path, rid, moved)
    review = client_api.publish_review(rid, draft, today=dt.date(2026, 10, 7))      # the week began two days ago
    keys = [b["key"] for b in review["blockers"]]
    assert keys == [f"late_change:{WEEK[1]}"]
    text = review["blockers"][0]["text"]
    assert text.startswith("This replaces the week staff already have. 1 day of shifts changed inside your "
                           "14-day notice window")
    assert "-2 days" not in text and "starts in" not in text


def test_a_week_already_under_way_says_when_it_started(db_path, rid):
    sr.save_compliance(rid, {"notice_days": 14}, db_path=db_path)
    hid = _history(db_path, rid, CLEAN)
    [b] = client_api.publish_review(rid, hid, today=dt.date(2026, 10, 7))["blockers"]
    assert b["key"] == "notice_short"
    assert b["text"] == ("Less notice than your 14-day schedule notice rule: the week of 10/5/26 started 2 days ago")
    [b] = client_api.publish_review(rid, hid, today=dt.date(2026, 10, 5))["blockers"]
    assert b["text"].endswith("the week of 10/5/26 starts today")


def test_saved_changes_to_a_sent_week_carry_the_warning_when_sent(db_path, rid):
    sr.save_compliance(rid, {"notice_days": 14}, db_path=db_path)
    hid = _history(db_path, rid, CLEAN, published=True)
    sv.append(rid, hid, "published", CLEAN, saved_by="owner")
    moved = CLEAN.replace("Bob,Server,4:00pm", "Bob,Server,6:00pm")
    conn = get_conn(db_path)
    conn.execute("BEGIN IMMEDIATE")
    sv.write_on(conn, rid, hid, "edited", moved)
    conn.commit(); conn.close()
    keys = [b["key"] for b in client_api.publish_review(rid, hid, today=dt.date(2026, 10, 2))["blockers"]]
    assert keys == [f"late_change:{WEEK[1]}"]
