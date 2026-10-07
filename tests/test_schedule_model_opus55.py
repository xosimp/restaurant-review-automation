"""The schedule runs on Opus 5.5 with adaptive thinking (schedule audit
10/3/26 PR-6, PR-29, PR-30; owner 10/3/26: "use opus 5.5 for the model call
instead of sonnet").

Thinking cannot be turned off on Opus 5.5, so the call asks for it at high
effort with room to reason (thinking shares max_tokens with the rows) and
streams the answer. Sonnet 5.5 refuses thinking={"type": "disabled"} with a
400 — the default every other call gets — so its lowest setting is sent
instead. A job deadline stops the retry loop starting attempts it cannot
finish."""
import time
import types

import pytest

import ai_utils
import labor


def test_schedule_defaults_to_opus_55(monkeypatch):
    monkeypatch.delenv("SCHEDULE_MODEL", raising=False)
    assert ai_utils.model_for("schedule") == "claude-opus-5-5"


@pytest.mark.parametrize("model,kwargs,want", [
    ("claude-sonnet-5", {}, {"type": "disabled"}),
    ("claude-haiku-4-5-20251001", {}, {"type": "disabled"}),
    ("claude-sonnet-5-5", {}, {"type": "between_tools"}),
    ("claude-sonnet-5-5", {"output_config": {"effort": "high"}}, {"type": "between_tools"}),
    ("claude-sonnet-5-5", {"output_config": {"effort": "xhigh"}}, None),
    ("claude-opus-5-5", {}, None),
    ("claude-opus-5", {"output_config": {"effort": "max"}}, None),
    ("claude-opus-5", {"output_config": {"effort": "high"}}, {"type": "disabled"}),
])
def test_default_thinking_never_sends_a_setting_the_model_refuses(model, kwargs, want):
    assert ai_utils.default_thinking(model, kwargs) == want


def _quiet(monkeypatch):
    monkeypatch.setattr(ai_utils, "ai_budget_exceeded", lambda *a, **k: None)
    monkeypatch.setattr(ai_utils, "_breaker_check", lambda *a, **k: None)
    monkeypatch.setattr(ai_utils, "_log_usage_safe", lambda *a, **k: None)
    monkeypatch.setattr(ai_utils, "_record_trace_safe", lambda *a, **k: None)
    monkeypatch.setattr(ai_utils, "_breaker_record", lambda *a, **k: None)
    monkeypatch.setattr(ai_utils, "_log_failure_safe", lambda *a, **k: None)


def test_stream_true_collects_the_stream_into_one_message(monkeypatch):
    _quiet(monkeypatch)
    seen = {}
    final = types.SimpleNamespace(content=[], stop_reason="end_turn", usage=None)

    class _Stream:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def get_final_message(self):
            return final

    class _Messages:
        def stream(self, **kw):
            seen["stream_kwargs"] = kw
            return _Stream()

        def create(self, **kw):
            raise AssertionError("a streamed call must not use create")

    client = types.SimpleNamespace(messages=_Messages())
    out = ai_utils.create_with_retry(client, model="claude-opus-5-5", max_tokens=64000, stream=True,
                                     messages=[{"role": "user", "content": "x"}])
    assert out is final
    assert "stream" not in seen["stream_kwargs"]
    # Opus 5.5 thinks whatever is asked: nothing is forced on it
    assert "thinking" not in seen["stream_kwargs"]


def test_stream_true_on_a_double_without_stream_uses_create(monkeypatch):
    _quiet(monkeypatch)
    msg = types.SimpleNamespace(content=[], stop_reason="end_turn", usage=None)
    client = types.SimpleNamespace(messages=types.SimpleNamespace(create=lambda **kw: msg))
    assert ai_utils.create_with_retry(client, model="claude-sonnet-5", max_tokens=10, stream=True,
                                      messages=[{"role": "user", "content": "x"}]) is msg


def test_a_deadline_stops_retries_it_cannot_finish(monkeypatch):
    _quiet(monkeypatch)
    calls = []
    monkeypatch.setattr(ai_utils, "_is_retryable", lambda e: True)
    monkeypatch.setattr(ai_utils.time, "sleep", lambda s: None)

    def create(**kw):
        calls.append(1)
        raise RuntimeError("overloaded")
    client = types.SimpleNamespace(messages=types.SimpleNamespace(create=create))
    with pytest.raises(RuntimeError):
        ai_utils.create_with_retry(client, model="claude-sonnet-5", max_tokens=10, retries=2,
                                   deadline=time.time() + 1, messages=[{"role": "user", "content": "x"}])
    assert len(calls) == 1          # no retry fits before the deadline: none was started
    calls.clear()
    # Past the deadline no call is sent at all — it could only be cut, and a
    # streamed one is billed for what it sent (schedule audit 10/3/26 P-22).
    with pytest.raises(ai_utils.CallDeadlineExceeded):
        ai_utils.create_with_retry(client, model="claude-sonnet-5", max_tokens=10, retries=2,
                                   deadline=0, messages=[{"role": "user", "content": "x"}])
    assert calls == []


def test_the_schedule_call_asks_opus_55_to_think_at_medium_effort_and_streams(monkeypatch):
    monkeypatch.delenv("SCHEDULE_MODEL", raising=False)
    captured = {}

    def fake(client, **kwargs):
        captured.update(kwargs)
        return types.SimpleNamespace(
            content=[types.SimpleNamespace(type="text", text='{"shifts": [], "summary": ["ok"]}')],
            stop_reason="end_turn")
    monkeypatch.setattr(labor, "create_with_retry", fake)
    labor.generate_optimized_schedule(
        {"overall_labor_pct": 28.0, "overstaffed_days": [], "understaffed_days": [], "dow_summary": {}},
        [{"employee": "Alex", "role": "Server", "date": "2026-06-01", "day": "Monday",
          "scheduled_hours": 8, "actual_hours": 8}],
        restaurant_name="Test Bistro", hourly_rate=26.0, labor_target=23.0, monthly_revenue_target=365000.0)
    # Since 10/7/26 the model is the route's (AI orchestration, owner
    # decision 3): a call outside a job runs on the labor_schedule ladder's
    # first rung, T3 - Sonnet 5.5 thinking at medium. Opus 5.5 is T4 and the
    # SCHEDULE_MODEL pin (tests/test_schedule_route.py).
    assert captured["model"] == "claude-sonnet-5-5"
    assert captured["thinking"] == {"type": "adaptive", "display": "summarized"}
    assert captured["output_config"]["effort"] == "medium"
    assert captured["output_config"]["format"]["type"] == "json_schema"
    assert captured["max_tokens"] == labor.SCHEDULE_MAX_TOKENS_THINKING >= 64000
    assert captured["stream"] is True


def test_an_older_schedule_model_override_keeps_the_thinking_off_shape(monkeypatch):
    monkeypatch.setenv("SCHEDULE_MODEL", "claude-sonnet-5")
    captured = {}

    def fake(client, **kwargs):
        captured.update(kwargs)
        return types.SimpleNamespace(
            content=[types.SimpleNamespace(type="text", text='{"shifts": [], "summary": ["ok"]}')],
            stop_reason="end_turn")
    monkeypatch.setattr(labor, "create_with_retry", fake)
    labor.generate_optimized_schedule(
        {"overall_labor_pct": 28.0, "overstaffed_days": [], "understaffed_days": [], "dow_summary": {}},
        [{"employee": "Alex", "role": "Server", "date": "2026-06-01", "day": "Monday",
          "scheduled_hours": 8, "actual_hours": 8}],
        restaurant_name="Test Bistro", hourly_rate=26.0, labor_target=23.0, monthly_revenue_target=365000.0)
    assert "thinking" not in captured
    assert "effort" not in captured.get("output_config", {})
    assert captured["max_tokens"] == 16000
