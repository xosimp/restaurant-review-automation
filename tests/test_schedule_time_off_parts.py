"""Part of a day off (schedule audit 10/3/26 D-39).

Time off was whole days only: "off until 4pm" or "can't do dinner Saturday"
could not be asked for, so it became a free-text note nothing checked — or
a whole day off the owner then had to undo.
"""
import datetime as dt

import pytest

import models
import person_note_holds as pnh
import schedule_engine as se
import schedule_rules as sr
import time_off

WEEK = ["2026-10-05", "2026-10-06", "2026-10-07", "2026-10-08", "2026-10-09", "2026-10-10", "2026-10-11"]
WED = "2026-10-07"
TODAY = dt.date(2026, 10, 3)


@pytest.fixture(autouse=True)
def _db(db_path, monkeypatch):
    real = models.get_conn
    fake = lambda *a, **k: real(db_path)  # noqa: E731
    monkeypatch.setattr(models, "get_conn", fake)
    monkeypatch.setattr(models, "DB_PATH", db_path)
    monkeypatch.setattr(time_off, "get_conn", fake)
    monkeypatch.setattr(sr, "get_conn", fake)
    import staff_settings
    monkeypatch.setattr(staff_settings, "get_conn", fake)
    import shift_requests
    monkeypatch.setattr(shift_requests, "_tell_managers", lambda *a, **k: None)
    yield


def _rid():
    rid = models.create_restaurant(models.Restaurant(name="Parts Co", owner_email="pt@x.test", module_labor=1))
    for n in ("Ana", "Ben"):
        models.add_manual_team_member(rid, n, role="Server")
    return rid


def _row(name, start, end, d=WED):
    s, e = sr.parse_minutes(start), sr.parse_minutes(end)
    return {"date": d, "day": "Wednesday", "employee": name, "role": "Server", "shift_start": start,
            "shift_end": end, "scheduled_hours": str(round(((e - s) % 1440) / 60, 1)), "notes": ""}


def _approve(rid, **kw):
    row, err = time_off.request_time_off(rid, "Ana", WED, WED, today=TODAY, **kw)
    assert err is None, err
    return time_off.decide(rid, row["id"], True)


def test_a_request_can_be_for_part_of_a_day():
    rid = _rid()
    row, err = time_off.request_time_off(rid, "Ana", WED, WED, today=TODAY, end_time="4pm")
    assert err is None and row["end_time"] == "4:00pm" and row["start_time"] is None
    assert time_off.span_label(row) == "10/7/26, until 4:00pm"
    row2, _ = time_off.request_time_off(rid, "Ben", WED, WED, today=TODAY, daypart="dinner")
    assert row2["daypart"] == "night" and time_off.span_label(row2) == "10/7/26, dinner"
    assert time_off.request_time_off(rid, "Ben", "2026-10-08", "2026-10-08", today=TODAY,
                                     start_time="4pm", daypart="lunch")[1] == "Give a time or lunch/dinner, not both."
    assert time_off.request_time_off(rid, "Ben", "2026-10-09", "2026-10-09", today=TODAY,
                                     start_time="4pm", end_time="1pm")[1] == "The end is before the start."
    assert [r["span_label"] for r in time_off.recent(rid, today=TODAY)] == ["10/7/26, until 4:00pm", "10/7/26, dinner"]


def test_off_until_four_blocks_the_morning_and_leaves_the_evening():
    rid = _rid()
    _approve(rid, end_time="4:00pm")
    c = sr.build_constraints(rid, WEEK, list(sr.DAYS))
    assert c.can_work("Ana", WED)[0]                                   # the day is not off
    ok, why = c.window_ok("Ana", WED, "10:00am", "3:00pm")
    assert not ok and why == "on approved time off (until 4:00pm)"
    assert c.window_ok("Ana", WED, "4:00pm", "10:00pm")[0]
    kinds = {v["kind"] for v in sr.violations([_row("Ana", "10:00am", "3:00pm")], c)}
    assert "approved_time_off" in kinds and "outside_window" not in kinds
    assert not sr.violations([_row("Ana", "4:30pm", "10:00pm")], c, person_only=True)
    assert not c.can_add(_row("Ana", "2:00pm", "8:00pm"), [])[0]       # a fill can't reach into it either
    # Part of a day off is the person's AVAILABLE column (C1, PR-33, PR-20).
    import labor
    import schedule_prompt
    line = schedule_prompt.roster_table(labor._roster_people([("Ana", "Server")], facts=sr.person_facts(c, ["Ana"])))
    assert "Wed 2026-10-07 off until 4:00pm (time off)" in line
    # The availability block lists whole days only.
    assert time_off.approved_in_window(rid, WED, WED, whole_days_only=True) == {}
    assert time_off.approved_in_window(rid, WED, WED) == {"Ana": [WED]}


def test_a_dinner_off_is_that_daypart_only():
    rid = _rid()
    _approve(rid, daypart="dinner")
    c = sr.build_constraints(rid, WEEK, list(sr.DAYS))
    assert c.can_work("Ana", WED, "morning")[0]
    assert not c.can_work("Ana", WED, "night")[0]
    assert {v["kind"] for v in sr.violations([_row("Ana", "5:00pm", "10:00pm")], c)} >= {"approved_time_off"}
    assert not sr.violations([_row("Ana", "10:00am", "2:00pm")], c, person_only=True)


def test_the_swap_search_reads_part_of_a_day_off():
    rid = _rid()
    _approve(rid, end_time="4:00pm")
    rules = se._rules_for_swaps(sr.build_constraints(rid, WEEK, list(sr.DAYS)))
    assert rules["time_windows"]["ana"]["Wednesday"] == (16 * 60, None)
    assert WED not in (rules["blocked_dates"].get("ana") or {})


def test_approving_part_of_a_day_names_only_the_shifts_inside_it(monkeypatch):
    rid = _rid()
    csv = ("date,day,employee,role,shift_start,shift_end,scheduled_hours,notes\n"
           f"{WED},Wednesday,Ana,Server,10:00am,2:00pm,4,\n2026-10-07,Wednesday,Ana,Server,6:00pm,10:00pm,4,\n")
    hid = models.save_schedule_history(rid, WEEK[0], WEEK[-1], 8, 0, 0, csv, [])
    conn = models.get_conn()
    try:
        conn.execute("UPDATE schedule_history SET published_at=datetime('now') WHERE id=?", (hid,))
        conn.commit()
    finally:
        conn.close()
    part = {"start_time": None, "end_time": "4:00pm", "daypart": None}
    hits = time_off.published_conflicts(rid, "Ana", WED, WED, part=part)
    assert [h["shift_start"] for h in hits] == ["10:00am"]
    assert len(time_off.published_conflicts(rid, "Ana", WED, WED)) == 2


def test_a_lunch_hold_on_part_of_the_week_holds_on_those_dates():
    rid = _rid()
    pnh.add_hold(rid, "Ben", "no lunches until 10/7", dayparts=["morning"], start="2026-10-01", end=WED)
    c = sr.build_constraints(rid, WEEK, list(sr.DAYS))
    assert not c.can_work("Ben", "2026-10-06", "morning")[0]
    assert c.can_work("Ben", "2026-10-08", "morning")[0]              # after the hold
    assert c.can_work("Ben", "2026-10-06", "night")[0]
