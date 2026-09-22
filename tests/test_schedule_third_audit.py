"""What the third audit found: a slice that wrote no Saturday was accepted
and a daypart-blind top-up wrote the weekend from morning templates.

Each of these is pinned so the pipeline can never again ship a week whose
busiest days were never drafted, or fill one in as though they had been.
"""
import json

import pytest

import models
import schedule_economics as econ
import schedule_engine as se
import schedule_rules as sr

WEEK = ["2026-10-05", "2026-10-06", "2026-10-07", "2026-10-08", "2026-10-09", "2026-10-10", "2026-10-11"]
DAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
HEADER = "date,day,employee,role,shift_start,shift_end,scheduled_hours,notes"


def _csv(rows):
    return HEADER + "\n" + "\n".join(f"{d},{DAYS[WEEK.index(d)]},{e},{r},{s},{en},{h}," for d, e, r, s, en, h in rows)


def _row(date, emp, start, end, role="Server", hours=6.0, notes=""):
    return {"date": date, "day": DAYS[WEEK.index(date)], "employee": emp, "role": role, "shift_start": start,
            "shift_end": end, "scheduled_hours": str(hours), "notes": notes}


def _c(**kw):
    c = sr.Constraints(restaurant_id=1, week_dates=WEEK, week_days=DAYS)
    for k, v in kw.items():
        setattr(c, k, v)
    return c


# ── F1: a slice that omits a date is retried once, then fails the week ──

def _fake_generator(answers):
    """Each call pops the next answer: a list of (date, employee) rows."""
    calls = []

    def fake(analysis, shifts, week_slice=None, prior_rows=None, **kwargs):
        calls.append({"slice": week_slice, "extra": kwargs.get("extra_blocks") or ""})
        rows = answers.pop(0)
        return {"schedule_csv": _csv([(d, e, "Server", "4:00pm", "10:00pm", 6) for d, e in rows]),
                "narrative": ["ok"], "generation_seconds": 1.0, "truncated": False, "stop_reason": "end_turn"}
    fake.calls = calls
    return fake


def test_a_slice_that_skips_a_day_is_retried_with_the_days_named_then_fails(monkeypatch):
    import labor
    monkeypatch.setattr(se, "_expected_rows", lambda shifts, roster: 300)          # two slices
    monkeypatch.setattr(se, "_week_monday", lambda today, ws=None: __import__("datetime").datetime(2026, 10, 5))
    fake = _fake_generator([
        [(d, "Ana") for d in WEEK[:4]],                    # Mon–Thu fine
        [(WEEK[4], "Ana")],                                # Fri only — Sat, Sun missing
        [(WEEK[4], "Ana")],                                # the retry misses again
    ])
    monkeypatch.setattr(labor, "generate_optimized_schedule", fake)
    with pytest.raises(ValueError) as e:
        se._generate_in_parts({}, [], [("Ana", "Server")], {"tz_name": None})
    assert "Saturday, Sunday" in str(e.value) and "not saved" in str(e.value)
    assert len(fake.calls) == 3 and "WROTE NO SHIFTS FOR 2026-10-10, 2026-10-11" in fake.calls[2]["extra"]


def test_a_retry_that_writes_the_missing_days_is_merged_and_logged(monkeypatch):
    import labor
    monkeypatch.setattr(se, "_expected_rows", lambda shifts, roster: 300)
    monkeypatch.setattr(se, "_week_monday", lambda today, ws=None: __import__("datetime").datetime(2026, 10, 5))
    fake = _fake_generator([
        [(d, "Ana") for d in WEEK[:4]],
        [(WEEK[4], "Ana")],
        [(d, "Bob") for d in WEEK[4:]],
    ])
    monkeypatch.setattr(labor, "generate_optimized_schedule", fake)
    out = se._generate_in_parts({}, [], [("Ana", "Server")], {"tz_name": None})
    assert se._missing_dates(out["schedule_csv"], WEEK) == []
    assert out["chunked"] == 3
    assert [s.get("retried", False) for s in out["slices"]] == [False, True, False]
    assert out["slices"][1]["missing"] == [WEEK[5], WEEK[6]]


def test_a_single_call_that_skips_a_day_goes_to_parts(monkeypatch):
    import labor
    monkeypatch.setattr(se, "_expected_rows", lambda shifts, roster: 50)           # one call
    monkeypatch.setattr(se, "_week_monday", lambda today, ws=None: __import__("datetime").datetime(2026, 10, 5))
    fake = _fake_generator([
        [(d, "Ana") for d in WEEK[:6]],                    # no Sunday
        [(d, "Ana") for d in WEEK[:4]],
        [(d, "Ana") for d in WEEK[4:]],
    ])
    monkeypatch.setattr(labor, "generate_optimized_schedule", fake)
    # Two people: a one-person roster may legitimately leave a day off
    # (SCHED-1), two can cover seven days, so a skipped Sunday is a miss.
    out = se._generate_in_parts({}, [], [("Ana", "Server"), ("Bob", "Server")], {"tz_name": None})
    assert se._missing_dates(out["schedule_csv"], WEEK) == [] and out["chunked"] == 2
    assert out["slices"][0]["missing"] == [WEEK[6]]


def test_very_large_rosters_split_by_department_first(monkeypatch):
    import labor
    monkeypatch.setattr(se, "_expected_rows", lambda shifts, roster: 900)
    monkeypatch.setattr(se, "_week_monday", lambda today, ws=None: __import__("datetime").datetime(2026, 10, 5))
    roster = [("Ana", "Server"), ("Bob", "Line Cook")]
    seen = []

    def fake(analysis, shifts, week_slice=None, prior_rows=None, **kwargs):
        seen.append((tuple(n for n, _ in kwargs.get("roster") or []), tuple(week_slice)))
        return {"schedule_csv": _csv([(d, kwargs["roster"][0][0], "Server", "4:00pm", "10:00pm", 6) for d in week_slice]),
                "narrative": [], "generation_seconds": 1.0, "truncated": False, "stop_reason": "end_turn"}
    monkeypatch.setattr(labor, "generate_optimized_schedule", fake)
    out = se._generate_in_parts({}, [], roster, {"tz_name": None})
    assert out["departments"] == ["KITCHEN", "FRONT OF HOUSE"]
    assert {r for r, _ in seen} == {("Bob",), ("Ana",)}
    assert out["chunked"] == len(seen) >= 4


def test_expected_rows_counts_the_newest_week():
    shifts = [{"date": "2026-09-01"}] * 10 + [{"date": "2026-09-08"}] * 10 + [{"date": "2026-09-15"}] * 40
    assert se._expected_rows(shifts, []) == 40


# ── F2: the top-up is keyed by daypart, capped, and never writes an empty day ──

def _no_avail(monkeypatch):
    monkeypatch.setattr(models, "get_staff_availability", lambda r, *a, **k: [])


def test_top_up_never_fills_a_day_the_model_left_empty(monkeypatch):
    _no_avail(monkeypatch)
    rows = [_row(d, n, "4:00pm", "10:00pm") for d in WEEK[:5] for n in ("Ana", "Bob", "Cy")]      # nothing Sat/Sun
    out, added, dates = se._top_up_hours_gap(list(rows), {d: 40.0 for d in WEEK}, 280.0, 90.0, 1, {}, {}, constraints=_c())
    assert dates == {} and added == 0


def test_top_up_thinks_in_dayparts_and_copies_that_dayparts_times(monkeypatch):
    _no_avail(monkeypatch)
    rows = []
    for d in WEEK[:6]:
        rows += [_row(d, n, "11:00am", "3:00pm", hours=4) for n in ("Ana", "Bob")]                 # two lunch servers
        rows += [_row(d, n, "4:00pm", "9:00pm", hours=5) for n in ("Cy", "Dee", "Eve")]           # three dinner servers
    rows += [_row(WEEK[6], "Ana", "11:00am", "3:00pm", hours=4), _row(WEEK[6], "Bob", "11:00am", "3:00pm", hours=4),
             _row(WEEK[6], "Cy", "4:00pm", "9:00pm", hours=5)]                                    # Sunday dinner is thin
    out, added, dates = se._top_up_hours_gap(list(rows), {d: 40.0 for d in WEEK}, 400.0, 200.0, 1, {}, {}, constraints=_c())
    new = [r for r in out if r["date"] == WEEK[6] and r not in rows]
    assert new and all(r["shift_start"] == "4:00pm" for r in new)          # dinner rows, not morning clones
    assert all(r["employee"] in ("Dee", "Eve") for r in new)


def test_top_up_is_capped_at_a_quarter_of_the_days_target(monkeypatch):
    _no_avail(monkeypatch)
    rows = []
    for d in WEEK[:6]:
        rows += [_row(d, n, "4:00pm", "9:00pm", hours=5) for n in ("Ana", "Bob", "Cy", "Dee", "Eve", "Fay")]
    rows += [_row(WEEK[6], "Ana", "4:00pm", "9:00pm", hours=5)]                                    # Sunday: 1 of 6
    out, added, dates = se._top_up_hours_gap(list(rows), {d: 40.0 for d in WEEK}, 400.0, 185.0, 1, {}, {}, constraints=_c())
    sunday_added = sum(float(r["scheduled_hours"]) for r in out if r["date"] == WEEK[6] and r not in rows)
    assert 0 < sunday_added <= 10.0                                          # 25% of 40h, not the five missing shifts


def test_floor_pass_keeps_trying_candidates_after_a_refusal(monkeypatch):
    _no_avail(monkeypatch)
    floors = {"Cook": {"morning": 0, "night": 2, "days": {}}}
    # Ana already has 38h (a 4h add would pass 40) and Bob is free: the old
    # loop spent one of its two slots on refusing Ana and added only one cook.
    rows = [_row(WEEK[0], "Ana", "4:00pm", "10:00pm", role="Cook", hours=38), _row(WEEK[0], "Bob", "4:00pm", "10:00pm", role="Cook"),
            _row(WEEK[0], "Cy", "4:00pm", "10:00pm", role="Cook")]
    out, added, dates = se._ensure_role_floors(list(rows), WEEK[:2], DAYS[:2], 1, {}, {}, floors=floors, constraints=_c())
    tue_cooks = [r["employee"] for r in out if r["date"] == WEEK[1]]
    assert sorted(tue_cooks) == ["Bob", "Cy"]


# ── F3: a crash in the checks fails the job rather than shipping the raw week ──

def test_a_crash_in_the_checks_is_a_failed_job_not_a_clean_week(monkeypatch, db_path):
    import client_api
    real = models.get_conn
    for mod in (models, client_api, se, sr):
        monkeypatch.setattr(mod, "get_conn", lambda *a, **k: real(db_path), raising=False)
    monkeypatch.setattr(models, "DB_PATH", db_path)
    monkeypatch.setattr(models, "_cached_shifts", lambda r: [])
    rid = models.create_restaurant(models.Restaurant(name="Crash Co", owner_email="c@x.com"), db_path=db_path)
    monkeypatch.setattr(se, "_build_schedule_result", lambda r, week_start=None: {
        "ok": True, "schedule_csv": _csv([(WEEK[0], "Ana", "Server", "4:00pm", "10:00pm", 6)]), "roster": ["Ana"],
        "week_dates": WEEK, "week_days": DAYS, "summary": [], "hours_budget": 40})
    monkeypatch.setattr(se._rules, "build_constraints", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    finished = {}
    monkeypatch.setattr(se._ops, "finish_async_job", lambda job_id, status, result: finished.update(status=status, result=result))
    monkeypatch.setattr(models, "get_staff_notes", lambda r: [])
    monkeypatch.setattr(models, "get_close_times", lambda r: {})
    monkeypatch.setattr(models, "get_role_close_buffers", lambda r: {})
    se._run_schedule_job("job", rid)
    assert finished["status"] == "error"
    assert "checks failed" in finished["result"]["error"] and "nothing was saved" in finished["result"]["error"]
    conn = models.get_conn(db_path)
    assert conn.execute("SELECT COUNT(*) FROM schedule_history WHERE restaurant_id=?", (rid,)).fetchone()[0] == 0
    conn.close()


# ── rules: stay after close, an unusable manager rule, sibling published-only, overtime at 40 ──

def test_the_last_of_a_role_must_stay_until_its_minutes_after_close():
    c = _c(close_times={"Monday": "9:00pm"}, close_mins={"bartender": 60})
    rows = [_row(WEEK[0], "Ana", "4:00pm", "9:00pm", role="Bartender", hours=5), _row(WEEK[0], "Bob", "5:00pm", "9:30pm", role="Bartender", hours=4.5)]
    v = [x for x in sr.violations(rows, c) if x["kind"] == "ends_before_role_close"]
    assert len(v) == 1 and v[0]["employee"] == "Bob" and "until 10:00pm" in v[0]["detail"] and not v[0]["hard"]
    rows[1]["shift_end"] = "10:00pm"
    assert not [x for x in sr.violations(rows, c) if x["kind"] == "ends_before_role_close"]
    assert "Stays after close" in sr.prompt_block(c)


def test_manager_on_duty_with_nobody_to_satisfy_it_says_so():
    c = _c(compliance={**sr.DEFAULTS, "manager_on_duty": True})
    v = sr.violations([_row(WEEK[0], "Ana", "4:00pm", "10:00pm")], c)
    assert [x["kind"] for x in v] == ["manager_rule_unusable"] and not v[0]["hard"]


def test_sibling_collisions_read_only_published_weeks():
    src = open(models.__file__).read()
    body = src[src.index("def sibling_location_shifts"):src.index("def sibling_location_shifts") + 3000]
    assert "published_at IS NOT NULL" in body


def test_overtime_is_priced_at_forty_whatever_the_owners_ceiling():
    src = open(se.__file__).read()
    assert 'ceiling=min(float(_constraints.compliance.get("weekly_hours_ceiling") or 40), 40.0)' in src
    rows = [_row(d, "Ana", "9:00am", "5:00pm", hours=8) for d in WEEK[:6]]
    assert econ.priced_cost(rows, {"Server": 20.0}, 20.0, ceiling=min(48, 40))["overtime_hours"] == 8.0
