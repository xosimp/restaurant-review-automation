"""Scheduler audit 10/10/26, workstream H2 — the durable job queue (RM-7)
and what moved onto it: schedule generation (SCALE-1, SCALE-10, ARCH-4),
resume across a deploy (SCALE-2, SCALE-22), coordination in the database
instead of process memory (SCALE-24, ARCH-22), the auto-draft (SCALE-13)
and the nightly learners (SCALE-25)."""
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
    return db_path


@pytest.fixture
def quiet(monkeypatch):
    """No consumer threads: the test drives claim / run_one itself."""
    import job_queue
    monkeypatch.setattr(job_queue, "kick", lambda *a, **k: None)
    return job_queue


def _restaurant(db_path, name="Queue Grill", **cols):
    rid = create_restaurant(Restaurant(name=name, owner_email=f"{name.split()[0].lower()}@x.com"), db_path=db_path)
    if cols:
        conn = models.get_conn(db_path)
        conn.execute("UPDATE restaurants SET " + ", ".join(f"{k}=?" for k in cols) + " WHERE id=?",
                     (*cols.values(), rid))
        conn.commit()
        conn.close()
    return rid


def _row(qid):
    conn = models.get_conn()
    try:
        return dict(conn.execute("SELECT * FROM jobs WHERE id=?", (qid,)).fetchone())
    finally:
        conn.close()


def _set(qid, **cols):
    conn = models.get_conn()
    try:
        conn.execute("UPDATE jobs SET " + ", ".join(f"{k}=?" for k in cols) + " WHERE id=?", (*cols.values(), qid))
        conn.commit()
    finally:
        conn.close()


# ── the row and its claim ───────────────────────────────────────────────────

def test_enqueue_is_idempotent_on_its_key(db, quiet):
    jq = quiet
    a, created = jq.enqueue("learner", {"x": 1}, idempotency_key="k1")
    b, again = jq.enqueue("learner", {"x": 2}, idempotency_key="k1")
    assert created and not again and a == b
    assert json.loads(_row(a)["payload_json"]) == {"x": 1}


def test_a_groups_concurrency_is_a_count_of_claimed_rows_across_processes(db, quiet, monkeypatch):
    """Two consumers (two processes) share ONE bound: the in-memory slots
    multiplied with every process (SCALE-24, ARCH-22)."""
    jq = quiet
    monkeypatch.setenv("QUEUE_GENERATION_CONCURRENCY", "2")
    rids = [_restaurant(db, name=f"R{i} Grill") for i in range(3)]
    for rid in rids:
        jq.enqueue("schedule_generate", {}, restaurant_id=rid)
    a = jq.claim("web-1", groups=("generation",))
    b = jq.claim("worker-1", groups=("generation",))
    c = jq.claim("worker-2", groups=("generation",))
    assert a and b and c is None
    jq.complete(a.id, "web-1")
    assert jq.claim("worker-2", groups=("generation",)) is not None


def test_one_running_generation_per_restaurant(db, quiet, monkeypatch):
    jq = quiet
    monkeypatch.setenv("QUEUE_GENERATION_CONCURRENCY", "4")
    rid = _restaurant(db)
    other = _restaurant(db, name="Other Grill")
    jq.enqueue("schedule_generate", {"n": 1}, restaurant_id=rid)
    jq.enqueue("auto_draft", {"n": 2}, restaurant_id=rid)
    jq.enqueue("auto_draft", {"n": 3}, restaurant_id=other)
    first = jq.claim("w", groups=("generation",))
    second = jq.claim("w", groups=("generation",))
    assert first.restaurant_id == rid if hasattr(first, "restaurant_id") else first["restaurant_id"] == rid
    assert second["restaurant_id"] == other
    assert jq.claim("w", groups=("generation",)) is None      # rid's draft waits for its press


def test_presses_go_before_drafts_and_drafts_by_earliest_deadline(db, quiet, monkeypatch):
    jq = quiet
    monkeypatch.setenv("QUEUE_GENERATION_CONCURRENCY", "1")
    r1, r2, r3 = (_restaurant(db, name=f"P{i} Grill") for i in range(3))
    now = time.time()
    late, _ = jq.enqueue("auto_draft", {}, restaurant_id=r1, deadline=now + 5000)
    soon, _ = jq.enqueue("auto_draft", {}, restaurant_id=r2, deadline=now + 100)
    press, _ = jq.enqueue("schedule_generate", {}, restaurant_id=r3)
    order = []
    for _ in range(3):
        j = jq.claim("w", groups=("generation",))
        order.append(j.id)
        jq.complete(j.id, "w")
    assert order == [press, soon, late]


def test_a_tenant_with_many_rows_cannot_starve_one_with_few(db, quiet):
    """Weighted fair queueing across tenants: a restaurant that queued ten
    learner rows first is served once, then the restaurant behind it."""
    jq = quiet
    big = _restaurant(db, name="Big Grill")
    small = _restaurant(db, name="Small Grill")
    for i in range(10):
        jq.enqueue("learner", {"i": i}, restaurant_id=big)
    jq.enqueue("learner", {"i": "s"}, restaurant_id=small)
    served = []
    for _ in range(3):
        j = jq.claim("w", groups=("bulk",))
        served.append(j["restaurant_id"])
        jq.complete(j.id, "w")
    assert served[:2] == [big, small] or served[:2] == [small, big]
    assert small in served[:2]


def test_an_organization_is_one_tenant_weighted_by_its_locations(db, quiet):
    jq = quiet
    conn = models.get_conn()
    try:
        conn.execute("INSERT INTO organizations (name, owner_email) VALUES ('Group', 'g@x.com')")
        org = conn.execute("SELECT MAX(id) FROM organizations").fetchone()[0]
        conn.commit()
    finally:
        conn.close()
    locs = [_restaurant(db, name=f"Loc{i} Grill", organization_id=org) for i in range(4)]
    single = _restaurant(db, name="Single Grill")
    for rid in locs:
        jq.enqueue("learner", {}, restaurant_id=rid)
    jq.enqueue("learner", {}, restaurant_id=single)
    assert {_row(1)["tenant_key"]} == {f"org:{org}"}
    conn = models.get_conn()
    try:
        assert jq._tenant_weight(conn, f"org:{org}") == 3.0        # 1 + log2(4)
        assert jq._tenant_weight(conn, f"r:{single}") == 1.0
    finally:
        conn.close()
    served = []
    for _ in range(5):
        j = jq.claim("w", groups=("bulk",))
        served.append(j["restaurant_id"])
        jq.complete(j.id, "w")
    # The group (weight 3) is served up to three times for the single's one,
    # never all four before it.
    assert served.index(single) <= 3


def test_a_tenant_past_its_burst_waits_behind_tenants_with_tokens(db, quiet, monkeypatch):
    jq = quiet
    monkeypatch.setitem(jq.BUCKETS, "interactive", (1.0, 0.0))
    monkeypatch.setenv("QUEUE_GENERATION_CONCURRENCY", "1")
    a = _restaurant(db, name="A Grill")
    b = _restaurant(db, name="B Grill")
    jq.enqueue("schedule_generate", {}, restaurant_id=a)
    j = jq.claim("w", groups=("generation",))
    jq.complete(j.id, "w")
    jq.enqueue("schedule_generate", {}, restaurant_id=a)       # a's burst is spent
    jq.enqueue("schedule_generate", {}, restaurant_id=b)
    nxt = jq.claim("w", groups=("generation",))
    assert nxt["restaurant_id"] == b
    jq.complete(nxt.id, "w")
    # Work-conserving: with nobody else waiting, a still runs.
    assert jq.claim("w", groups=("generation",))["restaurant_id"] == a


# ── leases, retries, drain ──────────────────────────────────────────────────

def test_a_dead_processes_row_is_claimed_again_and_counted(db, quiet):
    jq = quiet
    qid, _ = jq.enqueue("learner", {}, restaurant_id=None)
    j = jq.claim("dead", groups=("bulk",))
    assert j["attempts"] == 1
    assert jq.claim("other", groups=("bulk",)) is None          # leased
    _set(qid, lease_until=time.time() - 1)                       # its process died
    again = jq.claim("other", groups=("bulk",))
    assert again.id == qid and again["attempts"] == 2 and again.resumed
    assert not jq.complete(qid, "dead")                          # the dead try cannot finish it
    assert jq.complete(qid, "other")


def test_a_row_whose_process_died_on_its_last_try_fails(db, quiet):
    jq = quiet
    qid, _ = jq.enqueue("learner", {}, max_attempts=1)
    jq.claim("dead", groups=("bulk",))
    _set(qid, lease_until=time.time() - 1)
    assert jq.claim("other", groups=("bulk",)) is None
    assert _row(qid)["state"] == "failed"


def test_a_failed_try_is_retried_after_backoff_then_failed(db, quiet):
    jq = quiet
    qid, _ = jq.enqueue("learner", {}, max_attempts=2)
    j = jq.claim("w", groups=("bulk",))
    assert jq.fail(qid, "w", RuntimeError("boom")) == "queued"
    assert _row(qid)["not_before"] > time.time() + 20
    assert jq.claim("w", groups=("bulk",)) is None
    _set(qid, not_before=0)
    j = jq.claim("w", groups=("bulk",))
    assert jq.fail(j.id, "w", RuntimeError("boom")) == "failed"


def test_a_draining_consumer_hands_its_rows_back_uncounted(db, quiet):
    jq = quiet
    qid, _ = jq.enqueue("learner", {}, restaurant_id=None)
    j = jq.claim("w", groups=("bulk",))
    jq._held[j.id] = j
    token = jq._CURRENT.set(j)
    jq.checkpoint("repair", calls=2)
    try:
        assert jq.shutdown() == 1
    finally:
        jq._CURRENT.reset(token)
        jq._draining.clear()
        jq._held.clear()
    row = _row(qid)
    assert row["state"] == "queued" and row["attempts"] == 0 and row["lease_owner"] is None
    again = jq.claim("next", groups=("bulk",))
    assert again.resumed and again.checkpoint["stage"] == "repair" and again.checkpoint["calls"] == 2


def test_a_row_past_its_deadline_unstarted_expires_and_hands_back(db, quiet, monkeypatch):
    jq = quiet
    import ops
    import strategy_jobs
    released = []
    monkeypatch.setattr(ops, "release_period", lambda name, period: released.append((name, period)))
    rid = _restaurant(db)
    qid, _ = jq.enqueue("auto_draft", {"restaurant_id": rid, "period": "2026-10-12", "target": "2026-10-19",
                                       "attempt": 0}, restaurant_id=rid, deadline=time.time() - 1)
    assert jq.claim("w", groups=("generation",)) is None
    assert _row(qid)["state"] == "expired"
    assert (f"auto_draft:{rid}", "2026-10-12") in released
    assert (f"auto_draft_attempt:{rid}", "2026-10-19#0") in released


def test_a_press_joining_a_queued_draft_promotes_it_in_the_database(db, quiet, monkeypatch):
    jq = quiet
    import schedule_engine as se
    monkeypatch.setenv("QUEUE_GENERATION_CONCURRENCY", "1")
    a = _restaurant(db, name="A Grill")
    b = _restaurant(db, name="B Grill")
    draft, _ = jq.enqueue("auto_draft", {}, restaurant_id=a, external_id="auto-a", deadline=time.time() + 60)
    jq.enqueue("schedule_generate", {}, restaurant_id=b, external_id="press-b")
    assert se.promote_generation("auto-a") == "waiting"
    row = _row(draft)
    assert row["priority_class"] == "interactive" and row["deadline"] is None
    # Now an interactive row queued first: it goes first.
    assert jq.claim("w", groups=("generation",))["external_id"] == "auto-a"


def test_checkpoints_keep_the_stages_reached(db, quiet):
    jq = quiet
    qid, _ = jq.enqueue("learner", {})
    j = jq.claim("w", groups=("bulk",))
    token = jq._CURRENT.set(j)
    try:
        jq.checkpoint("inputs_and_model")
        jq.checkpoint("repair")
    finally:
        jq._CURRENT.reset(token)
    cp = json.loads(_row(qid)["stage_checkpoint"])
    assert cp["stage"] == "repair" and cp["stages"] == ["inputs_and_model", "repair"]
    assert jq.checkpoint("outside") is False                    # no job: a no-op


# ── modes and consumers ─────────────────────────────────────────────────────

def test_the_modes_and_the_worker_fallback(db, monkeypatch):
    """Worker mode is the default since blind re-audit 10/11/26 (SCALE-17):
    the queue worker process consumes, and the web process consumes only
    while no worker is alive. QUEUE_WORKER=0 is in-process consumption. The
    JOB_QUEUE=0 paths are gone (ARCH-10)."""
    import job_queue as jq
    assert jq.mode() == "worker" and jq.worker_process_wanted()
    monkeypatch.setenv("QUEUE_WORKER", "0")
    assert jq.mode() == "inprocess" and jq.this_process_consumes() and not jq.worker_process_wanted()
    monkeypatch.setenv("QUEUE_WORKER", "1")
    assert jq.mode() == "worker"
    monkeypatch.setenv("QUEUE_WORKER_PROCESS", "0")
    assert not jq.worker_process_wanted()
    # No worker has beaten its heartbeat: the web process consumes after all.
    assert jq.this_process_consumes()
    conn = models.get_conn()
    try:
        conn.execute("INSERT INTO job_workers (worker_id, role, pid, host, started_at, heartbeat_at) "
                     "VALUES ('w1', 'worker', 1, 'h', ?, ?)", (time.time(), time.time()))
        conn.commit()
    finally:
        conn.close()
    assert jq.worker_alive() and not jq.this_process_consumes()
    monkeypatch.setenv("JOB_QUEUE", "0")
    assert jq.mode() == "worker" and not hasattr(jq, "enabled")


def test_the_in_process_consumer_runs_a_row_through_its_handler(db, monkeypatch):
    import job_queue as jq
    ran = []
    monkeypatch.setitem(jq.HANDLERS, "learner", f"{__name__}:_record_handler")
    _HANDLER_LOG.clear()
    qid, _ = jq.enqueue("learner", {"n": 7}, wake=False)
    fut = jq.local_future(qid)
    jq.kick("bulk")
    assert fut.result(10) == {"n": 7}
    assert _HANDLER_LOG == [7] and _row(qid)["state"] == "done"
    ran.append(1)


_HANDLER_LOG = []


def _record_handler(job):
    import job_queue
    assert job_queue.current() is job
    _HANDLER_LOG.append(job.payload["n"])
    return {"n": job.payload["n"]}


def test_a_handler_that_raises_is_captured_and_retried(db, quiet, monkeypatch):
    jq = quiet
    import ops
    captured = []
    monkeypatch.setattr(ops, "capture", lambda e, job=None, **k: captured.append(job))
    monkeypatch.setitem(jq.HANDLERS, "learner", f"{__name__}:_boom_handler")
    qid, _ = jq.enqueue("learner", {})
    j = jq.claim("w", groups=("bulk",))
    jq.run_one(j)
    assert captured == ["queue:learner"] and _row(qid)["state"] == "queued"


def _boom_handler(job):
    raise RuntimeError("learner broke")


def test_queue_stats_say_the_wait_p95_and_depth(db, quiet):
    jq = quiet
    now = time.time()
    for wait in (5, 10, 200):
        qid, _ = jq.enqueue("schedule_generate", {})
        _set(qid, enqueued_at=now - wait - 1, started_at=now - 1, state="done")
    jq.enqueue("learner", {})
    st = jq.stats()
    assert st["wait_p95_seconds"] == 200.0
    assert st["groups"]["bulk"]["queued"] == 1


# ── the boot sweep leaves a resumable generation pending ────────────────────

def test_the_boot_sweep_leaves_a_resumable_job_pending_and_fails_the_rest(db, quiet):
    jq = quiet
    import ops
    rid = _restaurant(db)
    ops.claim_async_job("resumable", "schedule", rid)
    other = _restaurant(db, name="Other Grill")
    ops.claim_async_job("dead", "schedule", other)
    jq.enqueue("schedule_generate", {"job_id": "resumable"}, restaurant_id=rid, external_id="resumable")
    j = jq.claim("old-process", groups=("generation",))
    _set(j.id, lease_until=time.time() - 1)
    assert ops.sweep_stale_jobs() == 1
    assert ops.read_async_job("resumable")["status"] == "pending"
    assert ops.read_async_job("dead")["status"] == "error"
    assert ops.read_async_job("resumable").get("seconds_left", 0) > 15 * 60


# ── resume: answers already paid for are read, not bought again ─────────────

def _record(job_id, answer, model="claude-sonnet-5-5", dates=("2026-10-19",)):
    import schedule_output
    request = {"model": model, "messages": [{"role": "user", "content": "WEEK " + " ".join(dates)}],
               "output_config": {"format": {"type": "json_schema", "schema": {"x": 1}}}}
    return schedule_output.record_call(1, request, answer=answer, generation_id="g1", job_id=job_id,
                                       outcome="ok", stop_reason="end_turn"), request


def test_a_resumed_generation_reads_each_stored_answer_once(db):
    import schedule_resume
    rec, request = _record("job-r", '{"rows": []}')
    assert schedule_resume.stored_answer(request) is None             # not resuming: call as usual
    with schedule_resume.resuming("job-r"):
        msg = schedule_resume.stored_answer(request)
        assert msg is not None and msg._cavnar_resumed is True
        assert msg.content[0].text == '{"rows": []}' and msg.usage.output_tokens == 0
        assert schedule_resume.stored_answer(request) is None         # each answer once
        assert schedule_resume.answers_reused() == [rec]
    # Through the call site's hook: the batch hook falls through to it.
    import schedule_batch
    with schedule_resume.resuming("job-r"):
        assert schedule_batch.answer_for(request) is not None
    # Another job's answers are never read.
    with schedule_resume.resuming("job-other"):
        assert schedule_resume.stored_answer(request) is None


def test_an_errored_or_already_resumed_answer_is_not_reused(db):
    import schedule_output
    import schedule_resume
    request = {"model": "m", "messages": [{"role": "user", "content": "x"}]}
    schedule_output.record_call(1, request, answer="bad", job_id="job-e", outcome="error")
    schedule_output.record_call(1, request, answer="again", job_id="job-e", outcome="ok", usage={"resumed": True})
    with schedule_resume.resuming("job-e"):
        assert schedule_resume.stored_answer(request) is None


def test_the_call_record_keeps_the_generations_job(db, monkeypatch):
    """labor's recorder stamps the job from the generation's clock, so the
    resume can find the answers (SCALE-2)."""
    import labor
    import schedule_engine as se
    import schedule_output
    clock = se.GenerationClock.__new__(se.GenerationClock)
    clock.job_id = "job-clock"
    token = se._CLOCK.set(clock)
    try:
        rid = labor._record_schedule_call(1, {"model": "m", "messages": []}, {}, "answer", None,
                                          outcome="ok")
    finally:
        se._CLOCK.reset(token)
    conn = models.get_conn()
    try:
        row = conn.execute("SELECT job_id, call_key FROM schedule_model_calls WHERE id=?", (rid,)).fetchone()
    finally:
        conn.close()
    assert row["job_id"] == "job-clock" and row["call_key"]
    assert schedule_output.stored_answers("job-clock")[0]["answer"] == "answer"


def test_a_resumed_try_past_the_saved_stage_finishes_with_that_draft(db, quiet, monkeypatch):
    """A deploy killed the try in the quality gate's rewrite: the next try
    finishes the job with the saved draft and payload — nothing generated,
    nothing paid — and pushes the watchers."""
    jq = quiet
    import ops
    import schedule_engine as se
    import schedule_resume
    rid = _restaurant(db)
    ops.claim_async_job("job-s", "schedule", rid)
    qid, _ = jq.enqueue("schedule_generate", {"job_id": "job-s", "restaurant_id": rid, "kwargs": {}},
                        restaurant_id=rid, external_id="job-s")
    first = jq.claim("old", groups=("generation",))
    token = jq._CURRENT.set(first)
    try:
        schedule_resume.checkpoint_saved(41, {"ok": True, "history_id": 41, "review": {"lines": []}},
                                         week_start="2026-10-19")
        jq.checkpoint("repair")                                         # the gate's rewrite, later
    finally:
        jq._CURRENT.reset(token)
    _set(qid, lease_until=time.time() - 1)
    restored, notified = [], []
    monkeypatch.setattr(se, "_restore_draft", lambda r, keep, drop=None: restored.append(keep))
    monkeypatch.setattr(se, "notify_generation_watchers", lambda j, r, exclude=None: notified.append(j))
    monkeypatch.setattr(se, "_run_schedule_job", lambda *a, **k: pytest.fail("a saved try is never regenerated"))
    again = jq.claim("new", groups=("generation",))
    out = jq.run_one(again)
    assert out == {"resumed": "saved"} and restored == [41] and notified == ["job-s"]
    job = ops.read_async_job("job-s", restaurant_id=rid)
    assert job["status"] == "done" and job["result"]["history_id"] == 41 and job["result"]["resumed"] is True


def test_a_resumed_try_before_the_save_runs_again_reading_stored_answers(db, quiet, monkeypatch):
    jq = quiet
    import ops
    import schedule_engine as se
    import schedule_resume
    rid = _restaurant(db)
    ops.claim_async_job("job-m", "schedule", rid)
    _rec, request = _record("job-m", "stored answer")
    qid, _ = jq.enqueue("schedule_generate", {"job_id": "job-m", "restaurant_id": rid,
                                              "kwargs": {"week_start": "2026-10-19"}},
                        restaurant_id=rid, external_id="job-m")
    jq.claim("old", groups=("generation",))
    _set(qid, lease_until=time.time() - 1)
    seen = {}

    def run(job_id, r, **k):
        seen["kwargs"] = k
        seen["answer"] = schedule_resume.stored_answer(request)
        seen["clock"] = se.current_clock() is not None
        ops.finish_async_job(job_id, "done", {"ok": True, "history_id": 9})
    monkeypatch.setattr(se, "_run_schedule_job", run)
    monkeypatch.setattr(se, "notify_generation_watchers", lambda *a, **k: 0)
    out = jq.run_one(jq.claim("new", groups=("generation",)))
    assert out["resumed"] is True and out["history_id"] == 9
    assert seen["kwargs"] == {"week_start": "2026-10-19"}
    assert seen["answer"] is not None and seen["answer"].content[0].text == "stored answer"
    # The claimed row is the slot; the job runs on its own clock.
    assert seen["clock"]


def test_a_resumed_try_the_owner_stopped_runs_nothing(db, quiet, monkeypatch):
    jq = quiet
    import ops
    import schedule_engine as se
    rid = _restaurant(db)
    ops.claim_async_job("job-x", "schedule", rid)
    qid, _ = jq.enqueue("schedule_generate", {"job_id": "job-x", "restaurant_id": rid, "kwargs": {}},
                        restaurant_id=rid, external_id="job-x")
    jq.claim("old", groups=("generation",))
    ops.stop_async_job("job-x", {"ok": False, "stopped": True, "error": se.STOPPED_MESSAGE})
    _set(qid, lease_until=time.time() - 1)
    monkeypatch.setattr(se, "_run_schedule_job", lambda *a, **k: pytest.fail("a stopped job runs nothing"))
    assert jq.run_one(jq.claim("new", groups=("generation",))) == {"resumed": "not_pending"}


# ── the press, end to end, and its watchers ─────────────────────────────────

def test_a_press_is_a_queue_row_its_attribution_kept_and_its_secrets_dropped(db, monkeypatch):
    import job_queue as jq
    import ops
    import schedule_engine as se
    import ai_utils
    rid = _restaurant(db)
    ops.claim_async_job("job-p", "schedule", rid)
    ran = []
    monkeypatch.setattr(se, "_run_schedule_job",
                        lambda job_id, r, **k: ran.append((job_id, k, ai_utils.attribution_for_thread()))
                        or ops.finish_async_job(job_id, "done", {"ok": True}))
    with ai_utils.ai_context(trigger="owner", actor_user_id=77, correlation_id="press-1"):
        fut = se.submit_generation("job-p", rid, rejection={"kind": "draft_discarded", "history_id": 3,
                                                            "user": {"id": 77, "role": "owner",
                                                                     "password_hash": "x", "session_token": "y"}},
                                   week_start="2026-10-19")
    fut.result(10)
    assert ran[0][0] == "job-p" and ran[0][1] == {"week_start": "2026-10-19"}
    assert ran[0][2]["actor_user_id"] == 77 and ran[0][2]["trigger"] == "owner"
    row = jq.row_for("job-p")
    payload = json.loads(row["payload_json"])
    assert row["kind"] == "schedule_generate" and row["priority_class"] == "interactive" and row["state"] == "done"
    assert payload["rejection"]["user"] == {"id": 77, "role": "owner"}


def test_watchers_are_rows_taken_once(db):
    import schedule_engine as se
    se.watch_generation("job-w", {"id": 5, "is_admin": True})
    se.watch_generation("job-w", {"id": 6, "is_admin": True})
    assert se.generation_watchers("job-w") == {5: True, 6: True}
    assert se.take_generation_watchers("job-w") == {5: True, 6: True}
    assert se.take_generation_watchers("job-w") == {}


def test_generation_scope_is_a_clock_and_the_slot_is_the_row(db, quiet, monkeypatch):
    """ARCH-10 (blind re-audit 10/11/26): one store. The in-process
    GenerationSlots and the JOB_QUEUE=0 pool are gone; generation_scope is
    the clock alone, in a queue job or a direct call."""
    jq = quiet
    import inspect
    import schedule_engine as se
    monkeypatch.setattr(se._ops, "set_async_job_deadline", lambda *a, **k: None)
    for gone in ("GenerationSlots", "_GEN_SLOTS", "_gen_pool", "GenerationSlotTimeout", "GEN_BACKGROUND_WAIT_SECONDS"):
        assert not hasattr(se, gone), gone
    assert "_enqueue_generation" in inspect.getsource(se.submit_generation)
    jq.enqueue("schedule_generate", {})
    j = jq.claim("w", groups=("generation",))
    token = jq._CURRENT.set(j)
    try:
        with se.generation_scope("in-queue") as clock:
            assert se.current_clock() is clock
    finally:
        jq._CURRENT.reset(token)
    with se.generation_scope("direct") as clock:
        assert se.current_clock() is clock
    assert se.current_clock() is None


# ── the auto-draft and the learners as queue work ──────────────────────────

def test_the_auto_draft_pass_enqueues_instead_of_drafting(db, quiet, monkeypatch):
    jq = quiet
    import strategy_jobs
    from datetime import datetime
    rid = _restaurant(db, auto_draft_schedule=1, module_labor=1, auto_draft_weekday=3, auto_draft_weekday_chosen=1,
                      billing_status="active")
    monkeypatch.setattr(strategy_jobs, "_draft_one", lambda *a, **k: pytest.fail("the pass drafts nothing"))
    out = strategy_jobs.run_auto_draft_schedules(db_path=db, now=datetime(2026, 9, 24, 9, 0), enqueue=True)
    assert out["queued"] == 1 and out["attempted"] == 1 and out["ok"] == 1
    conn = models.get_conn()
    try:
        row = dict(conn.execute("SELECT * FROM jobs WHERE kind='auto_draft'").fetchone())
    finally:
        conn.close()
    assert row["restaurant_id"] == rid and row["priority_class"] == "scheduled" and row["deadline"] > time.time()
    # The same try is never queued twice.
    p = json.loads(row["payload_json"])
    r = models.get_restaurant(rid)
    again = strategy_jobs.enqueue_auto_draft(r, period=p["period"], target=p["target"], attempt=p["attempt"])
    assert again == row["id"]


def test_a_queued_auto_draft_runs_the_draft_in_its_slot(db, quiet, monkeypatch):
    jq = quiet
    import strategy_jobs
    rid = _restaurant(db, auto_draft_schedule=1, module_labor=1)
    monkeypatch.setattr(strategy_jobs, "AUTO_DRAFT_SPREAD_MINUTES", 0)
    calls = []
    monkeypatch.setattr(strategy_jobs, "_draft_one",
                        lambda r, db_path, se, bump, **k: calls.append((r.id, k, jq.in_group("generation")))
                        or bump("drafted"))
    r = models.get_restaurant(rid)
    strategy_jobs.enqueue_auto_draft(r, period="2026-09-24", target="2026-10-05", attempt=1)
    out = jq.run_one(jq.claim("w", groups=("generation",)))
    assert out == {"drafted": 1}
    assert calls == [(rid, {"period": "2026-09-24", "target": "2026-10-05", "attempt": 1, "batch": True}, True)]


def test_a_batched_answer_rides_its_row_to_the_call(db, quiet, monkeypatch):
    jq = quiet
    import strategy_jobs
    import schedule_batch
    rid = _restaurant(db, auto_draft_schedule=1, module_labor=1)
    got = []

    def draft(r, db_path, se, bump, **k):
        msg = schedule_batch.answer_for({"model": "m", "messages": [], "output_config": {}})
        got.append((msg.content[0].text, msg._cavnar_batch, msg.usage.output_tokens, k["batch"]))
    monkeypatch.setattr(strategy_jobs, "_draft_one", draft)
    key = schedule_batch.call_key({"model": "m", "messages": [], "output_config": {}})
    r = models.get_restaurant(rid)
    strategy_jobs.enqueue_auto_draft(r, period="p", target="2026-10-05", attempt=0, batch=False,
                                     answer={"key": key, "text": "the week", "stop_reason": "end_turn",
                                             "usage": {"input_tokens": 10, "output_tokens": 900}})
    jq.run_one(jq.claim("w", groups=("generation",)))
    assert got == [("the week", True, 900, False)]


def test_the_learners_fan_out_one_row_a_restaurant_a_day(db, quiet, monkeypatch):
    jq = quiet
    import strategy_jobs
    a = _restaurant(db, name="A Grill", module_labor=1)
    b = _restaurant(db, name="B Grill", module_labor=1)
    _restaurant(db, name="C Grill", module_labor=0)
    out = strategy_jobs.fan_out_learner("people_nightly", db_path=db, today="2026-10-10")
    assert out["queued"] == 3 and out["attempted"] == 3               # the backfill + two restaurants
    assert strategy_jobs.fan_out_learner("people_nightly", db_path=db, today="2026-10-10")["queued"] == 0
    ran = []
    monkeypatch.setitem(strategy_jobs.LEARNERS, "people_nightly", f"{__name__}:_learner_body")
    monkeypatch.setattr(strategy_jobs, "people_nightly_backfill", lambda db_path=None: ran.append("backfill") or {})
    _LEARNER_LOG.clear()
    while True:
        j = jq.claim("w", groups=("bulk",))
        if j is None:
            break
        jq.run_one(j)
    assert ran == ["backfill"] and sorted(_LEARNER_LOG) == sorted([(a, "2026-10-10"), (b, "2026-10-10")])


_LEARNER_LOG = []


def _learner_body(rid, today=None):
    _LEARNER_LOG.append((rid, today))
    return {"ok": True}


def test_the_scheduler_hands_the_learners_and_the_draft_to_the_queue():
    import inspect
    import scheduler
    import strategy_jobs
    loop = inspect.getsource(scheduler.scheduler_loop)
    for name in ("schedule_outcomes", "people_nightly", "schedule_memory"):
        assert f'_ops.run_job("{name}", learner_pass("{name}",' in loop
    assert "enqueue=True" in loop
    # The queue is the one path (ARCH-10): no switch puts the learners back
    # inline in the scheduler thread.
    import os
    os.environ["JOB_QUEUE"] = "0"
    try:
        assert strategy_jobs.learner_pass("x", "inline") != "inline"
    finally:
        os.environ.pop("JOB_QUEUE", None)


def test_the_worker_consumes_in_worker_mode_and_hands_rows_back_first():
    import inspect
    import worker
    src = inspect.getsource(worker)
    assert 'job_queue.mode() == "worker"' in src and "job_queue.run_worker" in src
    stop = inspect.getsource(worker._handle_stop)
    assert stop.index("job_queue.shutdown()") < stop.index("ops.shutdown_scheduler()")


# ── the cutover's write freeze, and the queue-wait trigger ─────────────────

def test_the_write_freeze_refuses_writes_stops_jobs_and_the_queue(db, quiet, monkeypatch):
    from flask import Flask
    import http_layer
    import scheduler
    jq = quiet
    app = Flask(__name__)
    http_layer.register(app)

    @app.route("/x", methods=["GET", "POST"])
    def x():
        return {"ok": True}
    monkeypatch.setenv("MAINTENANCE_READ_ONLY", "1")
    monkeypatch.setenv("RAILWAY_ENVIRONMENT", "production")
    c = app.test_client()
    r = c.post("/x")
    assert r.status_code == 503 and r.get_json()["maintenance"] is True and r.headers["Retry-After"]
    assert c.get("/x").status_code == 200
    assert scheduler.scheduling_allowed() is False
    jq.enqueue("learner", {})
    assert jq.claim("w", groups=("bulk",)) is None
    monkeypatch.delenv("MAINTENANCE_READ_ONLY")
    assert c.post("/x").status_code == 200 and scheduler.scheduling_allowed() is True
    assert jq.claim("w", groups=("bulk",)) is not None


def test_a_long_queue_wait_warns_the_operator(db, quiet, monkeypatch):
    import ops
    jq = quiet
    now = time.time()
    for wait in (200, 260, 300, 400, 500):
        qid, _ = jq.enqueue("schedule_generate", {})
        _set(qid, enqueued_at=now - wait - 1, started_at=now - 1, state="done")
    out = ops.check_platform_sla(send=False)
    assert out["queue"]["wait_p95_seconds"] == 500.0 and out["queue"]["wait_samples"] == 5
    assert any("wait" in p and "QUEUE_WORKER=1" in p for p in out["problems"])
