"""The DSR narrative through Message Batches (AI cost audit 10/7/26 #20).

The same restaurant and clock as tests/test_dsr_pipeline.py (Chicago, open
11am–11pm; business date Tuesday 9/22/26 closes at 04:00 UTC on 9/23, its
deadline is 09:00 UTC), the same fake POS and blocks — and a fake batches
client, so nothing reaches a model. Pinned: a sweep night goes out as a
batch item and waits in "writing"; the answer is stored once and the next
sweep finishes the night; past the cutoff the sweep writes it synchronously
and the late answer is dropped — never two narratives; an errored item
falls back; Close day and a local backend never batch; a gate's refusal is
stored as the synchronous path stores it; the cutoff never passes the end
of quiet hours or the moment the night would read missing. And with the
real narrative module: the batched request is the synchronous one, and its
answer is judged by the same checks into the same narrative.
"""
import json
import sqlite3
import types

import pytest

import ai_batches
import ai_utils
import dsr
import scheduler
from dsr import narrative as REAL_NARRATIVE
from dsr import pipeline, store
from models import Restaurant, create_restaurant, get_restaurant
from test_dsr_pipeline import DAY, U, _labor_in, _restaurant, _run, db, world  # noqa: F401  (fixtures)

NS = types.SimpleNamespace


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

    def answer(self, batch_id, cid, result):
        self.ended.add(batch_id)
        self.answers.setdefault(batch_id, []).append(NS(custom_id=cid, result=result))


def _msg(text, usage=(4400, 2100, 0, 0)):
    i, o, w, r = usage
    return NS(id="msg_b", model="claude-sonnet-5", stop_reason="end_turn", content=[NS(type="text", text=text)],
              usage=NS(input_tokens=i, output_tokens=o, cache_creation_input_tokens=w, cache_read_input_tokens=r))


@pytest.fixture
def allowed(monkeypatch):
    """This process is the production scheduler's host (batches may go), but
    nobody is ever emailed or pushed from a test."""
    monkeypatch.setattr(scheduler, "scheduling_allowed", lambda: True)
    from dsr import deliver
    monkeypatch.setattr(deliver, "_allowed", lambda: False)
    monkeypatch.delenv("AI_BATCHES_ENABLED", raising=False)
    monkeypatch.delenv("AI_BATCHES_WORKFLOWS", raising=False)
    ai_utils.reset_breaker()
    yield
    ai_utils.reset_breaker()


@pytest.fixture
def fake(db, monkeypatch):
    f = FakeBatches()
    monkeypatch.setattr(ai_batches, "_client", lambda: NS(messages=NS(batches=f)))
    return f


@pytest.fixture
def batched(world, monkeypatch):
    """The pipeline's fake narrative module, given the batch path: its item
    goes through the real ai_batches to the real pipeline callback; its
    finish() is the fake's "judge" (the real one is tested at the bottom)."""
    mod = __import__("sys").modules["dsr.narrative"]
    world["finished"] = []

    def submit_batch(ctx, facts, custom_id, context=None):
        out = ai_batches.submit("dsr_narrative", [{
            "custom_id": custom_id, "restaurant_id": ctx.restaurant_id, "action": "dsr_narrative",
            "request": {"model": "claude-sonnet-5", "max_tokens": 100,
                        "messages": [{"role": "user", "content": "the night"}]},
            "callback": REAL_NARRATIVE.BATCH_CALLBACK, "context": dict(context or {}, state={})}])
        return {"status": out.get(custom_id)}

    def finish(ctx, facts, msg, state, declined=None):
        world["finished"].append(facts)
        return {"ok": True, "narrative": {"executive_summary": {"text": ai_utils.extract_text(msg),
                                                                "facts": ["sales.net"]}}, "reason": None}
    monkeypatch.setattr(mod, "BATCH_WORKFLOW", "dsr_narrative", raising=False)
    monkeypatch.setattr(mod, "submit_batch", submit_batch, raising=False)
    monkeypatch.setattr(mod, "finish", finish, raising=False)
    monkeypatch.setattr(mod, "refusal_for", REAL_NARRATIVE.refusal_for, raising=False)
    return world


def _rows(db, sql, args=()):
    c = sqlite3.connect(db)
    c.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in c.execute(sql, args)]
    finally:
        c.close()


def _cid(r, v=1):
    return f"dsr-{r.id}-{DAY.isoformat()}-v{v}"


# ── the sweep sends it, the version waits ───────────────────────────────────

def test_a_sweep_night_goes_out_as_a_batch_item_and_waits_in_writing(db, batched, fake, allowed):
    r = _restaurant(db)
    _labor_in(db, r.id)
    out = _run(r, U(4, 10), db)
    assert out["action"] == "narrative_batched"
    rep = store.get_report(r.id, DAY, db_path=db)
    assert rep["status"] == "writing" and "narrative" not in rep["stages"] and rep["narrative"] is None
    assert batched["narratives"] == [], "no synchronous call"
    (sent,) = fake.created
    assert [x["custom_id"] for x in sent] == [_cid(r)]
    nb = rep["stages"]["narrative_batch"]
    assert nb["status"] == "pending" and nb["cutoff_at"] == "2026-09-23 04:55:00"
    assert rep["next_attempt_at"] == "2026-09-23 04:20:00"
    # Nothing back yet: the next sweep looks and waits, collecting nothing again.
    calls = batched["sales_calls"]
    out = _run(r, U(4, 20), db)
    assert out["action"] == "narrative_pending" and batched["sales_calls"] == calls
    assert store.get_report(r.id, DAY, db_path=db)["status"] == "writing"


def test_the_answer_is_stored_once_and_the_next_sweep_finishes_the_night(db, batched, fake, allowed):
    r = _restaurant(db)
    _labor_in(db, r.id)
    _run(r, U(4, 10), db)
    fake.answer("msgbatch_1", _cid(r), NS(type="succeeded", message=_msg("From the batch.")))
    assert ai_batches.run_collector()["ok"] == 1
    rep = store.get_report(r.id, DAY, db_path=db)
    assert rep["status"] == "writing" and rep["next_attempt_at"] is None, "made due for the next sweep"
    assert rep["narrative"]["executive_summary"]["text"] == "From the batch."
    assert rep["stages"]["narrative"] == {"status": "written", "reason": None}
    assert rep["stages"]["narrative_batch"]["status"] == "landed"
    out = _run(r, U(4, 25), db)
    assert out["action"] == "final"
    rep = store.get_report(r.id, DAY, db_path=db)
    assert rep["narrative"]["executive_summary"]["text"] == "From the batch."
    assert batched["narratives"] == [] and len(batched["finished"]) == 1
    (row,) = _rows(db, "SELECT * FROM ai_usage WHERE action='dsr_narrative'")
    assert row["price_version"] == ai_utils.BATCH_PRICE_VERSION
    assert row["cost_usd"] == pytest.approx(ai_utils._estimate_cost("claude-sonnet-5", 4400, 2100) / 2)
    # The same answer handed over again (a second collector) changes nothing.
    item = ai_batches._item_view(ai_batches.item("dsr_narrative", _cid(r)))
    pipeline.on_narrative_batch(item, message=_msg("A second copy."))
    assert store.get_report(r.id, DAY, db_path=db)["narrative"]["executive_summary"]["text"] == "From the batch."


def test_past_the_cutoff_the_sweep_writes_it_and_the_late_answer_is_dropped(db, batched, fake, allowed):
    r = _restaurant(db)
    _labor_in(db, r.id)
    _run(r, U(4, 10), db)
    out = _run(r, U(4, 55), db)                            # the cutoff
    assert out["action"] == "final"
    rep = store.get_report(r.id, DAY, db_path=db)
    assert len(batched["narratives"]) == 1 and rep["narrative"]["executive_summary"]["text"] == "A good night."
    assert rep["stages"]["narrative_batch"]["status"] == "fallback"
    assert rep["stages"]["narrative_batch"]["reason"] == "cutoff"
    assert ai_batches.item("dsr_narrative", _cid(r))["status"] == "cancelled"
    assert fake.cancelled == ["msgbatch_1"]
    # The answer lands after all: ledgered (it was billed), never stored.
    fake.answer("msgbatch_1", _cid(r), NS(type="succeeded", message=_msg("Too late.")))
    ai_batches.run_collector()
    assert ai_batches.item("dsr_narrative", _cid(r))["status"] == "discarded"
    assert batched["finished"] == []
    # Even handed straight to the callback (a collector that took it just
    # before the cancel), it cannot replace what went out.
    pipeline.on_narrative_batch(ai_batches._item_view(ai_batches.item("dsr_narrative", _cid(r))),
                                message=_msg("Too late."))
    rep = store.get_report(r.id, DAY, db_path=db)
    assert rep["narrative"]["executive_summary"]["text"] == "A good night." and rep["status"] == "final"
    assert store.versions(r.id, DAY, db_path=db)[-1]["version"] == 1, "one version, one narrative"


def test_an_errored_item_falls_back_on_the_next_sweep(db, batched, fake, allowed):
    r = _restaurant(db)
    _labor_in(db, r.id)
    _run(r, U(4, 10), db)
    err = NS(type="errored", error=NS(type="error", error=NS(type="api_error", message="overloaded")))
    fake.answer("msgbatch_1", _cid(r), err)
    ai_batches.run_collector()
    rep = store.get_report(r.id, DAY, db_path=db)
    assert rep["stages"]["narrative_batch"]["status"] == "failed" and rep["next_attempt_at"] is None
    out = _run(r, U(4, 25), db)
    assert out["action"] == "final" and len(batched["narratives"]) == 1
    assert store.get_report(r.id, DAY, db_path=db)["stages"]["narrative"] == {"status": "written", "reason": None}


def test_close_day_never_batches_and_writes_through_a_pending_item(db, batched, fake, allowed):
    r = _restaurant(db)
    _labor_in(db, r.id)
    out = _run(r, U(4, 10), db, trigger=pipeline.TRIGGER_MANUAL)
    assert out["action"] == "final" and fake.created == [] and len(batched["narratives"]) == 1

    r2 = _restaurant(db, name="Pipe Two")
    _labor_in(db, r2.id)
    assert _run(r2, U(4, 10), db)["action"] == "narrative_batched"
    out = _run(r2, U(4, 12), db, trigger=pipeline.TRIGGER_MANUAL)      # someone is watching
    assert out["action"] == "final" and len(batched["narratives"]) == 2
    nb = store.get_report(r2.id, DAY, db_path=db)["stages"]["narrative_batch"]
    assert nb["status"] == "fallback" and nb["reason"] == "close_day"


def test_a_local_backend_never_submits(db, batched, fake, monkeypatch):
    monkeypatch.setattr(scheduler, "scheduling_allowed", lambda: False)
    r = _restaurant(db)
    _labor_in(db, r.id)
    out = _run(r, U(4, 10), db)
    assert out["action"] == "final" and len(batched["narratives"]) == 1
    assert fake.created == [] and _rows(db, "SELECT * FROM ai_batch_items") == []
    assert "narrative_batch" not in store.get_report(r.id, DAY, db_path=db)["stages"]


def test_a_gate_refusal_is_stored_as_the_synchronous_path_stores_it(db, batched, fake, allowed, monkeypatch):
    monkeypatch.setattr(ai_utils, "ai_budget_exceeded", lambda *a, **k: "daily budget")
    r = _restaurant(db)
    _labor_in(db, r.id)
    out = _run(r, U(4, 10), db)
    assert out["action"] == "final", "the refusal is known at once: the night does not wait"
    rep = store.get_report(r.id, DAY, db_path=db)
    assert fake.created == [] and batched["narratives"] == [] and rep["narrative"] is None
    assert rep["stages"]["narrative"]["status"] == "skipped"
    assert rep["stages"]["narrative"]["reason"].startswith("AI is paused — this account has reached its daily budget")


def test_a_failed_submit_is_written_now(db, batched, fake, allowed, monkeypatch):
    def boom(requests):
        raise RuntimeError("network down")
    monkeypatch.setattr(fake, "create", boom)
    r = _restaurant(db)
    _labor_in(db, r.id)
    out = _run(r, U(4, 10), db)
    assert out["action"] == "final" and len(batched["narratives"]) == 1
    nb = store.get_report(r.id, DAY, db_path=db)["stages"]["narrative_batch"]
    assert nb["status"] == "fallback" and nb["reason"] == "submit_failed"


def test_a_late_data_version_batches_and_its_night_is_not_reported_missing(db, batched, fake, allowed):
    r = _restaurant(db)
    _labor_in(db, r.id)
    batched["closed"] = False
    assert _run(r, U(9, 5), db)["action"] == "provisional"
    batched["closed"] = True                               # the POS closed the day at last
    out = _run(r, U(11, 5), db)
    assert out["action"] == "narrative_batched" and out["version"] == 2
    assert [x["custom_id"] for x in fake.created[0]] == [_cid(r, 2)]
    # v1 went out; v2 waiting on its narrative does not make the night missing.
    assert pipeline.nights_missing(db_path=db, now_utc=U(11, 10), use_cache=False) == []
    fake.answer("msgbatch_1", _cid(r, 2), NS(type="succeeded", message=_msg("Sales came in late.")))
    ai_batches.run_collector()
    out = _run(r, U(11, 15), db)
    assert out["action"] == "final" and out["version"] == 2


# ── the cutoff ──────────────────────────────────────────────────────────────

def test_the_cutoff_never_passes_quiet_hours_end_or_the_missing_check(db, allowed):
    r = _restaurant(db)
    rep = store.create_report(r.id, DAY, db_path=db)
    cut = pipeline._batch_cutoff
    assert cut(r, rep, pipeline.TRIGGER_SWEEP, U(4, 10), db) == U(4, 55)
    assert cut(r, rep, pipeline.TRIGGER_MANUAL, U(4, 10), db) is None
    # Deadline 4am CDT = 09:00 UTC; missing an hour later; less the margin.
    assert cut(r, rep, pipeline.TRIGGER_SWEEP, U(9, 0), db) == U(9, 40)
    assert cut(r, rep, pipeline.TRIGGER_SWEEP, U(9, 30), db) is None, "too little time left: written now"
    # Quiet hours ending at midnight CDT (05:00 UTC): the held push goes then.
    from models import update_restaurant
    update_restaurant(r.id, {"alert_quiet_start": "22:00", "alert_quiet_end": "00:00"}, db_path=db)
    rq = get_restaurant(r.id, db_path=db)
    assert cut(rq, rep, pipeline.TRIGGER_SWEEP, U(4, 10), db) == U(4, 40)
    update_restaurant(r.id, {"alert_quiet_start": "22:00", "alert_quiet_end": "23:30"}, db_path=db)
    assert cut(get_restaurant(r.id, db_path=db), rep, pipeline.TRIGGER_SWEEP, U(4, 10), db) is None


def test_a_late_version_is_not_held_to_the_first_versions_deadline(db, allowed):
    r = _restaurant(db)
    v1 = store.create_report(r.id, DAY, db_path=db)
    store.set_stage(v1["id"], "provisional", db_path=db)
    v2 = store.create_report(r.id, DAY, db_path=db)
    assert pipeline._batch_cutoff(r, v2, pipeline.TRIGGER_LATE, U(16, 0), db) == U(16, 45)


def test_batching_is_documented_with_its_switches():
    env = open("docs/ops/ENVIRONMENT.md", encoding="utf-8").read()
    for v in ("AI_BATCHES_ENABLED", "AI_BATCHES_WORKFLOWS", "DSR_BATCH_CUTOFF_MINUTES", "RETAIN_AI_BATCHES_DAYS"):
        assert f"`{v}`" in env, v
    lib = open("PROMPT_LIBRARY.md", encoding="utf-8").read()
    assert "ai_batches" in lib and "Message Batches" in lib


# ── the real narrative: one request, one judge ──────────────────────────────

def test_the_batched_request_is_the_synchronous_one_and_its_answer_is_judged_the_same(db, fake, allowed,
                                                                                         monkeypatch):
    from test_dsr_narrative import _Client, strong_night, strong_reply
    rid = create_restaurant(Restaurant(name="Simple EJ's", owner_email="erik@example.com"), db_path=db)
    rest = get_restaurant(rid, db_path=db)
    facts = strong_night()
    day = facts["business_date"]
    rep = store.create_report(rid, day, db_path=db)
    store.set_stage(rep["id"], "writing", db_path=db)
    conn = sqlite3.connect(db)
    conn.execute("UPDATE dsr_reports SET facts_json=? WHERE id=?", (json.dumps(facts), rep["id"]))
    conn.commit()
    conn.close()
    cid = f"dsr-{rid}-{day}-v1"
    store.note(rep["id"], "narrative_batch", {"custom_id": cid, "status": "pending"}, db_path=db)

    ctx = dsr.Context(rest, day, db_path=db)
    out = REAL_NARRATIVE.submit_batch(ctx, facts, cid, context={"report_id": rep["id"], "trigger": "sweep"})
    assert out == {"status": "submitted"}
    (sent,) = fake.created
    params = sent[0]["params"]
    assert params["system"][0]["cache_control"] == {"type": "ephemeral"}, "#77: the cache marker stays"

    client = _Client(strong_reply())
    monkeypatch.setattr(ai_utils, "get_client", lambda timeout=None: client)
    sync = REAL_NARRATIVE.write(ctx, facts)
    assert sync["ok"]
    assert params == client.calls[0], "the batch item carries exactly the synchronous request"

    fake.answer("msgbatch_1", cid, NS(type="succeeded", message=_msg(json.dumps(strong_reply()))))
    ai_batches.run_collector()
    stored = store.get_report_by_id(rep["id"], db_path=db)
    assert stored["stages"]["narrative"] == {"status": "written", "reason": None}
    got, want = stored["narrative"], sync["narrative"]
    assert got["executive_summary"] == want["executive_summary"]
    assert got["verification"] == want["verification"]
    assert [a["key"] for a in got["actions_tomorrow"]] == [a["key"] for a in want["actions_tomorrow"]]


def test_a_bad_batch_answer_is_refused_by_the_same_checks(db, fake, allowed):
    from test_dsr_narrative import strong_night
    rid = create_restaurant(Restaurant(name="Simple EJ's", owner_email="erik@example.com"), db_path=db)
    rest = get_restaurant(rid, db_path=db)
    facts = strong_night()
    ctx = dsr.Context(rest, facts["business_date"], db_path=db)
    out = REAL_NARRATIVE.finish(ctx, facts, _msg("no json here"), {})
    assert out["ok"] is False and "wrong shape" in out["reason"]


def test_the_batch_path_reuses_the_synchronous_builders_and_judge():
    """Read from the source: the batch item is built by the same _prepare /
    night_readiness / request_for as the call, and its answer goes through
    narrative.finish — never a second copy of the checks."""
    import inspect
    sub = inspect.getsource(REAL_NARRATIVE.submit_batch)
    for name in ("_prepare(", "night_readiness(", "request_for(", "ai_batches.submit("):
        assert name in sub, name
    # The synchronous call is the night's dsr_narrative run (AI orchestration,
    # 10/7/26): _write reads the prompt and the gate, run() makes the call
    # with request_for and judges it with _judge — what finish() returns too.
    wr = inspect.getsource(REAL_NARRATIVE._write)
    for name in ("_prepare(", "night_readiness(", "run("):
        assert name in wr, name
    rn = inspect.getsource(REAL_NARRATIVE.run)
    for name in ("request_for(", "_judge(", "orch.generate("):
        assert name in rn, name
    assert "_judge(" in inspect.getsource(REAL_NARRATIVE.finish)
    assert "run(" in inspect.getsource(REAL_NARRATIVE.land_batch)
    cbk = inspect.getsource(pipeline.on_narrative_batch)
    assert "mod.finish(" in cbk and "mod.refusal_for(" in cbk and "land_narrative_batch(" in cbk
    assert "land_batch" in cbk
    assert REAL_NARRATIVE.BATCH_CALLBACK == "dsr.pipeline:on_narrative_batch"
