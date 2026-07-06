"""create_with_retry and the per-restaurant AI rate limiter — the two shared
guards every Claude call in the app now flows through."""
import anthropic
import httpx
import pytest

from ai_utils import create_with_retry, ai_rate_limited, extract_text, _ai_call_log


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
