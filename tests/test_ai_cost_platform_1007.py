"""AI cost audit 10/7/26 — the platform items.

#4   a model call on a request thread takes one of INTERACTIVE_AI_SLOTS
     process-wide slots (busy past a short wait), with a shorter default
     leash; background calls take none; an Ask turn counts once.
#6   the long AI jobs run on their own scheduler lane, not the loop thread.
#52  the weekly digest pass is bounded and resumable.
#8   the local snapshot is a gzipped VACUUM INTO copy, restorable, and the
     newest BACKUP_RETAIN_COUNT are kept.
#22  a platform-wide Google Places ceiling binds every request.
#23  the global AI pool grows $30 a paying client, not $200.
#89  the budget tier and paying-client count are memoised, and a write
     drops them.
#90  note_ai_spend's read-add-write holds a lock.
#91  the usage rollup reads days by created_at range and is not a boot step.
No test here reaches a provider, the scheduler thread or production.
"""
import gzip
import inspect
import os
import sqlite3
import sys
import threading
import types
from datetime import datetime

import anthropic
import httpx
import pytest
from flask import Flask

import ai_utils
import models
import ops
import scheduler
from models import Restaurant, create_restaurant


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
    # A fresh pair of slots per test: a test that leaked one must not starve
    # the next.
    monkeypatch.setattr(ai_utils, "_INTERACTIVE_SLOTS", threading.BoundedSemaphore(2))
    monkeypatch.setattr(ai_utils, "INTERACTIVE_AI_SLOTS", 2)
    monkeypatch.setattr(ai_utils, "INTERACTIVE_AI_WAIT_SECONDS", 0.05)
    ai_utils.reset_process_state(db_path)
    yield
    ai_utils.reset_process_state(db_path)


_APP = Flask("ai_cost_platform_test")


class _Usage:
    input_tokens, output_tokens = 10, 5
    cache_creation_input_tokens = cache_read_input_tokens = 0


def _msg():
    return types.SimpleNamespace(usage=_Usage(), stop_reason="end_turn", _request_id="req",
                                 content=[types.SimpleNamespace(type="text", text="ok")])


class _Client:
    """A test double with a real default timeout, like get_client()'s."""

    def __init__(self, replies=None, timeout=ai_utils.DEFAULT_AI_TIMEOUT):
        self.replies = list(replies or [])
        self.timeout = anthropic.Timeout(timeout, connect=5.0)
        self.messages = self
        self.seen = []

    def create(self, **kw):
        self.seen.append(kw)
        r = self.replies.pop(0) if self.replies else _msg()
        if isinstance(r, Exception):
            raise r
        return r


def _conn_error():
    return anthropic.APIConnectionError(request=httpx.Request("POST", "https://api.example/v1/messages"))


def _rows(db_path, where="1=1"):
    c = sqlite3.connect(db_path)
    c.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in c.execute(f"SELECT * FROM ai_usage WHERE {where} ORDER BY id")]
    finally:
        c.close()


def _call(client, **kw):
    return ai_utils.create_with_retry(client, model="claude-sonnet-5", max_tokens=10,
                                      readiness={"decision": "proceed"}, **kw)


# ── #4: the interactive guard ───────────────────────────────────────────────

def test_a_request_thread_call_is_busy_when_every_slot_is_held(db_path):
    assert ai_utils.acquire_interactive_slot() and ai_utils.acquire_interactive_slot()
    client = _Client()
    try:
        with _APP.test_request_context("/api/insight"):
            with pytest.raises(ai_utils.AIBusy):
                _call(client, restaurant_id=7, action="labor_insight")
    finally:
        ai_utils.release_interactive_slot()
        ai_utils.release_interactive_slot()
    assert client.seen == [], "a busy call must never reach the provider"
    (row,) = _rows(db_path, "reason='busy'")
    assert row["outcome"] == "blocked" and row["cost_usd"] == 0 and row["action"] == "labor_insight"


def test_background_calls_take_no_slot_and_are_never_busy():
    assert ai_utils.acquire_interactive_slot() and ai_utils.acquire_interactive_slot()
    try:
        assert not ai_utils.on_request_thread()
        out = _call(_Client(), action="weekly_plan")
        assert ai_utils.extract_text(out) == "ok"
        assert ai_utils.interactive_slots_free() == 0, "a background call took or freed a slot"
    finally:
        ai_utils.release_interactive_slot()
        ai_utils.release_interactive_slot()


def test_the_slot_is_given_back_however_the_call_ends():
    bad = anthropic.BadRequestError("bad", response=httpx.Response(
        400, request=httpx.Request("POST", "https://api.example/v1/messages")), body=None)
    with _APP.test_request_context("/api/insight"):
        _call(_Client())
        with pytest.raises(anthropic.BadRequestError):
            _call(_Client([bad]))
        with pytest.raises(anthropic.APIConnectionError):
            _call(_Client([_conn_error(), _conn_error()]))
    assert ai_utils.interactive_slots_free() == 2


def test_a_request_thread_call_defaults_to_a_short_leash():
    with _APP.test_request_context("/api/insight"):
        c = _Client([_conn_error(), _conn_error(), _conn_error()])
        with pytest.raises(anthropic.APIConnectionError):
            _call(c)
        assert len(c.seen) == 1 + ai_utils.INTERACTIVE_AI_RETRIES == 2
        assert c.seen[0]["timeout"].read == ai_utils.INTERACTIVE_AI_TIMEOUT == 40.0
        # A caller that names its retries keeps them...
        c = _Client([_conn_error(), _conn_error(), _conn_error()])
        with pytest.raises(anthropic.APIConnectionError):
            _call(c, retries=2)
        assert len(c.seen) == 3
        # ...and one that built its own client timeout keeps that.
        c = _Client(timeout=45.0)
        _call(c)
        assert "timeout" not in c.seen[0]
    # Off a request nothing changes: two retries, the client's own timeout.
    c = _Client([_conn_error(), _conn_error(), _conn_error()])
    with pytest.raises(anthropic.APIConnectionError):
        _call(c)
    assert len(c.seen) == 3 and "timeout" not in c.seen[0]


def test_a_turn_holds_one_slot_and_its_calls_take_none(monkeypatch):
    monkeypatch.setattr(ai_utils, "_INTERACTIVE_SLOTS", threading.BoundedSemaphore(1))
    with _APP.test_request_context("/api/ask-cavnar"):
        with ai_utils.interactive_slot() as took:
            assert took is True and ai_utils.interactive_slots_free() == 0
            # Three tool rounds on one slot: no deadlock, no busy, and the
            # turn keeps its own (Ask's) timeouts.
            c = _Client()
            for _ in range(3):
                _call(c)
            assert all("timeout" not in kw for kw in c.seen)
            with ai_utils.interactive_slot() as nested:
                assert nested is False
        assert ai_utils.interactive_slots_free() == 1
        # A second turn while one is open is busy.
        assert ai_utils.acquire_interactive_slot()
        with pytest.raises(ai_utils.AIBusy):
            with ai_utils.interactive_slot():
                pass
        ai_utils.release_interactive_slot()
    # Off a request a turn takes nothing.
    with ai_utils.interactive_slot() as took:
        assert took is False


def test_a_worker_finishing_a_turn_takes_no_slot_of_its_own(monkeypatch):
    """The Ask stream: the request thread takes the slot, the worker thread
    runs the tool loop under interactive_slot_held() and gives it back."""
    monkeypatch.setattr(ai_utils, "_INTERACTIVE_SLOTS", threading.BoundedSemaphore(1))
    assert ai_utils.acquire_interactive_slot()
    seen = {}

    def worker():
        with _APP.test_request_context("/copied-context"):     # even with a request context
            with ai_utils.interactive_slot_held():
                seen["out"] = ai_utils.extract_text(_call(_Client()))
        ai_utils.release_interactive_slot()
    t = threading.Thread(target=worker)
    t.start()
    t.join(5)
    assert seen["out"] == "ok" and ai_utils.interactive_slots_free() == 1


def test_busy_reads_as_busy_everywhere_an_owner_sees_an_error():
    e = ai_utils.AIBusy("x")
    msg, status = ai_utils.user_facing_error(e)
    assert status == 503 and "busy" in msg and "try again in a moment" in msg
    assert ai_utils.insight_error(e) == (msg, 503)
    assert ai_utils.is_platform_stop(e), "a busy server says nothing about the item being processed"
    # A real outage keeps its own words.
    assert ai_utils.user_facing_error(ai_utils.AIProviderDown("down"))[0] == "down"


def test_the_default_leaves_two_of_four_request_threads_free():
    src = inspect.getsource(ai_utils)
    assert 'os.getenv("INTERACTIVE_AI_SLOTS", "2")' in src
    assert open("Procfile").read().count("--threads 4") == 1


def test_both_ask_routes_count_a_turn_once():
    import client_api
    stream = inspect.getsource(client_api._ask_cavnar_stream_response)
    ask_slot = stream.index("_ASK_SLOTS.acquire(blocking=False)")
    ai_slot = stream.index("_ai_slots.acquire_interactive_slot()")
    assert ask_slot < ai_slot, "always the Ask slot first, then the interactive one"
    assert "with interactive_slot_held():" in stream
    finally_block = stream[stream.index("finally:\n            _release_interactive()"):]
    assert finally_block.index("_release_interactive()") < finally_block.index("_ASK_SLOTS.release()")
    plain = inspect.getsource(client_api._do_ask_cavnar)
    assert "with interactive_slot():" in plain and "AIBusy" in plain


# ── #22 / #23: the platform ceilings ────────────────────────────────────────

def _places_spend(db_path, rid, dollars):
    ai_utils.log_ai_usage(rid, "competitor_refresh", "google-places-details", 0, 0, vendor="google_places",
                          cost_usd=dollars)
    with ai_utils._budget_lock:
        ai_utils._budget_cache.clear()


def test_the_platform_places_ceiling_binds_every_request(db_path, monkeypatch):
    monkeypatch.setattr(ai_utils, "AI_PLACES_GLOBAL_MONTHLY_USD", 2.0)
    a = create_restaurant(Restaurant(name="Places A", owner_email="a@x.test"), db_path=db_path)
    b = create_restaurant(Restaurant(name="Places B", owner_email="b@x.test"), db_path=db_path)
    _places_spend(db_path, a, 1.5)
    assert ai_utils.places_budget_exceeded(b) is None
    _places_spend(db_path, None, 0.6)                 # an unattributed loop counts too
    assert ai_utils.places_budget_exceeded(None) == ai_utils.PLACES_GLOBAL_LABEL
    assert ai_utils.places_budget_exceeded(b) == ai_utils.PLACES_GLOBAL_LABEL, \
        "a restaurant under its own ceiling is still stopped by the platform's"
    st = ai_utils.places_budget_status()
    assert st["global_month"]["budget"] == 2.0 and st["month"]["over"]
    monkeypatch.setattr(ai_utils, "AI_PLACES_GLOBAL_MONTHLY_USD", 0.0)
    with ai_utils._budget_lock:
        ai_utils._budget_cache.clear()
    assert ai_utils.places_budget_exceeded(None) is None, "0 disables it"


def test_the_platform_places_ceiling_pages_at_80_percent(db_path, monkeypatch):
    monkeypatch.setattr(ai_utils, "AI_PLACES_GLOBAL_MONTHLY_USD", 10.0)
    pages = []
    monkeypatch.setattr(ai_utils, "_page", lambda key, subject, lines: pages.append((key, subject)))
    _places_spend(db_path, None, 8.5)
    assert ai_utils.places_budget_exceeded(None) is None
    assert pages and pages[0][0] == "budget_warn:google_places" and "Google Places" in pages[0][1]


def test_the_review_fetch_is_never_refused_by_the_platform_places_ceiling(db_path, monkeypatch):
    monkeypatch.setattr(ai_utils, "AI_PLACES_GLOBAL_MONTHLY_USD", 1.0)
    _places_spend(db_path, None, 5.0)
    import requests
    monkeypatch.setattr(requests, "get", lambda *a, **k: types.SimpleNamespace(
        status_code=200, json=lambda: {"status": "OK", "result": {}}))
    ai_utils.places_request("details", {"key": "k", "place_id": "p"}, restaurant_id=None, action="review_fetch")
    with pytest.raises(ai_utils.PlacesUnavailable, match="platform"):
        ai_utils.places_request("details", {"key": "k", "place_id": "p"}, restaurant_id=None, action="geocode")


def test_the_global_ai_pool_grows_thirty_dollars_a_paying_client():
    if not os.getenv("AI_GLOBAL_PER_CLIENT_USD"):
        assert ai_utils.AI_GLOBAL_PER_CLIENT_USD == 30.0
    assert 'os.getenv("AI_GLOBAL_MONTHLY_BUDGET_USD", "1500")' in inspect.getsource(ai_utils)


# ── #89 / #90: the memo and the lock ────────────────────────────────────────

def test_the_tier_is_read_once_a_minute_and_a_write_drops_it(db_path, monkeypatch):
    rid = create_restaurant(Restaurant(name="Memo Co", owner_email="m@x.test"), db_path=db_path)
    models.update_restaurant(rid, {"billing_status": "trial"}, db_path=db_path)
    reads = []
    real = ai_utils._read_budget_tier
    monkeypatch.setattr(ai_utils, "_read_budget_tier", lambda r, p=None: reads.append(r) or real(r, p))
    assert ai_utils._budget_tier(rid) == ai_utils.TIER_TRIAL
    assert ai_utils._budget_tier(rid) == ai_utils.TIER_TRIAL
    assert reads == [rid], "the tier was read again inside its minute"
    models.update_restaurant(rid, {"billing_status": "active"}, db_path=db_path)
    assert ai_utils._budget_tier(rid) == ai_utils.TIER_PAID, "an upgrade waited out a stale tier"
    assert reads == [rid, rid]


def test_a_failed_tier_read_fails_open_and_is_not_remembered(monkeypatch):
    monkeypatch.setattr(ai_utils, "_read_budget_tier", lambda r, p=None: None)
    assert ai_utils._budget_tier(99) == ai_utils.TIER_PAID
    assert not any(k[0] == "tier" for k in ai_utils._budget_memo)


def test_the_paying_client_count_is_memoised(db_path):
    n = ai_utils._paying_client_count()
    c = sqlite3.connect(db_path)
    try:
        c.execute("INSERT INTO restaurants (name, owner_email, billing_status) VALUES ('Raw Co', 'r@x.test', 'active')")
        c.commit()
    finally:
        c.close()
    assert ai_utils._paying_client_count() == n, "read again inside its minute"
    ai_utils.invalidate_budget_memo()
    assert ai_utils._paying_client_count() == n + 1


def test_spend_noted_from_many_threads_at_once_is_never_lost():
    month = ai_utils._current_windows()[1]
    with ai_utils._budget_lock:
        ai_utils._budget_cache[("g", month)] = (9e12, 0.0)     # far-future stamp: never refreshed here

    def burst():
        for _ in range(400):
            ai_utils.note_ai_spend(0.01, restaurant_id=None)
    threads = [threading.Thread(target=burst) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert ai_utils._budget_cache[("g", month)][1] == pytest.approx(32.0)
    assert "with _budget_lock:" in inspect.getsource(ai_utils.note_ai_spend)


# ── #91: the rollup ─────────────────────────────────────────────────────────

def _usage_at(db_path, stamp, cost=0.5):
    c = sqlite3.connect(db_path)
    c.execute("INSERT INTO ai_usage (restaurant_id, action, model, input_tokens, output_tokens, cost_usd, "
              "created_at, vendor, outcome) VALUES (1, 'a', 'claude-sonnet-5', 1, 1, ?, ?, 'anthropic', 'ok')",
              (cost, stamp))
    c.commit()
    c.close()


def test_the_rollup_finds_and_reads_days_by_range(db_path):
    ai_utils.init_ai_ops(db_path)
    _usage_at(db_path, "2026-10-01 08:00:00")
    _usage_at(db_path, "2026-10-01 23:59:59")
    _usage_at(db_path, "2026-10-02T00:00:00")
    out = ai_utils.rollup_usage(db_path)
    assert out["days"] >= 2
    c = sqlite3.connect(db_path)
    try:
        got = dict(c.execute("SELECT day, SUM(calls) FROM ai_usage_daily WHERE day IN ('2026-10-01','2026-10-02') "
                             "GROUP BY day").fetchall())
        plan = " ".join(str(r) for r in c.execute(
            "EXPLAIN QUERY PLAN SELECT 1 FROM ai_usage WHERE created_at >= ? AND created_at < ? LIMIT 1",
            ("2026-10-01", "2026-10-02")))
    finally:
        c.close()
    assert got == {"2026-10-01": 2, "2026-10-02": 1}
    assert "idx_ai_usage_created" in plan, plan
    for fn in (ai_utils._days_to_roll, ai_utils._roll_usage_day, ai_utils._roll_validation_day):
        assert "date(created_at)" not in inspect.getsource(fn), f"{fn.__name__} still scans by date()"


def test_the_rollup_is_not_a_boot_step(db_path):
    ai_utils.init_ai_ops(db_path)
    _usage_at(db_path, "2026-09-01 12:00:00")
    ai_utils.init_ai_ops(db_path)
    c = sqlite3.connect(db_path)
    try:
        assert c.execute("SELECT COUNT(*) FROM ai_usage_daily WHERE day='2026-09-01'").fetchone()[0] == 0
    finally:
        c.close()
    assert "rollup_usage" not in inspect.getsource(ai_utils.init_ai_ops).split('"""')[-1]
    assert ops._RETENTION_ROLLUP.get("ai_usage") == "ai_utils:rollup_usage", "the nightly rollup is gone"


# ── #6: the AI lane ─────────────────────────────────────────────────────────

_AI_LANE_JOBS = ("auto_draft_schedule", "weekly_plan", "recipe_drafts", "weekly_digests")


def test_the_long_ai_jobs_are_on_the_ai_lane():
    import jobs_registry
    loop = inspect.getsource(scheduler.scheduler_loop)
    assert set(scheduler._LANES) == {"intel", "ai"}
    for name in _AI_LANE_JOBS:
        assert f'_ops.run_in_lane("ai", "{name}"' in loop, name
        assert f'_ops.run_job("{name}"' not in loop, f"{name} still runs on the loop thread"
        assert jobs_registry.JOBS[name].get("lane") == "ai", name
    for claim in ("auto_draft_schedule", "weekly_plan", "recipe_drafts", "weekly_digest"):
        assert f'_ops.release_period("{claim}", f"{{today}}-{{now.hour}}")' in loop, \
            f"a busy lane does not give {claim}'s hour back"


class _Stop(BaseException):
    pass


@pytest.fixture
def loop(db_path, monkeypatch):
    import strategy_jobs
    ops._claim_fallback.clear()
    ran = []

    def rec(name):
        def fn(*a, **k):
            ran.append((name, threading.current_thread().name))
            return {"attempted": 0, "ok": 0, "failed": 0, "skipped": 0, "hit_bound": False}
        return fn
    for mod, attr in ((strategy_jobs, "run_auto_draft_schedules"), (strategy_jobs, "run_weekly_plan"),
                      (strategy_jobs, "run_recipe_drafts"), (scheduler, "run_weekly_digests"),
                      (scheduler, "_minute_duties")):
        monkeypatch.setattr(mod, attr, rec(attr))
    monkeypatch.setattr(scheduler._ops, "acquire_scheduler_lease", lambda *a, **k: True)
    monkeypatch.setattr(scheduler, "record_scheduler_heartbeat", lambda *a, **k: None)
    monkeypatch.setattr(scheduler, "run_health_checks", lambda *a, **k: None)
    monkeypatch.setattr(scheduler.time, "sleep", lambda s: (_ for _ in ()).throw(_Stop()))

    def tick(now, allow):
        monkeypatch.setattr(scheduler, "_chi_now", lambda: now)
        monkeypatch.setattr(scheduler._ops, "claim_period", lambda job, period: allow(job))
        del ran[:]
        with pytest.raises(_Stop):
            scheduler.scheduler_loop()
        for lane in scheduler._LANES.values():
            lane.join(5)
        return [r for r in ran if r[0] != "_minute_duties"]
    return tick


@pytest.mark.parametrize("claim, fn, now", [
    ("auto_draft_schedule", "run_auto_draft_schedules", datetime(2026, 10, 8, 6, 5)),
    ("weekly_plan", "run_weekly_plan", datetime(2026, 10, 5, 7, 5)),          # a Monday
    ("recipe_drafts", "run_recipe_drafts", datetime(2026, 10, 6, 5, 5)),      # a Tuesday
    ("weekly_digest", "run_weekly_digests", datetime(2026, 10, 5, 9, 5)),
])
def test_a_tick_runs_each_ai_job_on_the_lane_thread_not_the_loop(loop, claim, fn, now):
    ran = loop(now, lambda job: job == claim)
    assert ran == [(fn, "scheduler-lane-ai")], ran


def test_a_busy_ai_lane_gives_the_hour_back(loop, monkeypatch):
    released = []
    monkeypatch.setattr(scheduler._ops, "release_period", lambda job, period: released.append(job))
    monkeypatch.setattr(scheduler._LANES["ai"], "submit", lambda *a, **k: False)
    loop(datetime(2026, 10, 6, 5, 5), lambda job: job in ("recipe_drafts", "weekly_digest"))
    assert "recipe_drafts" in released and "weekly_digest" in released


# ── #52: the weekly digest is bounded and resumable ─────────────────────────

@pytest.fixture
def digest_world(db_path, monkeypatch):
    import emails
    import reporter
    import time_utils
    rids = [create_restaurant(Restaurant(name=f"Digest {i}", owner_email=f"d{i}@x.test"), db_path=db_path)
            for i in range(3)]
    monday = datetime(2026, 10, 5, 10, 0)
    monkeypatch.setattr(scheduler, "_chi_now", lambda: monday)
    monkeypatch.setattr(time_utils, "restaurant_now", lambda r=None, naive=False: monday)
    monkeypatch.setattr(models, "get_restaurants_for_digest", lambda day: [{"id": r} for r in rids])
    real_due = scheduler.local_due
    monkeypatch.setattr(scheduler, "local_due",
                        lambda r, h, claim_key=None, **k: real_due(r, h, claim_key=claim_key, now_local=monday))
    monkeypatch.setattr(scheduler, "get_owner_emails", lambda rid: [f"owner{rid}@x.test"])
    monkeypatch.setattr(reporter, "build_report_from_db", lambda rid, name, days=7: {"rid": rid})
    monkeypatch.setattr(reporter, "digest_has_data", lambda restaurant, report: True)
    monkeypatch.setattr(reporter, "render_digest", lambda rep, name, **k: {"html": "<p>x</p>", "subject": "s"})
    monkeypatch.setattr(reporter, "render_group_html", lambda items, **k: "<p>%d</p>" % len(items))
    monkeypatch.setattr(emails, "digest_preheader", lambda rep, r: "p")
    sent = []

    def deliver(payload=None, restaurant_id=None, email_type=None, log_send=True):
        sent.append(payload["to"][0])
        return emails.SendResult(True, message_id="m", status_code=200, attempts=1)
    monkeypatch.setattr(emails, "deliver", deliver)
    return rids, sent


def test_a_digest_pass_out_of_time_resumes_where_it_stopped(digest_world, monkeypatch):
    rids, sent = digest_world
    # One worker and no time at all: each pass finishes the unit it started
    # and stops — the rest give their day back.
    monkeypatch.setattr(scheduler, "DIGEST_WORKERS", 1)
    monkeypatch.setattr(scheduler, "DIGEST_MAX_SECONDS", 0)
    first = scheduler.run_weekly_digests()
    assert first["hit_bound"] is True and first["ok"] == 1 and len(sent) == 1
    for rid in rids:
        claimed = ops.period_claimed(f"weekly_digest:{rid}", "2026-10-05")
        assert claimed == (f"owner{rid}@x.test" in sent), f"restaurant {rid}'s day was not given back"
    scheduler.run_weekly_digests()
    scheduler.run_weekly_digests()
    last = scheduler.run_weekly_digests()
    assert sorted(sent) == sorted(f"owner{r}@x.test" for r in rids), "each owner exactly once"
    assert sent == [f"owner{r}@x.test" for r in sorted(rids)], "the cursor did not carry the next pass on"
    assert last["attempted"] == 0 and not last["hit_bound"]


def test_a_digest_pass_inside_its_bound_sends_everyone_once(digest_world, monkeypatch):
    rids, sent = digest_world
    monkeypatch.setattr(scheduler, "DIGEST_WORKERS", 2)
    out = scheduler.run_weekly_digests()
    assert out["ok"] == 3 and not out["hit_bound"] and sorted(sent) == sorted(f"owner{r}@x.test" for r in rids)
    assert scheduler.run_weekly_digests()["attempted"] == 0


def test_a_shared_address_still_gets_one_email_for_every_location(digest_world, monkeypatch):
    rids, sent = digest_world
    monkeypatch.setattr(scheduler, "get_owner_emails", lambda rid: ["same@x.test"])
    out = scheduler.run_weekly_digests()
    assert sent == ["same@x.test"] and out["ok"] == 1


# ── #8: the local snapshot ──────────────────────────────────────────────────

@pytest.fixture
def live(tmp_path, db_path, monkeypatch):
    monkeypatch.setattr(scheduler, "_chi_now", lambda: datetime(2026, 10, 7, 2, 0))
    monkeypatch.setenv("BACKUP_DIR", str(tmp_path / "backups"))
    monkeypatch.delenv("BACKUP_ENCRYPTION_KEY", raising=False)
    monkeypatch.setattr(ops, "page_operator", lambda *a, **k: None)
    monkeypatch.setattr(ops, "ping_healthcheck", lambda *a, **k: None)
    rid = create_restaurant(Restaurant(name="Snapshot Co", owner_email="s@x.test"), db_path=db_path)
    models.update_restaurant(rid, {"gmb_refresh_token": "tok-kept"}, db_path=db_path)
    return tmp_path / "backups"


def test_the_local_snapshot_is_gzip_and_restores_whole(live, tmp_path, db_path):
    with pytest.raises(scheduler.BackupFailed):      # no key: no off-site copy (#1), local still written
        scheduler.backup_db()
    snap = live / "cavnar_ai_backup_2026-10-07.db.gz"
    assert snap.exists() and snap.read_bytes()[:2] == b"\x1f\x8b"
    assert sorted(os.listdir(live)) == ["cavnar_ai_backup_2026-10-07.db.gz"], "the work copy was left behind"
    plain = str(tmp_path / "back.db")
    scheduler.gunzip_snapshot(str(snap), plain)
    c = sqlite3.connect(plain)
    try:
        assert c.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert c.execute("SELECT gmb_refresh_token FROM restaurants WHERE name='Snapshot Co'").fetchone()[0] \
            == "tok-kept", "the local snapshot must stay unredacted"
    finally:
        c.close()
    assert "VACUUM INTO" in inspect.getsource(scheduler._write_consistent_snapshot)


def test_restore_from_takes_a_gzipped_snapshot(live, tmp_path, monkeypatch):
    import db_restore
    with pytest.raises(scheduler.BackupFailed):
        scheduler.backup_db()
    target = str(tmp_path / "restored" / "reviews.db")
    os.makedirs(os.path.dirname(target))
    monkeypatch.setenv("RESTORE_FROM", str(live / "cavnar_ai_backup_2026-10-07.db.gz"))
    out = db_restore.restore_if_requested(target)
    assert out["restored"] and out["restaurants"] >= 1
    c = sqlite3.connect(target)
    try:
        assert c.execute("SELECT COUNT(*) FROM restaurants WHERE name='Snapshot Co'").fetchone()[0] == 1
    finally:
        c.close()
    assert not os.path.exists(target + ".restoring")


def test_a_corrupt_gzipped_snapshot_changes_nothing(tmp_path, monkeypatch):
    import db_restore
    bad = tmp_path / "cavnar_ai_backup_2026-10-07.db.gz"
    bad.write_bytes(gzip.compress(b"not a database at all" * 100))
    target = tmp_path / "reviews.db"
    target.write_bytes(b"live")
    monkeypatch.setenv("RESTORE_FROM", str(bad))
    with pytest.raises(Exception):
        db_restore.restore_if_requested(str(target))
    assert target.read_bytes() == b"live" and not (tmp_path / "reviews.db.restoring").exists()


def test_the_restore_drill_reads_a_gzipped_snapshot(live, monkeypatch):
    import emails
    monkeypatch.setattr(emails, "deliver", lambda **k: True)
    with pytest.raises(scheduler.BackupFailed):
        scheduler.backup_db()
    report = scheduler.run_restore_drill()
    assert report["snapshot"].endswith(".db.gz") and report["integrity"] == "ok" and report["restaurants"] >= 1


def test_the_newest_three_snapshots_are_kept_old_plain_ones_included(tmp_path):
    bdir = tmp_path / "b"
    bdir.mkdir()
    for name in ("cavnar_ai_backup_2026-09-30.db", "cavnar_ai_backup_2026-10-01.db",
                 "cavnar_ai_backup_2026-10-05.db.gz", "cavnar_ai_backup_2026-10-06.db.gz",
                 "cavnar_ai_backup_2026-10-07.db.gz", "unrelated.txt"):
        (bdir / name).write_bytes(b"x")
    scheduler._prune_old_backups(str(bdir))
    assert sorted(os.listdir(bdir)) == ["cavnar_ai_backup_2026-10-05.db.gz", "cavnar_ai_backup_2026-10-06.db.gz",
                                        "cavnar_ai_backup_2026-10-07.db.gz", "unrelated.txt"]
    if not os.getenv("BACKUP_RETAIN_COUNT"):
        assert scheduler.BACKUP_RETAIN_COUNT == 3


def test_the_free_space_precondition_is_the_new_process_s(live, monkeypatch):
    import shutil as _sh
    if not os.getenv("BACKUP_FREE_SPACE_FACTOR"):
        assert scheduler.BACKUP_FREE_SPACE_FACTOR == 1.5
    db = os.path.getsize(models.DB_PATH)
    # Room for 1.6x the database is enough to start now; it was 3.5x.
    monkeypatch.setattr(_sh, "disk_usage", lambda p: types.SimpleNamespace(free=int(1.6 * db), total=10 ** 12,
                                                                           used=0))
    with pytest.raises(scheduler.BackupFailed, match="BACKUP_ENCRYPTION_KEY"):
        scheduler.backup_db()
    assert (live / "cavnar_ai_backup_2026-10-07.db.gz").exists()


def test_the_offsite_copy_checks_its_own_room(live, monkeypatch):
    import shutil as _sh
    from cryptography.fernet import Fernet
    monkeypatch.setenv("BACKUP_ENCRYPTION_KEY", Fernet.generate_key().decode())
    monkeypatch.setattr(scheduler, "_resend_key", lambda: "re_test")
    calls = {"n": 0}
    real = _sh.disk_usage

    def usage(p):
        calls["n"] += 1
        u = real(p)
        # Plenty to start; nothing left once the snapshot is written.
        return u if calls["n"] == 1 else types.SimpleNamespace(free=0, total=u.total, used=u.total)
    monkeypatch.setattr(_sh, "disk_usage", usage)
    with pytest.raises(scheduler.BackupFailed, match="encrypted copy"):
        scheduler.backup_db()
    assert sorted(os.listdir(live)) == ["cavnar_ai_backup_2026-10-07.db.gz"], \
        "the local snapshot stands; no scratch or encrypted file is left"
