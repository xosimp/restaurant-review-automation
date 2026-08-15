"""labor.py's generate_optimized_schedule — output-format regression guards.

A real generation once opened with a literal "<think>...</think>" block of
plain-text reasoning that alone consumed the entire max_tokens budget,
leaving zero room for actual CSV rows (stop_reason: max_tokens,
hours_scheduled: 0.0) — a silent, expensive failure (still billed, still a
~70s wait) that looked like a slow success rather than a broken one. An
assistant-message prefill would have blocked this structurally, but this
model rejects prefill outright ("This model does not support assistant
message prefill"), so the fix is a strengthened prompt instruction only.
These tests guard the two things that made that fix work from silently
regressing: the no-preamble instruction, and a max_tokens ceiling generous
enough that natural completion (not truncation) is what stops the model.
"""
import types

import labor
from labor import generate_optimized_schedule


def _minimal_analysis():
    return {"overall_labor_pct": 28.0, "overstaffed_days": [], "understaffed_days": [], "dow_summary": {}}


def _shifts():
    return [
        {"employee": "Alex", "role": "Server", "date": "2026-06-01", "day": "Monday",
         "scheduled_hours": 8, "actual_hours": 8},
    ]


def _capture_create_with_retry(monkeypatch):
    captured = {}

    def fake(client, **kwargs):
        captured.update(kwargs)
        return types.SimpleNamespace(
            content=[types.SimpleNamespace(text="date,day,employee,role,shift_start,shift_end,scheduled_hours,notes\n---SUMMARY---\n- ok")],
            stop_reason="end_turn",
        )
    monkeypatch.setattr(labor, "create_with_retry", fake)
    return captured


def test_prompt_explicitly_forbids_think_blocks_and_preamble(monkeypatch):
    captured = _capture_create_with_retry(monkeypatch)

    generate_optimized_schedule(
        _minimal_analysis(), _shifts(),
        restaurant_name="Test Bistro", hourly_rate=26.0, labor_target=23.0,
        monthly_revenue_target=365000.0,
    )

    prompt = captured["messages"][0]["content"]
    assert "<think>" in prompt  # names the specific failure mode it's blocking
    assert "DO NOT write any explanation, reasoning, preamble" in prompt


def test_max_tokens_generous_enough_to_avoid_truncation(monkeypatch):
    captured = _capture_create_with_retry(monkeypatch)

    generate_optimized_schedule(
        _minimal_analysis(), _shifts(),
        restaurant_name="Test Bistro", hourly_rate=26.0, labor_target=23.0,
        monthly_revenue_target=365000.0,
    )

    # Real generations were observed truncating (stop_reason: max_tokens)
    # at 8000 with zero actual CSV rows. Don't let this silently regress
    # back down.
    assert captured["max_tokens"] >= 16000


def test_no_assistant_prefill_is_sent(monkeypatch):
    """This model rejects assistant-message prefill outright (confirmed
    live: "This model does not support assistant message prefill. The
    conversation must end with a user message."). If a future change
    re-adds one, every schedule generation call will hard-fail."""
    captured = _capture_create_with_retry(monkeypatch)

    generate_optimized_schedule(
        _minimal_analysis(), _shifts(),
        restaurant_name="Test Bistro", hourly_rate=26.0, labor_target=23.0,
        monthly_revenue_target=365000.0,
    )

    messages = captured["messages"]
    assert len(messages) == 1
    assert messages[0]["role"] == "user"
