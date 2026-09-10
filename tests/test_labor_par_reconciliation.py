"""labor.py's generate_optimized_schedule — the PAR hours CEILING.

A reported "-270 hours from PAR" schedule was first answered by telling
the model that landing under budget meant the restaurant "can now afford"
more staff, and to close the gap by adding headcount across every day.
That made a labor target behave like a quota: a restaurant running an
efficient 24% against a 30% target had people added until it reached 30%,
inside the one module whose headline number is savings.

The real cause of that -270 was upstream. TYPICAL HEADCOUNT divided a
deduplicated set of employees by the number of weeks of history, so a
stable roster — the same six servers every Friday — reported two. The
model was staffing a third of reality and the budget gap was the
symptom. That divisor is fixed; see
test_labor_typical_headcount.py.

These tests hold the ceiling semantics in place: under budget is a good
outcome that needs no correction, and the hours figure may only ever
remove hours, never add them. Capture style mirrors
test_labor_weather.py."""
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


def test_par_block_is_a_ceiling_not_a_quota(monkeypatch):
    captured = _capture_create_with_retry(monkeypatch)

    generate_optimized_schedule(
        _minimal_analysis(), _shifts_with_headcount(),
        restaurant_name="Test Bistro", hourly_rate=26.0, labor_target=23.0,
        monthly_revenue_target=365000.0,
    )

    prompt = captured["messages"][0]["content"]
    assert "PAR HOURS CEILING" in prompt
    assert "This is a ceiling, not a quota" in prompt
    assert "Coming in under it is a good outcome" in prompt
    assert "it never adds them" in prompt
    # The spend-to-budget instructions must be gone. Each of these told the
    # model to raise payroll toward the target.
    for banned in ("close most of",
                   "Never land silently far under budget",
                   "doesn't reflect what this restaurant can now afford",
                   "the hours target is what governs"):
        assert banned not in prompt, banned


def test_the_old_priority_ordering_is_still_gone(monkeypatch):
    """The ceiling replaced the quota; it must not have restored the
    original unconditional cap that started this whole line of work."""
    captured = _capture_create_with_retry(monkeypatch)
    generate_optimized_schedule(
        _minimal_analysis(), _shifts_with_headcount(),
        restaurant_name="Test Bistro", hourly_rate=26.0, labor_target=23.0,
        monthly_revenue_target=365000.0,
    )
    prompt = captured["messages"][0]["content"]
    assert "PRIORITY ORDER: 1) TYPICAL HEADCOUNT per day, 2) per-day YoY targets, 3) total hours target." not in prompt


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
    # Headcount stays a starting point that an event or a real volume
    # spike can move — but never the hours ceiling.
    assert "starting point" in prompt
    assert "proportionally across roles" in prompt
    assert "is NOT a reason to go over" in prompt


def test_weekly_hours_bullet_forbids_padding_to_reach_the_figure(monkeypatch):
    captured = _capture_create_with_retry(monkeypatch)

    generate_optimized_schedule(
        _minimal_analysis(), _shifts_with_headcount(),
        restaurant_name="Test Bistro", hourly_rate=26.0, labor_target=23.0,
        monthly_revenue_target=365000.0,
    )

    prompt = captured["messages"][0]["content"]
    assert "must not EXCEED" in prompt
    assert "Landing under it is fine and expected" in prompt
    assert "more than ~15% under this target" not in prompt
    # Nowhere in the prompt may headcount be justified by an hours gap.
    assert "closing a >15% PAR hours gap" not in prompt


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
