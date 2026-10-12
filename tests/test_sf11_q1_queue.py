"""Blind re-audit 10/11/26, workstream Q1 — the queue and its processes:
the claim over the whole queue (SCALE-2), organizations under the scheduled
class (SCALE-3), fair-queueing virtual time (SCALE-4), the CPU slot given
back while the model writes (SCALE-1, STRESS-13), the draft's reserved slot
and spread (SCALE-28), the idle claim without the write lock (SCALE-32),
one store for generations (ARCH-10), the queue worker process and its
recycling (SCALE-17, SCALE-31)."""
import inspect
import threading
import time

import pytest

import models
from models import Restaurant, create_restaurant


@pytest.fixture
def db(db_path, monkeypatch):
    real = models.get_conn

    def conn(*a, **k):
        return real(db_path)
    import sys
    for mod in list(sys.modules.values()):
        bound = getattr(mod, "get_conn", None) if mod is not None else None
        if bound is real:
            monkeypatch.setattr(mod, "get_conn", conn)
    monkeypatch.setattr(models, "get_conn", conn)
    monkeypatch.setattr(models, "DB_PATH", db_path)
    import job_queue
    job_queue.reset_for_tests()
    monkeypatch.delenv("QUEUE_WORKER", raising=False)
    monkeypatch.setattr(job_queue, "kick", lambda *a, **k: None)
    return job_queue


def _restaurants(db_path, n, org=None):
    conn = models.get_conn()
    try:
        cur = conn.execute("SELECT COALESCE(MAX(id), 0) FROM restaurants").fetchone()[0]
        conn.executemany("INSERT INTO restaurants (name, owner_email, organization_id) VALUES (?,?,?)",
                         [(f"R{cur + i}", f"r{cur + i}@x.example", org) for i in range(1, n + 1)])
        conn.commit()
        return [r[0] for r in conn.execute("SELECT id FROM restaurants WHERE id>? ORDER BY id", (cur,)).fetchall()]
    finally:
        conn.close()


def _org():
    conn = models.get_conn()
    try:
        conn.execute("INSERT INTO organizations (name, owner_email) VALUES ('Group', 'g@x.example')")
        oid = conn.execute("SELECT MAX(id) FROM organizations").fetchone()[0]
        conn.commit()
        return oid
    finally:
        conn.close()


def _sql(sql, args=()):
    conn = models.get_conn()
    try:
        out = conn.execute(sql, args).fetchall()
        conn.commit()
        return out
    finally:
        conn.close()


def _done(job):
    _sql("UPDATE jobs SET state='done', lease_owner=NULL WHERE id=?", (job["id"],))


# ── SCALE-2: a press behind any backlog ─────────────────────────────────────

def test_a_press_behind_more_than_the_window_of_drafts_is_claimed_first(db, monkeypatch):
    jq = db
    monkeypatch.setenv("QUEUE_GENERATION_CONCURRENCY", "50")
    rids = _restaurants(None, jq.CANDIDATES * 3 + 1)
    now = time.time()
    jq.enqueue_many("auto_draft", [{"restaurant_id": r, "payload": {}, "idempotency_key": f"ad:{r}",
                                    "deadline": now + 3600 + i} for i, r in enumerate(rids[:-1])],
                    priority_class="scheduled", wake=False)
    jq.enqueue_many("learner", [{"restaurant_id": r, "payload": {}, "idempotency_key": f"l:{r}"}
                                for r in rids[:-1]], wake=False)
    press, _ = jq.enqueue("schedule_generate", {}, restaurant_id=rids[-1], wake=False)
    assert press > jq.CANDIDATES * 2          # behind more rows than the old 500-row window
    first = jq.claim("w", groups=("generation",))
    assert first.id == press


def test_a_bulk_tenant_queued_last_is_served_by_its_turn_not_its_id(db):
    """Every tenant's head is seen (no window of the oldest rows): a tenant
    whose only row is behind thousands of another's is served next."""
    jq = db
    big, small = _restaurants(None, 2)
    jq.enqueue_many("learner", [{"restaurant_id": big, "payload": {"i": i}} for i in range(jq.CANDIDATES + 50)],
                    wake=False)
    jq.enqueue("learner", {}, restaurant_id=small, wake=False)
    a = jq.claim("w", groups=("bulk",))
    _done(a)
    b = jq.claim("w", groups=("bulk",))
    assert {a["restaurant_id"], b["restaurant_id"]} == {big, small}


# ── SCALE-3: an organization's auto-drafts ─────────────────────────────────

def test_an_organizations_locations_are_not_held_behind_every_single_restaurant(db, monkeypatch):
    jq = db
    monkeypatch.setenv("QUEUE_GENERATION_CONCURRENCY", "50")
    oid = _org()
    locs = _restaurants(None, 40, org=oid)
    singles = _restaurants(None, 120)
    now = time.time()
    # Queued together, the locations first: EDF serves them in their turn.
    items = [{"restaurant_id": r, "payload": {}, "idempotency_key": f"o:{r}", "deadline": now + 6 * 3600 + i}
             for i, r in enumerate(locs + singles)]
    jq.enqueue_many("auto_draft", items, priority_class="scheduled", wake=False)
    order = []
    while True:
        j = jq.claim("w", groups=("generation",))
        if j is None:
            break
        order.append(j["restaurant_id"])
        _done(j)
    pos = [i for i, r in enumerate(order) if r in set(locs)]
    assert len(order) == 160 and pos == list(range(40)), "the group's 40 locations, in their deadline order"
    # Its bucket is per location: one restaurant's row spends its own token.
    tenants = {r["tenant_key"] for r in _sql("SELECT tenant_key FROM job_tenants WHERE klass='scheduled'")}
    assert f"r:{locs[0]}" in tenants and f"org:{oid}" in tenants


def test_an_organization_weighs_its_locations_for_per_restaurant_work(db):
    jq = db
    oid = _org()
    _restaurants(None, 40, org=oid)
    conn = models.get_conn()
    try:
        assert jq._tenant_weight(conn, f"org:{oid}", "scheduled") == 40.0
        assert jq._tenant_weight(conn, f"org:{oid}", "bulk") == 40.0
        assert jq._tenant_weight(conn, f"org:{oid}", "interactive") == 4.0      # presses: capped
    finally:
        conn.close()


def test_queue_stats_say_who_missed_their_deadline(db):
    jq = db
    (rid,) = _restaurants(None, 1)
    jq.enqueue("auto_draft", {}, restaurant_id=rid, deadline=time.time() - 1, wake=False)
    assert jq.claim("w", groups=("generation",)) is None
    assert jq.stats()["expired_by_tenant"] == {f"r:{rid}": 1}


# ── SCALE-4: virtual time ───────────────────────────────────────────────────

def test_a_long_idle_tenant_reenters_at_the_system_virtual_time(db):
    """A tenant whose stored virtual time is far behind (served long ago)
    ranks AT the system virtual time, not below every busy tenant: the busy
    tenant queued first is served first."""
    jq = db
    busy, idle = _restaurants(None, 2)
    for i in range(30):
        jq.enqueue("learner", {"i": i}, restaurant_id=busy, wake=False)
        _done(jq.claim("w", groups=("bulk",)))
    system = _sql("SELECT vtime FROM job_tenants WHERE tenant_key=? AND klass='bulk'", (jq.SYSTEM_TENANT,))[0][0]
    assert system >= 29
    _sql("INSERT INTO job_tenants (tenant_key, klass, tokens, refilled_at, vtime, served_at) "
         "VALUES (?, 'bulk', 30, ?, 0, ?)", (f"r:{idle}", time.time(), time.time() - 30 * 86400))
    jq.enqueue("learner", {}, restaurant_id=busy, wake=False)
    jq.enqueue("learner", {}, restaurant_id=idle, wake=False)
    first = jq.claim("w", groups=("bulk",))
    # The busy tenant's tag is system + 1 at most; the idle one's is the
    # system time itself, so it goes first — but only by one turn, never 29.
    _done(first)
    second = jq.claim("w", groups=("bulk",))
    assert {first["restaurant_id"], second["restaurant_id"]} == {busy, idle}
    vt_idle = _sql("SELECT vtime FROM job_tenants WHERE tenant_key=? AND klass='bulk'", (f"r:{idle}",))[0][0]
    assert vt_idle >= system, "brought up to the system virtual time, not left at 0"


def test_idle_tenants_are_pruned_and_the_class_clock_kept(db):
    jq = db
    old = time.time() - (jq.TENANT_IDLE_DAYS + 1) * 86400
    _sql("INSERT INTO job_tenants (tenant_key, klass, tokens, refilled_at, vtime, served_at) VALUES "
         "('r:1','bulk',1,?,5,?), ('r:2','bulk',1,?,5,?), (?, 'bulk', 0, ?, 9, ?)",
         (old, old, time.time(), time.time(), jq.SYSTEM_TENANT, old, old))
    assert jq.prune_tenants() == 1
    keys = {r[0] for r in _sql("SELECT tenant_key FROM job_tenants")}
    assert keys == {"r:2", jq.SYSTEM_TENANT}


# ── SCALE-32: the idle claim ────────────────────────────────────────────────

def test_an_idle_or_full_claim_never_takes_the_write_lock(db, monkeypatch):
    jq = db
    seen = []
    real = jq.get_conn

    class Spy:
        def __init__(self, c):
            self.c = c

        def execute(self, sql, *a):
            seen.append(sql.strip().split()[0].upper())
            return self.c.execute(sql, *a)

        def __getattr__(self, k):
            return getattr(self.c, k)
    monkeypatch.setattr(jq, "get_conn", lambda db_path=None: Spy(real(db_path)))
    assert jq.claim("w") is None
    assert "BEGIN" not in seen and "UPDATE" not in seen
    monkeypatch.setenv("QUEUE_GENERATION_CONCURRENCY", "1")
    a, b = _restaurants(None, 2)
    jq.enqueue("schedule_generate", {}, restaurant_id=a, wake=False)
    jq.enqueue("schedule_generate", {}, restaurant_id=b, wake=False)
    assert jq.claim("w", groups=("generation",)) is not None
    seen.clear()
    assert jq.claim("w", groups=("generation",)) is None        # the one slot is held
    assert "BEGIN" not in seen


def test_the_claim_reads_search_their_class_index():
    """The per-class reads must stay index searches (a scan of every queued
    row was the 8.8 ms claim)."""
    import job_queue
    src = inspect.getsource(job_queue)
    for idx in ("idx_jobs_class_tenant", "idx_jobs_class_deadline", "idx_jobs_deadline"):
        assert idx in src
    assert "+not_before<=?" in inspect.getsource(job_queue._fair_head)
    assert "ORDER BY id LIMIT ?\",\n                (grp, now, now, CANDIDATES)" not in src


# ── SCALE-1 / STRESS-13: the slot is given back while the model writes ─────

def _claim_as_current(jq, rid, kind="schedule_generate"):
    jq.enqueue(kind, {}, restaurant_id=rid, wake=False)
    job = jq.claim("w", groups=("generation",))
    return job


def test_a_row_waiting_on_the_model_holds_no_cpu_slot(db, monkeypatch):
    jq = db
    monkeypatch.setenv("QUEUE_GENERATION_CONCURRENCY", "1")
    a, b = _restaurants(None, 2)
    first = _claim_as_current(jq, a)
    jq.enqueue("schedule_generate", {}, restaurant_id=b, wake=False)
    assert jq.claim("w2", groups=("generation",)) is None       # the slot is held by the code work
    token = jq._CURRENT.set(first)
    try:
        with jq.model_wait():
            assert _sql("SELECT phase FROM jobs WHERE id=?", (first.id,))[0][0] == "model"
            second = jq.claim("w2", groups=("generation",))
            assert second is not None and second["restaurant_id"] == b
            done = threading.Event()

            def finish_second():
                time.sleep(0.3)
                jq.complete(second.id, "w2")
                done.set()
            threading.Thread(target=finish_second).start()
        # Back from the model: it waited for the slot the second row held.
        assert done.is_set()
        assert _sql("SELECT phase FROM jobs WHERE id=?", (first.id,))[0][0] is None
    finally:
        jq._CURRENT.reset(token)


def test_rows_in_flight_are_bounded_by_max_running(db, monkeypatch):
    jq = db
    monkeypatch.setenv("QUEUE_GENERATION_CONCURRENCY", "1")
    monkeypatch.setenv("QUEUE_GENERATION_MAX_RUNNING", "2")
    rids = _restaurants(None, 3)
    j1 = _claim_as_current(jq, rids[0])
    _sql("UPDATE jobs SET phase='model' WHERE id=?", (j1.id,))
    j2 = _claim_as_current(jq, rids[1])
    assert j2 is not None
    _sql("UPDATE jobs SET phase='model' WHERE id=?", (j2.id,))
    jq.enqueue("schedule_generate", {}, restaurant_id=rids[2], wake=False)
    assert jq.claim("w", groups=("generation",)) is None, "two in flight, the bound"
    assert jq.max_running("generation") == 2


def test_a_row_back_from_the_model_goes_ahead_of_new_claims(db, monkeypatch):
    jq = db
    monkeypatch.setenv("QUEUE_GENERATION_CONCURRENCY", "1")
    a, b, c = _restaurants(None, 3)
    j1 = _claim_as_current(jq, a)
    _sql("UPDATE jobs SET phase='await' WHERE id=?", (j1.id,))       # back, waiting for its slot
    jq.enqueue("schedule_generate", {}, restaurant_id=b, wake=False)
    assert jq.claim("w", groups=("generation",)) is None
    assert jq._take_slot(j1) is True


def test_every_model_attempt_is_a_model_wait_and_the_clock_excuses_the_slot_wait():
    import ai_utils
    import schedule_engine as se
    import job_queue
    src = inspect.getsource(ai_utils.create_with_retry)
    assert "with _jq_wait.model_wait():" in src and src.index("model_wait()") < src.index("message = _send(")
    assert se._excuse_slot_wait in job_queue.SLOT_WAIT_HOOKS
    clock = se.GenerationClock(None)
    before = clock.deadline
    clock.excuse(120)
    assert clock.deadline == before + 120
    clock.excuse(10 ** 6)
    assert clock.excused == se.SLOT_WAIT_EXCUSE_SECONDS


def test_the_call_pool_is_sized_for_every_generation_that_may_be_writing(monkeypatch):
    import schedule_engine as se
    import job_queue
    monkeypatch.delenv("SCHEDULE_CALL_POOL_WORKERS", raising=False)
    assert se.call_pool_workers() == job_queue.max_running("generation") * se.parallel_calls()


# ── SCALE-28: the draft's reserved slot, and the spread ────────────────────

def test_a_press_keeps_a_slot_and_a_waiting_draft_keeps_one(db, monkeypatch):
    jq = db
    monkeypatch.setenv("QUEUE_GENERATION_CONCURRENCY", "2")
    rids = _restaurants(None, 6)
    now = time.time()
    for i, r in enumerate(rids[:3]):
        jq.enqueue("auto_draft", {}, restaurant_id=r, deadline=now + 3600 + i, wake=False)
    d1 = jq.claim("w", groups=("generation",))
    assert d1["priority_class"] == "scheduled"
    # Drafts alone never take the slot kept for presses.
    assert jq.claim("w", groups=("generation",)) is None
    p1, _ = jq.enqueue("schedule_generate", {}, restaurant_id=rids[3], wake=False)
    jq.enqueue("schedule_generate", {}, restaurant_id=rids[4], wake=False)
    assert jq.claim("w", groups=("generation",)).id == p1
    _done(d1)
    # A draft is waiting: the second press does not take the draft's slot.
    nxt = jq.claim("w", groups=("generation",))
    assert nxt["priority_class"] == "scheduled"
    assert jq.reservations("generation") == {"interactive": 1, "scheduled": 1}
    monkeypatch.setenv("QUEUE_GENERATION_CONCURRENCY", "1")
    assert jq.reservations("generation") == {}


def test_presses_take_every_slot_when_no_draft_waits(db, monkeypatch):
    jq = db
    monkeypatch.setenv("QUEUE_GENERATION_CONCURRENCY", "2")
    a, b = _restaurants(None, 2)
    jq.enqueue("schedule_generate", {}, restaurant_id=a, wake=False)
    jq.enqueue("schedule_generate", {}, restaurant_id=b, wake=False)
    assert jq.claim("w", groups=("generation",)) and jq.claim("w", groups=("generation",))


def test_auto_drafts_are_spread_from_the_pass_and_a_paid_answer_is_not(db, monkeypatch):
    jq = db
    import strategy_jobs
    monkeypatch.setattr(strategy_jobs, "AUTO_DRAFT_SPREAD_MINUTES", 120)
    rids = _restaurants(None, 20)
    offs = {strategy_jobs.auto_draft_offset_seconds(r) for r in rids}
    assert len(offs) > 10 and all(0 <= o < 7200 for o in offs)
    assert strategy_jobs.auto_draft_offset_seconds(rids[0]) == strategy_jobs.auto_draft_offset_seconds(rids[0])
    r = models.get_restaurant(rids[0])
    before = time.time()
    qid = strategy_jobs.enqueue_auto_draft(r, period="2026-10-12", target="2026-10-14", attempt=0)
    nb, dl = _sql("SELECT not_before, deadline FROM jobs WHERE id=?", (qid,))[0]
    assert nb >= before + strategy_jobs.auto_draft_offset_seconds(rids[0]) - 1
    assert abs((dl - nb) - strategy_jobs.AUTO_DRAFT_QUEUE_HOURS * 3600) < 1
    ans = strategy_jobs.enqueue_auto_draft(r, period="2026-10-12", target="2026-10-14", attempt=0,
                                           answer={"key": "k", "text": "x"})
    assert _sql("SELECT not_before FROM jobs WHERE id=?", (ans,))[0][0] <= time.time()


# ── ARCH-10: one store ──────────────────────────────────────────────────────

def test_the_owners_poll_reads_the_queue_rows_verdict(db):
    jq = db
    import ops
    (rid,) = _restaurants(None, 1)
    ops.claim_async_job("job-q1", "schedule", rid)
    qid, _ = jq.enqueue("schedule_generate", {}, restaurant_id=rid, external_id="job-q1", wake=False)
    assert ops.read_async_job("job-q1")["status"] == "pending"
    # Queued a long time (past the age a job is called dead by): still its turn.
    _sql("UPDATE async_jobs SET created_at=datetime('now', '-120 minutes') WHERE job_id='job-q1'")
    assert ops.read_async_job("job-q1")["status"] == "pending"
    _sql("UPDATE jobs SET state='expired' WHERE id=?", (qid,))
    out = ops.read_async_job("job-q1")
    assert out["status"] == "error" and "started in time" in out["result"]["error"]
    assert ops.job_still_pending("job-q1") is False


def test_both_joined_branches_promote_in_the_queue():
    import mobile_api
    import schedule_engine as se
    assert inspect.getsource(mobile_api.mobile_generate_schedule).count("promote_generation(") == 2
    assert "_jq_p.promote(job_id)" in inspect.getsource(se.promote_generation)


# ── SCALE-17 / SCALE-31: the queue worker process ──────────────────────────

def test_the_worker_recycles_after_its_jobs_and_hands_back_nothing(db, monkeypatch):
    jq = db
    monkeypatch.setitem(jq.HANDLERS, "learner", f"{__name__}:_ok_handler")
    for i in range(3):
        jq.enqueue("learner", {"i": i}, wake=False)
    stop = threading.Event()
    out = jq.run_worker(groups=("bulk",), stop=stop, threads=1, max_jobs=2)
    assert out == "recycle"
    states = sorted(r[0] for r in _sql("SELECT state FROM jobs"))
    assert states == ["done", "done", "queued"]


def _ok_handler(job):
    return {"ok": True}


# ── SCALE-27: people_nightly reads once and writes only what moved ─────────

def test_a_nightly_quarter_rollup_rewrites_nothing_that_did_not_move(db):
    from datetime import date, timedelta
    import shift_facts
    (rid,) = _restaurants(None, 1)
    rows = [{"date": (date(2026, 7, 1) + timedelta(days=i)).isoformat(), "day": "Monday", "employee": p,
             "role": "Server", "shift_start": "16:00", "shift_end": "22:00", "scheduled_hours": "6",
             "actual_hours": "6", "sales": "1000"} for i in range(20) for p in ("Ana B.", "Ben C.")]
    shift_facts.ingest(rid, rows, "upload")
    assert shift_facts.rollup_quarters(rid, today=date(2026, 9, 29)) == 2
    _sql("UPDATE person_quarters SET updated_at='2000-01-01 00:00:00' WHERE restaurant_id=?", (rid,))
    assert shift_facts.rollup_quarters(rid, today=date(2026, 9, 29)) == 2
    stamps = {r[0] for r in _sql("SELECT updated_at FROM person_quarters WHERE restaurant_id=?", (rid,))}
    assert stamps == {"2000-01-01 00:00:00"}, "unchanged quarters are not rewritten every night"
    # A quarter that moved is.
    shift_facts.ingest(rid, rows + [dict(rows[0], date="2026-08-30")], "upload")
    shift_facts.rollup_quarters(rid, today=date(2026, 9, 29))
    moved = _sql("SELECT employee_name FROM person_quarters WHERE restaurant_id=? "
                 "AND updated_at<>'2000-01-01 00:00:00'", (rid,))
    assert [r[0] for r in moved] == ["Ana B."]


def test_identity_reads_the_restaurants_questions_and_organization_once(db):
    import people
    (rid,) = _restaurants(None, 1)
    conn = people._conn()
    reads = []

    class Spy:
        def execute(self, sql, *a):
            if "FROM person_questions" in sql or "SELECT organization_id" in sql:
                reads.append(sql)
            return conn.execute(sql, *a)

        def __getattr__(self, k):
            return getattr(conn, k)
    try:
        spy = Spy()
        idx = people._Index(spy, rid)
        pids = [people._create_person(spy, idx, f"Kim Tran {c}", "test") for c in "ABCD"]
        for a in pids:
            for b in pids:
                people._ask(spy, idx, a, b, "same_person", "a close spelling")
        conn.commit()
    finally:
        conn.close()
    assert sum("person_questions" in s for s in reads) == 1
    assert sum("organization_id" in s for s in reads) == 1
    assert len(_sql("SELECT id FROM person_questions WHERE restaurant_id=?", (rid,))) == 6


def test_gunicorns_master_runs_the_queue_worker_beside_the_web_process():
    import importlib.util
    import os
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    spec = importlib.util.spec_from_file_location("gunicorn_conf", os.path.join(root, "gunicorn.conf.py"))
    conf = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(conf)
    assert callable(conf.when_ready) and callable(conf.on_exit)
    src = open(os.path.join(root, "gunicorn.conf.py")).read()
    assert "worker.py" in src and "--queue" in src and "worker_process_wanted()" in src
    import worker
    w = inspect.getsource(worker)
    assert "QUEUE_WORKER_MAX_JOBS" in w and "QUEUE_WORKER_MAX_RSS_MB" in w and "run_worker(" in w
