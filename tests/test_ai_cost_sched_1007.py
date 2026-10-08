"""AI cost audit 10/7/26 — the scheduler, sweep and storage items.

#54        the minute-sensitive duties run at the top of each tick, and the
           long 5am sweeps on their own "sweep" lane, gated on what they read.
#53        the diagnosis sweeps run several restaurants at once.
#55        competitor analysis runs several restaurants at once.
#45        a quiet Places-only restaurant is fetched twice a day, not four.
#100       the labor read is pre-warmed after the POS sync for owners who
           open Labor.
#92        synchronous=NORMAL under WAL (tests/test_conn_pool.py).
#94        schedule_model_calls kept 90 days (tests/test_schedule_model_record.py).
#58 #63    the diagnoses batched at half price, prompts split for the cache.
#93        not built this round: docs/plans/POS_LINES_ARCHIVE_PLAN.md says why.
"""
import functools
import inspect
import json
import os
import re
import sqlite3
import threading
import time

import pytest

# Imported at collection, so the modules that bind get_conn at import
# (CLAUDE.md, bound imports) bind the real one, never a test's patch.
import admin_routes  # noqa: F401
import models
import morning_brief
import ops
import scheduler
import jobs_registry

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SCHED_SRC = open(os.path.join(ROOT, "scheduler.py"), encoding="utf-8").read()


def _loop_src():
    return _SCHED_SRC[_SCHED_SRC.index("def scheduler_loop():"):_SCHED_SRC.index("def scheduling_allowed():")]


@pytest.fixture
def db(db_path, monkeypatch):
    orig, default = models.get_conn, models.DB_PATH
    monkeypatch.setattr(models, "get_conn",
                        lambda path=None, *a, **k: orig(db_path if path in (None, default) else path))
    monkeypatch.setattr(models, "DB_PATH", db_path)
    monkeypatch.setattr(morning_brief, "get_conn", models.get_conn)
    for name in ("get_restaurant",):
        monkeypatch.setattr(models, name, functools.partial(getattr(models, name), db_path=db_path))
    ops._claim_fallback.clear()
    return db_path


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


def _rid(db_path, name, **kw):
    return models.create_restaurant(models.Restaurant(name=name, owner_email=f"{name[:3].lower()}@x.test", **kw),
                                    db_path=db_path)


class _Overlap:
    """Counts how many calls are inside `fn` at once."""

    def __init__(self, hold=0.15):
        self.lock = threading.Lock()
        self.now = 0
        self.peak = 0
        self.hold = hold
        self.seen = []

    def __call__(self, key, result=None):
        with self.lock:
            self.now += 1
            self.peak = max(self.peak, self.now)
            self.seen.append(key)
        time.sleep(self.hold)
        with self.lock:
            self.now -= 1
        return result


# ── #55: competitor analysis, several at once ──────────────────────────────

def test_competitor_analysis_runs_several_restaurants_at_once(db, monkeypatch):
    import competitor
    ids = [_rid(db, f"Full {i}", google_place_id=f"pl{i}", service_tier="full") for i in range(5)]
    monkeypatch.setattr(models, "is_full_tier", lambda r: True)
    over = _Overlap()
    monkeypatch.setattr(competitor, "run_competitor_analysis", lambda rid: over(rid, {"ok": True}))
    assert scheduler.COMPETITOR_WORKERS == 3
    out = scheduler.run_weekly_competitor_analysis()
    assert sorted(over.seen) == ids
    assert over.peak > 1, "the pass ran one restaurant at a time"
    assert over.peak <= scheduler.COMPETITOR_WORKERS
    assert out["ok"] == 5 and out["attempted"] == 5 and out["failed"] == 0
    # The cursor is still the finished prefix: the whole pass.
    assert _q(db, "SELECT value FROM job_cursors WHERE key='competitor_sweep_cursor'")[0]["value"] == str(ids[-1])


def test_ai_visibility_stays_on_one_worker():
    """Visibility is paced against one Perplexity rate limit (#55 left it)."""
    src = inspect.getsource(scheduler.run_weekly_ai_visibility)
    assert '_weekly_sweep("ai_visibility", _VISIBILITY_CURSOR_KEY, eligible, _check)' in src


# ── #54: the order of a tick, the sweep lane and its gates ─────────────────

_SWEEP_JOBS = ("review_diagnoses_batch", "inventory_depletion", "food_cost_snapshots", "forecast_scoring",
               "food_cost_diagnoses_batch", "data_health_daily", "review_diagnoses", "food_cost_diagnoses",
               "learning_memory")


def test_the_minute_sensitive_jobs_run_before_any_daily_job_in_the_tick():
    """Asserted against the source: under catch-up a tick runs what it
    reaches in order, and the DSR, the reminders, the batch collector and
    intraday must never wait behind the 2-6am work again."""
    loop = _loop_src()
    first_daily = loop.index('claim_period("backup_db"')
    for job in ("ai_batch_collect", "dsr_sweep", "dsr_delivery", "staff_reminders", "intraday"):
        assert loop.index(f'claim_period("{job}"') < first_daily, job
    assert loop.index('claim_period("ai_batch_collect"') < loop.index('claim_period("dsr_sweep"'), \
        "the collector still runs before the DSR sweep, so a landed narrative finishes in the same tick"
    # The briefs: at the top when their inputs are settled, else at the end.
    assert loop.index('_ops.run_job("morning_brief"') < first_daily
    assert loop.count('_ops.run_job("morning_brief"') == 2 and "_briefs_ran" in loop


def test_the_long_morning_sweeps_run_on_the_sweep_lane_and_give_a_busy_claim_back():
    loop = _loop_src()
    assert set(scheduler._LANES) == {"intel", "ai", "sweep"}
    for job in _SWEEP_JOBS:
        assert f'_ops.run_in_lane("sweep", "{job}"' in loop, job
        assert f'_ops.run_job("{job}"' not in loop, f"{job} still runs on the loop thread"
        assert f'_ops.release_period("{job}", str(today))' in loop, f"a busy lane keeps {job}'s day"
        assert jobs_registry.JOBS[job].get("lane") == "sweep", job
    assert jobs_registry.JOBS["labor_prewarm"].get("lane") == "ai"
    assert '_ops.run_in_lane("ai", "labor_prewarm"' in loop


def test_each_sweep_job_waits_for_what_it_reads():
    loop = _loop_src()

    def gate(job):
        i = loop.index(f'claim_period("{job}"')
        return loop[loop.rindex("if ", 0, i):i]
    assert '_ops.settled("inventory_depletion"' in gate("food_cost_snapshots")
    assert '_ops.settled("food_cost_snapshots"' in gate("forecast_scoring")
    assert '_ops.settled("food_cost_snapshots"' in gate("food_cost_diagnoses_batch")
    assert '_ops.settled("food_cost_snapshots"' in gate("data_health_daily")
    assert '_ops.settled("review_diagnoses_batch"' in gate("review_diagnoses")
    fd = gate("food_cost_diagnoses")
    assert '_ops.settled("food_cost_snapshots"' in fd and '_ops.settled("food_cost_diagnoses_batch"' in fd
    assert '_ops.settled("outcome_rechecks"' in gate("learning_memory")
    assert '_ops.settled("pos_sync"' in gate("labor_prewarm")


def test_settled_means_claimed_and_not_running_anywhere(monkeypatch):
    p = scheduler._PulsedOps()
    claimed = {("snap", "d")}
    monkeypatch.setattr(ops, "period_claimed", lambda job, period: (job, period) in claimed)
    monkeypatch.setattr(ops, "is_running", lambda job: False)
    monkeypatch.setattr(ops, "running_elsewhere", lambda job, db_path=None: None)
    assert p.settled("snap", "d") is True
    assert p.settled("other", "d") is False, "never claimed today is not settled"
    monkeypatch.setattr(ops, "is_running", lambda job: job == "snap")
    assert p.settled("snap", "d") is False, "running on the loop"
    monkeypatch.setattr(ops, "is_running", lambda job: False)
    monkeypatch.setattr(ops, "running_elsewhere", lambda job, db_path=None: {"job": job})
    assert p.settled("snap", "d") is False, "running in another process"
    monkeypatch.setattr(ops, "running_elsewhere", lambda job, db_path=None: None)
    lane = scheduler._LANES["sweep"]
    gate = threading.Event()
    monkeypatch.setattr(ops, "acquire_scheduler_lease", lambda *a, **k: True)
    monkeypatch.setattr(ops, "run_job", lambda name, fn, *a, **k: fn(*a, **k))
    assert lane.submit("snap", lambda: gate.wait(5)) is True
    try:
        assert lane.holds("snap") and p.settled("snap", "d") is False, "submitted to a lane"
    finally:
        gate.set()
        lane.join(5)
    assert p.settled("snap", "d") is True
    scheduler._SWEEP_DONE.clear()


def test_a_failed_lane_run_gives_its_day_back_after_the_backoff(db, monkeypatch):
    """The 5am sweeps are retry=True: on the loop a failed night retried
    after 10 minutes; on a lane it must too."""
    monkeypatch.setattr(ops, "acquire_scheduler_lease", lambda *a, **k: True)
    p = scheduler._PulsedOps()
    p._claimed["inventory_depletion"] = "2026-10-07"

    def boom():
        raise RuntimeError("depletion failed")
    assert p.run_in_lane("sweep", "inventory_depletion", boom) is True
    scheduler._LANES["sweep"].join(5)
    retry = p._retries.get(("inventory_depletion", "2026-10-07"))
    assert retry and retry["attempt"] == 1, p._retries
    assert retry["at"] > time.monotonic(), "the retry waits out its backoff"
    scheduler._SWEEP_DONE.clear()


def test_the_pause_between_ticks_ends_early_when_a_sweep_job_finishes(monkeypatch):
    slept = []
    monkeypatch.setattr(scheduler.time, "sleep", lambda s: slept.append(s))
    scheduler._SWEEP_DONE.clear()
    scheduler._tick_pause()
    assert slept == [scheduler.SCHEDULER_TICK_SECONDS]
    scheduler._SWEEP_DONE.set()
    scheduler._tick_pause()
    assert slept[-1] == scheduler.SWEEP_FOLLOW_SECONDS and not scheduler._SWEEP_DONE.is_set()
    # A sweep job that ends during the tick wakes the loop too.
    monkeypatch.setattr(ops, "acquire_scheduler_lease", lambda *a, **k: True)
    monkeypatch.setattr(ops, "run_job", lambda name, fn, *a, **k: fn(*a, **k))
    scheduler._LANES["sweep"].submit("quick", lambda: None)
    scheduler._LANES["sweep"].join(5)
    scheduler._tick_pause()
    assert slept[-1] == scheduler.SWEEP_FOLLOW_SECONDS
    assert len(slept) == 3, "always exactly one sleep per pause (tests stop the loop on it)"


class _P:
    """A stand-in for _PulsedOps.settled."""

    def __init__(self, unsettled=()):
        self.unsettled = set(unsettled)

    def settled(self, job, period):
        return job not in self.unsettled


def test_the_briefs_wait_for_the_morning_chain_but_never_past_8am():
    from datetime import datetime
    six = datetime(2026, 10, 7, 6, 10)
    assert scheduler._morning_input_pending(_P(), six) is None
    assert scheduler._morning_input_pending(_P({"review_diagnoses"}), six) == "review_diagnoses"
    assert scheduler._morning_input_pending(_P({"food_cost_snapshots"}), six) == "food_cost_snapshots"
    # A 6am input is not pending at 5:30 — it is not due yet.
    assert scheduler._morning_input_pending(_P({"review_diagnoses"}), datetime(2026, 10, 7, 5, 30)) is None
    # From BRIEF_INPUT_WAIT_UNTIL_HOUR the briefs go on what is there.
    assert scheduler._morning_input_pending(_P({"review_diagnoses"}), datetime(2026, 10, 7, 8, 0)) is None
    # The digest waits only for the diagnoses.
    assert scheduler._morning_input_pending(_P({"event_memory"}), six, scheduler.DIGEST_INPUTS) is None
    assert scheduler._morning_input_pending(_P({"food_cost_diagnoses"}), six,
                                            scheduler.DIGEST_INPUTS) == "food_cost_diagnoses"
    loop = _loop_src()
    i = loop.index('claim_period("weekly_digest"')
    assert "_morning_input_pending(_ops, now, DIGEST_INPUTS) is None" in loop[i - 200:i]


class _Stop(BaseException):
    pass


@pytest.fixture
def tick(db_path, monkeypatch):
    import status_manager
    real, default = models.get_conn, models.DB_PATH
    monkeypatch.setattr(models, "get_conn", lambda path=None, *a, **k: real(db_path if path in (None, default)
                                                                              else path))
    monkeypatch.setattr(models, "DB_PATH", db_path)
    monkeypatch.setattr(status_manager, "DB_PATH", db_path)
    ops._claim_fallback.clear()
    briefs = []
    monkeypatch.setattr(morning_brief, "run_due", lambda *a, **k: briefs.append(1) or {"attempted": 0})
    monkeypatch.setattr(scheduler, "_minute_duties", lambda *a, **k: {"attempted": 0, "ok": 0, "failed": 0,
                                                                      "skipped": 0, "hit_bound": False})
    import billing_jobs
    monkeypatch.setattr(billing_jobs, "run_owed_sends", lambda *a, **k: {"attempted": 0})
    monkeypatch.setattr(scheduler._ops, "acquire_scheduler_lease", lambda *a, **k: True)
    monkeypatch.setattr(scheduler, "record_scheduler_heartbeat", lambda *a, **k: None)
    monkeypatch.setattr(scheduler, "run_health_checks", lambda *a, **k: None)
    monkeypatch.setattr(scheduler.time, "sleep", lambda s: (_ for _ in ()).throw(_Stop()))
    monkeypatch.setattr(scheduler._ops, "claim_period", lambda job, period: False)

    def run(now, unsettled=()):
        monkeypatch.setattr(scheduler, "_chi_now", lambda: now)
        monkeypatch.setattr(scheduler._PulsedOps, "settled", lambda self, job, period: job not in unsettled)
        del briefs[:]
        with pytest.raises(_Stop):
            scheduler.scheduler_loop()
        for lane in scheduler._LANES.values():
            lane.join(5)
        return len(briefs)
    return run


def test_a_tick_holds_the_briefs_while_a_diagnosis_is_still_being_written(tick):
    from datetime import datetime
    assert tick(datetime(2026, 10, 7, 6, 10), unsettled={"review_diagnoses"}) == 0
    assert tick(datetime(2026, 10, 7, 6, 10)) == 1, "settled: once, at the top of the tick"
    assert tick(datetime(2026, 10, 7, 8, 5), unsettled={"review_diagnoses"}) == 1, "never past 8am"
    assert tick(datetime(2026, 10, 7, 14, 0)) == 1


# ── #53: the diagnoses, several restaurants at once ────────────────────────

def test_the_review_diagnoses_run_several_restaurants_at_once(db, monkeypatch):
    import review_intelligence as ri
    ids = [_rid(db, f"Diag {i}") for i in range(5)]
    over = _Overlap()
    monkeypatch.setattr(ri, "diagnose", lambda rid: over(rid, []))
    monkeypatch.setattr(scheduler, "_ai_budget_spent", lambda rid: False)
    monkeypatch.setattr(scheduler, "_cancel_open_batch_items", lambda wf, rid: None)
    assert scheduler.DIAGNOSES_WORKERS == 3
    out = scheduler.run_review_diagnoses()
    assert sorted(over.seen) == sorted(ids) and 1 < over.peak <= 3
    assert out["attempted"] == 5 and out["failed"] == 0


def test_the_food_cost_diagnoses_run_several_restaurants_at_once(db, monkeypatch):
    import food_cost_intelligence as fci
    ids = [_rid(db, f"Food {i}") for i in range(5)]
    over = _Overlap()
    monkeypatch.setattr(fci, "diagnose", lambda rid: over(rid, {"ok": True}))
    monkeypatch.setattr(scheduler, "_ai_budget_spent", lambda rid: False)
    monkeypatch.setattr(scheduler, "_cancel_open_batch_items", lambda wf, rid: None)
    out = scheduler.run_food_cost_diagnoses()
    assert sorted(over.seen) == sorted(ids) and 1 < over.peak <= 3
    assert out["diagnosed"] == 5


def test_the_fallback_pass_cancels_what_the_batch_has_not_landed(db, monkeypatch):
    import ai_batches
    import review_intelligence as ri
    rid = _rid(db, "Cancel Co")
    order = []
    monkeypatch.setattr(ai_batches, "open_items", lambda wf, r: order.append(("open", wf, r)) or ["rd-1"])
    monkeypatch.setattr(ai_batches, "cancel", lambda wf, cid: order.append(("cancel", wf, cid)) or True)
    monkeypatch.setattr(ri, "diagnose", lambda r: order.append(("diagnose", r)) or [])
    monkeypatch.setattr(scheduler, "_ai_budget_spent", lambda r: False)
    scheduler.run_review_diagnoses()
    assert order == [("open", "review_diagnosis", rid), ("cancel", "review_diagnosis", "rd-1"),
                     ("diagnose", rid)]


# ── #58 / #63: the diagnoses batched, the prompts split for the cache ───────

def _cluster_reviews(db_path, rid, n=4):
    conn = sqlite3.connect(db_path)
    ids = []
    for i in range(n):
        cur = conn.execute(
            "INSERT INTO reviews (restaurant_id, platform, external_id, author, rating, text, review_date, "
            "fetched_at, sentiment, categories, summary, urgency, processed, response_status, severity, "
            "specific_complaint) VALUES (?, 'google', ?, ?, 2, 'the entree arrived cold again', "
            "date('now', ?), datetime('now'), 'negative', '[\"food_quality\"]', 's', 'normal', 1, 'pending', "
            "'operational', 'cold entree')", (rid, f"x{i}", f"Guest{i}", f"-{i + 2} days"))
        ids.append(cur.lastrowid)
    conn.commit()
    conn.close()
    return ids


def _msg(text):
    import types
    return types.SimpleNamespace(content=[types.SimpleNamespace(type="text", text=text)], stop_reason="end_turn")


def test_the_diagnosis_prompts_put_the_static_rules_in_a_cached_system_block():
    import food_cost_intelligence as fci
    import review_intelligence as ri
    for mod in (ri, fci):
        system = mod.DIAGNOSE_SYSTEM
        # Over the 1,024-token floor below which a prefix is never cached
        # (~4 characters a token is generous for this prose).
        assert len(system) > 4600, (mod.__name__, len(system))
        assert not re.search(r"\{[a-z_]+\}", system), "an unformatted placeholder"
        assert "CAUSE VOCABULARY" in system and "EVIDENCE RULES" in system
        assert not re.search(r"^(RESTAURANT|TODAY): ", system, re.M), "restaurant data in the shared prefix"
        req = mod.diagnosis_request({"user": "U"})
        assert req["system"] == [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}]
        assert req["messages"] == [{"role": "user", "content": "U"}]
    assert "{evidence_rule}" in ri.DIAGNOSE_USER and "{evidence_shape}" in ri.DIAGNOSE_USER
    assert "EVIDENCE RULE FOR THIS CLUSTER" in ri.DIAGNOSE_SYSTEM


def test_a_batched_review_diagnosis_is_stored_exactly_as_a_synchronous_one(db, monkeypatch):
    import data_health
    import ai_utils
    import review_intelligence as ri
    rid = _rid(db, "Batch Co")
    review_ids = _cluster_reviews(db, rid)
    monkeypatch.setattr(data_health, "unattended_readiness", lambda *a, **k: {"decision": "proceed"})
    monkeypatch.setattr(ri, "operational_context", lambda *a, **k: {})
    out = ri.diagnosis_batch_items(rid, "2026-10-07")
    assert len(out["items"]) == 1, out
    item = out["items"][0]
    assert item["custom_id"] == ri.batch_custom_id(rid, "food_quality", "2026-10-07")
    assert item["callback"] == ri.BATCH_CALLBACK and item["action"] == "review_diagnosis"
    assert item["request"]["system"][0]["cache_control"] == {"type": "ephemeral"}
    answer = json.dumps({"cause": "The pass holds plates too long at peak.", "alternative_cause": "a",
                         "what_would_confirm": "w", "evidence_review_ids": review_ids[:2],
                         "operational_evidence": [], "confidence": "medium",
                         "recommended_action": "Put a runner on the pass.",
                         "expected_outcome": "If the cause is right, cold-food mentions fall."})
    ri.on_diagnosis_batch({"restaurant_id": rid, "context": item["context"]}, message=_msg(answer))
    stored = ri.get_diagnoses(rid)
    assert stored and stored[0]["cause"] == "The pass holds plates too long at peak."
    state = ri._unpack_state(item["context"]["state_z"])
    assert ri._diagnosis_keys(rid)["food_quality"]["evidence_hash"] == state["ev_hash"]
    # The 6am pass then reuses it: no second call for the same reviews.
    calls = []
    monkeypatch.setattr(ai_utils, "create_with_retry", lambda *a, **k: calls.append(1))
    monkeypatch.setattr(ai_utils, "get_client", lambda *a, **k: object())
    ri.diagnose(rid)
    assert calls == [], "the batched answer was paid for twice"


def test_a_batched_answer_that_cites_what_it_was_not_given_is_refused_and_recorded(db, monkeypatch):
    import data_health
    import ai_utils
    import review_intelligence as ri
    rid = _rid(db, "Refused Co")
    _cluster_reviews(db, rid)
    monkeypatch.setattr(data_health, "unattended_readiness", lambda *a, **k: {"decision": "proceed"})
    monkeypatch.setattr(ri, "operational_context", lambda *a, **k: {})
    item = ri.diagnosis_batch_items(rid, "2026-10-07")["items"][0]
    events = []
    monkeypatch.setattr(ai_utils, "record_quality_event", lambda *a, **k: events.append((a, k)))
    bad = json.dumps({"cause": "x", "evidence_review_ids": [999999], "confidence": "high"})
    ri.on_diagnosis_batch({"restaurant_id": rid, "context": item["context"]}, message=_msg(bad))
    assert ri.get_diagnoses(rid, include_stale=True) == []
    assert any(a[:2] == ("review_diagnosis", "output_rejected") for a, _k in events), events


def test_a_batched_answer_never_overwrites_a_fresher_read(db, monkeypatch):
    import data_health
    import review_intelligence as ri
    rid = _rid(db, "Fresh Co")
    _cluster_reviews(db, rid)
    monkeypatch.setattr(data_health, "unattended_readiness", lambda *a, **k: {"decision": "proceed"})
    monkeypatch.setattr(ri, "operational_context", lambda *a, **k: {})
    item = ri.diagnosis_batch_items(rid, "2026-10-07")["items"][0]
    state = ri._unpack_state(item["context"]["state_z"])
    called = []
    monkeypatch.setattr(ri, "_written_since", lambda r, c, stamp, db_path=None: True)
    monkeypatch.setattr(ri, "finish_diagnosis", lambda *a, **k: called.append(1))
    ri.on_diagnosis_batch({"restaurant_id": rid, "context": item["context"]}, message=_msg("{}"))
    assert called == [] and state["prepared_at"]
    # A failed or expired item is left to the 6am pass.
    ri.on_diagnosis_batch({"restaurant_id": rid, "context": item["context"]}, error=RuntimeError("expired"))
    assert called == []


def test_the_batch_job_sends_one_batch_for_the_fleet_and_nothing_late(db, monkeypatch):
    import ai_batches
    import review_intelligence as ri
    from datetime import datetime
    ids = [_rid(db, f"Fleet {i}", module_reviews=1) for i in range(3)]
    monkeypatch.setattr(ai_batches, "enabled", lambda wf: True)
    sent = []
    monkeypatch.setattr(ai_batches, "submit", lambda wf, items: sent.append((wf, list(items))) or
                        {it["custom_id"]: ai_batches.SUBMITTED for it in items})
    monkeypatch.setattr(ri, "diagnosis_batch_items",
                        lambda rid, day: {"items": [{"custom_id": f"rd-{rid}-x"}], "reused": 0})
    monkeypatch.setattr(scheduler, "_ai_budget_spent", lambda rid: False)
    out = scheduler.run_review_diagnoses_batch(now=datetime(2026, 10, 7, 4, 5))
    assert len(sent) == 1 and sent[0][0] == "review_diagnosis", "one batch for the fleet"
    assert sorted(c["custom_id"] for c in sent[0][1]) == sorted(f"rd-{r}-x" for r in ids)
    assert out["items"] == 3 and out["ok"] == 3 and out["failed"] == 0
    late = scheduler.run_review_diagnoses_batch(now=datetime(2026, 10, 7, 5, 45))
    assert late["items"] == 0 and len(sent) == 1 and "too late" in late["reason"]
    monkeypatch.setattr(ai_batches, "enabled", lambda wf: False)
    off = scheduler.run_review_diagnoses_batch(now=datetime(2026, 10, 7, 4, 5))
    assert off["items"] == 0 and len(sent) == 1


def test_the_diagnoses_batch_by_default():
    import ai_batches
    assert {"review_diagnosis", "food_cost_diagnosis"} <= set(ai_batches.DEFAULT_WORKFLOWS.split(","))


def test_open_items_lists_only_what_is_still_out(db):
    import ai_batches
    conn = sqlite3.connect(db)
    for cid, rid, status in (("a", 1, "submitted"), ("b", 1, "done"), ("c", 2, "queued"), ("d", 1, "queued")):
        conn.execute("INSERT INTO ai_batch_items (workflow, custom_id, restaurant_id, callback, status) "
                     "VALUES ('review_diagnosis', ?, ?, 'm:f', ?)", (cid, rid, status))
    conn.commit()
    conn.close()
    assert sorted(ai_batches.open_items("review_diagnosis", 1)) == ["a", "d"]
    assert sorted(ai_batches.open_items("review_diagnosis")) == ["a", "c", "d"]


# ── #45: the quiet Places-only cadence ──────────────────────────────────────

def test_a_listing_is_quiet_only_on_14_days_of_slow_growth(db):
    import fetcher
    from datetime import date, timedelta
    rid = _rid(db, "Quiet Co")
    today = date(2026, 10, 7)
    assert fetcher.decide_quiet(rid, False, today=today) is False, "no history: the full cadence"
    for back in range(15, -1, -1):
        fetcher.record_places_total(rid, 300 + (15 - back), today=today - timedelta(days=back))
    assert fetcher.places_growth_per_day(rid, today=today) == pytest.approx(1.0, rel=0.2)
    assert fetcher.decide_quiet(rid, False, today=today) is True
    assert fetcher.review_fetch_slots(rid) == fetcher.QUIET_SLOTS == (8, 16)
    assert fetcher.review_cadence(rid) == (8, "twice a day")
    assert fetcher.decide_quiet(rid, True, today=today) is False, "a Business Profile restaurant never"
    assert fetcher.review_fetch_slots(rid) == fetcher.REVIEW_FETCH_SLOTS


def test_a_busy_listing_or_a_fresh_bad_review_keeps_all_four_slots(db):
    import fetcher
    from datetime import date, timedelta
    today = date(2026, 10, 7)
    busy = _rid(db, "Busy Co")
    for back in range(15, -1, -1):
        fetcher.record_places_total(busy, 300 + 3 * (15 - back), today=today - timedelta(days=back))
    assert fetcher.decide_quiet(busy, False, today=today) is False
    slow = _rid(db, "Slow Co")
    for back in range(15, -1, -1):
        fetcher.record_places_total(slow, 100, today=today - timedelta(days=back))
    assert fetcher.decide_quiet(slow, False, today=today) is True
    _x(db, "INSERT INTO reviews (restaurant_id, platform, external_id, author, rating, text, review_date, "
           "fetched_at) VALUES (?, 'google', 'bad1', 'G', 1, 'awful', date('now'), datetime('now'))", (slow,))
    assert fetcher.decide_quiet(slow, False, today=today) is False, "a 1-2 star review in the last week"


def test_the_fetch_skips_a_quiet_restaurant_in_the_noon_and_8pm_slots(db, monkeypatch):
    import fetcher
    from datetime import datetime
    from zoneinfo import ZoneInfo
    quiet = _rid(db, "Q Co", reviews_live=1)
    loud = _rid(db, "L Co", reviews_live=1)
    monkeypatch.setattr(fetcher, "decide_quiet", lambda rid, has_gbp, **k: rid == quiet)
    seen = []
    monkeypatch.setattr(scheduler, "resumable_sweep",
                        lambda key, ids, fn, max_s, workers=1, job=None: seen.append(sorted(ids)) or (0, False))
    ct = ZoneInfo("America/Chicago")
    for hour, expect in ((12, [loud]), (20, [loud]), (8, [quiet, loud]), (16, [quiet, loud])):
        monkeypatch.setattr(scheduler, "_chi_now", lambda h=hour: datetime(2026, 10, 7, h, 5, tzinfo=ct))
        out = scheduler.run_daily_fetch()
        assert seen[-1] == sorted(expect), (hour, seen[-1])
        assert out["quiet_skipped"] == (1 if hour in (12, 20) else 0)


def test_sync_now_always_fetches_a_quiet_restaurant(db, monkeypatch):
    import fetcher
    from datetime import datetime
    from zoneinfo import ZoneInfo
    quiet = _rid(db, "Q2 Co", reviews_live=1)
    monkeypatch.setattr(fetcher, "decide_quiet", lambda rid, has_gbp, **k: True)
    monkeypatch.setattr(scheduler, "_chi_now", lambda: datetime(2026, 10, 7, 12, 5,
                                                                tzinfo=ZoneInfo("America/Chicago")))
    monkeypatch.setattr(models, "get_restaurant", lambda rid, **k: None)
    out = scheduler.run_daily_fetch(restaurant_ids=[quiet])
    assert out.get("quiet_skipped", 0) == 0 and out["restaurants"] == 1


def test_the_owner_surfaces_say_a_quiet_restaurants_own_slots(db, monkeypatch):
    import data_health
    import fetcher
    from datetime import datetime, timezone
    rid = _rid(db, "Slots Co")
    monkeypatch.setattr(fetcher, "is_quiet", lambda r, db_path=None: r == rid)
    s = data_health._with_own_cadence({"id": rid}, {"key": "reviews", "state": "current"})
    assert s["slots"] == [8, 16] and s["cadence_hours"] == 8 and s["cadence_word"] == "twice a day"
    # 12:30pm Chicago (17:30 UTC): the next read is 4pm, never the skipped noon slot's successor.
    nxt = data_health.next_slot("reviews", datetime(2026, 10, 7, 17, 30, tzinfo=timezone.utc), hours=s["slots"])
    assert nxt == datetime(2026, 10, 7, 21, 0, tzinfo=timezone.utc)
    line = data_health.source_line(dict(s, label="Reviews", pct=90),
                                   now=datetime(2026, 10, 7, 17, 30, tzinfo=timezone.utc))
    assert line["cadence"] == "twice a day"
    # A restaurant on the full cadence is unchanged.
    assert "slots" not in data_health._with_own_cadence({"id": rid + 1}, {"key": "reviews"})
    # Seven hours after an 8am read is still current on the quiet cadence.
    st = dict(s, last_ok_at="2026-10-07T13:05:00Z")
    assert data_health.counts_as_current(st, datetime(2026, 10, 7, 20, 0, tzinfo=timezone.utc)) is True
    assert data_health.counts_as_current({"key": "reviews", "state": "current",
                                          "last_ok_at": "2026-10-07T13:05:00Z"},
                                         datetime(2026, 10, 7, 20, 0, tzinfo=timezone.utc)) is False


# ── #100: the Labor read pre-warmed after the POS sync ─────────────────────

def test_the_prewarm_reads_only_restaurants_whose_labor_read_was_opened(db):
    web = _rid(db, "Web Co", module_labor=1)
    phone = _rid(db, "Phone Co", module_labor=1)
    warmed = _rid(db, "Warm Co", module_labor=1)
    idle = _rid(db, "Idle Co", module_labor=1)
    old = _rid(db, "Old Co", module_labor=1)
    _x(db, "INSERT INTO activity_log (restaurant_id, event_type, event_data, created_at) "
           "VALUES (?, 'tab_view', '{\"tab\": \"labor\"}', datetime('now', '-2 days'))", (web,))
    _x(db, "INSERT INTO activity_log (restaurant_id, event_type, event_data, created_at) "
           "VALUES (?, 'tab_view', '{\"tab\": \"reviews\"}', datetime('now', '-1 days'))", (idle,))
    _x(db, "INSERT INTO activity_log (restaurant_id, event_type, event_data, created_at) "
           "VALUES (?, 'tab_view', '{\"tab\": \"labor\"}', datetime('now', '-10 days'))", (old,))
    _x(db, "INSERT INTO ai_usage (restaurant_id, action, model, \"trigger\", created_at) "
           "VALUES (?, 'labor_insight', 'm', 'request', datetime('now', '-1 days'))", (phone,))
    # The pre-warm's own read never keeps a restaurant on the list.
    _x(db, "INSERT INTO ai_usage (restaurant_id, action, model, \"trigger\", created_at) "
           "VALUES (?, 'labor_insight', 'm', 'scheduler', datetime('now', '-1 days'))", (warmed,))
    assert scheduler.labor_prewarm_restaurants() == sorted([web, phone])


def test_the_prewarm_writes_the_read_through_the_routes_own_function(db, monkeypatch):
    import client_api
    import labor
    import time_utils
    from datetime import datetime
    rid = _rid(db, "Prewarm Co", module_labor=1)
    other_day = _rid(db, "Hawaii Co", module_labor=1)
    monkeypatch.setattr(scheduler, "labor_prewarm_restaurants", lambda db_path=None: [rid, other_day])
    monkeypatch.setattr(scheduler, "_ai_budget_spent", lambda r: False)
    monkeypatch.setattr(client_api, "labor_analysis_safe", lambda r: {"is_live": True, "rid": r})
    notes = []
    monkeypatch.setattr(labor, "labor_note", lambda r, analysis, **k: notes.append((r, analysis, k)) or "note")
    monkeypatch.setattr(time_utils, "restaurant_now_by_id",
                        lambda r: datetime(2026, 10, 6, 22, 40) if r == other_day else datetime(2026, 10, 7, 3, 40))
    out = scheduler.run_labor_prewarm(now=datetime(2026, 10, 7, 3, 40))
    assert [n[0] for n in notes] == [rid], "a restaurant whose local day is still yesterday is skipped"
    assert notes[0][1] == {"is_live": True, "rid": rid} and notes[0][2]["restaurant_name"] == "Prewarm Co"
    assert out["ok"] == 1 and out["skipped"] == 1 and out["failed"] == 0


def test_a_batched_food_cost_diagnosis_is_stored_and_then_reused(db, monkeypatch):
    import ai_utils
    import data_health
    import food_cost_intelligence as fci
    rid = _rid(db, "Food Batch Co", module_inventory=1)
    drv = {"available": True, "drivers": [
        {"kind": "waste", "label": "Salmon waste above tolerance", "item": "Salmon", "dollars_monthly": 400.0,
         "confidence": "high", "difficulty": "low", "evidence": "e", "if_ignored": "i"}],
        "total_monthly": 400.0, "total_monthly_deduplicated": 400.0, "degraded_sources": []}
    ev = {"drivers": drv, "food_cost": {"ok": False, "missing": []}, "coverage": {}, "waste_sources": {},
          "weekday": {}, "seasonal": {}, "operational": {}, "profitability": {"available": False}}
    monkeypatch.setattr(fci, "build_evidence", lambda r, db_path=None: ev)
    for name in ("_position_block", "_profit_block", "_trust_block", "_pattern_block", "_operational_block"):
        monkeypatch.setattr(fci, name, lambda *a, **k: "")
    monkeypatch.setattr(data_health, "unattended_readiness", lambda *a, **k: {"decision": "proceed"})
    out = fci.diagnosis_batch_items(rid, "2026-10-07")
    assert len(out["items"]) == 1 and out["items"][0]["custom_id"] == f"fd-{rid}-20261007"
    item = out["items"][0]
    assert item["request"]["system"][0]["text"] == fci.DIAGNOSE_SYSTEM
    answer = json.dumps({"headline": "Salmon waste is the cost to fix first.",
                         "cause": "Salmon is ordered past its shelf life.", "alternative_cause": "a",
                         "what_would_confirm": "w", "operational_evidence": [], "confidence": "medium",
                         "recommended_action": "Order salmon twice a week.",
                         "expected_outcome": "If the cause is right, salmon waste falls within a month."})
    fci.on_diagnosis_batch({"restaurant_id": rid, "context": item["context"]}, message=_msg(answer))
    stored = fci.get_diagnosis(rid)
    assert stored and stored["cause"] == "Salmon is ordered past its shelf life."
    calls = []
    monkeypatch.setattr(ai_utils, "create_with_retry", lambda *a, **k: calls.append(1))
    monkeypatch.setattr(ai_utils, "get_client", lambda *a, **k: object())
    again = fci.diagnose(rid)
    assert calls == [] and again.get("cause") == stored["cause"], "the batched read was paid for twice"


# ── re-audit P5: a 6am pass waits for its own batch, then collects first ────

def test_a_fallback_pass_waits_for_its_batch_until_the_latest_time(monkeypatch):
    import ai_batches
    from datetime import datetime as _dt
    out = {"food_cost_diagnosis": ["fd-1"]}
    monkeypatch.setattr(ai_batches, "open_items", lambda wf, r=None: list(out.get(wf, [])))
    assert scheduler.DIAGNOSES_FALLBACK_LATEST == (6, 45)
    assert scheduler.DIAGNOSES_FALLBACK_LATEST > scheduler.DIAGNOSES_BATCH_UNTIL
    assert scheduler._batch_waited("food_cost_diagnosis", _dt(2026, 10, 7, 6, 10)) is False
    assert scheduler._batch_waited("food_cost_diagnosis", _dt(2026, 10, 7, 6, 45)) is True
    assert scheduler._batch_waited("review_diagnosis", _dt(2026, 10, 7, 6, 0)) is True, "nothing out: run now"

    def _broken(*a, **k):
        raise RuntimeError("no table")
    monkeypatch.setattr(ai_batches, "open_items", _broken)
    assert scheduler._batch_waited("food_cost_diagnosis", _dt(2026, 10, 7, 6, 0)) is True


def test_both_fallback_gates_wait_on_their_own_workflow():
    import food_cost_intelligence as fci
    import review_intelligence as ri
    loop = _loop_src()

    def gate(job):
        i = loop.index(f'claim_period("{job}"')
        return loop[loop.rindex("if ", 0, i):i]
    assert f'_batch_waited("{ri.BATCH_WORKFLOW}", now)' in gate("review_diagnoses")
    assert f'_batch_waited("{fci.BATCH_WORKFLOW}", now)' in gate("food_cost_diagnoses")
    # The briefs still wait for both passes to settle — never read early.
    assert ("review_diagnoses", 6) in scheduler.BRIEF_INPUTS
    assert ("food_cost_diagnoses", 6) in scheduler.BRIEF_INPUTS


def test_the_food_fallback_collects_before_it_cancels(db, monkeypatch):
    import ai_batches
    import food_cost_intelligence as fci
    rid = _rid(db, "Collect Co", module_inventory=1)
    order = []
    monkeypatch.setattr(ai_batches, "run_collector", lambda *a, **k: order.append("collect") or {})
    monkeypatch.setattr(ai_batches, "open_items", lambda wf, r=None: order.append(("open", r)) or ["fd-1"])
    monkeypatch.setattr(ai_batches, "cancel", lambda wf, cid: order.append(("cancel", cid)) or True)
    monkeypatch.setattr(fci, "diagnose", lambda r: order.append(("diagnose", r)) or {})
    monkeypatch.setattr(scheduler, "_ai_budget_spent", lambda r: False)
    scheduler.run_food_cost_diagnoses()
    assert order[0] == "collect" and order.index("collect") < order.index(("cancel", "fd-1"))
    assert ("diagnose", rid) in order


def test_the_review_fallback_collects_first_too():
    src = inspect.getsource(scheduler.run_review_diagnoses)
    assert src.index('_collect_before_cancelling("review_diagnoses")') < src.index("_cancel_open_batch_items(")
