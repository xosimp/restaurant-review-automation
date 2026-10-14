"""Schedule fix round 10/11/26, workstream SP — what a generation's solves
and passes may use: deterministic work scaled to the model, bounded memory
returned to the OS, no search skipped for time, the Studio's what-if and
cache bounded, a generation's reads frozen (blind re-audit STRESS-11,
STRESS-12, SCALE-15, SCALE-16, SCALE-18, SCALE-19, SCALE-20, SCALE-21,
SCALE-22, SCALE-24)."""
import json
import os
import sqlite3
import time

import pytest

import job_queue
import models
import ops
import schedule_cpsat as cps
import schedule_engine as se
import schedule_rules as sr
import shift_quality as sq

pytestmark = pytest.mark.skipif(not cps.available(), reason="OR-Tools is not installed here")

WEEK = ["2026-10-05", "2026-10-06", "2026-10-07", "2026-10-08", "2026-10-09", "2026-10-10", "2026-10-11"]
DAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]


def _model(n=300, seed=3):
    """A small assignment-like model with one optimum: n booleans, pick
    exactly n/3 of them, weighted costs."""
    from ortools.sat.python import cp_model as cp
    import random
    rng = random.Random(seed)
    m = cp.CpModel()
    xs = [m.new_bool_var(f"x{i}") for i in range(n)]
    m.add(sum(xs) == n // 3)
    for i in range(0, n - 1, 2):
        m.add(xs[i] + xs[i + 1] <= 1)
    m.minimize(sum(rng.randint(1, 50) * x for x in xs))
    return m, xs


def _solver(budget=5.0):
    s = cps._solver_for(budget, workers=2, deterministic=True)
    return s


# ── STRESS-11 / SCALE-18: the solve runs in a child, the same answer ─────────

def test_a_large_solve_runs_in_a_child_process_and_answers_exactly_as_in_this_one(monkeypatch):
    m, xs = _model()
    monkeypatch.setenv("SCHEDULE_CPSAT_ISOLATE", "0")
    inline = _solver()
    st_in = cps.run(inline, m)
    assert inline.sp_info["isolated"] is False
    monkeypatch.setenv("SCHEDULE_CPSAT_ISOLATE", "fork")
    monkeypatch.setattr(cps, "ISOLATE_MIN_VARS", 1)
    child = _solver()
    st_ch = cps.run(child, m)
    assert child.sp_info["isolated"] is True and child.sp_info["peak_mb"] > 0
    # the same model and parameters, a deterministic search: the same week
    assert st_ch == st_in == cps._cp.OPTIMAL
    assert child.objective_value == inline.objective_value
    assert [child.value(x) for x in xs] == [inline.value(x) for x in xs]
    assert child.status_name(st_ch) == "OPTIMAL" and child.wall_time >= 0
    # CP-SAT's own memory bound is set at what this process holds plus the allowance
    assert child.parameters.max_memory_in_mb >= cps.SOLVE_MEMORY_MB


def test_a_child_past_its_wall_clock_is_killed_and_answers_nothing_never_retried_here(monkeypatch):
    m, _xs = _model(n=3000)
    monkeypatch.setenv("SCHEDULE_CPSAT_ISOLATE", "fork")
    monkeypatch.setattr(cps, "ISOLATE_MIN_VARS", 1)
    s = _solver(budget=60.0)
    s.parameters.max_time_in_seconds = 0.5
    monkeypatch.setattr(cps, "ISOLATE_GRACE_SECONDS", -0.5)        # its deadline is now
    calls = []
    real = s.solve
    s.solve = lambda model: calls.append(1) or real(model)          # an in-process retry would land here
    st = cps.run(s, m)
    assert st == cps._cp.UNKNOWN
    assert s.sp_info["killed"] is True and "wall clock" in s.sp_info["note"]
    assert calls == []


def _square_in_child(x):
    return {"pid": os.getpid(), "value": x * x, "in_child": cps._IN_CHILD, "nested": cps.isolation()}


def _boom():
    raise ValueError("the same error here")


def _sleep_forever():
    time.sleep(60)


def test_stage_one_runs_in_a_child_that_answers_what_this_process_would(monkeypatch):
    monkeypatch.setenv("SCHEDULE_CPSAT_ISOLATE", "fork")
    got = cps.isolated(_square_in_child, 7)
    assert got["value"] == 49 and got["pid"] != os.getpid()
    # inside the child everything solves inline: no grandchildren
    assert got["in_child"] is True and got["nested"] == "off"
    assert cps._IN_CHILD is False
    monkeypatch.setenv("SCHEDULE_CPSAT_ISOLATE", "0")
    assert cps.isolated(_square_in_child, 7)["pid"] == os.getpid()


def test_a_child_that_raises_is_run_here_so_the_caller_sees_what_it_always_did(monkeypatch):
    monkeypatch.setenv("SCHEDULE_CPSAT_ISOLATE", "fork")
    with pytest.raises(ValueError, match="the same error here"):
        cps.isolated(_boom)


def test_a_child_past_its_time_is_killed_and_said_never_rerun_here(monkeypatch):
    monkeypatch.setenv("SCHEDULE_CPSAT_ISOLATE", "fork")
    t0 = time.monotonic()
    with pytest.raises(cps.IsolatedKilled, match="wall clock"):
        cps.isolated(_sleep_forever, timeout=0.3)
    assert time.monotonic() - t0 < 10


# ── SCALE-19: work scaled to the model, never below what was stated ──────────

def test_the_work_budget_scales_with_the_model_and_never_falls_below_the_stated_one(monkeypatch):
    monkeypatch.setattr(cps, "DET_PER_KVAR", 0.6)
    monkeypatch.setattr(cps, "DET_CAP", 120.0)
    assert cps.work_budget(7.5, 2_000) == 7.5                       # a small week: exactly as before
    assert cps.work_budget(7.5, 33_208) == pytest.approx(19.92)      # 250 people's Stage 1
    assert cps.work_budget(20.0, 1_000_000) == 120.0                 # the cap
    assert cps.work_budget(200.0, 1_000_000) == 200.0                # never below what was stated


def test_the_memory_bounded_portfolio_is_only_for_a_large_model(monkeypatch):
    monkeypatch.setattr(cps, "LEAN_MIN_VARS", 15_000)
    small = cps._solver_for(5.0, n_vars=2_000)
    large = cps._solver_for(5.0, n_vars=40_000)
    assert list(small.parameters.ignore_subsolvers) == []
    assert set(large.parameters.ignore_subsolvers) == set(cps.LEAN_IGNORE)
    # the three that prove the schedule models — measured at 250 people
    assert not {"default_lp", "max_lp", "no_lp"} & set(cps.LEAN_IGNORE)


def test_a_phase_already_at_its_least_cost_is_proved_there_with_no_search():
    from ortools.sat.python import cp_model as cp
    m = cp.CpModel()
    a, b = m.new_bool_var("a"), m.new_bool_var("b")
    k = m.new_int_var(0, 9, "k")
    m.add(a + b >= 1)
    m.minimize(3 * a + 2 * k)
    assert cps._at_bound(m, [0, 1, 0]) == 0                          # a=0, k=0: the least it can be
    assert cps._at_bound(m, [1, 0, 0]) is None                       # 3: a search could do better
    assert cps._at_bound(m, [0, 1]) is None                          # not a value per variable


def test_the_solver_draft_records_its_scaled_budgets_and_how_it_ran(monkeypatch):
    """Stage 1 and Stage 2 each read the budget their model's size gives and
    say how they ran (SP: a fixed 7.5 s Stage 1 found no week at all at 250
    people, and the draft silently fell back to the backtracking solver)."""
    monkeypatch.setattr(cps, "DET_PER_KVAR", 1000.0)                 # any model: past its stated budget
    monkeypatch.setattr(cps, "DET_CAP", 9.0)
    c = sr.Constraints(restaurant_id=None, week_dates=WEEK[:2], week_days=DAYS[:2],
                       roster_names=["Ann", "Bo", "Cy"], active={"ann", "bo", "cy"})
    c.roster_roles = {"Ann": "Server", "Bo": "Server", "Cy": "Server"}
    reqs = [{"date": d, "day": DAYS[i], "daypart": "night",
             "roles": [{"role": "Server", "required": 1, "floor": 1, "typical": 1}]} for i, d in enumerate(WEEK[:2])]
    got = cps.solver_draft(WEEK[:2], c, reqs, max_seconds=4.0, roster_roles={"Ann": "Server", "Bo": "Server",
                                                                               "Cy": "Server"})
    st = got["stats"]
    assert st["budget"]["shapes_scaled"] == 9.0                       # raised to the cap by its size
    sh = st["shapes"]
    assert sh["budget"] == 9.0 and sh["variables"] > 0 and "memory_capped" in sh
    if st.get("status") in ("optimal", "time_limit"):
        assert st["budget"]["assign_scaled"] == 9.0
        assert st["memory"]["assign"]["memory_capped"] is False
        assert st["memory_capped"] is False and st["wall_capped"] is False


# ── SCALE-15: no search skipped for time; picks on rows read, never seconds ───

class _Clock:
    def __init__(self, left):
        self.left = left

    def seconds_left(self):
        return self.left

    def stopped(self):
        return False

    def stop_requested(self):
        return False


def _x(left=None):
    c = sr.Constraints(restaurant_id=1, week_dates=list(WEEK), week_days=list(DAYS), roster_names=["Ann"],
                       active={"ann"})
    return se.RepairContext(1, {"week_dates": WEEK, "week_days": DAYS, "roster": ["Ann"]}, c,
                            clock=_Clock(left) if left is not None else None)


def test_a_search_runs_even_when_the_job_clock_has_nothing_left(monkeypatch):
    ran = []
    table = {k: (lambda rows, x: {"rows": rows}) for k in se._STAGE_FNS}
    table["solver"] = lambda rows, x: ran.append(("solver", x.search_seconds())) or {"rows": rows}
    table["optimizer"] = lambda rows, x: ran.append(("optimizer", x.search_seconds())) or {"rows": rows}
    monkeypatch.setattr(se, "_STAGE_FNS", table)
    rows = [{"date": WEEK[0], "day": DAYS[0], "employee": "Ann", "role": "Server", "shift_start": "4:00pm",
             "shift_end": "10:00pm", "scheduled_hours": "6", "notes": ""}]
    loop = se.repair_week(rows, _x(left=0.0))
    assert [k for k, _s in ran] == ["solver", "optimizer"]
    # its wall time reserved whatever the clock and the loop's other stages took
    assert all(s >= se.REPAIR_SEARCH_RESERVE_SECONDS for _k, s in ran)
    assert loop["skipped"] == []


def test_a_searchs_reserved_time_does_not_shrink_with_the_loops_own_stages():
    x = _x(left=10_000.0)
    x.started = time.monotonic() - 10 * se.REPAIR_MAX_SECONDS        # the Python stages took ages
    assert x.search_seconds() == pytest.approx(10_000.0 - se.REPAIR_RESERVE_SECONDS, abs=1.0)
    x = _x(left=5.0)
    assert x.search_seconds() == se.REPAIR_SEARCH_RESERVE_SECONDS


def test_a_memory_capped_solve_is_said_on_the_week_and_counted():
    x = _x()
    x.memory_capped.append("solver")
    em = se._emergency_of({"cycles": 1}, x)
    assert em == {"memory": ["solver"]}
    assert "stopped at its memory limit" in se.repair_emergency_line({"emergency": em})
    result = {"repair": {"emergency": em}, "draft_source": {"solver": {"wall_capped": True}}}
    assert se.solve_capped(result) == ["wall:draft", "memory:solver"]
    assert se.solve_capped({}) == []


class _FakeScorer:
    def __init__(self, work):
        self.work = work
        self.started = time.monotonic() - 10 ** 6                    # long ago: the old clock said "out of time"
        self.asked = 0

    def best(self, options):
        self.asked += 1
        return (len(options) - 1, 1.0)


def test_a_fill_pick_consults_its_scorer_by_rows_read_never_by_seconds():
    legal = [("Ann", {"employee": "Ann"}), ("Bo", {"employee": "Bo"})]
    fresh = _FakeScorer(work=10)
    assert se._fill_pick(fresh, [], legal)[0] == "Bo" and fresh.asked == 1
    spent = _FakeScorer(work=se.FILL_SCORE_WORK + 1)
    assert se._fill_pick(spent, [], legal)[0] == "Ann" and spent.asked == 0


def test_the_local_scorer_counts_the_rows_it_reads():
    rows = [{"date": WEEK[i], "day": DAYS[i], "employee": n, "role": "Cook", "shift_start": "4:00pm",
             "shift_end": "10:00pm", "scheduled_hours": "6", "notes": ""} for i in range(3) for n in ("Ana", "Bob")]
    sc = sq.LocalScorer(rows, profiles=[sq.ShiftProfile()])
    first = sc.work
    assert first == len(rows) + len(rows)                            # the frame, then every date built
    sc.score([r for r in rows if not (r["date"] == WEEK[1] and r["employee"] == "Bob")])
    assert sc.work == first + 5 + 1                                  # the frame, then Tuesday's one row


# ── SCALE-16: generation slots sized to the cores ────────────────────────────

def test_generation_slots_are_the_cores_over_each_solves_threads(monkeypatch):
    monkeypatch.delenv("QUEUE_GENERATION_CONCURRENCY", raising=False)
    monkeypatch.setattr(job_queue, "_gateway_streams", lambda: None)
    monkeypatch.setattr(cps, "WORKERS", 4)
    monkeypatch.setattr(job_queue, "cpu_cores", lambda: 8)
    monkeypatch.setattr(se, "SCHEDULE_GEN_WORKERS", 4)
    assert job_queue.group_concurrency("generation") == 2
    monkeypatch.setattr(job_queue, "cpu_cores", lambda: 3)
    assert job_queue.group_concurrency("generation") == 1
    monkeypatch.setenv("QUEUE_GENERATION_CONCURRENCY", "3")            # the operator's own word stands
    assert job_queue.group_concurrency("generation") == 3


def test_the_cores_are_the_containers_quota_not_the_hosts(monkeypatch, tmp_path):
    import builtins
    real = builtins.open

    def fake_open(path, *a, **k):
        if str(path) == "/sys/fs/cgroup/cpu.max":
            p = tmp_path / "cpu.max"
            p.write_text("200000 100000\n")
            return real(p, *a, **k)
        return real(path, *a, **k)
    monkeypatch.setattr(builtins, "open", fake_open)
    monkeypatch.setattr(os, "sched_getaffinity", lambda pid: set(range(16)), raising=False)
    assert job_queue.cpu_cores() == 2                                  # 2 CPUs of quota on a 16-core host


# ── SCALE-20: the top-up re-reads only the day it added to ───────────────────

def test_the_top_up_reads_each_days_shortfall_once_and_again_only_where_it_added(monkeypatch):
    monkeypatch.setattr(models, "get_staff_availability", lambda r, *a, **k: [])
    rows = [{"date": WEEK[i], "day": DAYS[i], "employee": n, "role": "Server", "shift_start": "4:00pm",
             "shift_end": "9:00pm", "scheduled_hours": "5", "notes": ""} for i in range(7) for n in ("Ana",)]
    c = sr.Constraints(restaurant_id=1, week_dates=WEEK, week_days=DAYS)
    c.roster_roles = {"Dee": "Server", "Eve": "Server", "Ana": "Server"}
    reqs = [{"date": d, "day": DAYS[i], "daypart": "night",
             "roles": [{"role": "Server", "required": 2, "floor": 0, "typical": 2}]} for i, d in enumerate(WEEK)]
    seen = []
    real = se.requirement_shortfall

    def counted(rs, index, date, c=None, skip=frozenset()):
        seen.append(date)
        assert all(r.get("date") == date for r in rs)                   # only that day's rows are read
        return real(rs, index, date, c, skip)
    monkeypatch.setattr(se, "requirement_shortfall", counted)
    out, added, dates = se._top_up_hours_gap([dict(r) for r in rows], {d: 40.0 for d in WEEK}, 400.0, 35.0, 1,
                                              {}, {}, constraints=c, requirements=reqs)
    assert dates                                                        # it filled
    # each day once, then once more for each day a shift went onto
    assert len(seen) <= len(WEEK) + sum(dates.values())
    assert sorted(set(seen)) == sorted(WEEK)


def test_rest_reads_the_published_tail_of_the_person_and_whoever_is_linked_to_them():
    c = sr.Constraints(restaurant_id=1, week_dates=WEEK, week_days=DAYS)
    c.compliance = {"min_rest_hours": 10}
    tail = {"date": "2026-10-04", "employee": "Ann", "role": "Server", "shift_start": "6:00pm",
            "shift_end": "11:30pm"}
    other = {"date": "2026-10-04", "employee": "Bo", "role": "Server", "shift_start": "6:00pm",
             "shift_end": "11:30pm"}
    c.base_rows = {"ann": [tail], "bo": [other]}
    early = {"date": WEEK[0], "employee": "Ann", "role": "Server", "shift_start": "6:00am", "shift_end": "2:00pm"}
    ok, why = c.rest_ok("Ann", early, [])
    assert not ok and "rest" in why.lower()
    assert c.rest_ok("Cy", dict(early, employee="Cy"), [])[0] is True          # nobody's tail but their own
    c.linked = {"cy": "bo", "bo": "bo"}
    assert c.rest_ok("Cy", dict(early, employee="Cy"), [])[0] is False         # Bo's tail is Cy's too
    c.base_rows["cy"] = []                                                      # the index follows the tail
    assert c.rest_ok("Cy", dict(early, employee="Cy"), [])[0] is False


# ── SCALE-21 / SCALE-22: the Studio's what-if and cache ──────────────────────

def test_the_what_if_is_bounded_on_a_request_thread_and_says_so():
    from flask import Flask
    assert se.what_if_seconds() is None                                # a generation: its sixty, deterministic
    with Flask(__name__).test_request_context("/"):
        assert se.what_if_seconds() == se.STUDIO_WHAT_IF_SECONDS
    rows = [{"date": WEEK[i], "day": DAYS[i], "employee": n, "role": "Server", "shift_start": "4:00pm",
             "shift_end": "10:00pm", "scheduled_hours": "6", "notes": ""} for i in range(3) for n in ("Ann", "Kim")]
    out = sq.compare_candidates(rows, profiles=[sq.ShiftProfile()], max_seconds=0,
                                scores={"Ann": 2, "Kim": 5}, roster=["Ann", "Kim", "Lee"])
    assert out["ran"] is True and out["evaluated"] == 0
    assert out["cut_short"] is True and "time limit" in out["verdict"]
    full = sq.compare_candidates(rows, profiles=[sq.ShiftProfile()], scores={"Ann": 2, "Kim": 5},
                                 roster=["Ann", "Kim", "Lee"])
    assert full["cut_short"] is False and "time limit" not in full["verdict"]


# ── SCALE-24: a generation reads the restaurant and its roster once ──────────

def test_a_generation_scope_reads_the_roster_once_and_again_after_a_write(db_path, monkeypatch):
    rid = models.create_restaurant(models.Restaurant(name="SP Co", owner_email="sp@x.test"), db_path=db_path)
    calls = []
    real = models._get_capabilities
    monkeypatch.setattr(models, "_get_capabilities", lambda *a: calls.append(1) or real(*a))
    with models.read_scope(restaurant_rows=True, roster=True):
        models.get_capabilities(rid, db_path=db_path)
        got = models.get_capabilities(rid, db_path=db_path)
        assert len(calls) == 1
        got["edited by the caller"] = {}                                 # a copy: the kept read is untouched
        assert "edited by the caller" not in models.get_capabilities(rid, db_path=db_path)
        models.set_capability(rid, "Ann", "overall", score=4, db_path=db_path)   # a write drops it
        assert "Ann" in models.get_capabilities(rid, db_path=db_path) and len(calls) == 2
        models.refresh_scope(rid)                                         # the P-41 point re-reads
        models.get_capabilities(rid, db_path=db_path)
        assert len(calls) == 3
    with models.read_scope():                                             # any other scope: never frozen
        models.get_capabilities(rid, db_path=db_path)
        models.get_capabilities(rid, db_path=db_path)
    assert len(calls) == 5


def test_a_reused_connection_skips_the_pragmas_nobody_changed(db_path, monkeypatch):
    seen = []
    real = models._TrackedConnection.execute

    def ex(self, sql, *a, **k):
        if str(sql).lstrip().upper().startswith("PRAGMA"):
            seen.append(str(sql))
        return real(self, sql, *a, **k)
    monkeypatch.setattr(models._TrackedConnection, "execute", ex)
    conn = models.get_conn(db_path)
    conn.execute("SELECT 1").fetchone()
    conn.close()
    seen.clear()
    conn = models.get_conn(db_path)                                       # reused, untouched: no PRAGMA
    conn.execute("SELECT 1").fetchone()
    conn.execute("PRAGMA foreign_keys=OFF")                               # a borrower turns them off
    conn.close()
    assert seen == ["PRAGMA foreign_keys=OFF"]
    conn = models.get_conn(db_path)                                       # ...so the next checkout resets them
    assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    conn.close()
    assert "PRAGMA foreign_keys=ON" in seen and "PRAGMA busy_timeout=30000" in seen


# ── the platform check counts the capped solves ──────────────────────────────

def test_the_platform_check_alerts_on_the_rate_of_capped_solves(db_path, monkeypatch):
    conn = sqlite3.connect(db_path)
    try:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(schedule_history)")}
        assert {"total_seconds", "review_json"} <= cols
        for i in range(10):
            capped = ["wall:solver"] if i < 3 else []
            conn.execute("INSERT INTO schedule_history (restaurant_id, week_start, total_seconds, review_json) "
                         "VALUES (1, '2026-10-05', 30, ?)", (json.dumps({"solve_capped": capped}),))
        conn.commit()
    finally:
        conn.close()
    got = ops.schedule_solve_capped(db_path)
    assert got["n"] == 10 and got["capped"] == 3 and got["kinds"] == {"wall:solver": 3}
    assert got["over"] is (10 >= ops.SCHEDULE_P95_MIN and 0.3 > ops.SCHEDULE_CAPPED_ALERT)
