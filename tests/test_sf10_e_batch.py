"""Scheduler audit 10/10/26, workstream E — RM-10 / AI-15 / SCALE-12: the
unattended auto-draft's model call goes through Message Batches (half the
price, no generation slot held while it waits) and its answer finishes the
ordinary auto-draft; no answer, it runs synchronously. No model is called."""
import json
import types

import pytest

import ai_batches
import ai_workflows as wf
import labor
import schedule_batch as sb
import schedule_prompt as sp
import strategy_jobs as sj

WEEK = ["2026-10-12", "2026-10-13", "2026-10-14", "2026-10-15", "2026-10-16", "2026-10-17", "2026-10-18"]


def _msg(days):
    body = {"days": [{"date": d, "shifts": [{"employee": "S0", "role": "Server", "start": "4:00pm",
                                              "end": "10:00pm"}]} for d in days], "summary": ["ok"]}
    return types.SimpleNamespace(content=[types.SimpleNamespace(type="text", text=json.dumps(body))],
                                 stop_reason="end_turn",
                                 usage=types.SimpleNamespace(input_tokens=100, output_tokens=200,
                                                             cache_read_input_tokens=0,
                                                             cache_creation_input_tokens=0))


def _generate(monkeypatch, sent):
    def fake(client, **kw):
        sent.append(kw)
        return _msg(sp.request_dates(kw["messages"][0]["content"]))
    monkeypatch.setattr(labor, "create_with_retry", fake)
    monkeypatch.setattr(labor, "get_client", lambda *a, **k: None)
    monkeypatch.setattr(labor, "model_for", lambda k: "claude-sonnet-5-5")
    recorded = []
    monkeypatch.setattr(labor, "_record_schedule_call",
                        lambda rid, call, args, raw, msg, **k: recorded.append(labor._usage_of(msg) if msg else None))
    return lambda: labor.generate_optimized_schedule(
        {"overall_labor_pct": 28.0, "total_sales": 60000, "period_days": 21, "by_day": {}}, [],
        restaurant_name="T", hourly_rate=20.0, labor_target=30.0, week_start=WEEK[0],
        roster=[(f"S{i}", "Server") for i in range(3)]), recorded


def test_with_no_hook_the_call_goes_out_as_before(monkeypatch):
    sent = []
    run, _rec = _generate(monkeypatch, sent)
    run()
    assert len(sent) == 1


def test_capture_raises_the_request_before_anything_is_sent_or_recorded(monkeypatch):
    sent = []
    run, recorded = _generate(monkeypatch, sent)
    with pytest.raises(sb.CallCaptured) as got:
        with sb.capture():
            run()
    assert not sent and not recorded
    req = got.value.request
    assert req["model"] == "claude-sonnet-5-5" and req["messages"] and "readiness" in req
    assert sp.request_dates(req["messages"][0]["content"]) == WEEK


def test_the_batched_answer_is_used_once_for_its_call_and_priced_as_a_batch(monkeypatch):
    sent = []
    run, recorded = _generate(monkeypatch, sent)
    with pytest.raises(sb.CallCaptured) as got:
        with sb.capture():
            run()
    key = sb.call_key(got.value.request)
    with sb.answering(key, _msg(WEEK)):
        out = run()
        assert not sent                                       # the batch's answer, no call
        assert {r["date"] for r in out["rows"]} == set(WEEK)
        run()                                                 # used once: the next call goes out
    assert len(sent) == 1
    assert recorded[0].get("batch") is True and "batch" not in recorded[1]


def test_an_answer_for_another_request_is_not_used(monkeypatch):
    sent = []
    run, _rec = _generate(monkeypatch, sent)
    with sb.answering("not-this-call", _msg(WEEK)):
        run()
    assert len(sent) == 1


def test_submit_sends_the_captured_request_with_a_cutoff_and_the_callback(monkeypatch):
    monkeypatch.setattr(ai_batches, "enabled", lambda w: w == sb.WORKFLOW)
    request = {"model": "claude-sonnet-5-5", "messages": [{"role": "user", "content": [{"type": "text",
                                                                                         "text": "x"}]}]}
    monkeypatch.setattr(sb, "capture_request", lambda rid, week_start=None: request)
    items = []
    monkeypatch.setattr(ai_batches, "submit", lambda w, its: items.extend(its) or
                        {its[0]["custom_id"]: ai_batches.SUBMITTED})
    r = types.SimpleNamespace(id=7)
    assert sb.submit_auto_draft(r, period="2026-10-08") == "submitted"
    it = items[0]
    assert it["callback"] == sb.CALLBACK and it["restaurant_id"] == 7 and it["cutoff_at"] is not None
    assert it["context"] == {"key": sb.call_key(request), "period": "2026-10-08", "week_start": None,
                             "target": None, "attempt": None}
    ai_batches._resolve(it["callback"])                       # resolves by name


def test_no_batch_when_batches_are_off_or_nothing_would_be_sent(monkeypatch):
    r = types.SimpleNamespace(id=7)
    monkeypatch.setattr(ai_batches, "enabled", lambda w: False)
    assert sb.submit_auto_draft(r) is None
    monkeypatch.setattr(ai_batches, "enabled", lambda w: True)
    monkeypatch.setattr(sb, "capture_request", lambda rid, week_start=None: None)
    assert sb.submit_auto_draft(r) is None


def test_the_auto_draft_submits_first_and_holds_no_job_or_slot(monkeypatch):
    import ops
    import schedule_engine
    monkeypatch.setattr(schedule_engine, "generation_request", lambda rid, **k: {"week_start": WEEK[0]})
    got = {}
    monkeypatch.setattr(sb, "submit_auto_draft", lambda r, **k: got.update(k) or "submitted")
    monkeypatch.setattr(ops, "claim_async_job", lambda *a, **k: (_ for _ in ()).throw(AssertionError("claimed")))
    bumped = []
    sj._draft_one(types.SimpleNamespace(id=3), None, None, bumped.append, period="2026-10-08")
    assert bumped == ["batched"] and got["week_start"] == WEEK[0] and got["period"] == "2026-10-08"


def test_with_the_queue_on_the_answer_rides_a_queue_row(monkeypatch):
    """Scheduler audit 10/10/26 RM-7: the answered auto-draft is a job_queue
    row (its slot taken by the queue's rules), the answer carried in it —
    tests/test_sf10_h2_queue.py runs such a row to the call site."""
    import models
    import job_queue
    monkeypatch.delenv("JOB_QUEUE", raising=False)
    r = types.SimpleNamespace(id=3, auto_draft_schedule=1, module_labor=1)
    monkeypatch.setattr(models, "get_restaurant", lambda rid, *a, **k: r)
    monkeypatch.setattr(sj, "_draft_one", lambda *a, **k: pytest.fail("not on the callback thread"))
    rows = []
    monkeypatch.setattr(sj, "enqueue_auto_draft", lambda rr, **k: rows.append(k))
    assert job_queue.enabled()
    sb.on_auto_draft({"restaurant_id": 3, "context": {"key": "k1", "period": "p", "target": "t", "attempt": 0}},
                     message=_msg(WEEK))
    sb.on_auto_draft({"restaurant_id": 3, "context": {"key": "k1"}}, error=ai_batches.BatchItemFailed("cutoff"))
    sb.on_auto_draft({"restaurant_id": 3, "context": {"key": "k1"}}, error=RuntimeError("budget"))
    assert len(rows) == 2 and rows[0]["batch"] is False and rows[0]["answer"]["key"] == "k1"
    assert rows[0]["answer"]["text"] and rows[1]["answer"] is None


def test_the_answer_runs_the_ordinary_auto_draft_with_the_answer_in_hand(monkeypatch):
    import models
    # The old path, with the job queue off (JOB_QUEUE=0).
    monkeypatch.setenv("JOB_QUEUE", "0")
    r = types.SimpleNamespace(id=3, auto_draft_schedule=1, module_labor=1)
    monkeypatch.setattr(models, "get_restaurant", lambda rid, *a, **k: r)
    seen = []

    def draft(rr, db_path, se_, bump, period=None, target=None, attempt=None, batch=True):
        h = sb._HOOK.get()
        seen.append((batch, h and h["mode"], h and h["key"]))
    monkeypatch.setattr(sj, "_draft_one", draft)
    sb.on_auto_draft({"restaurant_id": 3, "context": {"key": "k1", "period": None}}, message=_msg(WEEK))
    sb.on_auto_draft({"restaurant_id": 3, "context": {"key": "k1"}}, error=ai_batches.BatchItemFailed("cutoff"))
    sb.on_auto_draft({"restaurant_id": 3, "context": {"key": "k1"}}, error=RuntimeError("budget"))
    assert seen == [(False, "answer", "k1"), (False, None, None)]


def test_labor_schedule_may_batch():
    assert wf.policy("labor_schedule").batch is True
    assert "labor_schedule" in ai_batches.DEFAULT_WORKFLOWS.split(",")
    assert ai_batches.policy_allows("labor_schedule")


def test_a_batched_calls_cost_is_the_batch_rate(monkeypatch, db_path):
    import models
    import schedule_metrics as sm
    import schedule_output as so
    from ai_utils import _estimate_cost, BATCH_PRICE_MULTIPLIER
    real = models.get_conn
    monkeypatch.setattr(sm, "get_conn", lambda p=None: real(db_path))
    monkeypatch.setattr(so, "get_conn", lambda p=None: real(db_path))
    so.record_call(None, {"model": "claude-sonnet-5-5"}, generation_id="gb", model="claude-sonnet-5-5",
                   usage={"input_tokens": 1000, "output_tokens": 30000, "batch": True})
    full = _estimate_cost("claude-sonnet-5-5", 1000, 30000)
    assert sm.generation_cost("gb")["cost_usd"] == pytest.approx(full * BATCH_PRICE_MULTIPLIER, rel=1e-3)
