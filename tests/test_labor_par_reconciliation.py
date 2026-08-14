"""labor.py's generate_optimized_schedule — PAR hours target reconciliation.

Root cause of a reported "-270 hours from PAR" schedule: the prompt's
TYPICAL HEADCOUNT block was an unconditional "do NOT exceed" ceiling, and
its own PRIORITY ORDER put the hours target dead last — so any restaurant
whose on-file shift history undershoots what its revenue target now
affords was structurally capped below PAR with no way for the AI to close
the gap or even explain it. These tests confirm the reconciliation
language (headcount as a starting point, scale up on a >15% gap, explain
it in the summary) actually reaches the prompt, mirroring the
create_with_retry-capture style test_labor_weather.py already uses."""
import types

import labor
from labor import generate_optimized_schedule


def _minimal_analysis():
    return {"overall_labor_pct": 28.0, "overstaffed_days": [], "understaffed_days": [], "dow_summary": {}}


def _shifts_with_headcount():
    # Two employees on the same day/role, so _headcount_block actually
    # gets built (labor.py only emits it when _hc_lines is non-empty).
    return [
        {"employee": "Alex", "role": "Server", "date": "2026-06-01", "day": "Monday",
         "scheduled_hours": 8, "actual_hours": 8},
        {"employee": "Sam", "role": "Server", "date": "2026-06-01", "day": "Monday",
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


def test_par_block_states_reconciliation_rule_not_unconditional_priority(monkeypatch):
    captured = _capture_create_with_retry(monkeypatch)

    generate_optimized_schedule(
        _minimal_analysis(), _shifts_with_headcount(),
        restaurant_name="Test Bistro", hourly_rate=26.0, labor_target=23.0,
        monthly_revenue_target=365000.0,
    )

    prompt = captured["messages"][0]["content"]
    assert "PAR HOURS TARGET" in prompt
    assert "RECONCILIATION" in prompt
    # The old unconditional ordering must be gone.
    assert "PRIORITY ORDER: 1) TYPICAL HEADCOUNT per day, 2) per-day YoY targets, 3) total hours target." not in prompt
    # The new rule explicitly permits closing a large gap instead of
    # silently landing under budget.
    assert "close most of" in prompt
    assert "Never land silently far under budget" in prompt


def test_headcount_block_allows_scaling_up_instead_of_hard_cap(monkeypatch):
    captured = _capture_create_with_retry(monkeypatch)

    generate_optimized_schedule(
        _minimal_analysis(), _shifts_with_headcount(),
        restaurant_name="Test Bistro", hourly_rate=26.0, labor_target=23.0,
        monthly_revenue_target=365000.0,
    )

    prompt = captured["messages"][0]["content"]
    assert "TYPICAL HEADCOUNT PER DAY" in prompt
    # The old hard "CRITICAL... Do NOT exceed" ceiling must be gone.
    assert "CRITICAL: these are the actual staff counts" not in prompt
    assert "Do NOT exceed these numbers per role per day" not in prompt
    # Replaced with a conditional allowance tied to the PAR gap.
    assert "starting point" in prompt
    assert "proportionally across roles" in prompt


def test_weekly_hours_bullet_permits_headcount_for_large_gaps(monkeypatch):
    captured = _capture_create_with_retry(monkeypatch)

    generate_optimized_schedule(
        _minimal_analysis(), _shifts_with_headcount(),
        restaurant_name="Test Bistro", hourly_rate=26.0, labor_target=23.0,
        monthly_revenue_target=365000.0,
    )

    prompt = captured["messages"][0]["content"]
    assert "Weekly hours target is" in prompt
    assert "more than ~15% under this target" in prompt
    # The old absolute "Never schedule extra people purely to fill hours"
    # line is gone — replaced with a conditional version further down.
    assert "Never schedule extra people purely to fill hours." not in prompt


def test_hours_budget_computed_from_monthly_revenue_target_when_set(monkeypatch):
    """Confirms the exact real-world case this was reported against: a
    monthly_revenue_target set (as Gia Mia's is) takes priority over YoY/
    recent-sales fallbacks for projected_revenue."""
    captured = _capture_create_with_retry(monkeypatch)

    generate_optimized_schedule(
        _minimal_analysis(), _shifts_with_headcount(),
        restaurant_name="Test Bistro", hourly_rate=26.0, labor_target=23.0,
        monthly_revenue_target=365000.0,
    )

    prompt = captured["messages"][0]["content"]
    # weekly = 365000 / 4.33 ≈ 84296; budget% of that ≈ 19388; /26/hr ≈ 745.7h
    assert "Projected revenue: $84,296" in prompt or "Projected revenue: $84,297" in prompt
    assert "745" in prompt  # hours_budget, allowing for rounding
