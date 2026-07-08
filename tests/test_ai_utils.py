"""create_with_retry and the per-restaurant AI rate limiter — the two shared
guards every Claude call in the app now flows through."""
import anthropic
import httpx
import pytest

from ai_utils import (
    create_with_retry, ai_rate_limited, extract_text, _ai_call_log,
    log_ai_usage, usage_summary,
)


class FakeBlock:
    def __init__(self, **kwargs):
        for k, v in kwargs.items():
            setattr(self, k, v)


class FakeMessage:
    def __init__(self, content):
        self.content = content


class FakeClient:
    """Stands in for anthropic.Anthropic() — fails N times, then succeeds."""
    def __init__(self, failures, exc):
        self.calls = 0
        self.failures = failures
        self.exc = exc
        self.messages = self
        self.last_kwargs = None

    def create(self, **kwargs):
        self.calls += 1
        self.last_kwargs = kwargs
        if self.calls <= self.failures:
            raise self.exc
        return {"ok": True, "kwargs": kwargs}


def _conn_error():
    return anthropic.APIConnectionError(request=httpx.Request("POST", "https://api.anthropic.com"))


def test_retry_recovers_from_transient_failure(monkeypatch):
    monkeypatch.setattr("ai_utils.time.sleep", lambda s: None)
    client = FakeClient(failures=2, exc=_conn_error())
    result = create_with_retry(client, retries=2, model="m", max_tokens=10)
    assert result["ok"] and client.calls == 3


def test_retry_gives_up_after_budget(monkeypatch):
    monkeypatch.setattr("ai_utils.time.sleep", lambda s: None)
    # Final failure triggers ops.capture — keep the test from writing to the
    # real database.
    import ops
    captured = []
    monkeypatch.setattr(ops, "capture", lambda e, **kw: captured.append(kw))
    client = FakeClient(failures=99, exc=_conn_error())
    with pytest.raises(anthropic.APIConnectionError):
        create_with_retry(client, retries=2, model="m", max_tokens=10)
    assert client.calls == 3  # 1 try + 2 retries, no more
    assert captured and captured[0]["job"] == "ai_call"  # exhaustion was reported


def test_non_retryable_error_raises_immediately():
    req = httpx.Request("POST", "https://api.anthropic.com")
    resp = httpx.Response(400, request=req)
    exc = anthropic.BadRequestError("bad", response=resp, body=None)
    client = FakeClient(failures=99, exc=exc)
    with pytest.raises(anthropic.BadRequestError):
        create_with_retry(client, retries=2, model="m", max_tokens=10)
    assert client.calls == 1  # a caller mistake is never retried


def test_rate_limiter_sliding_window():
    _ai_call_log.clear()
    key = "test:limiter"
    results = [ai_rate_limited(key, max_calls=3, window_secs=60) for _ in range(5)]
    assert results == [False, False, False, True, True]


def test_rate_limiter_isolated_per_key():
    _ai_call_log.clear()
    assert ai_rate_limited("rest:1", max_calls=1, window_secs=60) is False
    assert ai_rate_limited("rest:1", max_calls=1, window_secs=60) is True
    # restaurant 2 is unaffected by restaurant 1 exhausting its budget
    assert ai_rate_limited("rest:2", max_calls=1, window_secs=60) is False


def test_create_with_retry_disables_thinking_by_default():
    """Regression test for a real production outage: claude-sonnet-5 prepends
    a ThinkingBlock to non-trivial prompts, which has no .text attribute —
    every one of the 14 message.content[0].text call sites in this codebase
    raised AttributeError until thinking was disabled here. If this default
    ever gets removed, every AI-generation feature breaks silently again."""
    client = FakeClient(failures=0, exc=None)
    create_with_retry(client, model="m", max_tokens=10)
    assert client.last_kwargs["thinking"] == {"type": "disabled"}


def test_create_with_retry_respects_explicit_thinking_override():
    client = FakeClient(failures=0, exc=None)
    create_with_retry(client, model="m", max_tokens=10, thinking={"type": "enabled", "budget_tokens": 1024})
    assert client.last_kwargs["thinking"] == {"type": "enabled", "budget_tokens": 1024}


def test_extract_text_skips_leading_thinking_block():
    msg = FakeMessage([FakeBlock(thinking="reasoning about the answer"), FakeBlock(text="the real answer")])
    assert extract_text(msg) == "the real answer"


def test_extract_text_plain_text_response():
    msg = FakeMessage([FakeBlock(text="just text, no thinking")])
    assert extract_text(msg) == "just text, no thinking"


def test_extract_text_no_text_block_returns_empty():
    msg = FakeMessage([FakeBlock(thinking="only reasoning, response got cut off")])
    assert extract_text(msg) == ""


# ── AI usage/cost tracking ──────────────────────────────────────────────────
# Nothing tracked what any of this cost before — no per-restaurant spend
# visibility as client count grows. Every create_with_retry() call now logs
# to ai_usage on success.

class FakeUsage:
    def __init__(self, input_tokens, output_tokens):
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens


class FakeMessageWithUsage:
    def __init__(self, usage):
        self.usage = usage


class FakeClientWithUsage:
    def __init__(self, input_tokens=100, output_tokens=50):
        self.messages = self
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens

    def create(self, **kwargs):
        return FakeMessageWithUsage(FakeUsage(self.input_tokens, self.output_tokens))


def test_create_with_retry_logs_usage_on_success(monkeypatch):
    logged = []
    monkeypatch.setattr("ai_utils.log_ai_usage", lambda *a, **k: logged.append(a))
    client = FakeClientWithUsage(input_tokens=100, output_tokens=50)
    create_with_retry(client, model="claude-haiku-4-5-20251001", max_tokens=10,
                       restaurant_id=42, action="test_action")
    assert len(logged) == 1
    restaurant_id, action, model, input_tokens, output_tokens = logged[0]
    assert (restaurant_id, action, model, input_tokens, output_tokens) == (42, "test_action", "claude-haiku-4-5-20251001", 100, 50)


def test_create_with_retry_does_not_log_when_response_has_no_usage(monkeypatch):
    """FakeClient (used throughout the rest of this file) returns a plain
    dict with no .usage attribute — logging must no-op, not crash."""
    logged = []
    monkeypatch.setattr("ai_utils.log_ai_usage", lambda *a, **k: logged.append(a))
    client = FakeClient(failures=0, exc=None)
    create_with_retry(client, model="m", max_tokens=10)
    assert logged == []


def test_create_with_retry_pops_restaurant_id_and_action_before_calling_api():
    """restaurant_id/action are for our own logging only — the Anthropic
    client should never see them as kwargs."""
    client = FakeClient(failures=0, exc=None)
    create_with_retry(client, model="m", max_tokens=10, restaurant_id=7, action="foo")
    assert "restaurant_id" not in client.last_kwargs
    assert "action" not in client.last_kwargs


def test_create_with_retry_usage_logging_failure_does_not_break_call(monkeypatch):
    """A broken DB should never take down the AI call it's trying to measure."""
    monkeypatch.setattr("ai_utils.log_ai_usage", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("db down")))
    client = FakeClientWithUsage()
    result = create_with_retry(client, model="m", max_tokens=10)
    assert isinstance(result, FakeMessageWithUsage)  # call still succeeded


def test_log_and_summarize_usage(db_path):
    log_ai_usage(1, "draft_response", "claude-sonnet-5", 1000, 200, db_path=db_path)
    log_ai_usage(1, "draft_response", "claude-sonnet-5", 500, 100, db_path=db_path)
    log_ai_usage(2, "draft_response", "claude-sonnet-5", 1000, 200, db_path=db_path)

    rows = usage_summary(restaurant_id=1, db_path=db_path)
    assert len(rows) == 1
    assert rows[0]["calls"] == 2
    assert rows[0]["input_tokens"] == 1500
    assert rows[0]["output_tokens"] == 300
    assert rows[0]["cost_usd"] > 0


def test_usage_summary_groups_by_action_and_model(db_path):
    log_ai_usage(1, "draft_response", "claude-sonnet-5", 1000, 200, db_path=db_path)
    log_ai_usage(1, "review_analysis", "claude-haiku-4-5-20251001", 200, 50, db_path=db_path)

    rows = usage_summary(restaurant_id=1, db_path=db_path)
    actions = {r["action"] for r in rows}
    assert actions == {"draft_response", "review_analysis"}


def test_usage_summary_unscoped_covers_all_restaurants(db_path):
    log_ai_usage(1, "draft_response", "claude-sonnet-5", 1000, 200, db_path=db_path)
    log_ai_usage(2, "draft_response", "claude-sonnet-5", 1000, 200, db_path=db_path)

    rows = usage_summary(db_path=db_path)
    assert sum(r["calls"] for r in rows) == 2
