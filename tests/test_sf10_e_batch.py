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


def test_planning_lists_the_request_before_anything_is_sent_or_recorded(monkeypatch):
    # Blind re-audit 10/11/26 ARCH-15: an ordinary Exception (never a
    # BaseException tunnelled past cleanup), the request listed.
    sent = []
    run, recorded = _generate(monkeypatch, sent)
    assert issubclass(sb.CallPlanned, Exception) and not hasattr(sb, "CallCaptured")
    with sb.planning() as plan:
        with pytest.raises(sb.CallPlanned) as got:
            run()
    assert not sent and not recorded
    req = got.value.request
    assert plan["requests"] == [req]
    assert req["model"] == "claude-sonnet-5-5" and req["messages"] and "readiness" in req
    assert sp.request_dates(req["messages"][0]["content"]) == WEEK


@pytest.fixture
def store(db_path, monkeypatch):
    import models
    real = models.get_conn
    monkeypatch.setattr(models, "get_conn", lambda *a, **k: real(db_path))
    monkeypatch.setattr(models, "DB_PATH", db_path)
    return db_path


def test_a_groups_batched_answer_is_read_once_by_its_call_key_and_priced_as_a_batch(monkeypatch, store):
    import schedule_resume
    sent = []
    run, recorded = _generate(monkeypatch, sent)
    with sb.planning() as plan:
        with pytest.raises(sb.CallPlanned):
            run()
    req = plan["requests"][0]
    ctx = {"group": "sd-7-g1", "n": 1, "key": sb.call_key(req), "prompt_sig": __import__("schedule_output")
           .prompt_sig(req)}
    sb._store_item(7, ctx, message=_msg(WEEK))
    with schedule_resume.resuming("sd-7-g1"):
        out = run()
        assert not sent                                       # the batch's answer, no call
        assert {r["date"] for r in out["rows"]} == set(WEEK)
        assert schedule_resume.resume_facts()["inputs_moved"] is False
        run()                                                 # used once: the next call goes out
    assert len(sent) == 1
    assert recorded[0].get("batch") is True and recorded[0]["output_tokens"] == 200
    assert "batch" not in recorded[1]


def test_an_answer_for_another_request_is_not_used(monkeypatch, store):
    import schedule_resume
    sent = []
    run, _rec = _generate(monkeypatch, sent)
    sb._store_item(7, {"group": "sd-7-g2", "n": 1, "key": "not-this-call"}, message=_msg(WEEK))
    with schedule_resume.resuming("sd-7-g2"):
        run()
    assert len(sent) == 1


def test_submit_sends_every_planned_call_as_one_group_with_a_cutoff_and_the_callback(monkeypatch):
    monkeypatch.setattr(ai_batches, "enabled", lambda w: w == sb.WORKFLOW)
    reqs = [{"model": "claude-sonnet-5-5", "messages": [{"role": "user", "content": [{"type": "text",
                                                                                      "text": t}]}]}
            for t in ("kitchen", "front")]
    monkeypatch.setattr(sb, "plan_requests", lambda rid, week_start=None: reqs)
    items = []
    monkeypatch.setattr(ai_batches, "submit", lambda w, its: items.extend(its) or
                        {it["custom_id"]: ai_batches.SUBMITTED for it in its})
    r = types.SimpleNamespace(id=7)
    assert sb.submit_auto_draft(r, period="2026-10-08") == "submitted"
    assert len(items) == 2 and len({it["context"]["group"] for it in items}) == 1
    for i, it in enumerate(items):
        assert it["callback"] == sb.CALLBACK and it["restaurant_id"] == 7 and it["cutoff_at"] is not None
        assert it["custom_id"] == f"{it['context']['group']}-{i}"
        assert {k: it["context"][k] for k in ("n", "i", "key", "period")} == \
            {"n": 2, "i": i, "key": sb.call_key(reqs[i]), "period": "2026-10-08"}
    ai_batches._resolve(items[0]["callback"])                 # resolves by name


def test_a_partly_submitted_group_is_cancelled_and_runs_now(monkeypatch):
    monkeypatch.setattr(ai_batches, "enabled", lambda w: True)
    monkeypatch.setattr(sb, "plan_requests", lambda rid, week_start=None: [{"model": "m"}, {"model": "m"}])
    monkeypatch.setattr(ai_batches, "submit", lambda w, its: {its[0]["custom_id"]: ai_batches.SUBMITTED,
                                                              its[1]["custom_id"]: "failed"})
    cancelled = []
    monkeypatch.setattr(ai_batches, "cancel", lambda w, cid, client=None: cancelled.append(cid) or True)
    assert sb.submit_auto_draft(types.SimpleNamespace(id=7)) is None and len(cancelled) == 1


def test_no_batch_when_batches_are_off_or_nothing_would_be_sent(monkeypatch):
    r = types.SimpleNamespace(id=7)
    monkeypatch.setattr(ai_batches, "enabled", lambda w: False)
    assert sb.submit_auto_draft(r) is None
    monkeypatch.setattr(ai_batches, "enabled", lambda w: True)
    monkeypatch.setattr(sb, "plan_requests", lambda rid, week_start=None: [])
    assert sb.submit_auto_draft(r) is None


def test_the_engine_plans_every_independent_call_of_its_first_round(monkeypatch, db_path):
    """The round the engine sends together (date slices of one part, here)
    is the plan: every call listed, none sent, no week built."""
    from test_schedule_b2_calls import _build_harness
    import models
    import schedule_engine as se
    real = models.get_conn
    monkeypatch.setattr(models, "get_conn", lambda *a, **k: real(db_path))
    monkeypatch.setattr(models, "DB_PATH", db_path)
    rid = _build_harness(monkeypatch, db_path)
    monkeypatch.setattr(se, "rows_per_call", lambda *a, **k: 1)
    monkeypatch.setattr(se, "parallel_calls", lambda: 4)
    sent = []

    def fake(analysis, shifts, week_slice=None, prior_rows=None, **kw):
        if kw.get("prepare_only"):
            return {"schedule_csv": "", "rows": [], "week_dates": [], "prepared": True}
        req = {"model": "m", "messages": [{"role": "user", "content": ",".join(week_slice or ["all"])}]}
        if sb.plan_call(req):
            raise sb.CallPlanned(req)
        sent.append(req)
        raise AssertionError("sent while planning")
    monkeypatch.setattr(labor, "generate_optimized_schedule", fake)
    got = sb.plan_requests(rid, week_start="2026-10-05")
    assert len(got) > 1 and not sent
    assert len({r["messages"][0]["content"] for r in got}) == len(got)


def test_the_groups_last_answer_queues_the_draft_with_its_answers(monkeypatch, store):
    import models
    import job_queue
    monkeypatch.delenv("JOB_QUEUE", raising=False)
    r = types.SimpleNamespace(id=3, auto_draft_schedule=1, module_labor=1)
    monkeypatch.setattr(models, "get_restaurant", lambda rid, *a, **k: r)
    monkeypatch.setattr(sj, "_draft_one", lambda *a, **k: pytest.fail("not on the callback thread"))
    rows = []
    monkeypatch.setattr(sj, "enqueue_auto_draft", lambda rr, **k: rows.append(k))
    assert job_queue.enabled()
    ctx = {"group": "sd-3-g", "n": 2, "period": "p", "target": None, "attempt": 0}
    sb.on_auto_draft({"restaurant_id": 3, "context": dict(ctx, key="k1", i=0)}, message=_msg(WEEK))
    assert rows == [], "one of two answers in: nothing runs yet"
    sb.on_auto_draft({"restaurant_id": 3, "context": dict(ctx, key="k2", i=1)},
                     error=ai_batches.BatchItemFailed("cutoff"))
    assert len(rows) == 1 and rows[0]["answers_job"] == "sd-3-g" and rows[0]["batch"] is False
    import schedule_output
    assert [a["call_key"] for a in schedule_output.stored_answers("sd-3-g")] == ["k1"]   # the cutoff goes sync
    # A gate's refusal of any item drafts nothing.
    blocked = {"group": "sd-3-b", "n": 2, "period": "p"}
    sb.on_auto_draft({"restaurant_id": 3, "context": dict(blocked, key="k1", i=0)}, error=RuntimeError("budget"))
    sb.on_auto_draft({"restaurant_id": 3, "context": dict(blocked, key="k2", i=1)}, message=_msg(WEEK))
    assert len(rows) == 1


def test_with_the_queue_on_an_item_from_before_the_groups_still_rides_a_queue_row(monkeypatch):
    """Scheduler audit 10/10/26 RM-7: an item submitted before ARCH-15 (no
    group) carries its one answer in the row, as it did."""
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


def test_the_answer_runs_the_ordinary_auto_draft_with_the_answer_in_hand(monkeypatch, store):
    import models
    import schedule_resume
    # The old path, with the job queue off (JOB_QUEUE=0): an item from before
    # the groups reads its one answer; a group's last answer runs the draft
    # under resuming(group).
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
    resumed = []
    monkeypatch.setattr(sj, "_draft_one", lambda *a, **k: resumed.append(schedule_resume.resume_facts()["resumed"]))
    sb.on_auto_draft({"restaurant_id": 3, "context": {"group": "sd-3-q", "n": 1, "key": "k1"}}, message=_msg(WEEK))
    sb.on_auto_draft({"restaurant_id": 3, "context": {"group": "sd-3-q", "n": 1, "key": "k1"}}, message=_msg(WEEK))
    assert resumed == [True], "the group's draft runs once, reading the stored answers"


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
