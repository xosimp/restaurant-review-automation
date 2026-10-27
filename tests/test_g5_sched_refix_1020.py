"""Blind re-audit 10/20/26 fix round, agent G5 — the schedule generator, the
Studio and publish: R5-01 … R5-12, R9-10, R9-17. No network, no provider
keys: every provider is a stub."""
import datetime as dt
import json
import sys
import time

import pytest

import models
from models import Restaurant, create_restaurant


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
    import job_queue
    job_queue.reset_for_tests()
    monkeypatch.delenv("QUEUE_WORKER", raising=False)
    monkeypatch.setattr(job_queue, "kick", lambda *a, **k: None)
    return db_path


def _restaurant(db_path, name="G5 Grill", **cols):
    rid = create_restaurant(Restaurant(name=name, owner_email=f"{name.split()[0].lower()}@x.com"), db_path=db_path)
    if cols:
        conn = models.get_conn(db_path)
        conn.execute("UPDATE restaurants SET " + ", ".join(f"{k}=?" for k in cols) + " WHERE id=?",
                     (*cols.values(), rid))
        conn.commit()
        conn.close()
    return rid


def _sql(sql, args=()):
    conn = models.get_conn()
    try:
        out = conn.execute(sql, args).fetchall()
        conn.commit()
        return out
    finally:
        conn.close()


WEEK = ["2026-11-02", "2026-11-03", "2026-11-04", "2026-11-05", "2026-11-06", "2026-11-07", "2026-11-08"]


def _csv(names_by_date):
    lines = ["date,day,employee,role,shift_start,shift_end,scheduled_hours,notes"]
    for d, name in names_by_date:
        lines.append(f"{d},Monday,{name},Server,4:00pm,10:00pm,6,")
    return "\n".join(lines)


# ── R5-01: a queued auto-draft never writes over the owner's week ───────────

def test_an_empty_week_only_save_refuses_a_week_with_a_draft_or_a_sent_copy(db):
    rid = _restaurant(db)
    # Empty: it saves.
    first = models.save_schedule_history(rid, WEEK[0], WEEK[-1], 6, 0, 30, _csv([(WEEK[0], "Ana")]), [],
                                         empty_week_only=True)
    assert first
    # The owner's draft is there now: refused, nothing written or superseded.
    with pytest.raises(models.WeekAlreadyDrafted):
        models.save_schedule_history(rid, WEEK[0], WEEK[-1], 6, 0, 30, _csv([(WEEK[0], "Bo")]), [],
                                     empty_week_only=True)
    assert [tuple(r) for r in _sql("SELECT id, superseded_by FROM schedule_history WHERE restaurant_id=?",
                                   (rid,))] == [(first, None)]
    # A sent week (its only row published, even superseded) is refused too.
    _sql("UPDATE schedule_history SET published_at=datetime('now') WHERE id=?", (first,))
    with pytest.raises(models.WeekAlreadyDrafted):
        models.save_schedule_history(rid, WEEK[0], WEEK[-1], 6, 0, 30, _csv([(WEEK[0], "Cy")]), [],
                                     empty_week_only=True)
    # An owner's own generation (no flag) still saves as before.
    assert models.save_schedule_history(rid, WEEK[0], WEEK[-1], 6, 0, 30, _csv([(WEEK[0], "Di")]), [])


def test_the_job_refuses_a_taken_week_in_words_and_never_reruns(db, monkeypatch):
    import schedule_engine as se
    import schedule_job as sjob
    rid = _restaurant(db)
    models.save_schedule_history(rid, WEEK[0], WEEK[-1], 6, 0, 30, _csv([(WEEK[0], "Ana")]), [])
    with pytest.raises(se.ScheduleGenerationError) as why:
        se._save_durably(None, None, rid, WEEK[0], WEEK[-1], 6, 0, 30, _csv([(WEEK[0], "Bo")]), [],
                         empty_week_only=True)
    assert str(why.value) == se.WEEK_TAKEN_MESSAGE
    assert sjob.Request("j", rid).empty_week_only is False


def _auto_restaurant(db):
    return _restaurant(db, "Auto Diner", auto_draft_schedule=1, module_labor=1)


def test_a_queued_auto_draft_rechecks_the_week_when_it_runs(db, monkeypatch):
    import job_queue
    import ops
    import strategy_jobs
    rid = _auto_restaurant(db)
    ran = []
    monkeypatch.setattr(strategy_jobs, "_draft_one", lambda *a, **k: ran.append(k))
    job = job_queue.Job({"id": None, "payload": {"restaurant_id": rid, "period": "2026-10-29", "target": WEEK[0],
                                                 "attempt": 0, "batch": False}})
    # Still empty: it drafts.
    strategy_jobs.run_queued_auto_draft(job)
    assert len(ran) == 1
    # The owner drafted the week while the row waited: skipped, nothing run.
    models.save_schedule_history(rid, WEEK[0], WEEK[-1], 6, 0, 30, _csv([(WEEK[0], "Ana")]), [])
    assert strategy_jobs.run_queued_auto_draft(job) == {"skipped": "week already has a schedule"}
    assert len(ran) == 1
    # Another week, already drafted by an earlier row: skipped by its claim.
    ops.claim_period(f"auto_draft_done:{rid}", WEEK[1])
    job2 = job_queue.Job({"id": None, "payload": {"restaurant_id": rid, "period": "2026-10-29",
                                                  "target": WEEK[1], "attempt": 0, "batch": False}})
    assert strategy_jobs.run_queued_auto_draft(job2) == {"skipped": "week already drafted"}
    assert len(ran) == 1


def test_an_auto_draft_saves_only_over_an_empty_week_and_a_taken_week_is_settled_quietly(db, monkeypatch):
    import ops
    import schedule_engine as se
    import strategy_jobs
    rid = _auto_restaurant(db)
    r = models.get_restaurant(rid)
    seen, told = [], []

    def fake_run(job_id, restaurant_id, **kw):
        seen.append(kw)
        # The owner's draft landed while it ran: the save refused it.
        models.save_schedule_history(restaurant_id, WEEK[0], WEEK[-1], 6, 0, 30, _csv([(WEEK[0], "Ana")]), [])
        ops.finish_async_job(job_id, "error", {"ok": False, "error": se.WEEK_TAKEN_MESSAGE})
    monkeypatch.setattr(se, "_run_schedule_job", fake_run)
    monkeypatch.setattr(se, "generation_request", lambda rid_, **k: {"week_start": WEEK[0]})
    monkeypatch.setattr(strategy_jobs, "_tell_auto_draft_gave_up", lambda *a, **k: told.append(a))
    counts = {}
    strategy_jobs._draft_one(r, db, se, lambda k: counts.__setitem__(k, counts.get(k, 0) + 1),
                             period="2026-10-29", target=WEEK[0], attempt=strategy_jobs.AUTO_DRAFT_ATTEMPTS - 1,
                             batch=False)
    assert seen and seen[0].get("empty_week_only") is True
    assert counts == {"skipped": 1} and told == []                  # never "gave up" over the owner's own week
    assert ops.period_claimed(f"auto_draft_done:{rid}", WEEK[0])


# ── R5-02: a switchback never starts by itself ──────────────────────────────

def test_a_switchback_runs_only_once_an_admin_lists_it(db, monkeypatch):
    import schedule_experiments as sx
    monkeypatch.delenv(sx.PIN_ENV, raising=False)
    monkeypatch.delenv(sx.SWITCHBACKS_ENV, raising=False)
    monkeypatch.setattr(sx, "powered", lambda db_path=None: {"powered": True, "restaurants": 40, "need": 5,
                                                             "window_days": 56})
    weeks = [(dt.date(2026, 1, 5) + dt.timedelta(weeks=k)).isoformat() for k in range(16)]

    def arms(exp_key):
        return [next(a for a in sx.arms_for(7, w, db_path=db) if a["experiment"] == exp_key) for w in weeks]
    # Powered, never started: every week the memory and learning are on, pinned and not counted.
    for key, on in (("learning_v1", "memory_on"), ("learning_v2", "learning_on")):
        got = arms(key)
        assert {a["arm"] for a in got} == {on} and {a["pin_source"] for a in got} == {"not_started"}
    # The other experiments are not switchbacks: they run their hash once powered.
    assert {a["pin_source"] for a in arms("draft_v1")} == {None}
    # Started by an admin: the switchback's hash.
    monkeypatch.setenv(sx.SWITCHBACKS_ENV, "learning_v1")
    got = arms("learning_v1")
    assert "memory_off" in {a["arm"] for a in got} and {a["pin_source"] for a in got} == {None}
    assert {a["pin_source"] for a in arms("learning_v2")} == {"not_started"}
    monkeypatch.setenv(sx.SWITCHBACKS_ENV, "all")
    assert "learning_off" in {a["arm"] for a in arms("learning_v2")}


# ── R5-05: a group's presses spend each location's own bucket ──────────────

def _org():
    conn = models.get_conn()
    try:
        conn.execute("INSERT INTO organizations (name, owner_email) VALUES ('Group', 'g@x.example')")
        oid = conn.execute("SELECT MAX(id) FROM organizations").fetchone()[0]
        conn.commit()
        return oid
    finally:
        conn.close()


def _restaurants(n, org=None):
    conn = models.get_conn()
    try:
        cur = conn.execute("SELECT COALESCE(MAX(id), 0) FROM restaurants").fetchone()[0]
        conn.executemany("INSERT INTO restaurants (name, owner_email, organization_id) VALUES (?,?,?)",
                         [(f"R{cur + i}", f"r{cur + i}@x.example", org) for i in range(1, n + 1)])
        conn.commit()
        return [r[0] for r in conn.execute("SELECT id FROM restaurants WHERE id>? ORDER BY id", (cur,)).fetchall()]
    finally:
        conn.close()


def test_a_groups_seventh_press_in_an_hour_is_not_held_behind_every_single_restaurant(db, monkeypatch):
    import job_queue as jq
    monkeypatch.setenv("QUEUE_GENERATION_CONCURRENCY", "50")
    oid = _org()
    locs = _restaurants(12, org=oid)
    singles = _restaurants(12)
    for r in locs:
        jq.enqueue("schedule_generate", {}, restaurant_id=r, wake=False)
    for _ in range(3):
        for r in singles:
            jq.enqueue("schedule_generate", {}, restaurant_id=r, wake=False)
    order = []
    while True:
        j = jq.claim("w", groups=("generation",))
        if j is None:
            break
        order.append(j["restaurant_id"])
        _sql("UPDATE jobs SET state='done', lease_owner=NULL WHERE id=?", (j["id"],))
    assert len(order) == 48
    # The group's 7th press of the hour onwards is served in its fair turn
    # (4 to every 12 single presses: the organization's weight), never after
    # every single restaurant's rows: each location spends its own bucket.
    # With one shared bucket the 7th went behind all 36 (index 42 on).
    pos = [i for i, r in enumerate(order) if r in set(locs)]
    assert len(pos) == 12 and pos[6] < 36
    buckets = {r[0] for r in _sql("SELECT tenant_key FROM job_tenants WHERE klass='interactive'")}
    assert f"r:{locs[0]}" in buckets and f"org:{oid}" in buckets


# ── R5-06 / R5-08: memory admission by roster, measured cores ──────────────

def test_a_generations_memory_follows_its_roster_as_measured():
    import job_queue as jq
    assert jq.generation_memory_mb(None) == jq.GEN_SLOT_MB
    assert jq.generation_memory_mb(60) == pytest.approx(844 * jq.GEN_MEMORY_MARGIN, abs=0.2)
    assert jq.generation_memory_mb(500) == pytest.approx(3798 * jq.GEN_MEMORY_MARGIN, abs=0.2)
    assert jq.generation_memory_mb(185) < jq.generation_memory_mb(250) < jq.generation_memory_mb(400)
    assert jq.generation_memory_mb(1000) > jq.generation_memory_mb(500)          # extrapolated, never flat


def test_a_big_roster_waits_for_memory_while_small_ones_fit(db, monkeypatch):
    import job_queue as jq
    monkeypatch.setenv("QUEUE_GENERATION_CONCURRENCY", "10")
    monkeypatch.setattr(jq, "memory_limit_mb", lambda: 1536.0 + 9000.0)            # 9 GB for generations
    rids = _restaurants(4)
    big = jq.generation_memory_mb(500)                                           # ~4.4 GB each
    for r in rids[:3]:
        jq.enqueue("schedule_generate", {"mem_mb": big}, restaurant_id=r, wake=False)
    a = jq.claim("w", groups=("generation",))
    b = jq.claim("w", groups=("generation",))
    assert a and b
    assert jq.claim("w", groups=("generation",)) is None                         # a third would be 13 GB
    jq.enqueue("schedule_generate", {"mem_mb": 300.0}, restaurant_id=rids[3], wake=False)
    # Memory frees as one finishes: the next big one runs.
    _sql("UPDATE jobs SET state='done', lease_owner=NULL WHERE id=?", (a["id"],))
    c = jq.claim("w", groups=("generation",))
    assert c is not None and c["restaurant_id"] == rids[2]
    # Nothing running: one is always admitted, however big.
    _sql("UPDATE jobs SET state='done', lease_owner=NULL WHERE state IN ('running', 'queued')")
    huge, _c = jq.enqueue("schedule_generate", {"mem_mb": 99999.0}, restaurant_id=rids[0], wake=False)
    got = jq.claim("w", groups=("generation",))
    assert got is not None and got["id"] == huge


def test_the_slots_are_the_cores_over_what_a_generation_measured(monkeypatch):
    import job_queue as jq
    monkeypatch.setattr(jq, "cpu_cores", lambda: 24)
    assert jq.cpu_generation_slots() == 12


def test_a_solver_process_the_system_killed_is_never_solved_again_in_process(monkeypatch):
    import schedule_cpsat_runtime as rt
    if rt._cp is None:
        pytest.skip("OR-Tools not installed")

    class Killed:
        returncode = -9

        def communicate(self, payload=None, timeout=None):
            return b"", b""

        def poll(self):
            return -9
    monkeypatch.setattr(rt, "_take_process", lambda: Killed())
    monkeypatch.setattr(rt, "ISOLATE_MIN_VARS", 1)
    m = rt._cp.CpModel()
    x = m.new_int_var(0, 10, "x")
    m.maximize(x)
    s = rt._cp.CpSolver()
    s.parameters.max_time_in_seconds = 5
    called = []
    real_solve = s.solve
    s.solve = lambda *a, **k: called.append(1) or real_solve(*a, **k)
    st = rt.run(s, m)
    assert st == rt._cp.UNKNOWN and called == []
    assert s.sp_info["killed"] and s.sp_info["memory_capped"] and "not solved again" in s.sp_info["note"]


# ── R5-07: an expired auto-publish tells the owner; the ablation sheds ─────

def test_an_expired_auto_publish_check_tells_the_owner(db, monkeypatch):
    import job_queue
    import scheduler
    import strategy_jobs
    assert job_queue.EXPIRE_HOOKS["auto_publish"] == "scheduler:on_auto_publish_expired"
    rid = _restaurant(db, "Pub House", auto_publish_schedule=1, module_labor=1)
    told = []
    monkeypatch.setattr(strategy_jobs, "_reach", lambda *a, **k: told.append(a))
    job = job_queue.Job({"id": 1, "payload": {"restaurant_id": rid}})
    assert scheduler.on_auto_publish_expired(job) is False and told == []        # nothing drafted: nothing to say
    future = (dt.date.today() + dt.timedelta(days=10)).isoformat()
    models.save_schedule_history(rid, future, future, 6, 0, 30, _csv([(future, "Ana")]), [])
    assert scheduler.on_auto_publish_expired(job) is True
    assert told and told[0][1] == "schedule_publish_held" and "didn't go out" in told[0][2]


def test_the_learning_ablation_is_shed_while_the_bulk_queue_is_backed_up(db, monkeypatch):
    import job_queue
    import schedule_scorecard as ssc
    monkeypatch.setattr(ssc, "ABLATION_SHED_QUEUED", 3)
    assert ssc.ablation_wanted(["a learned habit"])
    rid = _restaurant(db)
    job_queue.enqueue_many("learner", [{"restaurant_id": rid, "payload": {"i": i}} for i in range(3)], wake=False)
    assert not ssc.ablation_wanted(["a learned habit"])
    monkeypatch.setattr(ssc, "ABLATION_SHED_QUEUED", 0)
    assert ssc.ablation_wanted(["a learned habit"])


# ── R5-09: a manager gap covered by name puts that manager on the week ─────

def test_a_named_cover_is_put_on_the_gap_and_an_owner_covering_themselves_is_not(monkeypatch):
    import schedule_publish as sp
    b = {"key": "rule:no_manager:2026-11-03:960", "date": "2026-11-03", "gap": [960, 1080],
         "cover_candidates": ["Maria Lopez", "Gm"]}
    review = {"blockers": [b, {"key": "rule:rest_gap:x", "date": "2026-11-03"}]}
    specs = sp.cover_specs(review, {b["key"]: {"covered_by": "maria  lopez"}})
    assert specs == [{"date": "2026-11-03", "start": 960, "end": 1080, "name": "Maria Lopez",
                      "candidates": ["Maria Lopez", "Gm"]}]
    assert sp.cover_specs(review, {b["key"]: {"covered_by": "me"}}) == []
    rows = [{"date": "2026-11-02", "employee": "Maria Lopez", "role": "Floor Manager", "shift_start": "9:00am",
             "shift_end": "5:00pm"},
            {"date": "2026-11-03", "employee": "Ana", "role": "Server", "shift_start": "3:00pm", "shift_end": "9:00pm"}]
    added = sp._cover_rows(rows, specs)
    assert len(added) == 1
    a = added[0]
    assert (a["employee"], a["role"], a["shift_start"], a["shift_end"], a["day"]) == \
        ("Maria Lopez", "Floor Manager", "4:00pm", "6:00pm", "Tuesday")
    assert a["notes"] == sp.COVER_NOTE and a["shift_id"]
    # Already on across the gap: nothing added.
    rows.append({"date": "2026-11-03", "employee": "Maria Lopez", "role": "Floor Manager", "shift_start": "3:00pm",
                 "shift_end": "11:00pm"})
    assert sp._cover_rows(rows, specs) == []


# ── R5-10: a thinned draft's projection is pruned and never re-projected ────

def test_a_thinned_drafts_shift_projection_goes_and_its_rows_still_read(db, monkeypatch):
    import ops
    import schedule_shifts as ss
    rid = _restaurant(db)
    old = ["2026-01-05", "2026-01-11"]
    first = models.save_schedule_history(rid, old[0], old[1], 6, 0, 30, _csv([(old[0], "Ana")]), [])
    second = models.save_schedule_history(rid, old[0], old[1], 6, 0, 30, _csv([(old[0], "Bo")]), [])
    _sql("UPDATE schedule_history SET generated_at=datetime('now', '-60 days') WHERE id=?", (first,))
    assert _sql("SELECT superseded_by FROM schedule_history WHERE id=?", (first,))[0][0] == second
    assert _sql("SELECT COUNT(*) FROM schedule_shifts WHERE history_id=?", (first,))[0][0] == 1
    conn = models.get_conn()
    try:
        out = ops._thin_drafts(conn, time.monotonic() + 60)
    finally:
        conn.close()
    assert out.get("schedule_shifts_of_thinned_drafts") == 1
    assert _sql("SELECT COUNT(*) FROM schedule_shifts WHERE history_id=?", (first,))[0][0] == 0
    assert _sql("SELECT COUNT(*) FROM schedule_shift_weeks WHERE history_id=?", (first,))[0][0] == 0
    # Read from its text, and the boot backfill leaves it so.
    assert [r["employee"] for r in ss.rows_of(first, "working")] == ["Ana"]
    _sql("DELETE FROM job_cursors WHERE 1=1")
    ss.backfill()
    assert _sql("SELECT COUNT(*) FROM schedule_shifts WHERE history_id=?", (first,))[0][0] == 0
    assert _sql("SELECT COUNT(*) FROM schedule_shifts WHERE history_id=?", (second,))[0][0] == 1


# ── R5-11: a try that saved never generates the week again ─────────────────

def test_a_resumed_try_whose_save_committed_is_settled_without_a_second_draft(db):
    import job_queue
    import ops
    import schedule_job as sjob
    rid = _restaurant(db)
    job_id, _joined = ops.claim_async_job("g5-saved", "schedule", rid)
    hid = models.save_schedule_history(rid, WEEK[0], WEEK[-1], 6, 0, 30, _csv([(WEEK[0], "Ana")]), [],
                                       job_id=job_id)
    job = job_queue.Job({"id": 9, "payload": {"job_id": job_id, "restaurant_id": rid}, "checkpoint": {}})
    assert sjob.finish_from_checkpoint(job) is True
    state = ops.read_async_job(job_id, restaurant_id=rid)
    assert state["result"]["saved"] and state["result"]["history_id"] == hid
    # A try that saved nothing still generates.
    other, _j = ops.claim_async_job("g5-unsaved", "schedule", rid)
    job2 = job_queue.Job({"id": 10, "payload": {"job_id": other, "restaurant_id": rid}, "checkpoint": {}})
    assert sjob.finish_from_checkpoint(job2) is False


def test_the_saved_checkpoint_is_written_at_the_commit_before_the_after_save_steps():
    import inspect
    import schedule_job as sjob
    src = inspect.getsource(sjob._commit)
    assert src.index("_checkpoint_saved(job, winner, history_id)") < src.index("_after_save(job, winner")


# ── R5-12: Stop cancels the row; a lost lease stops the try ─────────────────

def test_a_row_this_process_still_runs_is_never_reclaimed_by_it(db, monkeypatch):
    import job_queue as jq
    rid = _restaurant(db)
    qid, _c = jq.enqueue("learner", {}, restaurant_id=rid, wake=False)
    job = jq.claim("w1", groups=("bulk",))
    assert job["id"] == qid
    _sql("UPDATE jobs SET lease_until=? WHERE id=?", (time.time() - 5, qid))       # a stall past the lease
    with jq._held_lock:
        jq._held[qid] = job
    try:
        assert jq.claim("w1", groups=("bulk",)) is None
    finally:
        with jq._held_lock:
            jq._held.pop(qid, None)
    again = jq.claim("w2", groups=("bulk",))                                      # another process resumes it
    assert again and again["id"] == qid


def test_a_try_that_lost_its_lease_stops_and_settles_nothing(db, monkeypatch):
    import job_queue as jq
    rid = _restaurant(db)
    qid, _c = jq.enqueue("learner", {}, restaurant_id=rid, wake=False)
    job = jq.claim("w1", groups=("bulk",))
    job["lease_lost"] = True
    seen = []

    def handler(j):
        seen.append(jq.lease_lost())
        jq.raise_if_lease_lost()
        return {"ran": True}
    monkeypatch.setitem(jq.HANDLERS, "learner", "tests.test_g5_sched_refix_1020:_never")
    monkeypatch.setattr(jq, "_resolve", lambda path: handler)
    _sql("UPDATE jobs SET lease_owner='w2' WHERE id=?", (qid,))                    # taken by another consumer
    assert jq.run_one(job) is None and seen == [True]
    assert _sql("SELECT state, lease_owner FROM jobs WHERE id=?", (qid,))[0][:] == ("running", "w2")


def test_stop_cancels_the_queued_row():
    import inspect
    import mobile_api
    src = inspect.getsource(mobile_api.mobile_schedule_stop) if hasattr(mobile_api, "mobile_schedule_stop") else \
        open(mobile_api.__file__).read()
    assert "_jq_stop.cancel(external_id=job_id" in src


# ── R9-10: predictability pay on an overnight start ─────────────────────────

def test_a_small_hours_start_is_the_next_calendar_day_for_notice():
    import predictability_pay as pp
    start = pp._start_dt({"date": "2026-10-09", "shift_start": "12:30am"})
    assert start == dt.datetime(2026, 10, 10, 0, 30)
    assert pp._start_dt({"date": "2026-10-09", "shift_start": "6:00am"}) == dt.datetime(2026, 10, 9, 6, 0)
    sent = dt.datetime(2026, 10, 8, 22, 0)                                         # Thursday 10pm
    spec = {"window_days": 14, "tiers": [{"kinds": ["add"], "under_hours": 24, "pay": {"hours": 4}},
                                         {"kinds": ["add"], "at_least_hours": 24, "under_hours": 336,
                                          "pay": {"hours": 1}}]}
    owed = pp.price([{"employee": "Ana", "kind": "add", "date": "2026-10-09", "first_start": start,
                      "after": {"role": "Server"}}], spec, sent)
    # 26.5 hours of notice (it read 2.5, and recorded the under-a-day tier).
    assert owed and owed[0]["notice_hours"] == 26.5 and owed[0]["premium_hours"] == 1.0


# ── R9-17: the Studio's per-call sweep is never left on the shared entry ────

def test_a_studio_score_hands_its_sweep_back_and_leaves_the_shared_entry_alone(db, monkeypatch):
    import threading
    import schedule_engine as se
    rid = _restaurant(db)
    mine = [{"kind": "rest_gap", "employee": "Ana"}]

    def fake_prepared(restaurant_id, rows, result, prepared, extra, profiles):
        return {"checked": False, "recommendations": []}, {}, {}, mine
    monkeypatch.setattr(se, "_score_prepared", fake_prepared)
    prepared = {"lock": threading.Lock(), "signals": {}, "weights": {}}
    box = {}
    se._score_schedule_quality(rid, [], {}, what_if=False, prepared=prepared, sweep_out=box)
    assert box["violations"] is mine and "violations" not in prepared
