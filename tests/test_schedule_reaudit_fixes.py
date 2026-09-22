"""What the second schedule audit found in the deterministic layer.

Each of these was a real error on a real generation: the fix pass
stripped an over-hours server of her whole week, a lunch-into-dinner
double read as a rest breach, a built-in profile zeroed a fully staffed
Saturday on ratings for people who do not work there, a three-week-old
draft seeded "7 days in a row" warnings, engine marks reached the
staff's own schedule, and a stale name crashed the portal.
"""
import datetime as dt

import pytest

import labor
import models
import schedule_engine
import schedule_rules as sr
import shift_quality as sq
from models import create_restaurant, Restaurant, get_conn

WEEK = ["2026-10-05", "2026-10-06", "2026-10-07", "2026-10-08", "2026-10-09", "2026-10-10", "2026-10-11"]
DAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
HEADER = "date,day,employee,role,shift_start,shift_end,scheduled_hours,notes\n"


def _row(date, emp, start, end, role="Server", hours=8.0):
    return {"date": date, "day": DAYS[WEEK.index(date)], "employee": emp, "role": role, "shift_start": start,
            "shift_end": end, "scheduled_hours": str(hours), "notes": ""}


def _c(**kw):
    c = sr.Constraints(restaurant_id=1, week_dates=WEEK, week_days=DAYS)
    for k, v in kw.items():
        setattr(c, k, v)
    return c


# ── F1: the fix pass moves only the excess hours ───────────────────────

def test_over_hours_fix_moves_only_the_excess_not_the_whole_week():
    rows = [_row(d, "Ana", "9:00am", "5:00pm") for d in WEEK[:6]]           # 48h
    rows += [_row(d, "Bob", "9:00am", "5:00pm") for d in WEEK[:2]]           # 16h, same role
    c = _c(active={"ana", "bob"})
    viols = sr.violations(rows, c)
    assert sum(1 for v in viols if v["kind"] == "over_max_hours") == 6
    out = sq.apply_fixes(rows, viols, roster=["Ana", "Bob"], rules=schedule_engine._rules_for_swaps(c))
    ana_hours = sum(float(r["scheduled_hours"]) for r in out["rows"] if r["employee"] == "Ana")
    assert ana_hours == 40.0, ana_hours                       # not 0, not 48
    assert len(out["fixes"]) == 1 and out["fixes"][0]["from"] == "Ana" and out["fixes"][0]["to"] == "Bob"
    assert out["fixes"][0]["index"] == 5                      # the latest row moved, not the first
    assert sr.violations(out["rows"], c) == []


# ── F2: a same-day double is not a rest breach ─────────────────────────

def test_lunch_into_dinner_double_is_not_a_rest_gap():
    rows = [_row(WEEK[0], "Ana", "11:00am", "4:00pm", hours=5), _row(WEEK[0], "Ana", "4:00pm", "10:00pm", hours=6)]
    assert [v["kind"] for v in sr.violations(rows, _c())] == []
    ok, why = _c().rest_ok("Ana", rows[1], [rows[0]])
    assert ok
    idx = sq._SwapIndex(rows, {}, {}, {"min_rest_hours": 10})
    assert idx.person_fits("Ana", rows[1], ignore_index=1)     # the swap index agrees
    # The overnight turnaround is still the rule, and an overlap is still wrong.
    nxt = _row(WEEK[1], "Ana", "7:00am", "3:00pm")
    assert any(v["kind"] == "rest_gap" for v in sr.violations(rows + [nxt], _c()))
    over = _row(WEEK[0], "Ana", "3:00pm", "9:00pm", hours=6)
    assert any(v["kind"] == "overlap" for v in sr.violations([rows[0], over], _c()))


# ── F3 / F19: phantom ratings and the built-in leader wish ─────────────

def test_ratings_for_people_off_the_roster_do_not_judge_the_week():
    signals = {"roster": ["Ana", "Bob"], "scores": {"Ana": 4, "Ghost": 5}, "leader_flags": {"Ghost": True, "Bob": True}}
    schedule_engine._reconcile_to_roster(signals)
    assert signals["scores"] == {"Ana": 4} and signals["leader_flags"] == {"Bob": True}
    signals = {"roster": [], "scores": {"Ghost": 5}}
    schedule_engine._reconcile_to_roster(signals)
    assert signals["scores"] == {"Ghost": 5}                  # no roster on file: everything stands


def test_a_built_in_profiles_leader_wish_costs_points_but_never_caps_the_shift():
    rows = [_row(WEEK[5], n, "4:00pm", "10:00pm", role="Bartender") for n in ("Ana", "Bob")]
    rows += [_row(WEEK[5], n, "4:00pm", "10:00pm", role="Server") for n in ("Cy", "Dee", "Eve")]
    # One rating exists, so the engine is not blind; nobody is a leader.
    res = sq.score_rows(rows, scores={"Ana": 3}, typical_headcount={"Bartender": 2, "Server": 3})
    sat = next(s for s in res["shifts"] if s["date"] == WEEK[5])
    lead = next((d for d in sat["dimensions"] if d["key"] == "leadership"), None)
    assert lead is not None and lead["score"] == 0
    assert sat.get("capped_by") != "leadership"
    assert sat["score"] > 0
    # An owner's own rule is critical and does cap.
    res2 = sq.score_rows(rows, scores={"Ana": 3}, typical_headcount={"Bartender": 2, "Server": 3},
                         leader_rules=[{"role": "Bartender", "count": 1, "min_score": 4, "days": ["Saturday"]}])
    sat2 = next(s for s in res2["shifts"] if s["date"] == WEEK[5])
    assert sat2.get("capped_by") == "leadership"


# ── F4: only the adjacent week seeds fatigue and fairness ──────────────

@pytest.fixture
def rid(db_path, monkeypatch):
    real = models.get_conn
    for mod in (models, schedule_engine, sr):
        monkeypatch.setattr(mod, "get_conn", lambda *a, **k: real(db_path), raising=False)
    monkeypatch.setattr(models, "DB_PATH", db_path)
    return create_restaurant(Restaurant(name="Seed Co", owner_email="s@x.com"), db_path=db_path)


def _history(db_path, rid, week_start, week_end, csv_text, published=False):
    conn = get_conn(db_path)
    models._ensure_history_columns(conn)
    cur = conn.execute("INSERT INTO schedule_history (restaurant_id, week_start, week_end, hours_scheduled, hours_budget, "
                       "labor_target, schedule_csv, summary_json, published_at) VALUES (?,?,?,?,?,?,?,'[]',?)",
                       (rid, week_start, week_end, 8, 40, 30, csv_text, "2026-09-01 10:00:00" if published else None))
    conn.commit()
    conn.close()
    return cur.lastrowid


def test_a_stale_draft_never_seeds_the_prior_week(db_path, rid):
    stale = HEADER + "2026-09-13,Sunday,Ana,Server,4:00pm,10:00pm,6.0,\n"
    _history(db_path, rid, "2026-09-07", "2026-09-13", stale)                # three weeks old
    assert schedule_engine._prior_week_assignments(rid, before=WEEK[0]) == {}
    adjacent = HEADER + "2026-10-04,Sunday,Ana,Server,4:00pm,10:00pm,6.0,\n"
    _history(db_path, rid, "2026-09-28", "2026-10-04", adjacent)
    seeded = schedule_engine._prior_week_assignments(rid, before=WEEK[0])
    assert list(seeded) == ["Ana"] and seeded["Ana"][0]["date"] == "2026-10-04"


def test_a_published_adjacent_week_wins_over_a_newer_stale_one(db_path, rid):
    _history(db_path, rid, "2026-09-28", "2026-10-04", HEADER + "2026-10-03,Saturday,Bob,Server,4:00pm,10:00pm,6.0,\n", published=True)
    assert list(schedule_engine._prior_week_assignments(rid, before=WEEK[0])) == ["Bob"]


# ── F8: too many days in a row is a hard breach, tail included ─────────

def test_seven_days_in_a_row_is_hard_and_counts_the_published_tail():
    tail = {"date": "2026-10-03", "day": "Saturday", "employee": "Ana", "role": "Server", "shift_start": "4:00pm",
            "shift_end": "10:00pm", "scheduled_hours": "6"}
    tail2 = dict(tail, date="2026-10-04", day="Sunday")
    c = _c(base_rows={"ana": [tail, tail2]})
    rows = [_row(d, "Ana", "9:00am", "3:00pm", hours=6) for d in WEEK[:5]]     # Mon–Fri after Sat+Sun published = 7
    v = [x for x in sr.violations(rows, c) if x["kind"] == "long_run"]
    assert len(v) == 1 and v[0]["hard"] and "7 days in a row" in v[0]["detail"]
    assert v[0]["date"] == WEEK[4]                                             # the row to drop is the last of the run
    assert not any(x["kind"] == "long_run" for x in sr.violations(rows, _c()))  # five alone is fine
    assert "days in a row" in sr.prompt_block(c)


# ── F10: engine marks never reach staff ────────────────────────────────

def test_staff_see_the_note_without_the_engines_marks():
    csv_text = HEADER + (f"{WEEK[0]},Monday,Ana,Server,4:00pm,10:00pm,6.0,double carryover — NEEDS REVIEW: not enough rest\n"
                         f"{WEEK[1]},Tuesday,Ana,Server,4:00pm,10:00pm,6.0,opener (was Bob — over their hours ceiling)\n"
                         f"{WEEK[2]},Wednesday,Ana,Server,4:00pm,10:00pm,6.0,added — coverage top-up\n"
                         f"{WEEK[3]},Thursday,Ana,Server,4:00pm,10:00pm,6.0,close\n")
    notes = [s["notes"] for s in labor.employee_shifts_from_csv(csv_text, "Ana")]
    assert notes == ["double carryover", "opener", "", "close"]


# ── F12: role floors on the daypart split, from the day's hours ────────

def test_floor_windows_follow_the_days_open_and_close():
    c = _c(open_times={"Monday": "6:00am"}, close_times={"Monday": "11:00pm"})
    windows = dict(schedule_engine._daypart_windows(c, "Monday"))
    assert windows["morning"] == (6 * 60, 15 * 60) and windows["night"] == (15 * 60, 23 * 60)
    early_cook = _row(WEEK[0], "Ana", "6:00am", "10:30am", role="Cook", hours=4.5)
    assert schedule_engine._window_overlap(early_cook, windows["morning"])
    assert not schedule_engine._window_overlap(early_cook, windows["night"])
    nothing = dict(schedule_engine._daypart_windows(_c(), "Monday"))
    assert nothing["morning"][0] == 6 * 60 and nothing["night"][1] == 24 * 60 - 1


# ── F16 / F37: deleting a week, and the portal's stale name ────────────

def test_deleting_a_week_needs_the_publish_permission_and_spares_published_weeks():
    src = open("mobile_api.py").read()
    body = src[src.index("def mobile_schedule_history_delete"):src.index("def mobile_labor_availability")]
    assert "SCHEDULE_PUBLISH" in body and "published_at" in body
    assert body.index("published_at") < body.index("delete_schedule_history(history_id")


def test_the_portal_no_longer_names_a_variable_that_does_not_exist():
    import staff_schedule
    src = open(staff_schedule.__file__).read()
    assert "history[0]" not in src


def test_the_over_budget_blocker_is_said_once(db_path, rid, monkeypatch):
    import client_api
    monkeypatch.setattr(client_api, "get_conn", lambda *a, **k: models.get_conn(db_path))
    conn = get_conn(db_path)
    models._ensure_history_columns(conn)
    conn.execute("INSERT INTO schedule_history (restaurant_id, week_start, week_end, hours_scheduled, hours_budget, labor_target, "
                 "schedule_csv, summary_json, review_json) VALUES (?,?,?,?,?,?,?,'[]',?)",
                 (rid, WEEK[0], WEEK[6], 1457, 1314, 30, HEADER + f"{WEEK[0]},Monday,Ana,Server,4:00pm,10:00pm,6.0,\n",
                  '{"hard": 0, "lines": ["⚠ 1,457h scheduled against a 1,314h budget — 143h over the ceiling"]}'))
    conn.commit()
    hid = conn.execute("SELECT MAX(id) FROM schedule_history").fetchone()[0]
    conn.close()
    b = client_api.publish_blockers(rid, hid)
    assert sum(1 for x in b if "over the ceiling" in x) == 1
