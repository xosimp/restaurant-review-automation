"""Platform re-audit, 10/7/26 — ai_utils, ai_batches, ai_async, the scheduler.

  #1  the batch collector never runs a callback (a synchronous fallback, an
      escalation) on the scheduler loop thread: callbacks run on its own
      daemon pool, a slow one never holds the pass, the time bound is per
      item and a full pool stops the pass claiming.
  #3  a background pool started from a request (the shadow reviews, the
      learner's scoring) is never "on a request": no interactive slot, no
      request leash — and the owner AI job pool never was.
  #4  the invoice and recipe-card reads name their own leash on a request
      thread, never the 40s default.
  #5  each login gets its own owner AI job; the same login still joins.
  #6  a workflow run on a request thread holds ONE slot for its attempts and
      its reviewer gate, and its calls keep the request leash.
  #7  a callback a crash cut short is told again, once, wherever its batch.
  #9  the platform Places ceiling grows per paying client, capped.
  #10 a cut stream's output tokens are estimated from what had streamed.
  #11 Run now of a lane job runs on its lane, not the loop thread.
  #13 the scheduler lease is released before the thread pools are joined.

Every thread a test starts is a daemon or is released before the test ends:
a callback that blocks waits on an Event the test sets in a finally.
"""
import os
import sqlite3
import subprocess
import sys
import threading
import time
import types
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta

import anthropic
import httpx
import pytest
from flask import Flask, has_request_context

import ai_async
import ai_batches
import ai_utils
import jobs_registry
import models
import ops
import scheduler
from models import Restaurant, create_restaurant

NS = types.SimpleNamespace
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WF = "plat_wf"
SEEN = []
GATE = {"event": None}
_APP = Flask("platform_reaudit_test")


@pytest.fixture(autouse=True)
def _db(db_path, monkeypatch):
    real = models.get_conn
    redirect = lambda *a, **k: real(db_path)  # noqa: E731
    for mod in list(sys.modules.values()):
        try:
            if getattr(mod, "get_conn", None) is real:
                monkeypatch.setattr(mod, "get_conn", redirect)
        except Exception:
            pass
    monkeypatch.setattr(models, "get_conn", redirect)
    monkeypatch.setattr(models, "DB_PATH", db_path)
    monkeypatch.setattr(ai_utils.time, "sleep", lambda s: None)
    monkeypatch.setattr(ai_utils, "_INTERACTIVE_SLOTS", threading.BoundedSemaphore(2))
    monkeypatch.setattr(ai_utils, "INTERACTIVE_AI_SLOTS", 2)
    monkeypatch.setattr(ai_utils, "INTERACTIVE_AI_WAIT_SECONDS", 0.05)
    sys.modules.setdefault("test_platform_reaudit_1007", sys.modules[__name__])
    SEEN.clear()
    GATE["event"] = None
    ai_utils.reset_process_state(db_path)
    yield
    if GATE["event"] is not None:
        GATE["event"].set()                    # never leave a callback blocked
    ai_utils.reset_process_state(db_path)


def cb(item, message=None, error=None):
    """The test callback (reached by name, "test_platform_reaudit_1007:cb").
    Blocks while GATE holds an unset Event — a synchronous model call."""
    SEEN.append({"id": item["custom_id"], "message": message, "error": error,
                 "thread": threading.current_thread().name})
    ev = GATE["event"]
    if ev is not None:
        ev.wait(10)


class FakeBatches:
    def __init__(self):
        self.created, self.cancelled, self.ended, self.answers = [], [], set(), {}

    def create(self, requests):
        self.created.append([dict(r) for r in requests])
        return NS(id=f"msgbatch_{len(self.created)}")

    def retrieve(self, batch_id):
        return NS(id=batch_id, processing_status="ended" if batch_id in self.ended else "in_progress",
                  ended_at=None, request_counts=NS(succeeded=1, errored=0, expired=0, canceled=0))

    def results(self, batch_id):
        return iter(self.answers.get(batch_id, []))

    def cancel(self, batch_id):
        self.cancelled.append(batch_id)


@pytest.fixture
def batches(monkeypatch):
    fake = FakeBatches()
    monkeypatch.setattr(ai_batches, "_client", lambda: NS(messages=NS(batches=fake)))
    monkeypatch.setattr(scheduler, "scheduling_allowed", lambda: True)
    monkeypatch.setenv("AI_BATCHES_WORKFLOWS", WF)
    monkeypatch.delenv("AI_BATCHES_ENABLED", raising=False)
    return fake


def _rid(db_path, **kw):
    return create_restaurant(Restaurant(name="Platform Co", owner_email="p@x.test", **kw), db_path=db_path)


def _item(cid, rid, **kw):
    return dict({"custom_id": cid, "restaurant_id": rid, "action": "t_plat", "callback": "test_platform_reaudit_1007:cb",
                 "request": {"model": "claude-sonnet-5", "max_tokens": 50,
                             "messages": [{"role": "user", "content": "hi"}]}}, **kw)


def _msg(text="ok"):
    return NS(id="msg_1", model="claude-sonnet-5", stop_reason="end_turn", content=[NS(type="text", text=text)],
              usage=NS(input_tokens=100, output_tokens=50, cache_creation_input_tokens=0, cache_read_input_tokens=0))


def _ok(cid):
    return NS(custom_id=cid, result=NS(type="succeeded", message=_msg()))


def _q(db_path, sql, args=()):
    c = sqlite3.connect(db_path)
    c.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in c.execute(sql, args)]
    finally:
        c.close()


def _x(db_path, sql, args=()):
    c = sqlite3.connect(db_path)
    try:
        c.execute(sql, args)
        c.commit()
    finally:
        c.close()


def _settle(keys, seconds=10):
    assert ai_batches._wait_for(keys, seconds) == [], "a callback never finished"


# ── #1: callbacks run off the loop thread ───────────────────────────────────

def test_a_slow_callback_runs_on_the_pool_and_never_holds_the_pass(db_path, batches, monkeypatch):
    monkeypatch.setattr(ai_batches, "CALLBACK_INLINE_WAIT_SECONDS", 0.2)
    rid = _rid(db_path)
    ai_batches.submit(WF, [_item("s1", rid)])
    batches.ended.add("msgbatch_1")
    batches.answers["msgbatch_1"] = [_ok("s1")]
    GATE["event"] = threading.Event()
    began = time.monotonic()
    out = ai_batches.run_collector()
    try:
        assert time.monotonic() - began < 5, "the pass waited on a callback's model call"
        assert out["ok"] == 1 and out["callbacks_running"] == 1
        assert ai_batches.item(WF, "s1")["status"] == ai_batches.COLLECTING
        (seen,) = SEEN
        assert seen["thread"].startswith("ai-batch-callback-")
        assert seen["thread"] != threading.current_thread().name
    finally:
        GATE["event"].set()
    _settle([(WF, "s1")])
    assert ai_batches.item(WF, "s1")["status"] == ai_batches.DONE


def test_a_quick_callback_finishes_inside_the_pass(db_path, batches):
    """What keeps the DSR timely: a judged answer is stored before the
    collector returns, so the DSR sweep after it sees it in the same tick."""
    rid = _rid(db_path)
    ai_batches.submit(WF, [_item("q1", rid)])
    batches.ended.add("msgbatch_1")
    batches.answers["msgbatch_1"] = [_ok("q1")]
    out = ai_batches.run_collector()
    assert "callbacks_running" not in out
    assert ai_batches.item(WF, "q1")["status"] == ai_batches.DONE and SEEN[0]["message"] is not None


def test_a_cutoff_fallback_runs_on_the_pool_too(db_path, batches, monkeypatch):
    monkeypatch.setattr(ai_batches, "CALLBACK_INLINE_WAIT_SECONDS", 0.2)
    rid = _rid(db_path)
    ai_batches.submit(WF, [_item("c1", rid, cutoff_at=datetime.utcnow() - timedelta(minutes=1))])
    GATE["event"] = threading.Event()
    try:
        out = ai_batches.run_collector()
        assert out["cut_off"] == 1 and out["callbacks_running"] == 1
        row = ai_batches.item(WF, "c1")
        assert row["status"] == ai_batches.CUT_OFF and row["claimed_at"], "its callback is owed"
        assert not ai_batches.pending(WF, "c1") and ai_batches.cancel(WF, "c1") is False
        assert SEEN[0]["error"].kind == "cutoff" and SEEN[0]["thread"].startswith("ai-batch-callback-")
    finally:
        GATE["event"].set()
    _settle([(WF, "c1")])
    assert ai_batches.item(WF, "c1")["status"] == ai_batches.CANCELLED


def test_an_answer_landing_while_its_cutoff_fallback_runs_is_ledgered_and_dropped(db_path, batches, monkeypatch):
    monkeypatch.setattr(ai_batches, "CALLBACK_INLINE_WAIT_SECONDS", 0.1)
    rid = _rid(db_path)
    ai_batches.submit(WF, [_item("l1", rid, cutoff_at=datetime.utcnow() - timedelta(minutes=1))])
    GATE["event"] = threading.Event()
    try:
        ai_batches.run_collector()
        batches.ended.add("msgbatch_1")
        batches.answers["msgbatch_1"] = [_ok("l1")]
        ai_batches.run_collector()                    # the answer lands while the fallback runs
        row = ai_batches.item(WF, "l1")
        assert row["status"] == ai_batches.CUT_OFF and row["result_type"] == "succeeded" and row["cost_usd"] > 0
    finally:
        GATE["event"].set()
    _settle([(WF, "l1")])
    assert ai_batches.item(WF, "l1")["status"] == ai_batches.DISCARDED
    assert [s["message"] for s in SEEN] == [None], "the late answer is never handed over"
    assert len(_q(db_path, "SELECT * FROM ai_usage WHERE action='t_plat'")) == 1


def test_the_time_bound_is_checked_per_item_and_the_rest_wait_for_the_next_pass(db_path, batches, monkeypatch):
    rid = _rid(db_path)
    ai_batches.submit(WF, [_item("t1", rid), _item("t2", rid)])
    batches.ended.add("msgbatch_1")

    def results(batch_id):
        yield _ok("t1")
        monkeypatch.setattr(ai_batches, "COLLECT_MAX_SECONDS", -1)   # the bound passes mid-batch
        yield _ok("t2")
    monkeypatch.setattr(batches, "results", results)
    out = ai_batches.run_collector()
    assert out["hit_bound"] and out["items"] == 1
    assert ai_batches.item(WF, "t2")["status"] == ai_batches.SUBMITTED
    assert _q(db_path, "SELECT status FROM ai_batch_jobs")[0]["status"] == "in_progress"
    monkeypatch.setattr(ai_batches, "COLLECT_MAX_SECONDS", 120)
    monkeypatch.setattr(batches, "results", lambda b: iter([_ok("t1"), _ok("t2")]))
    out = ai_batches.run_collector()
    assert out["items"] == 1 and not out["hit_bound"]
    assert {s["id"] for s in SEEN} == {"t1", "t2"} and len(SEEN) == 2, "t1 is never handed over twice"
    assert _q(db_path, "SELECT status FROM ai_batch_jobs")[0]["status"] == "collected"


def test_a_full_pool_stops_the_pass_claiming(db_path, batches, monkeypatch):
    monkeypatch.setattr(ai_batches, "CALLBACK_MAX_PENDING", 1)
    monkeypatch.setattr(ai_batches, "CALLBACK_INLINE_WAIT_SECONDS", 0.1)
    rid = _rid(db_path)
    ai_batches.submit(WF, [_item("f1", rid), _item("f2", rid)])
    batches.ended.add("msgbatch_1")
    batches.answers["msgbatch_1"] = [_ok("f1"), _ok("f2")]
    GATE["event"] = threading.Event()
    try:
        out = ai_batches.run_collector()
        assert out["hit_bound"]
        assert ai_batches.item(WF, "f2")["status"] == ai_batches.SUBMITTED, "left for the next pass"
        assert _q(db_path, "SELECT status FROM ai_batch_jobs")[0]["status"] == "in_progress"
    finally:
        GATE["event"].set()
    _settle([(WF, "f1")])
    GATE["event"] = None
    ai_batches.run_collector()
    assert ai_batches.item(WF, "f2")["status"] == ai_batches.DONE


def test_no_collector_function_runs_a_callback_itself():
    """Where it must hold everywhere: only the pool's _run_callback (and
    submit's gate refusals, on the submitter's thread) call _invoke."""
    import inspect
    for fn in (ai_batches.run_collector, ai_batches._collect_batch, ai_batches._collect_one,
               ai_batches._sweep_cutoffs, ai_batches._sweep_stale):
        assert "_invoke(" not in inspect.getsource(fn), fn.__name__
    assert "_invoke(" in inspect.getsource(ai_batches._run_callback)
    assert all(t.daemon for t in ai_batches._cb_threads)


# ── #7: a callback cut short is told again, once ────────────────────────────

def _stale(db_path, cid, status=ai_batches.COLLECTING, reason=None, minutes=30):
    claimed = (datetime.utcnow() - timedelta(minutes=minutes)).strftime("%Y-%m-%d %H:%M:%S")
    _x(db_path, "UPDATE ai_batch_items SET status=?, claimed_at=?, reason=? WHERE custom_id=?",
       (status, claimed, reason, cid))


def test_a_collecting_item_left_by_a_dead_pass_is_told_stale_once(db_path, batches):
    rid = _rid(db_path)
    ai_batches.submit(WF, [_item("d1", rid), _item("d2", rid)])
    # Its batch was collected long ago: the old check never looked again.
    _x(db_path, "UPDATE ai_batch_jobs SET status='collected'")
    _stale(db_path, "d1")
    _stale(db_path, "d2", minutes=5)                 # claimed recently: still its pass's
    out = ai_batches.run_collector()
    assert out["stale"] == 1
    (seen,) = SEEN
    assert seen["id"] == "d1" and isinstance(seen["error"], ai_batches.BatchItemFailed)
    assert seen["error"].kind == "stale"
    assert ai_batches.item(WF, "d1")["status"] == ai_batches.FAILED
    assert ai_batches.item(WF, "d2")["status"] == ai_batches.COLLECTING
    # A second dead callback: closed, never a third call.
    _stale(db_path, "d1", reason=ai_batches._STALE_REASON)
    SEEN.clear()
    out = ai_batches.run_collector()
    assert SEEN == [] and out["stale_closed"] == 1
    row = ai_batches.item(WF, "d1")
    assert row["status"] == ai_batches.FAILED and "twice" in row["callback_error"]


def test_a_cut_off_item_whose_fallback_died_is_told_cutoff_again(db_path, batches):
    rid = _rid(db_path)
    ai_batches.submit(WF, [_item("k1", rid)])
    _stale(db_path, "k1", status=ai_batches.CUT_OFF)
    ai_batches.run_collector()
    assert [s["error"].kind for s in SEEN] == ["cutoff"]
    assert ai_batches.item(WF, "k1")["status"] == ai_batches.CANCELLED


def test_a_callback_still_running_here_is_not_stale(db_path, batches, monkeypatch):
    monkeypatch.setattr(ai_batches, "CALLBACK_INLINE_WAIT_SECONDS", 0.1)
    rid = _rid(db_path)
    ai_batches.submit(WF, [_item("r1", rid)])
    batches.ended.add("msgbatch_1")
    batches.answers["msgbatch_1"] = [_ok("r1")]
    GATE["event"] = threading.Event()
    try:
        ai_batches.run_collector()
        _stale(db_path, "r1")                        # a long model call, past the stale age
        out = ai_batches.run_collector()
        assert "stale" not in out and len(SEEN) == 1
    finally:
        GATE["event"].set()
    _settle([(WF, "r1")])
    assert ai_batches.item(WF, "r1")["status"] == ai_batches.DONE


# ── #3: a background pool is never "on a request" ───────────────────────────

def _probe():
    return {"request": has_request_context(), "on_request": ai_utils.on_request_thread(),
            "ctx": ai_utils.current_ai_context()}


def test_background_runner_drops_the_request_and_keeps_who_asked():
    pool = ThreadPoolExecutor(max_workers=1)
    try:
        with _APP.test_request_context("/api/insight"):
            with ai_utils.ai_context(trigger="owner", actor_user_id=5, correlation_id="corr:1"):
                copied = pool.submit(ai_utils.context_runner(_probe)).result(5)
                background = pool.submit(ai_utils.background_runner(_probe)).result(5)
    finally:
        pool.shutdown(wait=True)
    assert copied["request"] and copied["on_request"], "the hazard: a whole-context copy is 'on a request'"
    assert not background["request"] and not background["on_request"]
    assert background["ctx"]["trigger"] == "owner" and background["ctx"]["actor_user_id"] == 5
    assert background["ctx"]["correlation_id"] == "corr:1"


class _Usage:
    input_tokens, output_tokens = 10, 5
    cache_creation_input_tokens = cache_read_input_tokens = 0


def _reply():
    return NS(usage=_Usage(), stop_reason="end_turn", _request_id="req", content=[NS(type="text", text="ok")])


class _Client:
    """A test double with a real timeout, like get_client()'s."""

    def __init__(self, replies=None, timeout=ai_utils.DEFAULT_AI_TIMEOUT):
        self.replies = list(replies or [])
        self.timeout = anthropic.Timeout(timeout, connect=5.0)
        self.messages = self
        self.seen = []

    def create(self, **kw):
        self.seen.append(kw)
        r = self.replies.pop(0) if self.replies else _reply()
        if isinstance(r, Exception):
            raise r
        return r


def _conn_error():
    return anthropic.APIConnectionError(request=httpx.Request("POST", "https://api.example/v1/messages"))


def _call(client, **kw):
    return ai_utils.create_with_retry(client, model="claude-sonnet-5", max_tokens=10,
                                      readiness={"decision": "proceed"}, **kw)


def test_a_shadow_review_queued_from_a_request_takes_no_slot(db_path, monkeypatch):
    import ai_orchestrator
    monkeypatch.setenv("AI_SHADOW_REVIEW", "1")
    client = _Client()
    seen = []

    def review(result, mode):
        seen.append(_probe())
        _call(client, restaurant_id=None, action="shadow_test")     # every slot is held: still answered
        return ai_orchestrator.Verdict.passed()
    assert ai_utils.acquire_interactive_slot() and ai_utils.acquire_interactive_slot()
    try:
        with _APP.test_request_context("/api/drafts"):
            ai_orchestrator._shadow_review("run:x:1", "text", review, db_path)
        ai_orchestrator._SHADOW_POOL.submit(lambda: None).result(10)    # one worker: the review ran first
    finally:
        ai_utils.release_interactive_slot()
        ai_utils.release_interactive_slot()
    assert seen and not seen[0]["request"] and not seen[0]["on_request"]
    assert len(client.seen) == 1 and "timeout" not in client.seen[0], "no request leash in the background"
    assert not _q(db_path, "SELECT * FROM ai_usage WHERE reason='busy'")


def test_the_learner_scoring_pool_is_never_on_a_request():
    import ai_learning
    seen = []
    with _APP.test_request_context("/admin/learning"):
        ai_learning._score_async(lambda: seen.append(_probe()))
    ai_learning._SCORE_POOL.submit(lambda: None).result(10)
    assert seen and not seen[0]["request"] and not seen[0]["on_request"]


def test_the_owner_ai_job_pool_never_inherits_the_request(db_path):
    seen = []

    def work():
        seen.append(_probe())
        return {"ok": True}, 200
    rid = _rid(db_path)
    with _APP.test_request_context("/api/invoices/scan"):
        job_id, joined = ai_async.start("invoice_scan", rid, {"sha": "bg"}, work, by_user=3)
    deadline = time.time() + 10
    while ai_async.result(job_id, rid, {"id": 3})[0].get("status") != "done" and time.time() < deadline:
        time.sleep(0.01)
    assert seen and not seen[0]["request"] and not seen[0]["on_request"]


# ── #6: one slot for a whole workflow run ───────────────────────────────────

def test_a_run_on_a_request_thread_holds_one_slot_for_its_reviewer(db_path, monkeypatch):
    """The reviewer gate is the last call of a run: under pressure it was the
    one refused, and a refused gate passes the text unreviewed."""
    import ai_orchestrator
    monkeypatch.setattr(ai_orchestrator, "REPLAY_SAMPLE_RATE", 0.0)
    rid = _rid(db_path)
    client = _Client()
    taken = []
    verdicts = []

    def attempt(route, notes):
        msg = _call(client, restaurant_id=rid, action="guest_campaign_draft")
        # Another request takes every slot left before the reviewer runs.
        while ai_utils.acquire_interactive_slot(0):
            taken.append(1)
        return ai_utils.extract_text(msg)

    def review(result, mode):
        _call(client, restaurant_id=rid, action="ai_review")
        verdicts.append(mode)
        return ai_orchestrator.Verdict(ok=True, score=0.9, label="pass")
    try:
        with _APP.test_request_context("/api/campaigns/draft"):
            rr = ai_orchestrator.generate("guest_campaign_draft", rid, attempt, review=review, db_path=db_path)
    finally:
        for _ in taken:
            ai_utils.release_interactive_slot()
    assert rr.ok and verdicts == ["haiku_gate"], "the gate ran inside the run's own slot"
    assert taken == [1], "the run held one of the two slots throughout"
    assert ai_utils.interactive_slots_free() == 2, "and gave it back"
    assert not _q(db_path, "SELECT * FROM ai_usage WHERE reason='busy'")
    for kw in client.seen:                                          # the request leash still applies
        assert kw["timeout"].read == ai_utils.INTERACTIVE_AI_TIMEOUT


def test_a_run_that_finds_no_slot_refuses_as_before(db_path):
    import ai_orchestrator
    rid = _rid(db_path)
    client = _Client()
    assert ai_utils.acquire_interactive_slot() and ai_utils.acquire_interactive_slot()
    try:
        with _APP.test_request_context("/api/campaigns/draft"):
            with pytest.raises(ai_utils.AIBusy):
                ai_orchestrator.generate("guest_campaign_draft", rid, lambda route, notes: _call(
                    client, restaurant_id=rid, action="guest_campaign_draft"), db_path=db_path)
    finally:
        ai_utils.release_interactive_slot()
        ai_utils.release_interactive_slot()
    assert client.seen == []
    (row,) = _q(db_path, "SELECT * FROM ai_usage WHERE reason='busy'")
    assert row["action"] == "guest_campaign_draft"


def test_an_ask_turn_still_keeps_its_own_timeouts(db_path):
    client = _Client()
    with _APP.test_request_context("/api/ask-cavnar"):
        with ai_utils.interactive_slot():
            _call(client, restaurant_id=None, action="ask")
    assert "timeout" not in client.seen[0]


# ── #4: the slow reads name their own leash ─────────────────────────────────

_JPEG = b"\xff\xd8\xff\xe0" + b"0" * 64


def test_the_invoice_read_keeps_its_own_timeout_and_one_retry_on_a_request_thread(db_path):
    import invoices
    rid = _rid(db_path, module_inventory=1)
    client = _Client(replies=[_conn_error(), _conn_error(), _conn_error()])
    with _APP.test_request_context("/api/invoices/scan"):
        with pytest.raises(Exception):
            invoices.extract(rid, _JPEG, "image/jpeg", client=client)
    assert len(client.seen) == 2, "one retry, as the read names"
    assert all(kw["timeout"] == invoices.EXTRACT_TIMEOUT_SECONDS for kw in client.seen)
    assert invoices.EXTRACT_TIMEOUT_SECONDS > ai_utils.INTERACTIVE_AI_TIMEOUT


def test_the_recipe_card_read_keeps_its_own_timeout_on_a_request_thread(db_path):
    import recipes
    rid = _rid(db_path, module_inventory=1)
    client = _Client(replies=[_conn_error(), _conn_error(), _conn_error()])
    with _APP.test_request_context("/api/recipes/photo"):
        with pytest.raises(Exception):
            recipes.extract_from_image(rid, _JPEG, "image/jpeg", client=client, db_path=db_path)
    assert len(client.seen) == 2
    assert all(kw["timeout"] == recipes.PHOTO_TIMEOUT_SECONDS for kw in client.seen)
    assert recipes.PHOTO_TIMEOUT_SECONDS > ai_utils.INTERACTIVE_AI_TIMEOUT


# ── #5: a job per login ─────────────────────────────────────────────────────

class _Held:
    def __init__(self):
        self.jobs = []

    def submit(self, fn, *a, **k):
        self.jobs.append((fn, a, k))


def test_two_logins_get_their_own_jobs_and_one_login_still_joins(db_path, monkeypatch):
    monkeypatch.setattr(ai_async, "_executor", lambda: _Held())
    rid = _rid(db_path)
    work = lambda: ({"ok": True}, 200)  # noqa: E731
    a, joined_a = ai_async.start("invoice_scan", rid, {"sha": "same"}, work, by_user=7)
    b, joined_b = ai_async.start("invoice_scan", rid, {"sha": "same"}, work, by_user=8)
    c, joined_c = ai_async.start("invoice_scan", rid, {"sha": "same"}, work, by_user=7)
    assert a != b and not joined_a and not joined_b, "another login never joins a job it cannot read"
    assert c == a and joined_c
    assert ai_async.job_kind("invoice_scan", {"sha": "same"}, 7) != ai_async.job_kind("invoice_scan", {"sha": "same"}, 8)


# ── #9: the Places ceiling grows with paying clients ────────────────────────

def test_the_platform_places_ceiling_grows_per_paying_client_and_is_capped(db_path, monkeypatch):
    monkeypatch.setattr(ai_utils, "AI_PLACES_GLOBAL_MONTHLY_USD", 300.0)
    monkeypatch.setattr(ai_utils, "AI_PLACES_GLOBAL_PER_CLIENT_USD", 15.0)
    monkeypatch.setattr(ai_utils, "AI_PLACES_GLOBAL_MAX_MONTHLY_USD", 3000.0)
    monkeypatch.setattr(ai_utils, "_paying_client_count", lambda db_path=None: 10)
    assert ai_utils.places_global_monthly_budget() == 300.0, "the floor"
    monkeypatch.setattr(ai_utils, "_paying_client_count", lambda db_path=None: 60)
    assert ai_utils.places_global_monthly_budget() == 900.0
    assert ai_utils.places_budget_status()["global_month"]["budget"] == 900.0
    monkeypatch.setattr(ai_utils, "_paying_client_count", lambda db_path=None: 1000)
    assert ai_utils.places_global_monthly_budget() == 3000.0, "never past the cap"
    monkeypatch.setattr(ai_utils, "AI_PLACES_GLOBAL_MONTHLY_USD", 0.0)
    assert ai_utils.places_global_monthly_budget() == 0.0, "0 still disables it"


def test_the_places_ceiling_counts_real_paying_restaurants(db_path):
    ai_utils.invalidate_budget_memo()
    for i in range(25):
        create_restaurant(Restaurant(name=f"Paying {i}", owner_email=f"p{i}@x.test", billing_status="active"),
                          db_path=db_path)
    ai_utils.invalidate_budget_memo()
    n = ai_utils._paying_client_count()
    assert n >= 25
    assert ai_utils.places_global_monthly_budget() == min(max(ai_utils.AI_PLACES_GLOBAL_MONTHLY_USD,
                                                              n * ai_utils.AI_PLACES_GLOBAL_PER_CLIENT_USD),
                                                          ai_utils.AI_PLACES_GLOBAL_MAX_MONTHLY_USD)


# ── #10: a cut stream's output is estimated ─────────────────────────────────

def test_a_cut_streams_output_is_estimated_from_what_had_streamed(db_path):
    usage = NS(input_tokens=12000, output_tokens=1, cache_creation_input_tokens=0, cache_read_input_tokens=0)
    partial = NS(usage=usage, id="msg_cut", content=[NS(type="thinking", thinking="t" * 2000),
                                                     NS(type="text", text="x" * 2000)])
    ai_utils._log_stream_cut_safe(partial, "claude-sonnet-5", None, "cut_est", 10, 1, "call_est", {})
    (row,) = _q(db_path, "SELECT * FROM ai_usage WHERE action='cut_est'")
    assert row["output_tokens"] == 1000 and row["reason"] == ai_utils.STREAM_CUT_ESTIMATED_REASON
    assert ai_utils.mark_outcome("call_est", "unparseable", db_path=db_path) is False, \
        "an attempt's cut row keeps its own verdict"
    # Nothing streamed beyond message_start: the SDK's count stands, unmarked.
    bare = NS(usage=usage, id="msg_bare", content=[])
    ai_utils._log_stream_cut_safe(bare, "claude-sonnet-5", None, "cut_bare", 10, 1, "call_bare", {})
    (row,) = _q(db_path, "SELECT * FROM ai_usage WHERE action='cut_bare'")
    assert row["output_tokens"] == 1 and row["reason"] == ai_utils.STREAM_CUT_REASON


# ── #11: Run now of a lane job runs on its lane ─────────────────────────────

def _lane_job():
    for name, spec in jobs_registry.JOBS.items():
        if spec.get("lane") == "ai" and spec.get("runnable") and spec.get("target") and not spec.get("sends"):
            return name, spec
    pytest.skip("no runnable AI-lane job")


def test_run_now_of_a_lane_job_runs_on_its_lane_not_the_loop(db_path, monkeypatch):
    name, spec = _lane_job()
    mod, fn_name = spec["target"]
    ran = []

    def stub(**kw):
        ran.append({"thread": threading.current_thread().name, "ctx": ai_utils.current_ai_context()})
        return {"attempted": 1, "ok": 1, "failed": 0, "skipped": 0, "hit_bound": False}
    monkeypatch.setattr(sys.modules[mod] if mod in sys.modules else __import__(mod), fn_name, stub)
    monkeypatch.setattr(ops, "acquire_scheduler_lease", lambda *a, **k: True)
    lane = scheduler._Lane("ai")
    monkeypatch.setitem(scheduler._LANES, "ai", lane)
    req_id, _ = ops.request_job_run(name, "will")
    assert scheduler._run_manual_requests(scheduler._PulsedOps()) == 1
    lane.join(10)
    assert not lane.busy()
    (r,) = ran
    assert r["thread"] == "scheduler-lane-ai" and r["thread"] != threading.current_thread().name
    assert r["ctx"]["trigger"] == "admin" and r["ctx"]["correlation_id"] == f"run_now:{name}:{req_id}"
    (row,) = _q(db_path, "SELECT * FROM job_run_requests WHERE id=?", (req_id,))
    assert row["status"] == "done" and row["ok"] == 1
    (run,) = _q(db_path, "SELECT * FROM job_runs WHERE job=?", (name,))
    assert run["request_id"] == req_id


def test_run_now_of_a_lane_job_waits_when_its_lane_is_busy(db_path, monkeypatch):
    name, _spec = _lane_job()
    lane = scheduler._Lane("ai")
    monkeypatch.setattr(lane, "submit", lambda *a, **k: False)
    monkeypatch.setitem(scheduler._LANES, "ai", lane)
    req_id, _ = ops.request_job_run(name, "will")
    assert scheduler._run_manual_requests(scheduler._PulsedOps()) == 0
    (row,) = _q(db_path, "SELECT * FROM job_run_requests WHERE id=?", (req_id,))
    assert row["status"] == "pending" and row["taken_at"] is None, "handed back for a later tick"


# ── #13: the lease goes before the pools are joined ─────────────────────────

def test_register_shutdown_runs_before_the_thread_pool_joins():
    import atexit
    import concurrent.futures.thread as cft
    calls = []

    def release():
        calls.append(1)
    before = list(threading._threading_atexits)
    try:
        scheduler.register_shutdown(release)
        hooks = threading._threading_atexits
        mine = [i for i, h in enumerate(hooks) if getattr(h, "func", None) is release]
        pool_join = [i for i, h in enumerate(hooks) if getattr(h, "func", None) is cft._python_exit]
        assert mine and pool_join and mine[0] > pool_join[0], "hooks run newest first: ours before the joins"
    finally:
        threading._threading_atexits[:] = before
        atexit.unregister(release)
    import inspect
    assert "register_shutdown()" in inspect.getsource(scheduler.start_scheduler)
    src = open(os.path.join(ROOT, "hosted_dashboard.py"), encoding="utf-8").read()
    body = src[src.index("def _restart_scheduler_loop"):]
    assert "register_shutdown()" in body[:body.index("\ndef ", 1)]


_EXIT_SCRIPT = r"""
import os, sys, time, atexit, threading
sys.path.insert(0, os.environ["CAVNAR_ROOT"])
import dotenv
dotenv.load_dotenv = lambda *a, **k: False
import concurrent.futures
import scheduler
order = []
pool = concurrent.futures.ThreadPoolExecutor(max_workers=1)
started = threading.Event()
def job():
    started.set()
    time.sleep(1.0)
    order.append("pool job joined")
pool.submit(job)
started.wait(5)
scheduler.register_shutdown(lambda: order.append("lease released"))
atexit.register(lambda: print("ORDER=" + ",".join(order), flush=True))
"""


def test_at_exit_the_lease_is_released_before_a_long_pool_job_is_joined(tmp_path):
    """A real interpreter exit: the release runs while the pool's job is
    still going, not after Python has joined it."""
    env = {k: v for k, v in os.environ.items()
           if not any(s in k for s in ("API_KEY", "TOKEN", "SECRET_KEY_LIVE", "AUTH", "STRIPE", "TWILIO", "RESEND"))}
    env["CAVNAR_ROOT"] = ROOT
    env["RAILWAY_VOLUME_MOUNT_PATH"] = str(tmp_path)
    out = subprocess.run([sys.executable, "-c", _EXIT_SCRIPT], cwd=str(tmp_path), env=env, capture_output=True,
                         text=True, timeout=120)
    line = [ln for ln in out.stdout.splitlines() if ln.startswith("ORDER=")]
    assert line, out.stdout[-2000:] + out.stderr[-2000:]
    assert line[0].split("=", 1)[1].split(",")[:2] == ["lease released", "pool job joined"]
