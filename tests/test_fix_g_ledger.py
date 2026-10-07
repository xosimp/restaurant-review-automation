"""Fix round G — the AI ledger records every outcome and who caused it.

#48  a call stopped before it reaches a provider (budget, breaker, the
     readiness gate) is a zero-cost 'blocked' row with its reason.
#52  refusals, truncations and unparseable replies are their own outcomes,
     with the stop reason, attempts, total latency and the request id.
#68  every row names its vendor; Perplexity is priced as sonar, not at the
     unknown-model rate.
#122 trials have their own ceiling; an admin's call does not spend the
     client's.
#148 trigger / actor / correlation id / price version on every row; the
     pre-9/22 Sonnet 5 rows are repriced once.
#96  the rate limiter's window lives in the database.
No test here reaches a real provider.
"""
import sqlite3
import types

import anthropic
import httpx
import pytest

import ai_utils
import models
from models import Restaurant, create_restaurant


@pytest.fixture(autouse=True)
def _db(db_path, monkeypatch):
    real = models.get_conn
    monkeypatch.setattr(models, "get_conn", lambda *a, **k: real(db_path))
    monkeypatch.setattr(models, "DB_PATH", db_path)
    monkeypatch.setattr(ai_utils.time, "sleep", lambda s: None)
    ai_utils.reset_process_state(db_path)
    yield
    ai_utils.reset_process_state(db_path)


def _rid(db_path, billing_status="active", is_demo=0, name="Ledger Co"):
    rid = create_restaurant(Restaurant(name=name, owner_email=f"{name.split()[0].lower()}@x.test"), db_path=db_path)
    conn = sqlite3.connect(db_path)
    conn.execute("UPDATE restaurants SET billing_status=?, is_demo=? WHERE id=?", (billing_status, is_demo, rid))
    conn.commit()
    conn.close()
    ai_utils._budget_cache.clear()
    return rid


def _rows(db_path, where="1=1", args=()):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in conn.execute(f"SELECT * FROM ai_usage WHERE {where} ORDER BY id", args)]
    finally:
        conn.close()


class _Usage:
    def __init__(self, i=1000, o=500):
        self.input_tokens, self.output_tokens = i, o
        self.cache_creation_input_tokens = self.cache_read_input_tokens = 0


def _msg(text="ok", stop="end_turn", request_id="req_abc"):
    m = types.SimpleNamespace(usage=_Usage(), stop_reason=stop, _request_id=request_id,
                              content=[types.SimpleNamespace(type="text", text=text)])
    return m


class _Client:
    def __init__(self, replies):
        self.replies = list(replies)
        self.messages = self
        self.calls = 0

    def create(self, **kw):
        self.calls += 1
        r = self.replies.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


def _status_error(cls, code):
    req = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    return cls("boom", response=httpx.Response(code, request=req), body=None)


def _overloaded():
    from anthropic import _exceptions
    return _status_error(_exceptions.OverloadedError, 529)


# ── #52 outcomes ────────────────────────────────────────────────────────────

@pytest.mark.parametrize("stop,outcome,status", [("end_turn", "ok", "ok"), ("refusal", "refused", "error"),
                                                 ("max_tokens", "truncated", "error")])
def test_a_returned_message_is_filed_by_its_stop_reason(db_path, stop, outcome, status):
    rid = _rid(db_path)
    ai_utils.create_with_retry(_Client([_msg(stop=stop)]), model="claude-sonnet-5", max_tokens=10,
                               restaurant_id=rid, action="t_stop", messages=[{"role": "user", "content": "hi"}])
    (row,) = _rows(db_path, "action='t_stop'")
    assert (row["outcome"], row["status"], row["stop_reason"]) == (outcome, status, stop)
    assert row["request_id"] == "req_abc" and row["attempts"] == 1
    assert row["cost_usd"] > 0, "a refusal and a truncation are billed like any answer"
    assert row["vendor"] == "anthropic" and row["price_version"] == ai_utils.PRICE_VERSION
    assert row["call_id"]


def test_a_success_after_two_retries_records_its_attempts_and_the_whole_wait(db_path, monkeypatch):
    rid = _rid(db_path)
    clock = {"t": 1000.0}
    monkeypatch.setattr(ai_utils.time, "time", lambda: clock["t"])

    def sleep(s):
        clock["t"] += s
    monkeypatch.setattr(ai_utils.time, "sleep", sleep)
    conn_err = anthropic.APIConnectionError(request=httpx.Request("POST", "https://api.anthropic.com"))
    ai_utils.create_with_retry(_Client([conn_err, conn_err, _msg()]), model="claude-sonnet-5", max_tokens=10,
                               restaurant_id=rid, action="t_retry")
    (row,) = _rows(db_path, "action='t_retry'")
    assert row["attempts"] == 3 and row["outcome"] == "ok"
    assert row["latency_ms"] == int((1.5 + 1.5 ** 2) * 1000), "latency includes the backoff"


def test_an_unparseable_reply_is_refiled_against_its_row(db_path):
    rid = _rid(db_path)
    m = ai_utils.create_with_retry(_Client([_msg(text="no json here")]), model="claude-sonnet-5", max_tokens=10,
                                   restaurant_id=rid, action="t_parse")
    with pytest.raises(ValueError):
        ai_utils.parse_json_reply(ai_utils.extract_text(m), expect=dict, message=m)
    (row,) = _rows(db_path, "action='t_parse'")
    assert row["outcome"] == "unparseable" and row["status"] == "error" and row["cost_usd"] > 0


def test_every_call_site_parse_failure_the_packet_names_passes_its_message():
    import inspect
    import analyser
    import dsr.narrative as narrative
    import food_cost_intelligence
    import review_intelligence
    # The DSR narrative's answer is parsed in narrative.finish since AI cost
    # audit 10/7/26 #20 — the one judge of a synchronous answer and a batch
    # answer alike; _write only makes the call. Since the dsr_narrative run
    # (AI orchestration, 10/7/26) the parsing is narrative._judge, which
    # finish(), the run's attempts and land_batch() all go through. The
    # diagnoses' answers likewise in finish_diagnosis since AI cost audit
    # 10/7/26 #58 — shared by the 6am call and the batched answer.
    for fn in (analyser.analyse_review, review_intelligence.finish_diagnosis,
               food_cost_intelligence.finish_diagnosis, narrative._judge):
        assert "message=m" in inspect.getsource(fn), fn.__qualname__


# ── #48 blocked calls ───────────────────────────────────────────────────────

def test_a_budget_stop_is_a_blocked_row_with_its_reason(db_path, monkeypatch):
    rid = _rid(db_path)
    monkeypatch.setattr(ai_utils, "ai_budget_exceeded", lambda *a, **k: "daily budget")
    client = _Client([_msg()])
    with pytest.raises(ai_utils.AIBudgetExceeded):
        ai_utils.create_with_retry(client, model="claude-sonnet-5", max_tokens=10, restaurant_id=rid,
                                   action="t_budget")
    assert client.calls == 0
    (row,) = _rows(db_path, "action='t_budget'")
    assert (row["outcome"], row["status"], row["reason"], row["cost_usd"]) == ("blocked", "blocked", "budget", 0.0)
    assert "daily budget" in row["error"]


def test_the_readiness_gate_and_the_breaker_leave_blocked_rows(db_path):
    rid = _rid(db_path)
    with pytest.raises(ai_utils.DataNotReady):
        ai_utils.create_with_retry(_Client([_msg()]), model="claude-sonnet-5", max_tokens=10, restaurant_id=rid,
                                   action="t_gate", readiness={"decision": "refuse", "reason": "sales are stale"})
    ai_utils.trip_breaker("anthropic", "auth")
    with pytest.raises(ai_utils.AIProviderDown):
        ai_utils.create_with_retry(_Client([_msg()]), model="claude-sonnet-5", max_tokens=10, restaurant_id=rid,
                                   action="t_breaker")
    assert _rows(db_path, "action='t_gate'")[0]["reason"] == "data_not_ready"
    assert _rows(db_path, "action='t_breaker'")[0]["reason"] == "breaker"


def test_a_loop_hitting_a_stop_is_one_row_with_a_count_not_a_thousand_rows(db_path):
    rid = _rid(db_path)
    for _ in range(25):
        ai_utils.log_blocked(rid, "t_loop", "claude-sonnet-5", "budget", detail="daily budget")
    rows = _rows(db_path, "action='t_loop'")
    assert len(rows) == 1 and rows[0]["attempts"] == 25


# ── #104 529 is retryable and counted ───────────────────────────────────────

def test_an_overload_is_retried_and_counts_toward_the_breaker(db_path):
    rid = _rid(db_path)
    client = _Client([_overloaded(), _overloaded(), _overloaded()])
    with pytest.raises(Exception):
        ai_utils.create_with_retry(client, model="claude-sonnet-5", max_tokens=10, restaurant_id=rid,
                                   action="t_529", retries=2)
    assert client.calls == 3, "a 529 was not retried"
    assert ai_utils.breakers()["anthropic"]["failures"] == 1
    (row,) = _rows(db_path, "action='t_529'")
    assert row["reason"] == "overloaded" and row["attempts"] == 3


def test_a_revoked_key_is_not_retried_and_trips_the_breaker(db_path):
    rid = _rid(db_path)
    client = _Client([_status_error(anthropic.AuthenticationError, 401)])
    with pytest.raises(anthropic.AuthenticationError):
        ai_utils.create_with_retry(client, model="claude-sonnet-5", max_tokens=10, restaurant_id=rid,
                                   action="t_auth")
    assert client.calls == 1
    assert ai_utils.breaker_state("anthropic")[0] == "open"
    assert _rows(db_path, "action='t_auth'")[0]["reason"] == "auth"


# ── #68 vendor and price ────────────────────────────────────────────────────

def test_perplexity_is_priced_as_sonar_and_filed_under_its_own_vendor(db_path):
    rid = _rid(db_path)
    cost = ai_utils.log_api_call(rid, "ai_visibility", "perplexity-search", model="sonar", calls=1,
                                 input_tokens=40, output_tokens=100)
    (row,) = _rows(db_path, "action='ai_visibility'")
    assert row["vendor"] == "perplexity" and row["model"] == "sonar"
    assert cost == pytest.approx(0.005 + 140 / 1_000_000)
    assert ai_utils._price_for("perplexity-search") == ai_utils._price_for("sonar") == (1.0, 1.0)
    assert ai_utils.vendor_for("google-places-details") == "google_places"


# ── #148 price version and the one-time reprice ────────────────────────────

def test_legacy_sonnet_rows_written_at_the_old_rate_are_repriced_once(db_path):
    rid = _rid(db_path)
    conn = sqlite3.connect(db_path)
    old = ai_utils._estimate_cost("claude-sonnet-5", 1000, 1000, rates=(3.0, 15.0))
    new = ai_utils._estimate_cost("claude-sonnet-5", 1000, 1000)
    conn.execute("INSERT INTO ai_usage (restaurant_id, action, model, input_tokens, output_tokens, cost_usd, status, "
                 "created_at) VALUES (?, 'old', 'claude-sonnet-5', 1000, 1000, ?, 'ok', '2026-09-15 12:00:00')",
                 (rid, old))
    conn.execute("INSERT INTO ai_usage (restaurant_id, action, model, input_tokens, output_tokens, cost_usd, status, "
                 "created_at) VALUES (?, 'fine', 'claude-sonnet-5', 1000, 1000, ?, 'ok', '2026-09-25 12:00:00')",
                 (rid, new))
    conn.commit()
    conn.close()
    assert ai_utils._reprice_legacy_rows(db_path=db_path) == 1
    old_row = _rows(db_path, "action='old'")[0]
    assert old_row["cost_usd"] == pytest.approx(new) and old_row["price_version"] == ai_utils.REPRICED_VERSION
    fine = _rows(db_path, "action='fine'")[0]
    assert fine["cost_usd"] == pytest.approx(new) and fine["price_version"] == "legacy"
    assert ai_utils._reprice_legacy_rows(db_path=db_path) == 0, "it runs once"


def test_legacy_perplexity_rows_are_repriced_and_an_unknown_model_is_left_alone(db_path):
    rid = _rid(db_path)
    fee = ai_utils._PER_CALL_PRICING["perplexity-search"]
    at_unknown = fee + ai_utils._estimate_cost("perplexity-search", 40, 100, rates=(10.0, 50.0))
    guessed = ai_utils._estimate_cost("claude-x-preview", 1000, 1000, rates=(3.0, 15.0))
    conn = sqlite3.connect(db_path)
    conn.execute("INSERT INTO ai_usage (restaurant_id, action, model, input_tokens, output_tokens, cost_usd, status) "
                 "VALUES (?, 'aivis', 'perplexity-search', 40, 100, ?, 'ok')", (rid, at_unknown))
    conn.execute("INSERT INTO ai_usage (restaurant_id, action, model, input_tokens, output_tokens, cost_usd, status) "
                 "VALUES (?, 'odd', 'claude-x-preview', 1000, 1000, ?, 'ok')", (rid, guessed))
    conn.commit()
    conn.close()
    assert ai_utils._reprice_legacy_rows(db_path=db_path) == 1
    assert _rows(db_path, "action='aivis'")[0]["cost_usd"] == pytest.approx(fee + 140 / 1_000_000)
    odd = _rows(db_path, "action='odd'")[0]
    assert odd["cost_usd"] == pytest.approx(guessed) and odd["price_version"] == "legacy", \
        "history is never rewritten at the fail-safe unknown-model rate"


# ── #148 attribution ────────────────────────────────────────────────────────

def test_rows_carry_trigger_actor_and_correlation_from_ai_context(db_path):
    rid = _rid(db_path)
    with ai_utils.ai_context(trigger="scheduler", actor_user_id=7, correlation_id="run:1"):
        ai_utils.create_with_retry(_Client([_msg(), _msg()]), model="claude-sonnet-5", max_tokens=10,
                                   restaurant_id=rid, action="t_ctx")
    (row,) = _rows(db_path, "action='t_ctx'")
    assert (row["trigger"], row["actor_user_id"], row["correlation_id"]) == ("scheduler", 7, "run:1")


def test_off_a_request_the_stack_names_the_scheduler(db_path):
    rid = _rid(db_path)
    ns = {}
    exec(compile("def job(cb):\n    return cb()\n", "scheduler.py", "exec"), ns)
    ns["job"](lambda: ai_utils.create_with_retry(_Client([_msg()]), model="claude-sonnet-5", max_tokens=10,
                                                  restaurant_id=rid, action="t_stack"))
    assert _rows(db_path, "action='t_stack'")[0]["trigger"] == "scheduler"


def test_an_admin_request_is_attributed_to_the_admin(db_path):
    from flask import Flask
    rid = _rid(db_path)
    app = Flask(__name__)
    with app.test_request_context("/admin/seed-reviews/1", method="POST"):
        ai_utils.create_with_retry(_Client([_msg()]), model="claude-sonnet-5", max_tokens=10,
                                   restaurant_id=rid, action="t_admin")
    assert _rows(db_path, "action='t_admin'")[0]["trigger"] == "admin"


def test_the_weekly_plan_logs_under_its_own_action(db_path, monkeypatch):
    import ask_cavnar
    import inspect
    import strategy_jobs
    src = inspect.getsource(strategy_jobs.run_weekly_plan)
    assert 'action="weekly_plan"' in src
    assert 'action=action' in inspect.getsource(ask_cavnar.ask_with_tools)


# ── #122 ceilings ───────────────────────────────────────────────────────────

def test_a_trial_has_its_own_ceiling_and_a_demo_keeps_the_unpaid_one(db_path):
    trial = _rid(db_path, "trial", name="Trial Co")
    demo = _rid(db_path, "trial", is_demo=1, name="Demo Co")
    st = ai_utils.ai_budget_status(trial)
    assert (st["tier"], st["day"]["budget"], st["month"]["budget"]) == (
        "trial", ai_utils.AI_TRIAL_DAILY_BUDGET_USD, ai_utils.AI_TRIAL_MONTHLY_BUDGET_USD)
    assert (5.0, 50.0) == (ai_utils.AI_TRIAL_DAILY_BUDGET_USD, ai_utils.AI_TRIAL_MONTHLY_BUDGET_USD)
    assert ai_utils.ai_budget_status(demo)["tier"] == "unpaid"
    assert "trial" in ai_utils._TIER_LABELS["trial"][0]


def test_a_budget_status_warns_at_eighty_percent(db_path):
    rid = _rid(db_path, "trial")
    ai_utils.log_ai_usage(rid, "t", "claude-sonnet-5", 0, int(4.2 / 10 * 1_000_000))
    ai_utils._budget_cache.clear()
    st = ai_utils.ai_budget_status(rid)
    assert st["day"]["warn"] and not st["day"]["over"] and st["day"]["pct"] == pytest.approx(84.0)


def test_an_admin_triggered_call_does_not_spend_the_clients_ceiling(db_path):
    rid = _rid(db_path, "trial")
    with ai_utils.ai_context(trigger="admin"):
        ai_utils.log_ai_usage(rid, "draft_response", "claude-sonnet-5", 0, int(6 / 10 * 1_000_000))
    ai_utils._budget_cache.clear()
    st = ai_utils.ai_budget_status(rid)
    assert st["day"]["spend"] == 0.0, "the operator's re-run spent the client's ceiling"
    assert ai_utils.ai_budget_exceeded(rid) is None
    assert ai_utils._spend_since("2000-01-01", paid_only=True) == pytest.approx(6.0), \
        "admin spend is bounded by the global pool"


def test_places_spend_never_draws_down_the_ai_ceiling(db_path):
    rid = _rid(db_path, "trial")
    for _ in range(200):
        ai_utils.log_api_call(rid, "weather_geocode", "google-places-details")
    ai_utils._budget_cache.clear()
    assert ai_utils.ai_budget_status(rid)["day"]["spend"] == 0.0
    assert ai_utils.places_budget_status(rid)["day"]["spend"] == pytest.approx(200 * 0.017)
    assert ai_utils.places_budget_exceeded(rid) == "daily Google Places budget"


# ── #96 the rate limiter is durable ─────────────────────────────────────────

def test_the_rate_limit_survives_the_process_forgetting_it(db_path):
    assert ai_utils.ai_rate_limited("regen:1", max_calls=2, window_secs=60) is False
    ai_utils._ai_call_log.clear()           # a second worker, or a restart
    assert ai_utils.ai_rate_limited("regen:1", max_calls=2, window_secs=60) is False
    ai_utils._ai_call_log.clear()
    assert ai_utils.ai_rate_limited("regen:1", max_calls=2, window_secs=60) is True
    conn = sqlite3.connect(db_path)
    hits = conn.execute("SELECT bucket, hits FROM ai_rate_hits").fetchall()
    conn.close()
    assert hits == [("regen", 1)], "hits are counted by bucket, never by key (which can hold an IP)"


def test_expired_windows_are_pruned(db_path, monkeypatch):
    t = {"now": 1_000_000.0}
    monkeypatch.setattr(ai_utils.time, "time", lambda: t["now"])
    ai_utils.ai_rate_limited("guestoptin:203.0.113.9", max_calls=5, window_secs=300)
    t["now"] += 10_000
    ai_utils._rate_prune_state["at"] = 0.0
    ai_utils.ai_rate_limited("other:1", max_calls=5, window_secs=60)
    conn = sqlite3.connect(db_path)
    keys = [r[0] for r in conn.execute("SELECT key FROM ai_rate_events")]
    conn.close()
    assert keys == ["other:1"]


def test_without_the_table_the_process_window_still_limits_and_stays_bounded(monkeypatch):
    def broken(*a, **k):
        raise sqlite3.OperationalError("no such table: ai_rate_events")

    class _C:
        def execute(self, *a, **k):
            return broken()

        def rollback(self):
            pass

        def close(self):
            pass
    monkeypatch.setattr(ai_utils, "_conn", lambda *a, **k: _C())
    assert [ai_utils.ai_rate_limited("k:1", 2, 60) for _ in range(3)] == [False, False, True]
    monkeypatch.setattr(ai_utils, "_AI_CALL_LOG_MAX_KEYS", 10)
    for i in range(30):
        ai_utils.ai_rate_limited(f"ip:{i}", 5, 60)
    assert len(ai_utils._ai_call_log) <= 11
