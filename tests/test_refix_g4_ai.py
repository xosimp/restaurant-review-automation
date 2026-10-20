"""Blind re-audit 10/20/26, fix round G4 — AI infrastructure, models and
costs (R4-01 ... R4-11).

R4-01  the trial pool never has an absolute fleet maximum; bulk provisioning's
       billing status is budgeted safely whatever it names.
R4-02  an automatic fleet brake for unattended spend; the emergency stop read
       at call time and armable from the console without a deploy.
R4-03  the owner AI pool is fair, bounded and prioritised; a job the store
       failed is never run; a late (paid) answer is kept and reused.
R4-04  breakers per model family, and per tenant for its own timeouts.
R4-05  the gateway being off with a large fleet is a page.
R4-06  one-item batches grouped into one batch per pass; the collector covers
       every open batch in turn.
R4-07  a batch submission is chunked under the API's limits.
R4-08  staff answers and translations off the request threads.
R4-09  the Monday plan fans out as queue rows.
R4-10  run caps bind inside a single rung.
"""
import json
import sqlite3
import sys
import threading
import time
import types

import anthropic
import httpx
import pytest

import ai_async
import ai_batches
import ai_utils
import models
import ops
import scheduler
from ai_utils import AIBudgetExceeded, create_with_retry, log_ai_usage

NS = types.SimpleNamespace


@pytest.fixture(autouse=True)
def _isolate(db_path, monkeypatch):
    real, default = models.get_conn, models.DB_PATH

    def conn(path=None, *a, **k):
        return real(db_path if path in (None, default, models.DB_PATH, db_path) else path)
    monkeypatch.setattr(models, "get_conn", conn)
    monkeypatch.setattr(models, "DB_PATH", db_path)
    monkeypatch.setattr(ai_utils, "_page", lambda *a, **k: True)
    monkeypatch.setattr(ai_utils.time, "sleep", lambda s: None)
    monkeypatch.delenv("AI_EMERGENCY_STOP", raising=False)
    ai_utils.reset_process_state(db_path)
    ai_utils._rate_cache.clear()
    yield
    ai_utils.reset_process_state(db_path)
    ai_utils._rate_cache.clear()


# ── helpers ────────────────────────────────────────────────────────────────

class _Usage:
    input_tokens = 100
    output_tokens = 100
    cache_creation_input_tokens = 0
    cache_read_input_tokens = 0


class _Message:
    usage = _Usage()
    content = []
    stop_reason = "end_turn"


class _Client:
    def __init__(self, error=None):
        self.calls, self.error = [], error
        outer = self

        class _M:
            def create(self, **kwargs):
                outer.calls.append(kwargs)
                if outer.error is not None:
                    raise outer.error
                return _Message()
        self.messages = _M()


_REQ = httpx.Request("POST", "https://api.anthropic.com/v1/messages")


def _server_error():
    return anthropic.InternalServerError("boom", response=httpx.Response(500, request=_REQ), body=None)


def _timeout():
    return anthropic.APITimeoutError(request=_REQ)


def _rest(rid, status="active", is_demo=0):
    conn = models.get_conn()
    conn.execute("INSERT OR IGNORE INTO restaurants (id, name, owner_email, billing_status, is_demo) "
                 "VALUES (?,?,?,?,?)", (rid, f"R{rid}", f"r{rid}@example.test", status, is_demo))
    conn.commit()
    conn.close()
    ai_utils.invalidate_budget_memo()


def _spend(rid, action, dollars, ago_minutes=5, trigger=None):
    out_rate = ai_utils._MODEL_PRICING["claude-sonnet-5"][1]
    row = log_ai_usage(rid, action, "claude-sonnet-5", 0, int(dollars / out_rate * 1_000_000))
    conn = models.get_conn()
    conn.execute("UPDATE ai_usage SET created_at=datetime('now', ?) WHERE id=?",
                 (f"-{int(ago_minutes)} minutes", row))
    conn.commit()
    conn.close()
    ai_utils._budget_cache.clear()
    ai_utils._rate_cache.clear()


def _call(rid, action, model="claude-sonnet-5", client=None, **kw):
    client = client or _Client()
    create_with_retry(client, restaurant_id=rid, action=action, model=model, max_tokens=10,
                      messages=[{"role": "user", "content": "x"}], **kw)
    return client


# ── R4-01: the trial pool and the bulk status ──────────────────────────────

def test_the_trial_pool_has_no_absolute_maximum_at_any_size(monkeypatch):
    for n in (5, 330, 1500, 10000):
        monkeypatch.setattr(ai_utils, "_trial_client_count", lambda db_path=None, n=n: n)
        day, month = ai_utils.trial_pool_budgets()
        assert day == max(ai_utils.AI_TRIAL_POOL_DAILY_USD, n * ai_utils.AI_TRIAL_POOL_PER_TRIAL_DAILY_USD)
        assert month == max(ai_utils.AI_TRIAL_POOL_MONTHLY_USD, n * ai_utils.AI_TRIAL_POOL_PER_TRIAL_MONTHLY_USD)
    # Setting the retired variables changes nothing.
    monkeypatch.setenv("AI_TRIAL_POOL_MAX_MONTHLY_USD", "5000")
    import importlib
    src = open(ai_utils.__file__, encoding="utf-8").read()
    assert 'getenv("AI_TRIAL_POOL_MAX' not in src
    assert importlib.util.find_spec("ai_utils") is not None


def test_a_1500_trial_rollout_at_normal_use_is_never_refused(monkeypatch):
    """The audit's scenario: 1,500 trial stores at ~$15 a month."""
    monkeypatch.setattr(ai_utils, "_trial_client_count", lambda db_path=None: 1500)
    _day, month = ai_utils.trial_pool_budgets()
    assert month > 1500 * 15


def test_a_bulk_status_that_is_not_paying_is_budgeted_as_a_trial(monkeypatch):
    monkeypatch.setenv("BULK_PROVISION_BILLING_STATUS", "partner")
    assert ai_utils._bulk_trial_states() == frozenset({"partner"})
    monkeypatch.setenv("BULK_PROVISION_BILLING_STATUS", "active")
    assert ai_utils._bulk_trial_states() == frozenset(), "a paying status is budgeted as paid"
    monkeypatch.setenv("BULK_PROVISION_BILLING_STATUS", "trial")
    assert ai_utils._bulk_trial_states() == frozenset({"trial"})
    monkeypatch.setenv("BULK_PROVISION_BILLING_STATUS", "x'); DROP TABLE r;--")
    assert ai_utils._bulk_trial_states() == frozenset(), "only a plain status word is ever put in SQL"
    # With the constants as an import would set them, a partner store is a trial.
    monkeypatch.setattr(ai_utils, "_TRIAL_BILLING_STATES", frozenset({"trial", "partner"}))
    monkeypatch.setattr(ai_utils, "_TRIAL_STATES_SQL", "('partner','trial')")
    _rest(41, status="partner")
    assert ai_utils._read_budget_tier(41) == ai_utils.TIER_TRIAL
    assert ai_utils._trial_client_count() >= 1


# ── R4-02: the emergency stop and the fleet brake ──────────────────────────

def test_the_emergency_stop_is_read_at_call_time_from_the_environment(monkeypatch):
    _rest(4)
    _call(4, "review_reply")
    monkeypatch.setenv("AI_EMERGENCY_STOP", "1")
    with pytest.raises(AIBudgetExceeded, match="everyone"):
        _call(4, "review_reply")
    monkeypatch.setenv("AI_EMERGENCY_STOP", "0")
    _call(4, "review_reply")


def test_the_console_arms_and_clears_the_stop_for_every_process_without_a_deploy():
    _rest(4)
    ai_utils.arm_emergency_stop(actor="will", reason="test")
    assert ai_utils.emergency_stop_armed()
    # Another process reads the shared row (this one's cache forgotten).
    ai_utils._control_cache.clear()
    with pytest.raises(AIBudgetExceeded, match="everyone"):
        _call(4, "review_reply")
    assert ai_utils.ai_health()["emergency_stop"] is True
    ai_utils.clear_emergency_stop(actor="will")
    ai_utils._control_cache.clear()
    assert not ai_utils.emergency_stop_armed()
    _call(4, "review_reply")


def test_a_fleet_wide_loop_arms_the_brake_on_unattended_work_only(monkeypatch):
    monkeypatch.setattr(ai_utils, "AI_FLEET_BRAKE_CONFIRM_SECONDS", 0.0)
    _rest(5)
    _rest(6)
    for _ in range(6):
        _spend(6, "review_reply", 15.0)                  # $90 this hour, no baseline: past the $50 line
    first = ai_utils.check_fleet_rate()
    assert first["over"] is True and first["brake"] is None, "one reading is a sighting, not a brake"
    ai_utils._rate_cache.clear()
    second = ai_utils.check_fleet_rate()
    brake = second["brake"]
    assert brake and brake["actions"] == ["review_reply"] and 6 in brake["restaurants"]
    # Unattended calls of the workflow, or of the restaurant, are refused...
    assert ai_utils.runaway_refusal(5, "review_reply", trigger="scheduler") == "fleet brake"
    assert ai_utils.runaway_refusal(6, "ask_cavnar", trigger="system") == "fleet brake"
    # ...an owner's and an admin's never, nor another workflow of another restaurant.
    assert ai_utils.runaway_refusal(5, "review_reply", trigger="owner") is None
    assert ai_utils.runaway_refusal(5, "review_reply", trigger="admin") is None
    assert ai_utils.runaway_refusal(5, "ask_cavnar", trigger="scheduler") is None
    with ai_utils.ai_context(trigger="scheduler"):
        with pytest.raises(AIBudgetExceeded, match="background work"):
            _call(5, "review_reply")
    assert ai_utils.ai_health()["fleet_brake"]["actions"] == ["review_reply"]
    # Released from the console: off now, and not re-armed by itself.
    ai_utils.release_fleet_brake(actor="will")
    ai_utils._rate_cache.clear()
    assert ai_utils.check_fleet_rate()["brake"] is None
    ai_utils._rate_cache.clear()
    assert ai_utils.check_fleet_rate()["brake"] is None
    assert ai_utils.runaway_refusal(5, "review_reply", trigger="scheduler") is None


def test_with_nothing_singled_out_the_brake_covers_every_unattended_call(monkeypatch):
    monkeypatch.setattr(ai_utils, "AI_FLEET_BRAKE_CONFIRM_SECONDS", 0.0)
    for rid in range(10, 22):
        _rest(rid)
        _spend(rid, f"wf_{rid}", 4.5)                    # spread thin: no workflow or restaurant past $5
    ai_utils.check_fleet_rate()
    ai_utils._rate_cache.clear()
    brake = ai_utils.check_fleet_rate()["brake"]
    assert brake and brake["all"] is True
    assert ai_utils.runaway_refusal(None, None, trigger="scheduler") == "fleet brake"


def test_the_fleet_line_grows_with_the_expected_spend_of_the_clients(monkeypatch):
    monkeypatch.setattr(ai_utils, "_paying_client_count", lambda db_path=None: 1500)
    monkeypatch.setattr(ai_utils, "_trial_client_count", lambda db_path=None: 0)
    out = ai_utils.check_fleet_rate()
    expected = 1500 * ai_utils.AI_FLEET_EXPECTED_PER_CLIENT_DAILY_USD / 24
    assert out["line"] == pytest.approx(round(max(ai_utils.AI_RUNAWAY_FLEET_MIN_USD,
                                                  ai_utils.AI_RUNAWAY_FACTOR * expected), 2))


def test_the_brake_can_be_switched_off_and_still_pages(monkeypatch):
    monkeypatch.setattr(ai_utils, "AI_FLEET_BRAKE", False)
    monkeypatch.setattr(ai_utils, "AI_FLEET_BRAKE_CONFIRM_SECONDS", 0.0)
    _rest(6)
    _spend(6, "review_reply", 90.0)
    ai_utils.check_fleet_rate()
    ai_utils._rate_cache.clear()
    assert ai_utils.check_fleet_rate()["brake"] is None


# ── R4-04: breakers per model family, and per tenant ───────────────────────

def test_an_opus_overload_never_pauses_sonnet():
    _rest(1)
    for _ in range(ai_utils.CB_FAILURE_THRESHOLD):
        with pytest.raises(anthropic.InternalServerError):
            _call(1, "invoice_extract", model="claude-opus-5", client=_Client(_server_error()), retries=0)
    assert ai_utils.breaker_open("anthropic", "claude-opus-5")
    with pytest.raises(ai_utils.AIProviderDown):
        _call(1, "invoice_extract", model="claude-opus-5")
    # Sonnet and Haiku still answer; the provider's own breaker is closed.
    assert ai_utils.breaker_state("anthropic")[0] == "closed"
    assert len(_call(1, "ask_cavnar", model="claude-sonnet-5").calls) == 1
    assert ai_utils.breakers()["anthropic/claude-opus-5"]["state"] == "open"
    assert ai_utils.ai_health()["vendors"]["anthropic"]["breaker_families"]["claude-opus-5"] == "open"
    ai_utils.reset_breaker("anthropic")
    assert not ai_utils.breaker_open("anthropic", "claude-opus-5")


def test_one_tenants_timeouts_pause_that_tenant_alone():
    _rest(1)
    _rest(2)
    for _ in range(ai_utils.CB_TENANT_THRESHOLD):
        with pytest.raises(anthropic.APITimeoutError):
            _call(1, "invoice_extract", model="m", client=_Client(_timeout()), retries=0)
    with pytest.raises(ai_utils.AIProviderDown, match="this restaurant"):
        _call(1, "invoice_extract", model="m")
    # Another restaurant on the same model is not paused, and one tenant's
    # timeouts never counted against the family.
    assert len(_call(2, "invoice_extract", model="m").calls) == 1
    assert ai_utils.breakers().get("anthropic/m", {}).get("failures", 0) == 0
    # A second restaurant timing out makes it the model's problem.
    with pytest.raises(anthropic.APITimeoutError):
        _call(2, "invoice_extract", model="m", client=_Client(_timeout()), retries=0)
    assert ai_utils.breakers()["anthropic/m"]["failures"] == 1


def test_a_family_opening_is_shared_with_every_process():
    _rest(1)
    for _ in range(ai_utils.CB_FAILURE_THRESHOLD):
        with pytest.raises(anthropic.InternalServerError):
            _call(1, "x", model="claude-opus-5", client=_Client(_server_error()), retries=0)
    import model_gateway
    row = model_gateway.breaker_row("anthropic/claude-opus-5")
    assert row and float(row["open_until"]) > time.time()
    # Another process (this one's memory forgotten) adopts it.
    with ai_utils._breaker_lock:
        ai_utils._breakers.clear()
    ai_utils._breaker_synced.clear()
    assert ai_utils.breaker_open("anthropic", "claude-opus-5")


# ── R4-05: the gateway off with a large fleet ──────────────────────────────

def test_the_gateway_off_with_a_large_fleet_pages_once_a_day(monkeypatch):
    import model_gateway
    paged = []
    monkeypatch.setattr(ai_utils, "_page", lambda key, *a, **k: paged.append(key) or True)
    monkeypatch.setattr(ai_utils, "_paying_client_count", lambda db_path=None: 20)
    monkeypatch.setattr(ai_utils, "_trial_client_count", lambda db_path=None: 0)
    assert ai_utils._gateway_off_warning() is False, "a small fleet is not paged"
    monkeypatch.setattr(ai_utils, "_paying_client_count", lambda db_path=None: 500)
    assert ai_utils._gateway_off_warning() is True and paged == ["gateway_off"]
    assert ai_utils._gateway_off_warning() is False, "once a day"
    monkeypatch.setattr(model_gateway, "enabled", lambda: True)
    assert ai_utils._gateway_off_warning() is False


# ── R4-10: run caps bind inside a single rung ──────────────────────────────

def test_a_single_rung_run_is_stopped_past_twice_its_caps(monkeypatch):
    import ai_orchestrator as orch
    import ai_workflows as wf
    _rest(3)
    pol = wf.POLICIES["ask_cavnar"]
    limit = int(orch.IN_ATTEMPT_CAP_FACTOR * pol.caps.calls)
    sent = []

    def attempt(route, notes):
        for _ in range(limit + 5):
            try:
                _call(3, "ask_cavnar", model="claude-sonnet-5")
                sent.append(1)
            except ai_utils.AIRefused:
                return "stopped"
        return "ran away"
    rr = orch.generate("ask_cavnar", 3, attempt)
    assert rr.result == "stopped" and len(sent) == limit
    # Outside a run nothing is capped.
    assert orch.run_cap_refusal() is None


def test_a_reviewer_call_is_never_refused_by_the_run_cap():
    import ai_orchestrator as orch
    token = orch._RUN.set({"workflow": "x", "caps": NS(calls=1, usd=1.0), "calls": 50, "usd": 0.0,
                           "in_attempt": False})
    try:
        assert orch.run_cap_refusal() is None
    finally:
        orch._RUN.reset(token)


# ── R4-03: the owner AI pool ───────────────────────────────────────────────

def test_the_pool_takes_an_owners_press_first_then_the_least_served_restaurant():
    pool = ai_async._FairPool(1, queue_max=4, tenant_queue_max=2, per_tenant_running=0)
    gate, started, order = threading.Event(), threading.Event(), []
    assert pool.submit_fair("t1", ai_async.PRIORITY_PRESSED, lambda w: started.set() or gate.wait(5))
    assert started.wait(5)                            # the one worker is busy with t1's job
    assert pool.submit_fair("t1", ai_async.priority_of("insight_labor"), lambda w: order.append("t1 refresh"))
    assert pool.submit_fair("t1", ai_async.priority_of("invoice_scan"), lambda w: order.append("t1 invoice"))
    assert pool.submit_fair("t2", ai_async.priority_of("campaign_text"), lambda w: order.append("t2 draft"))
    assert not pool.submit_fair("t1", 0, lambda w: None), "one restaurant's share of the queue is bounded"
    assert pool.submit_fair("t3", 1, lambda w: None)
    assert not pool.submit_fair("t4", 0, lambda w: None), "the queue is bounded"
    gate.set()
    end = time.time() + 5
    while len(order) < 3 and time.time() < end:
        threading.Event().wait(0.01)
    assert order == ["t2 draft", "t1 invoice", "t1 refresh"]
    assert ai_async.priority_of("insight_reviews") == ai_async.PRIORITY_BACKGROUND


def _ai_rid():
    return models.create_restaurant(models.Restaurant(name="Jobs Co", owner_email="o@x.test"))


class _Held:
    def __init__(self):
        self.jobs = []

    def submit(self, fn, *a, **k):
        self.jobs.append((fn, a, k))


def test_a_full_pool_answers_busy_and_pays_nothing(monkeypatch):
    class _Full(ai_async._FairPool):
        def submit_fair(self, *a, **k):
            return False
    monkeypatch.setattr(ai_async, "_executor", lambda: _Full(1, 1, 1, 0))
    rid = _ai_rid()
    ran = []
    job, _ = ai_async.start("invoice_scan", rid, {"sha": "a"}, lambda: ran.append(1) or ({"ok": True}, 200),
                            by_user=7)
    payload, status = ai_async.result(job, rid, user={"id": 7})
    assert status == 503 and payload.get("busy") and ran == []


def test_a_queued_job_is_never_called_dead_while_it_waits_and_a_repress_joins_it(monkeypatch):
    held = _Held()
    monkeypatch.setattr(ai_async, "_executor", lambda: held)
    rid = _ai_rid()
    job, _ = ai_async.start("invoice_scan", rid, {"sha": "b"}, lambda: ({"ok": True}, 200), by_user=7)
    # Queued an hour (past JOB_MAX_MINUTES' 45): it carries its own deadline.
    conn = ops._async_conn()
    conn.execute("UPDATE async_jobs SET created_at=datetime('now','-60 minutes') WHERE job_id=?", (job,))
    conn.commit()
    conn.close()
    assert ai_async.result(job, rid, user={"id": 7})[0]["status"] == "pending"
    again, joined = ai_async.start("invoice_scan", rid, {"sha": "b"}, lambda: ({"ok": True}, 200), by_user=7)
    assert (again, joined) == (job, True) and len(held.jobs) == 1


def test_a_job_the_store_failed_is_never_run(monkeypatch):
    held = _Held()
    monkeypatch.setattr(ai_async, "_executor", lambda: held)
    rid = _ai_rid()
    ran = []
    job, _ = ai_async.start("recipe_scan", rid, {"sha": "c"}, lambda: ran.append(1) or ({"ok": True}, 200),
                            by_user=7)
    ops.finish_async_job(job, "error", {"ok": False, "error": "overdue"})
    fn, a, k = held.jobs[0]
    fn(*a, **k)
    assert ran == [], "the model call was never made"


def test_a_job_that_waited_past_the_bound_is_answered_busy_not_run(monkeypatch):
    held = _Held()
    monkeypatch.setattr(ai_async, "_executor", lambda: held)
    rid = _ai_rid()
    ran = []
    job, _ = ai_async.start("campaign_text", rid, {"p": 1}, lambda: ran.append(1) or ({"ok": True}, 200), by_user=7)
    fn, a, k = held.jobs[0]
    fn(*a, waited=ai_async.QUEUE_WAIT_SECONDS + 1)
    assert ran == [] and ai_async.result(job, rid, user={"id": 7})[1] == 503


def test_a_late_answer_is_kept_and_handed_to_the_same_request_again(monkeypatch):
    held = _Held()
    monkeypatch.setattr(ai_async, "_executor", lambda: held)
    rid = _ai_rid()
    calls = []

    def work():
        calls.append(1)
        # The poll called it dead while the model was writing.
        ops.finish_async_job(job_box[0], "error", {"ok": False, "error": "overdue"})
        return {"ok": True, "invoice": {"total": 12}}, 200
    job_box = []
    job, _ = ai_async.start("invoice_scan", rid, {"sha": "d"}, work, by_user=7)
    job_box.append(job)
    fn, a, k = held.jobs[0]
    fn(*a, **k)
    payload, status = ai_async.result(job, rid, user={"id": 7})
    assert status == 200 and payload["invoice"] == {"total": 12}
    again, joined = ai_async.start("invoice_scan", rid, {"sha": "d"}, work, by_user=7)
    assert (again, joined) == (job, True) and calls == [1], "never paid for twice"
    # A job's own failure is never replaced by a late answer.
    assert ai_async._keep_late("no-such-job", {"payload": {}, "http_status": 200}) is False


# ── R4-06 / R4-07: grouped and chunked batches ─────────────────────────────

WF = "g4_wf"
SEEN = []


def cb(item, message=None, error=None):
    SEEN.append({"item": item, "message": message, "error": error})


class _FakeBatches:
    def __init__(self):
        self.created, self.retrieved, self.ended, self.answers = [], [], set(), {}
        self.fail_on = set()

    def create(self, requests):
        n = len(self.created) + 1
        if n in self.fail_on:
            self.created.append(None)
            raise RuntimeError("upload failed")
        self.created.append([dict(r) for r in requests])
        return NS(id=f"msgbatch_{n}")

    def retrieve(self, batch_id):
        self.retrieved.append(batch_id)
        return NS(id=batch_id, processing_status="ended" if batch_id in self.ended else "in_progress",
                  ended_at=None, request_counts=NS(succeeded=1, errored=0, expired=0, canceled=0))

    def results(self, batch_id):
        return iter(self.answers.get(batch_id, []))

    def cancel(self, batch_id):
        pass


@pytest.fixture
def fake(monkeypatch):
    f = _FakeBatches()
    monkeypatch.setattr(ai_batches, "_client", lambda: NS(messages=NS(batches=f)))
    monkeypatch.setattr(scheduler, "scheduling_allowed", lambda: True)
    monkeypatch.setenv("AI_BATCHES_WORKFLOWS", WF)
    monkeypatch.delenv("AI_BATCHES_ENABLED", raising=False)
    sys.modules.setdefault("test_refix_g4_ai", sys.modules[__name__])
    SEEN.clear()
    return f


def _item(cid, rid):
    return {"custom_id": cid, "restaurant_id": rid, "action": "t_batch", "callback": "test_refix_g4_ai:cb",
            "request": {"model": "claude-sonnet-5", "max_tokens": 50,
                        "messages": [{"role": "user", "content": "hello " + cid}]}}


def _status(cid):
    return ai_batches.item(WF, cid)["status"]


def test_one_item_submits_go_out_as_one_batch_per_pass(fake):
    rid = _ai_rid()
    for cid in ("n1", "n2", "n3"):
        assert ai_batches.submit(WF, [_item(cid, rid)], group=True) == {cid: "submitted"}
    assert fake.created == [] and _status("n1") == ai_batches.DEFERRED and ai_batches.pending(WF, "n1")
    out = ai_batches.run_collector()
    assert out["flushed"] == 3 and len(fake.created) == 1 and len(fake.created[0]) == 3
    assert {ai_batches.item(WF, c)["batch_id"] for c in ("n1", "n2", "n3")} == {"msgbatch_1"}
    assert _status("n2") == ai_batches.SUBMITTED


def test_a_deferred_item_cancelled_before_the_flush_is_never_sent(fake):
    rid = _ai_rid()
    ai_batches.submit(WF, [_item("c1", rid)], group=True)
    ai_batches.submit(WF, [_item("c2", rid)], group=True)
    assert ai_batches.cancel(WF, "c1") is True
    ai_batches.flush_deferred()
    assert [r["custom_id"] for r in fake.created[0]] == ["c2"]


def test_a_flush_that_cannot_send_tells_each_callback(fake, monkeypatch):
    rid = _ai_rid()
    ai_batches.submit(WF, [_item("f1", rid)], group=True)
    monkeypatch.setenv("AI_BATCHES_ENABLED", "0")
    ai_batches.flush_deferred()
    end = time.time() + 5
    while not SEEN and time.time() < end:
        threading.Event().wait(0.01)
    assert _status("f1") == ai_batches.SUBMIT_FAILED
    assert isinstance(SEEN[0]["error"], ai_batches.BatchItemFailed) and SEEN[0]["error"].kind == "submit_failed"


def test_a_submission_is_chunked_and_a_failed_chunk_fails_alone(fake, monkeypatch):
    monkeypatch.setattr(ai_batches, "BATCH_MAX_ITEMS", 2)
    fake.fail_on = {2}
    rid = _ai_rid()
    out = ai_batches.submit(WF, [_item(f"k{i}", rid) for i in range(5)])
    assert [len(c) if c else None for c in fake.created] == [2, None, 1]
    assert [out[f"k{i}"] for i in range(5)] == ["submitted", "submitted", "submit_failed", "submit_failed",
                                                "submitted"]
    c = sqlite3.connect(models.DB_PATH)
    assert c.execute("SELECT COUNT(*) FROM ai_batch_jobs WHERE workflow=?", (WF,)).fetchone()[0] == 2
    c.close()


def test_a_chunk_is_cut_by_bytes_too(monkeypatch):
    monkeypatch.setattr(ai_batches, "BATCH_MAX_BYTES", 400)
    reqs = [{"custom_id": f"b{i}", "params": {"messages": [{"role": "user", "content": "x" * 150}]}}
            for i in range(4)]
    chunks = list(ai_batches._chunks(reqs))
    assert len(chunks) >= 2 and sum(len(c) for c in chunks) == 4


def test_the_collector_asks_every_open_batch_in_turn(fake, monkeypatch):
    monkeypatch.setattr(ai_batches, "COLLECT_MAX_BATCHES", 2)
    rid = _ai_rid()
    for i in range(3):
        ai_batches.submit(WF, [_item(f"r{i}", rid)])
    ai_batches.run_collector()
    ai_batches.run_collector()
    assert set(fake.retrieved) == {"msgbatch_1", "msgbatch_2", "msgbatch_3"}, "the newest is never starved"


# ── R4-09: the Monday plan as queue rows ───────────────────────────────────

def test_the_monday_plan_fans_out_as_queue_rows(monkeypatch):
    import strategy_jobs
    rid = _ai_rid()
    monkeypatch.setattr(strategy_jobs, "_weekly_plan_due", lambda r: ("2026-W43", 0) if r.id == rid else None)
    out = strategy_jobs.fan_out_weekly_plan(db_path=models.DB_PATH)
    assert out["queued"] == 1
    assert strategy_jobs.fan_out_weekly_plan(db_path=models.DB_PATH)["queued"] == 0, "the same week and attempt queue nothing new"
    c = sqlite3.connect(models.DB_PATH)
    (payload,) = [json.loads(r[0]) for r in c.execute("SELECT payload_json FROM jobs WHERE kind='learner'")]
    c.close()
    assert payload == {"learner": "weekly_plan", "restaurant_id": rid, "day": "2026-W43"}
    seen = []
    monkeypatch.setattr(strategy_jobs, "weekly_plan_one", lambda r, **k: seen.append(r) or {"ok": 1})
    strategy_jobs.run_queued_learner(NS(payload=payload))
    assert seen == [rid]
    import jobs_registry
    assert jobs_registry.JOBS["weekly_plan"]["target"] == ("strategy_jobs", "fan_out_weekly_plan")
