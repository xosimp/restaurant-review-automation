"""AI spend has a ceiling, not just a burst limit.

From the pre-launch audit: ai_rate_limited() caps six calls a minute, which
stops a client mashing "Regenerate" — and says nothing about the month. Six
a minute all day every day is a bill nobody notices until the card is
charged, and a loop inside a scheduled job (or a client scripting the API)
had no ceiling at all. The check sits in create_with_retry, the one place
every Claude call in the app passes through.
"""
import pytest

import ai_utils
import models
from ai_utils import AIBudgetExceeded, ai_budget_exceeded, ai_budget_status, create_with_retry, log_ai_usage


@pytest.fixture(autouse=True)
def _isolate(db_path, monkeypatch):
    real = models.get_conn
    monkeypatch.setattr(models, "get_conn", lambda *a, **k: real(db_path))
    monkeypatch.setattr(models, "DB_PATH", db_path)
    ai_utils._budget_cache.clear()
    yield
    ai_utils._budget_cache.clear()


class _FakeUsage:
    input_tokens = 1000
    output_tokens = 1000


class _FakeMessage:
    usage = _FakeUsage()
    content = []


def _client(recorder=None):
    class _Messages:
        def create(self, **kwargs):
            if recorder is not None:
                recorder.append(kwargs)
            return _FakeMessage()

    class _Client:
        messages = _Messages()

    return _Client()


def _spend(rid, dollars):
    """Write a row worth roughly `dollars` at Sonnet output pricing."""
    log_ai_usage(rid, "test", "claude-sonnet-5", 0, int(dollars / 15.00 * 1_000_000))
    ai_utils._budget_cache.clear()


# ── the ceilings ────────────────────────────────────────────────────────────

def test_nothing_spent_is_under_every_budget(db_path):
    assert ai_budget_exceeded(1) is None
    status = ai_budget_status(1)
    assert status["day"]["over"] is False and status["global_month"]["over"] is False


def test_a_client_past_its_daily_budget_is_stopped(db_path, monkeypatch):
    monkeypatch.setattr(ai_utils, "AI_DAILY_BUDGET_USD", 1.00)
    _spend(1, 1.50)
    assert ai_budget_exceeded(1) == "daily budget"


def test_one_client_overspending_does_not_stop_another(db_path, monkeypatch):
    monkeypatch.setattr(ai_utils, "AI_DAILY_BUDGET_USD", 1.00)
    monkeypatch.setattr(ai_utils, "AI_GLOBAL_MONTHLY_BUDGET_USD", 1000.00)
    _spend(1, 5.00)
    assert ai_budget_exceeded(1) == "daily budget"
    assert ai_budget_exceeded(2) is None


def test_the_global_ceiling_catches_spend_spread_across_clients(db_path, monkeypatch):
    """The real backstop: a hundred separately-under-budget clients still add
    up to one bill."""
    monkeypatch.setattr(ai_utils, "AI_DAILY_BUDGET_USD", 1000.00)
    monkeypatch.setattr(ai_utils, "AI_MONTHLY_BUDGET_USD", 1000.00)
    monkeypatch.setattr(ai_utils, "AI_GLOBAL_MONTHLY_BUDGET_USD", 3.00)
    for rid in range(1, 5):
        _spend(rid, 1.00)
    assert ai_budget_exceeded(9) == "monthly budget across all clients"


def test_a_budget_of_zero_disables_that_ceiling(db_path, monkeypatch):
    monkeypatch.setattr(ai_utils, "AI_DAILY_BUDGET_USD", 0)
    monkeypatch.setattr(ai_utils, "AI_MONTHLY_BUDGET_USD", 0)
    monkeypatch.setattr(ai_utils, "AI_GLOBAL_MONTHLY_BUDGET_USD", 0)
    _spend(1, 500.00)
    assert ai_budget_exceeded(1) is None


def test_the_check_fails_open(db_path, monkeypatch):
    """A broken ledger query must not take every AI feature offline — that
    would be a worse outage than the overspend it guards against."""
    monkeypatch.setattr(ai_utils, "_spend_since",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("db down")))
    assert ai_budget_exceeded(1) is None


# ── enforcement at the one chokepoint ───────────────────────────────────────

def test_a_call_under_budget_goes_through(db_path):
    calls = []
    create_with_retry(_client(calls), model="claude-sonnet-5", max_tokens=10,
                      messages=[], restaurant_id=1, action="test")
    assert len(calls) == 1


def test_a_call_over_budget_never_reaches_anthropic(db_path, monkeypatch):
    monkeypatch.setattr(ai_utils, "AI_DAILY_BUDGET_USD", 1.00)
    _spend(1, 2.00)
    calls = []
    with pytest.raises(AIBudgetExceeded) as exc:
        create_with_retry(_client(calls), model="claude-sonnet-5", max_tokens=10,
                          messages=[], restaurant_id=1, action="test")
    assert calls == [], "the point is that no request is made — that's the money"
    assert "daily budget" in str(exc.value)


def test_the_refusal_has_its_own_type(db_path, monkeypatch):
    """Callers need to tell 'we stopped spending' apart from 'the AI is
    down' — they mean different things to whoever reads the message."""
    monkeypatch.setattr(ai_utils, "AI_DAILY_BUDGET_USD", 1.00)
    _spend(1, 2.00)
    with pytest.raises(AIBudgetExceeded):
        create_with_retry(_client(), model="claude-sonnet-5", max_tokens=10,
                          messages=[], restaurant_id=1)
    assert issubclass(AIBudgetExceeded, RuntimeError)


def test_a_burst_inside_one_cache_window_still_trips_the_ceiling(db_path, monkeypatch):
    """Spend is cached for a minute to keep a SUM off every call — but the
    calls made during that minute have to count, or a tight loop slides
    under the ceiling forever."""
    monkeypatch.setattr(ai_utils, "AI_DAILY_BUDGET_USD", 0.05)
    client = _client()
    made = 0
    for _ in range(20):
        try:
            create_with_retry(client, model="claude-sonnet-5", max_tokens=10,
                              messages=[], restaurant_id=1, action="test")
            made += 1
        except AIBudgetExceeded:
            break
    assert made < 20, "the loop must hit the ceiling without waiting for a cache refresh"
