"""Workstream C (scheduler fix round 10/10/26): the generation job's own
verdicts — Stop (ARCH-2, BUGSGEN-12, BUGSLIFE-26), the save as its commit
point (ARCH-3), the owner's rejection recorded only on a saved draft
(ARCH-18), the week a queued press builds (BUGSGEN-11), open days never
silently closed (BUGSGEN-5), failed inputs that hold the week (ARCH-25), the
checks a week was judged by kept with it (ARCH-35), and an after-midnight
manager stretch a Studio edit keeps (BUGSGEN-10)."""
import inspect
import json
import sqlite3

import pytest

import models
import ops
import schedule_engine as se
import schedule_rules as sr
import labor
from test_schedule_b2_calls import db, _build_harness, _gen, _csv, _line, _restaurant, WEEK, DAYS  # noqa: F401


def _history(db_path, rid):
    conn = models.get_conn(db_path)
    try:
        return [dict(r) for r in conn.execute("SELECT id, superseded_by, review_json FROM schedule_history "
                                               "WHERE restaurant_id=? ORDER BY id", (rid,)).fetchall()]
    finally:
        conn.close()


def _week_answer():
    return [_line(d, "Ana") for d in WEEK]


# ── BUGSLIFE-26 / ARCH-2: the save checks the job in its own write ──────────

def test_a_stopped_job_can_never_save_and_a_saved_one_is_never_told_nothing_was(db):
    rid = _restaurant(db)
    ops.claim_async_job("stop-a", "schedule", rid)
    assert ops.stop_async_job("stop-a", {"ok": False, "stopped": True, "error": se.STOPPED_MESSAGE}) == "stopped"
    with pytest.raises(models.JobNotPending):
        models.save_schedule_history(rid, WEEK[0], WEEK[-1], 6, 0, 30, _csv([_line(WEEK[0], "Ana")]), [],
                                     db_path=db, job_id="stop-a")
    assert _history(db, rid) == []
    assert ops.job_stop_state("stop-a") == "stopped"
    # A job that saved first: Stop becomes a request the job answers.
    ops.claim_async_job("stop-b", "schedule", rid)
    hid = models.save_schedule_history(rid, WEEK[0], WEEK[-1], 6, 0, 30, _csv([_line(WEEK[0], "Ana")]), [],
                                       db_path=db, job_id="stop-b")
    assert ops.stop_async_job("stop-b", {"ok": False, "stopped": True}) == "stopping"
    assert ops.read_async_job("stop-b", restaurant_id=rid)["status"] == "pending"
    assert ops.job_stop_state("stop-b") == "stop_requested"
    with pytest.raises(models.JobNotPending) as stop:
        models.save_schedule_history(rid, WEEK[0], WEEK[-1], 6, 0, 30, _csv([_line(WEEK[1], "Ana")]), [],
                                     db_path=db, job_id="stop-b")
    assert stop.value.stop and [h["id"] for h in _history(db, rid)] == [hid]
    ops.finish_async_job("stop-b", "done", {"ok": True})
    assert ops.stop_async_job("stop-b", {}) == "finished"


def _stop_harness(monkeypatch, db, job, gates=True):
    rid = _build_harness(monkeypatch, db)
    calls = []
    monkeypatch.setattr(labor, "generate_optimized_schedule", _gen([_week_answer(), [_line(WEEK[5], "Bo")]], calls))
    ops.claim_async_job(job, "schedule", rid)
    gate = {"dates": [WEEK[5]], "focus": ["Saturday 2026-10-10 dinner: 2 servers short"],
            "reason": "1 busy day still had a staffing hole after repair"}

    def gate_then_stop(result):
        # The owner presses Stop while the gate decides. The gate's passes
        # are held in memory and only the winner is saved, once (scheduler
        # audit 10/10/26 ARCH-17): nothing is saved yet, so the route itself
        # finishes the job and "Stopped — nothing was saved" is true.
        assert ops.stop_async_job(job, {"ok": False, "stopped": True, "error": se.STOPPED_MESSAGE}) == "stopped"
        return dict(gate)
    monkeypatch.setattr(se, "_quality_gate", gate_then_stop if gates else (lambda result: None))
    return rid, calls


def test_stop_during_the_gate_saves_nothing_and_the_owners_draft_was_never_replaced(db, monkeypatch):
    rid, calls = _stop_harness(monkeypatch, db, "stop-gate")
    prior = models.save_schedule_history(rid, WEEK[0], WEEK[-1], 42, 0, 30, _csv(_week_answer()), [], db_path=db)
    with se.generation_scope("stop-gate") as clock:
        clock.prior_draft_id = prior
        se._run_schedule_job("stop-gate", rid)
    job = ops.read_async_job("stop-gate", restaurant_id=rid)
    assert job["status"] == "error" and job["result"]["stopped"] is True
    assert job["result"]["error"] == se.STOPPED_MESSAGE and "restored_history_id" not in job["result"]
    hist = _history(db, rid)
    assert [h["id"] for h in hist] == [prior] and hist[0]["superseded_by"] is None    # A in force throughout
    assert len(calls) == 1, "no paid rewrite after the Stop"


def test_stop_during_the_gate_with_no_draft_before_saves_nothing(db, monkeypatch):
    rid, calls = _stop_harness(monkeypatch, db, "stop-keep")
    with se.generation_scope("stop-keep"):
        se._run_schedule_job("stop-keep", rid)
    job = ops.read_async_job("stop-keep", restaurant_id=rid)
    assert job["status"] == "error" and job["result"]["stopped"] is True
    assert job["result"]["error"] == se.STOPPED_MESSAGE
    assert _history(db, rid) == []
    assert len(calls) == 1


def _stop_during_the_save(monkeypatch, job):
    """The owner's Stop lands while the job's one save is written: the save
    goes through first (its job check passed), then the Stop becomes a
    request the job answers (ARCH-2)."""
    real = se._save_durably

    def save_then_stop(*a, **k):
        hid = real(*a, **k)
        assert ops.stop_async_job(job, {"ok": False, "stopped": True, "error": se.STOPPED_MESSAGE}) == "stopping"
        return hid
    monkeypatch.setattr(se, "_save_durably", save_then_stop)


def test_a_stop_that_lands_during_the_save_puts_the_owners_draft_back_and_says_so(db, monkeypatch):
    rid, _calls = _stop_harness(monkeypatch, db, "stop-save", gates=False)
    prior = models.save_schedule_history(rid, WEEK[0], WEEK[-1], 42, 0, 30, _csv(_week_answer()), [], db_path=db)
    _stop_during_the_save(monkeypatch, "stop-save")
    with se.generation_scope("stop-save") as clock:
        clock.prior_draft_id = prior
        se._run_schedule_job("stop-save", rid)
    job = ops.read_async_job("stop-save", restaurant_id=rid)
    assert job["status"] == "error" and job["result"]["stopped"] is True
    assert job["result"]["restored_history_id"] == prior
    assert job["result"]["error"] == se.STOPPED_RESTORED_MESSAGE and "nothing was saved" not in job["result"]["error"]
    hist = _history(db, rid)
    assert next(h for h in hist if h["id"] == prior)["superseded_by"] is None          # A is in force again
    assert all(h["superseded_by"] == prior for h in hist if h["id"] != prior)          # B is not


def test_a_stop_that_lands_during_the_save_with_no_draft_before_keeps_it_and_says_so(db, monkeypatch):
    rid, _calls = _stop_harness(monkeypatch, db, "stop-save-keep", gates=False)
    _stop_during_the_save(monkeypatch, "stop-save-keep")
    with se.generation_scope("stop-save-keep"):
        se._run_schedule_job("stop-save-keep", rid)
    job = ops.read_async_job("stop-save-keep", restaurant_id=rid)
    assert job["status"] == "done" and job["result"]["stopped"] is True
    assert job["result"]["review"]["lines"][0] == se.STOPPED_KEPT_LINE
    first = job["result"]["history_id"]
    hist = _history(db, rid)
    assert [h["id"] for h in hist] == [first] and hist[0]["superseded_by"] is None


def test_stop_after_the_last_model_call_is_honoured_and_said_as_stopped(db, monkeypatch):
    # BUGSGEN-12: the repair loop, the score and the gate ran on to the save,
    # which then said "didn't finish in time".
    rid, calls = _stop_harness(monkeypatch, db, "stop-late", gates=False)
    real = se.repair_week

    def stop_then_repair(rows, x):
        ops.stop_async_job("stop-late", {"ok": False, "stopped": True, "error": se.STOPPED_MESSAGE})
        return real(rows, x)
    monkeypatch.setattr(se, "repair_week", stop_then_repair)
    with se.generation_scope("stop-late"):
        se._run_schedule_job("stop-late", rid)
    job = ops.read_async_job("stop-late", restaurant_id=rid)
    assert job["status"] == "error" and job["result"]["error"] == se.STOPPED_MESSAGE
    assert _history(db, rid) == []
    assert "_raise_if_stopped(x.clock)" in inspect.getsource(real)


# ── ARCH-3: the save is the job's commit point ─────────────────────────────

def test_a_save_that_keeps_failing_fails_the_job_never_ok_without_a_draft(db, monkeypatch):
    rid, _calls = _stop_harness(monkeypatch, db, "save-fail", gates=False)
    tries = []

    def locked(*a, **k):
        tries.append(1)
        raise sqlite3.OperationalError("database is locked")
    monkeypatch.setattr(models, "save_schedule_history", locked)
    monkeypatch.setattr(se, "SAVE_BACKOFF_SECONDS", (0, 0, 0))
    se._run_schedule_job("save-fail", rid)
    job = ops.read_async_job("save-fail", restaurant_id=rid)
    assert job["status"] == "error" and job["result"]["error"] == se._SAVE_FAILED_MESSAGE
    assert len(tries) == se.SAVE_ATTEMPTS


def test_a_busy_database_is_retried_and_the_draft_lands(db, monkeypatch):
    rid, _calls = _stop_harness(monkeypatch, db, "save-retry", gates=False)
    real, tries = models.save_schedule_history, []

    def flaky(*a, **k):
        tries.append(1)
        if len(tries) < 3:
            raise sqlite3.OperationalError("database is locked")
        return real(*a, **k)
    monkeypatch.setattr(models, "save_schedule_history", flaky)
    monkeypatch.setattr(se, "SAVE_BACKOFF_SECONDS", (0, 0, 0))
    se._run_schedule_job("save-retry", rid)
    job = ops.read_async_job("save-retry", restaurant_id=rid)
    assert job["status"] == "done" and job["result"]["history_id"] and len(tries) == 3


# ── ARCH-18: a rejection only for a draft that saved and stands ─────────────

def test_the_owners_rejection_is_recorded_only_when_the_new_draft_stands(db, monkeypatch):
    import schedule_versions as sv
    rid = _restaurant(db)
    draft = models.save_schedule_history(rid, WEEK[0], WEEK[-1], 6, 0, 30, _csv([_line(WEEK[0], "Ana")]), [],
                                         db_path=db)
    kept = []
    monkeypatch.setattr(sv, "record_rejection", lambda r, hid, kind, **k: kept.append((hid, kind)))
    rej = {"kind": "draft_discarded", "history_id": draft}
    for job, status, result in (("rj-stop", "error", {"ok": False, "stopped": True}),
                                ("rj-fail", "error", {"ok": False, "error": "x"}),
                                ("rj-kept-stopped", "done", {"ok": True, "history_id": 5, "stopped": True}),
                                ("rj-done", "done", {"ok": True, "history_id": 7})):
        ops.claim_async_job(job, "schedule", rid)
        ops.finish_async_job(job, status, result)
        se._settle_rejection(job, rid, rej)
    assert kept == [(draft, "draft_discarded")]


# ── BUGSGEN-11: a queued press builds the week it asked for ────────────────

def test_the_press_hands_the_job_the_week_it_resolved(db, monkeypatch):
    import ai_utils
    import auth
    import mobile_api
    from flask import Flask
    rid = _restaurant(db, module_labor=1)
    auth.init_auth(db_path=db)
    app = Flask(__name__, template_folder="../templates")
    app.register_blueprint(mobile_api.mobile_bp)
    uid = auth.create_user(rid, "owner", "owner@x.com", "pw", db_path=db)
    headers = {"Authorization": f"Bearer {auth.create_session(uid, db_path=db)}"}
    got = []
    monkeypatch.setattr(se, "submit_generation", lambda job_id, r, **k: got.append(k))
    monkeypatch.setattr(ai_utils, "ai_rate_limited", lambda *a, **k: False)
    r = app.test_client().post("/mobile/api/labor/generate-schedule", headers=headers, json={})
    assert r.status_code == 200, r.get_json()
    want = se.generation_request(rid)["week_start"]
    assert got[0]["week_start"] == want, "the job used to recompute 'next week' when a slot freed"
    import strategy_jobs
    assert 'week_start=_request["week_start"]' in inspect.getsource(strategy_jobs._draft_one)


# ── BUGSGEN-5: an open day the model skipped is never silently closed ──────

def test_a_day_with_hours_on_file_the_model_skipped_is_required_and_held(db, monkeypatch):
    # History trades Tuesday to Sunday only; the owner has set Monday hours.
    shifts = [{"date": d, "employee": "Ana", "role": "Server"}
              for d in ("2026-09-15", "2026-09-16", "2026-09-22", "2026-09-23", "2026-09-29", "2026-09-30")]
    rid = _build_harness(monkeypatch, db, shifts=shifts,
                         open_times_json=json.dumps({"Monday": "11:00am"}),
                         close_times_json=json.dumps({"Monday": "10:00pm"}))
    calls = []
    monkeypatch.setattr(labor, "generate_optimized_schedule",
                        _gen([[_line(d, "Ana") for d in WEEK[1:]], []], calls))
    out = se._build_schedule_result(rid, week_start=WEEK[0])
    assert len(calls) == 2 and calls[1]["slice"] == [WEEK[0]], "Monday is asked for again, named"
    assert [u["date"] for u in out["unwritten_dates"]] == [WEEK[0]]
    assert WEEK[0] not in out["closed_dates"] and out["open_unwritten"] == [WEEK[0]]
    assert any(ln.startswith("⚠") and "Monday" in ln for ln in out["generation_notes"])


def test_a_holiday_the_owner_has_hours_for_is_not_accepted_empty():
    # Thanksgiving 11/26/26 on a Thursday the owner has hours for: required.
    assert sr.trading_day("Thursday", {"Thursday": "11:00am"}, {}, {"Friday"})
    assert not sr.trading_day("Thursday", {}, {}, {"Friday"})
    assert sr.trading_day("Thursday", {}, {}, None)
    import schedule_skeleton
    assert "_rules.trading_day(wd, open_times, close_times, trading)" in inspect.getsource(
        schedule_skeleton.plan_manager_coverage)
    src = inspect.getsource(se._generate_in_parts)
    assert "_capacity_short(hours, trading_dates" in src and "accepted[d] = _accepted_why" in src


# ── ARCH-25: a safety-relevant input that failed holds the week ─────────────

def test_a_safety_input_leads_the_review_and_holds_the_publish(db, monkeypatch):
    assert se.input_class("staff notes") == "safety" and se.input_class("weather") == "optional"
    held = se._input_hold_line(["staff notes", "weather"])
    assert held.startswith("⚠") and "staff notes" in held and "weather" not in held
    assert "weather" in se._input_failure_line(["staff notes", "weather"])
    assert "staff notes" not in se._input_failure_line(["staff notes", "weather"])
    rid, _calls = _stop_harness(monkeypatch, db, "inputs-job", gates=False)

    def broken(*a, **k):
        raise RuntimeError("the other sites could not be read")
    monkeypatch.setattr(models, "sibling_location_shifts", broken)
    se._run_schedule_job("inputs-job", rid)
    job = ops.read_async_job("inputs-job", restaurant_id=rid)
    assert job["status"] == "done", job
    review = job["result"]["review"]
    assert review["inputs_held"] == ["other sites' shifts"]
    assert review["lead_lines"][0].startswith("⚠") and "shifts at your other locations" in review["lead_lines"][0]
    import client_api
    gate = client_api.publish_review(rid, job["result"]["history_id"])
    assert any("shifts at your other locations" in b["text"] for b in gate["blockers"])
    # and the input-failure rate is an SLO the platform check reads
    slo = ops.schedule_input_failures(db_path=db)
    assert slo["n"] >= 1 and slo["rates"].get("other sites' shifts") == 1.0


# ── ARCH-35: the checks a week was judged by are kept and replayable ───────

def test_a_saved_week_keeps_the_rules_it_was_checked_against(db, monkeypatch):
    import schedule_output as so
    rid, _calls = _stop_harness(monkeypatch, db, "snap-job", gates=False)
    se._run_schedule_job("snap-job", rid)
    hid = ops.read_async_job("snap-job", restaurant_id=rid)["result"]["history_id"]
    snap = so.check_snapshot(hid, restaurant_id=rid)
    assert isinstance(snap["constraints"], sr.Constraints) and snap["constraints"].week_dates == WEEK
    assert snap["rules_version"] == so.rules_version()
    replay = se.replay_checks(rid, hid)
    assert replay["ok"] and replay["same_code"]
    # judged by the kept rules, not today's: a role floor set since changes nothing
    models.update_restaurant(rid, {"role_floors_json": json.dumps({"Server": {"morning": 5, "night": 5}})})
    assert se.replay_checks(rid, hid)["review"]["hard"] == replay["review"]["hard"]


# ── SCALE-9: a large week never overflows the solver's search ───────────────

def test_the_solver_searches_a_thousand_unit_week_without_recursion():
    """250 people, about 1,120 shifts in one role: the depth-first search
    recursed once per assigned unit and raised RecursionError (the scale
    audit's repro), so the solver never ran for a large restaurant. It is an
    explicit stack now."""
    import random
    import schedule_solver as ss
    import shift_quality as sq
    rng = random.Random(9)
    names = [f"Emp{i:03d}" for i in range(250)]
    rows = []
    for d in WEEK:
        for j in range(160):
            start, end = ("11:00am", "4:00pm") if j % 2 else ("4:00pm", "11:00pm")
            h = sr.hours_text(sr.span_hours({"date": d, "shift_start": start, "shift_end": end}))
            rows.append({"date": d, "day": sq._day_name(d), "employee": names[(j * 7 + WEEK.index(d) * 3) % 250],
                         "role": "Server", "shift_start": start, "shift_end": end, "scheduled_hours": h,
                         "notes": ""})
    c = sr.Constraints(restaurant_id=1, week_dates=list(WEEK), week_days=list(DAYS), roster_names=list(names),
                       active={n.lower() for n in names})
    sig = {"roster": names, "roster_roles": {n: "Server" for n in names},
           "scores": {n: rng.randint(1, 5) for n in names}}
    res = ss.solve(rows, c, signals=sig, max_seconds=2.0)
    assert len(rows) == 1120
    assert res["status"] in ("optimal", "time_limit", "no_solution_in_time"), res.get("status")
    import inspect
    assert "self._dfs(" not in inspect.getsource(ss._Search._dfs)


class _Chain:
    """The search's own bookkeeping over a chain of units, each with a few
    values (some refuted by forward checking), for driving _Search._dfs."""

    def __init__(self, n, seed):
        import random
        import types
        rng = random.Random(seed)
        self.static = [{p: rng.randint(0, 9) for p in range(rng.choice((1, 2, 3)))} for _ in range(n)]
        self.dom = [set(v) for v in self.static]
        self.minc = [min(v.values()) for v in self.static]
        self.bad = {(u, p) for u in range(n) for p in self.dom[u] if rng.random() < 0.15}
        self.assign = [None] * n
        self.g, self.lb, self.best = 0, sum(self.minc), float("inf")
        self.stop = self.cut = False
        self.nodes, self.log = 0, []
        self.clock = __import__("schedule_solver").WorkClock()     # the work clock _enter ticks (ARCH-26)
        self.prob = types.SimpleNamespace(units=list(range(n)), leaf_penalty=lambda persons, st: 0,
                                          dynamic=lambda unit, p, st: 0)
        self.st = None

    def _tick(self):
        self.nodes += 1
        self.log.append(("tick",))

    def _select(self, free):
        return next((u for u in free if self.assign[u] is None), None)

    def _assign(self, u, p, c):
        self.assign[u] = p
        self.g += c
        self.lb -= self.minc[u]
        self.log.append(("assign", u, p))
        return (u, p) not in self.bad, c

    def _unassign(self, u, p, rec):
        self.assign[u] = None
        self.g -= rec
        self.lb += self.minc[u]
        self.log.append(("unassign", u, p))

    def _record(self, cost):
        self.best = cost
        self.log.append(("best", cost))


def _recursive_dfs(self, free, persons, disc):
    """The search as it was before SCALE-9 (one Python frame per unit) — the
    reference the explicit stack must match node for node."""
    self._tick()
    if self.stop:
        return
    u = self._select(free)
    if u is None:
        total = self.g + self.prob.leaf_penalty(persons, self.st)
        if total < self.best - 1e-9:
            self._record(total)
        return
    unit = self.prob.units[u]
    vals = sorted(((self.static[u][p] + self.prob.dynamic(unit, p, self.st), p) for p in self.dom[u]))
    rest_lb = self.lb - self.minc[u]
    taken = 0
    for c, p in vals:
        if self.stop:
            break
        if self.g + c + rest_lb >= self.best - 1e-9:
            break
        ok, rec = self._assign(u, p, c)
        if not ok:
            self._unassign(u, p, rec)
            continue
        if taken and disc is not None and disc <= 0:
            self._unassign(u, p, rec)
            self.cut = True
            break
        if self.g + self.lb < self.best - 1e-9:
            _recursive_dfs(self, free, persons, None if disc is None else disc - (1 if taken else 0))
        taken += 1
        self._unassign(u, p, rec)


@pytest.mark.parametrize("seed", range(20))
def test_the_explicit_stack_visits_exactly_the_nodes_the_recursion_did(seed):
    import schedule_solver as ss
    for disc in (None, 0, 1, 3):
        a, b = _Chain(9, seed), _Chain(9, seed)
        _recursive_dfs(a, list(range(9)), set(), disc)
        a._enter = None
        b._enter = ss._Search._enter.__get__(b)
        ss._Search._dfs(b, list(range(9)), set(), disc)
        assert b.log == a.log and (b.best, b.cut) == (a.best, a.cut), (seed, disc)
        assert all(x is None for x in b.assign)                      # every frame undid its own work


def test_the_explicit_stack_goes_deeper_than_python_can_recurse():
    import sys
    import schedule_solver as ss
    n = sys.getrecursionlimit() * 3
    b = _Chain(n, 1)
    b.bad = set()
    b._enter = ss._Search._enter.__get__(b)
    ss._Search._dfs(b, list(range(n)), set(), 0)
    assert b.best < float("inf") and ("best", b.best) in b.log


# ── BUGSGEN-10: an after-midnight manager stretch survives a Studio edit ────

def test_refresh_review_keeps_a_manager_stretch_after_midnight():
    c = sr.Constraints(restaurant_id=1, week_dates=list(WEEK), week_days=list(DAYS))
    c.compliance = dict(sr.DEFAULTS)
    c.managers = {"mia": "Manager"}
    c.roster_names, c.active = ["Mia", "Sam"], {"mia", "sam"}
    c.close_times = {"Friday": "2:00am"}
    fri = WEEK[4]
    rows = [{"date": fri, "day": "Friday", "employee": "Mia", "role": "Manager", "shift_start": "5:00pm",
             "shift_end": "12:30am", "scheduled_hours": "7.5", "notes": ""},
            {"date": fri, "day": "Friday", "employee": "Sam", "role": "Server", "shift_start": "5:00pm",
             "shift_end": "2:00am", "scheduled_hours": "9", "notes": ""}]
    gaps = sr.manager_gaps(rows, c)[fri]
    assert [(gs, ge) for gs, ge, _i in gaps] == [(24 * 60 + 30, 26 * 60)]
    for entry in ({"date": fri, "from": "12:30am", "to": "2:00am"},                         # a review from before
                  {"date": fri, "from": "12:30am", "to": "2:00am", "gap_start": 1470, "gap_end": 1560}):
        saved = {"manager_coverage": {"left": [entry], "shortfall": {"text": "no manager 12:30am-2:00am"}}}
        out = se.refresh_review(saved, sr.violations(rows, c), rows, constraints=c)
        assert out["manager_coverage"]["left"] == [entry] and out["manager_coverage"]["shortfall"], entry
