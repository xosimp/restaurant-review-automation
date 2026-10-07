"""ai_batches — the Message Batches pipeline (AI cost audit 10/7/26 #19).

A fake batches client stands in for Anthropic: nothing here reaches a model.
What is pinned: an item passes the same gates create_with_retry applies
(readiness, budget, breaker — a refused one is a blocked ledger row, never
sent, and its callback is told), a local backend never submits or collects,
an answer is ledgered at half the list price with its trace and handed to
its callback as a message the synchronous helpers read, an errored item
tells its callback so the caller can fall back, and a cancelled item's
answer is ledgered and dropped — never handed over.
"""
import json
import sqlite3
import sys
import types

import pytest

import ai_batches
import ai_utils
import models
import scheduler

NS = types.SimpleNamespace
WF = "test_wf"
SEEN = []


def cb(item, message=None, error=None):
    """The test callback (reached by name, "test_ai_batches:cb")."""
    SEEN.append({"item": item, "message": message, "error": error,
                 "text": ai_utils.extract_text(message) if message is not None else None,
                 "outcome": ai_utils.outcome_of(message) if message is not None else None})


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
def db(db_path, monkeypatch):
    real = models.get_conn

    def conn(path=None, *a, **k):
        return real(db_path if path in (None, models.DB_PATH) else path)
    monkeypatch.setattr(models, "get_conn", conn)
    monkeypatch.setattr(models, "DB_PATH", db_path)
    sys.modules.setdefault("test_ai_batches", sys.modules[__name__])
    SEEN.clear()
    ai_utils.reset_breaker()
    yield db_path
    ai_utils.reset_breaker()


@pytest.fixture
def batches(db, monkeypatch):
    fake = FakeBatches()
    monkeypatch.setattr(ai_batches, "_client", lambda: NS(messages=NS(batches=fake)))
    monkeypatch.setattr(scheduler, "scheduling_allowed", lambda: True)
    monkeypatch.setenv("AI_BATCHES_WORKFLOWS", WF)
    monkeypatch.delenv("AI_BATCHES_ENABLED", raising=False)
    return fake


def _rid(db):
    return models.create_restaurant(models.Restaurant(name="Batch Co", owner_email="b@x.com"), db_path=db)


def _item(cid, rid, **kw):
    return dict({"custom_id": cid, "restaurant_id": rid, "action": "t_batch", "callback": "test_ai_batches:cb",
                 "context": {"k": cid},
                 "request": {"model": "claude-sonnet-5", "max_tokens": 50, "temperature": 0.2,
                             "system": [{"type": "text", "text": "S", "cache_control": {"type": "ephemeral"}}],
                             "messages": [{"role": "user", "content": "hello"}]}}, **kw)


def _msg(text='{"a": 1}', stop="end_turn", usage=(4000, 2000, 0, 3000)):
    i, o, w, r = usage
    return NS(id="msg_1", model="claude-sonnet-5", stop_reason=stop, content=[NS(type="text", text=text)],
              usage=NS(input_tokens=i, output_tokens=o, cache_creation_input_tokens=w, cache_read_input_tokens=r))


def _rows(db, sql, args=()):
    c = sqlite3.connect(db)
    c.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in c.execute(sql, args)]
    finally:
        c.close()


# ── pricing ─────────────────────────────────────────────────────────────────

def test_a_batch_row_is_priced_at_half_of_list_including_the_cache_rates():
    args = ("claude-sonnet-5", 4000, 2000, 1000, 3000)
    assert ai_utils._estimate_cost(*args, batch=True) == pytest.approx(ai_utils._estimate_cost(*args) * 0.5)
    assert ai_utils.BATCH_PRICE_MULTIPLIER == 0.5
    assert ai_utils.BATCH_PRICE_VERSION.startswith(ai_utils.PRICE_VERSION) and "batch" in ai_utils.BATCH_PRICE_VERSION


def test_log_ai_usage_stamps_the_batch_price_version(db):
    rid = _rid(db)
    ai_utils.log_ai_usage(rid, "t_price", "claude-sonnet-5", 1000, 1000, batch=True)
    ai_utils.log_ai_usage(rid, "t_price_sync", "claude-sonnet-5", 1000, 1000)
    (b,) = _rows(db, "SELECT * FROM ai_usage WHERE action='t_price'")
    (s,) = _rows(db, "SELECT * FROM ai_usage WHERE action='t_price_sync'")
    assert b["price_version"] == ai_utils.BATCH_PRICE_VERSION and s["price_version"] == ai_utils.PRICE_VERSION
    assert b["cost_usd"] == pytest.approx(s["cost_usd"] / 2)


# ── where it may run ────────────────────────────────────────────────────────

def test_a_local_backend_never_submits_or_collects(db, monkeypatch):
    fake = FakeBatches()
    monkeypatch.setattr(ai_batches, "_client", lambda: NS(messages=NS(batches=fake)))
    monkeypatch.setattr(scheduler, "scheduling_allowed", lambda: False)
    monkeypatch.setenv("AI_BATCHES_WORKFLOWS", WF)
    rid = _rid(db)
    assert ai_batches.enabled(WF) is False
    assert ai_batches.submit(WF, [_item("a1", rid)]) == {"a1": ai_batches.DISABLED}
    assert fake.created == [] and _rows(db, "SELECT * FROM ai_batch_items") == []
    out = ai_batches.run_collector()
    assert {"attempted", "ok", "failed", "skipped", "hit_bound"} <= set(out) and out["attempted"] == 0


def test_the_kill_switch_and_the_workflow_list(batches, monkeypatch):
    assert ai_batches.enabled(WF) is True
    assert ai_batches.enabled("not_listed") is False
    monkeypatch.setenv("AI_BATCHES_ENABLED", "0")
    assert ai_batches.enabled(WF) is False
    monkeypatch.delenv("AI_BATCHES_WORKFLOWS")
    monkeypatch.delenv("AI_BATCHES_ENABLED")
    # The learner's shadow replays joined the DSR narrative (orchestration
    # design 10/7/26, phase 6): replays nobody waits on, at half price.
    assert ai_batches.workflows() == {"dsr_narrative", "shadow_arms"}, "the default batch workflows"


def test_a_bad_custom_id_or_callback_is_refused_at_submit(batches, db):
    rid = _rid(db)
    with pytest.raises(ValueError):
        ai_batches.submit(WF, [_item("has space", rid)])
    with pytest.raises(ValueError):
        ai_batches.submit(WF, [_item("ok1", rid, callback="test_ai_batches:no_such_fn")])
    assert batches.created == []


# ── the gates ───────────────────────────────────────────────────────────────

def test_submitted_items_are_shaped_as_create_with_retry_sends_them(batches, db):
    rid = _rid(db)
    out = ai_batches.submit(WF, [_item("s1", rid), _item("s2", rid)])
    assert out == {"s1": "submitted", "s2": "submitted"}
    (sent,) = batches.created
    assert [r["custom_id"] for r in sent] == ["s1", "s2"]
    params = sent[0]["params"]
    assert "temperature" not in params and "restaurant_id" not in params and "readiness" not in params
    assert params["system"][0]["cache_control"] == {"type": "ephemeral"}, "the cache marker is kept"
    rows = {r["custom_id"]: r for r in _rows(db, "SELECT * FROM ai_batch_items")}
    assert rows["s1"]["status"] == "submitted" and rows["s1"]["batch_id"] == "msgbatch_1"
    assert rows["s1"]["trigger"] in ai_utils.TRIGGERS
    assert ai_batches.pending(WF, "s1") is True
    (job,) = _rows(db, "SELECT * FROM ai_batch_jobs")
    assert job["status"] == "in_progress" and job["n_items"] == 2


def test_a_budget_stop_blocks_the_item_and_tells_its_callback(batches, db, monkeypatch):
    rid = _rid(db)
    monkeypatch.setattr(ai_utils, "ai_budget_exceeded", lambda *a, **k: "daily budget")
    out = ai_batches.submit(WF, [_item("b1", rid)])
    assert out == {"b1": "blocked"} and batches.created == []
    (seen,) = SEEN
    assert isinstance(seen["error"], ai_utils.AIBudgetExceeded) and "daily budget" in str(seen["error"])
    assert seen["item"]["context"] == {"k": "b1"}
    (row,) = _rows(db, "SELECT * FROM ai_usage WHERE action='t_batch'")
    assert (row["outcome"], row["reason"], row["cost_usd"]) == ("blocked", "budget", 0.0)
    assert ai_batches.item(WF, "b1")["status"] == "blocked"


def test_a_held_readiness_and_an_open_breaker_block_too(batches, db):
    rid = _rid(db)
    out = ai_batches.submit(WF, [_item("r1", rid, readiness={"decision": "refuse", "reason": "sales are stale"})])
    assert out == {"r1": "blocked"} and isinstance(SEEN[-1]["error"], ai_utils.DataNotReady)
    ai_utils.trip_breaker("anthropic", "auth")
    out = ai_batches.submit(WF, [_item("r2", rid)])
    assert out == {"r2": "blocked"} and isinstance(SEEN[-1]["error"], ai_utils.AIProviderDown)
    assert batches.created == []
    reasons = {r["reason"] for r in _rows(db, "SELECT reason FROM ai_usage WHERE action='t_batch'")}
    assert reasons == {"data_not_ready", "breaker"}


def test_a_failed_submit_sends_nothing_and_says_so(db, batches, monkeypatch):
    rid = _rid(db)

    def boom(requests):
        raise RuntimeError("network down")
    monkeypatch.setattr(batches, "create", boom)
    assert ai_batches.submit(WF, [_item("f1", rid)]) == {"f1": "submit_failed"}
    assert ai_batches.pending(WF, "f1") is False
    assert ai_batches.submit(WF, [_item("f1", rid)]) == {"f1": "duplicate"}, "a custom_id is used once"


# ── the collector ───────────────────────────────────────────────────────────

def test_the_collector_ledgers_at_half_price_and_hands_the_message_over(batches, db):
    rid = _rid(db)
    ai_batches.submit(WF, [_item("c1", rid)])
    assert ai_batches.run_collector()["skipped"] == 1 and SEEN == [], "a running batch is left alone"
    batches.ended.add("msgbatch_1")
    batches.answers["msgbatch_1"] = [NS(custom_id="c1", result=NS(type="succeeded", message=_msg()))]
    out = ai_batches.run_collector()
    assert (out["attempted"], out["ok"], out["failed"]) == (1, 1, 0)
    (seen,) = SEEN
    assert seen["text"] == '{"a": 1}' and seen["outcome"] == "ok" and seen["error"] is None
    call_id = getattr(seen["message"], "_cavnar_call_id")
    (row,) = _rows(db, "SELECT * FROM ai_usage WHERE action='t_batch'")
    assert row["call_id"] == call_id and row["price_version"] == ai_utils.BATCH_PRICE_VERSION
    assert row["cost_usd"] == pytest.approx(ai_utils._estimate_cost("claude-sonnet-5", 4000, 2000, 0, 3000) / 2)
    assert row["cache_read_tokens"] == 3000 and row["outcome"] == "ok"
    (trace,) = _rows(db, "SELECT * FROM ai_calls WHERE call_id=?", (call_id,))
    assert trace["usage_id"] == row["id"] and trace["outcome"] == "ok"
    item = ai_batches.item(WF, "c1")
    assert item["status"] == "done" and item["request_z"] is None and item["usage_id"] == row["id"]
    assert json.loads(item["usage_json"])["cache_read_tokens"] == 3000
    assert _rows(db, "SELECT status FROM ai_batch_jobs")[0]["status"] == "collected"
    # A second pass finds nothing to do and never hands the answer over twice.
    ai_batches.run_collector()
    assert len(SEEN) == 1 and len(_rows(db, "SELECT * FROM ai_usage WHERE action='t_batch'")) == 1


def test_an_errored_item_tells_its_callback_and_costs_nothing(batches, db):
    rid = _rid(db)
    ai_batches.submit(WF, [_item("e1", rid)])
    batches.ended.add("msgbatch_1")
    err = NS(type="errored", error=NS(type="error", error=NS(type="invalid_request_error", message="bad")))
    batches.answers["msgbatch_1"] = [NS(custom_id="e1", result=err)]
    ai_batches.run_collector()
    (seen,) = SEEN
    assert isinstance(seen["error"], ai_batches.BatchItemFailed) and seen["error"].kind == "errored"
    assert seen["message"] is None
    (row,) = _rows(db, "SELECT * FROM ai_usage WHERE action='t_batch'")
    assert (row["outcome"], row["reason"], row["cost_usd"]) == ("error", "batch_errored", 0.0)
    assert ai_batches.item(WF, "e1")["status"] == "failed"


def test_an_expired_item_falls_back_the_same_way(batches, db):
    rid = _rid(db)
    ai_batches.submit(WF, [_item("x1", rid)])
    batches.ended.add("msgbatch_1")
    batches.answers["msgbatch_1"] = [NS(custom_id="x1", result=NS(type="expired"))]
    ai_batches.run_collector()
    assert SEEN[0]["error"].kind == "expired"


def test_a_cancelled_item_is_ledgered_and_dropped_never_handed_over(batches, db):
    rid = _rid(db)
    ai_batches.submit(WF, [_item("k1", rid)])
    assert ai_batches.cancel(WF, "k1") is True
    assert batches.cancelled == ["msgbatch_1"], "no item of the batch is wanted: cancelled at Anthropic"
    assert ai_batches.cancel(WF, "k1") is False, "only once"
    batches.ended.add("msgbatch_1")
    batches.answers["msgbatch_1"] = [NS(custom_id="k1", result=NS(type="succeeded", message=_msg()))]
    ai_batches.run_collector()
    assert SEEN == []
    assert ai_batches.item(WF, "k1")["status"] == "discarded"
    (row,) = _rows(db, "SELECT * FROM ai_usage WHERE action='t_batch'")
    assert row["cost_usd"] > 0, "an answer that ran was billed, used or not"


def test_cancel_keeps_a_batch_others_still_want(batches, db):
    rid = _rid(db)
    ai_batches.submit(WF, [_item("m1", rid), _item("m2", rid)])
    assert ai_batches.cancel(WF, "m1") is True and batches.cancelled == []


def test_an_item_the_collector_already_claimed_cannot_be_cancelled(batches, db):
    rid = _rid(db)
    ai_batches.submit(WF, [_item("z1", rid)])
    assert ai_batches._claim(WF, "z1") is True
    assert ai_batches.cancel(WF, "z1") is False


def test_a_callback_that_raises_does_not_stop_the_others(batches, db, monkeypatch):
    rid = _rid(db)
    calls = []

    def flaky(item, message=None, error=None):
        calls.append(item["custom_id"])
        if item["custom_id"] == "p1":
            raise RuntimeError("caller bug")
    monkeypatch.setattr(sys.modules[__name__], "cb", flaky)
    ai_batches.submit(WF, [_item("p1", rid), _item("p2", rid)])
    batches.ended.add("msgbatch_1")
    batches.answers["msgbatch_1"] = [NS(custom_id=c, result=NS(type="succeeded", message=_msg()))
                                     for c in ("p1", "p2")]
    ai_batches.run_collector()
    assert calls == ["p1", "p2"]
    assert "caller bug" in ai_batches.item(WF, "p1")["callback_error"]
    assert ai_batches.item(WF, "p2")["callback_error"] is None


# ── registration ────────────────────────────────────────────────────────────

def test_the_collector_is_a_registered_job_and_its_tables_are_on_the_retention_registry():
    import inspect
    import jobs_registry
    import ops
    spec = jobs_registry.JOBS["ai_batch_collect"]
    assert spec["target"] == ("ai_batches", "run_collector") and spec["sends"] is False
    assert 'run_job("ai_batch_collect"' in inspect.getsource(scheduler.scheduler_loop)
    for t, col in (("ai_batch_jobs", "submitted_at"), ("ai_batch_items", "created_at")):
        assert t in ops._RETENTION_DAYS and ops._RETENTION_COLUMN[t] == col


def test_the_tables_are_made_at_boot_not_on_a_call_path():
    import inspect
    src = inspect.getsource(ai_batches)
    assert src.count("CREATE TABLE") == 2
    for fn in (ai_batches.submit, ai_batches.run_collector, ai_batches.cancel, ai_batches.item):
        assert "CREATE" not in inspect.getsource(fn)
    assert "init_ai_batches(db_path)" in inspect.getsource(models.init_db)
