"""Fix round G #104 — an AI outage pages the operator, and the public status
row reads what the AI layer is doing, not whether a key is set.

Breaker transitions, budget stops and credential / credit / overload
failures are recorded in ai_health_events; paging goes through
ops.alert_will, once per cooldown, only where the scheduler runs.
"""
import sqlite3
import types

import anthropic
import httpx
import pytest

import ai_utils
import models
import ops


@pytest.fixture(autouse=True)
def _db(db_path, monkeypatch):
    real = models.get_conn
    monkeypatch.setattr(models, "get_conn", lambda *a, **k: real(db_path))
    monkeypatch.setattr(models, "DB_PATH", db_path)
    monkeypatch.setattr(ai_utils.time, "sleep", lambda s: None)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    ai_utils.reset_process_state(db_path)
    yield
    ai_utils.reset_process_state(db_path)


@pytest.fixture
def pages(monkeypatch):
    """Paging on (as on Railway), synchronously, with alert_will recorded."""
    import scheduler
    import threading
    sent = []
    monkeypatch.setattr(scheduler, "scheduling_allowed", lambda: True)
    monkeypatch.setattr(ops, "alert_will", lambda subject, lines: sent.append((subject, list(lines))) or True)

    class _Now(threading.Thread):
        def start(self):
            self.run()
    monkeypatch.setattr(ai_utils.threading, "Thread", _Now)
    return sent


def _events(db_path, vendor=None):
    conn = sqlite3.connect(db_path)
    try:
        sql = "SELECT vendor, event, scope FROM ai_health_events"
        args = ()
        if vendor:
            sql += " WHERE vendor=?"
            args = (vendor,)
        return conn.execute(sql + " ORDER BY id", args).fetchall()
    finally:
        conn.close()


def _status_error(cls, code, text="boom"):
    req = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    return cls(text, response=httpx.Response(code, request=req), body=None)


class _Client:
    def __init__(self, exc):
        self.messages = self
        self.exc = exc

    def create(self, **kw):
        raise self.exc


def _ok_client():
    class _U:
        input_tokens, output_tokens = 10, 10
        cache_creation_input_tokens = cache_read_input_tokens = 0

    class _C:
        messages = None

        def create(self, **kw):
            return types.SimpleNamespace(usage=_U(), stop_reason="end_turn", content=[])
    c = _C()
    c.messages = c
    return c


def test_the_breaker_opening_is_recorded_and_pages_once(db_path, pages):
    for _ in range(ai_utils.CB_FAILURE_THRESHOLD + 3):
        ai_utils._breaker_record("anthropic", False, reason="server")
    assert [e[1] for e in _events(db_path, "anthropic")].count("breaker_open") == 1
    assert len([p for p in pages if "paused" in p[0]]) == 1
    ai_utils._breaker_record("anthropic", True)
    assert _events(db_path, "anthropic")[-2][1] == "breaker_close" or \
        "breaker_close" in [e[1] for e in _events(db_path, "anthropic")]


def test_a_revoked_key_is_recorded_and_pages(db_path, pages):
    with pytest.raises(anthropic.AuthenticationError):
        ai_utils.create_with_retry(_Client(_status_error(anthropic.AuthenticationError, 401)),
                                   model="claude-sonnet-5", max_tokens=5, restaurant_id=None, action="t")
    events = [e[1] for e in _events(db_path, "anthropic")]
    assert "auth_error" in events and "breaker_open" in events
    assert any("rejected the API key" in p[0] for p in pages)


def test_an_empty_credit_balance_is_recorded_as_such(db_path, pages):
    exc = _status_error(anthropic.BadRequestError, 400, "Your credit balance is too low to access the API.")
    with pytest.raises(anthropic.BadRequestError):
        ai_utils.create_with_retry(_Client(exc), model="claude-sonnet-5", max_tokens=5, action="t")
    assert ("anthropic", "credit_error", "credit") in _events(db_path, "anthropic")


def test_overloads_that_use_up_their_retries_are_recorded(db_path, pages):
    from anthropic import _exceptions
    with pytest.raises(Exception):
        ai_utils.create_with_retry(_Client(_status_error(_exceptions.OverloadedError, 529)),
                                   model="claude-sonnet-5", max_tokens=5, action="t", retries=1)
    assert ("anthropic", "overloaded", "overloaded") in _events(db_path, "anthropic")


def test_pages_are_cooled_down_and_never_sent_off_railway(db_path, monkeypatch):
    sent = []
    monkeypatch.setattr(ops, "alert_will", lambda *a: sent.append(a) or True)
    import scheduler
    monkeypatch.setattr(scheduler, "scheduling_allowed", lambda: False)
    assert ai_utils._page("t:anthropic", "subject", ["line"]) is False
    monkeypatch.setattr(scheduler, "scheduling_allowed", lambda: True)
    monkeypatch.setattr(ops, "claim_cooldown", lambda key, minutes: False)
    assert ai_utils._page("t:anthropic", "subject", ["line"]) is False
    assert sent == []


def test_the_global_budget_pages_at_eighty_percent_and_at_the_stop(db_path, pages, monkeypatch):
    monkeypatch.setattr(ai_utils, "global_monthly_budget", lambda db_path=None: 10.0)
    ai_utils.log_ai_usage(None, "t", "claude-sonnet-5", 0, int(8.5 / 10 * 1_000_000))
    ai_utils._budget_cache.clear()
    assert ai_utils.ai_budget_exceeded(None) is None
    assert any("past 80%" in p[0] for p in pages)
    ai_utils.log_ai_usage(None, "t", "claude-sonnet-5", 0, int(2 / 10 * 1_000_000))
    ai_utils._budget_cache.clear()
    with pytest.raises(ai_utils.AIBudgetExceeded):
        ai_utils.create_with_retry(_ok_client(), model="claude-sonnet-5", max_tokens=5, action="t")
    assert any("paused for every client" in p[0] for p in pages)
    assert "budget_stop" in [e[1] for e in _events(db_path)]


def test_ai_health_reports_breakers_error_rate_and_the_last_credential_failure(db_path):
    for _ in range(3):
        ai_utils.log_ai_usage(None, "t", "claude-sonnet-5", 10, 10)
    for _ in range(2):
        ai_utils.log_ai_usage(None, "t", "claude-sonnet-5", 0, 0, status="error", error="x")
    ai_utils.record_health_event("anthropic", "auth_error", scope="auth", detail="401")
    h = ai_utils.ai_health()
    a = h["vendors"]["anthropic"]
    assert (a["calls_1h"], a["errors_1h"], a["error_rate_1h"]) == (5, 2, 40.0)
    assert a["breaker"] == "closed" and a["key_configured"] is True
    assert h["last_auth_error"]["event"] == "auth_error"
    assert set(h["vendors"]) == {"anthropic", "perplexity", "google_places"}
    assert "global_month" in h["budget"] and "places_month" in h["budget"]


@pytest.mark.parametrize("ok,err,state", [(10, 0, "operational"), (7, 3, "degraded"), (4, 6, "outage")])
def test_the_status_row_follows_the_last_hours_error_rate(db_path, ok, err, state):
    for _ in range(ok):
        ai_utils.log_ai_usage(None, "t", "claude-sonnet-5", 10, 10)
    for _ in range(err):
        ai_utils.log_ai_usage(None, "t", "claude-sonnet-5", 0, 0, status="error", error="x")
    assert ai_utils.ai_service_status()[0] == state


def test_the_status_row_is_an_outage_after_a_credential_failure_with_nothing_since(db_path):
    ai_utils.log_ai_usage(None, "t", "claude-sonnet-5", 10, 10)
    conn = sqlite3.connect(db_path)
    conn.execute("UPDATE ai_usage SET created_at=datetime('now','-5 minutes')")
    conn.commit()
    conn.close()
    ai_utils.record_health_event("anthropic", "auth_error", scope="auth", detail="401")
    state, message = ai_utils.ai_service_status()
    assert state == "outage" and "401" not in (message or ""), "the public row carries no provider detail"


def test_status_manager_reads_the_ai_layer_not_the_key(db_path, monkeypatch):
    import status_manager
    seen = {}
    monkeypatch.setattr(status_manager, "update_service_status", lambda k, s, m=None: seen.update({k: (s, m)}))
    ai_utils.trip_breaker("anthropic", "server")
    status_manager._check_ai_drafting()
    assert seen["ai_drafting"][0] == "outage"
    monkeypatch.setenv("ANTHROPIC_API_KEY", "")
    status_manager._check_ai_drafting()
    assert seen["ai_drafting"] == ("outage", "API key not configured")


def test_a_breaker_that_stays_open_pages_again_from_the_status_check(db_path, pages):
    ai_utils.record_health_event("perplexity", "breaker_open", scope="auth", detail="401")
    conn = sqlite3.connect(db_path)
    conn.execute("UPDATE ai_health_events SET created_at=datetime('now','-30 minutes')")
    conn.commit()
    conn.close()
    assert ai_utils.check_ai_alerts() == ["perplexity"]
    assert any("failing for 30 minutes" in p[0] for p in pages)


def test_the_reset_breaker_action_closes_it_and_is_recorded(db_path):
    ai_utils.trip_breaker("perplexity", "auth")
    ai_utils.reset_breaker("perplexity", actor="will")
    assert ai_utils.breaker_state("perplexity")[0] == "closed"
    assert ("perplexity", "breaker_reset", None) in _events(db_path, "perplexity")
